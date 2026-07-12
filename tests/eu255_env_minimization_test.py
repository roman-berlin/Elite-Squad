"""EU-255: officer & gate subprocesses must NOT inherit Jira/Telegram credentials while building
or testing untrusted product code.

Two boundaries, one shared denylist (``backends.SENSITIVE_KEYS`` / ``SENSITIVE_PREFIXES``):

  1. The officer SDK subprocess seam ``agent._run_agent_unrouted`` — it merges
     ``backends.secret_strip_overrides()`` into ``options.env`` right after ``backends.apply``. The
     SDK builds ``{**os.environ, **options.env}`` (subprocess_cli.py), so removing a key requires
     BLANKING it in ``options.env`` (a key merely absent there still leaks through from the parent).
     The scrub is deliberately NOT inside ``apply`` (which stays a pure model/backend transform and
     is unit-tested as a no-op in model_selection_test.py) — it lives at the one real spawn seam so
     it covers BOTH backends.
  2. ``gate._subprocess_env(app)`` — the gate subprocess seam (``run_commands`` /
     ``preflight_imports``). Here we build the dict ourselves, so sensitive keys are simply
     omitted rather than blanked.

Both must preserve model auth (``CLAUDE_CODE_OAUTH_TOKEN`` / ``ANTHROPIC_API_KEY`` / the GLM
z.ai bearer) and leave ``os.environ`` in THIS (orchestrator) process completely untouched, so
Jira/Telegram/drain/notify keep working here.
"""
import asyncio
import sys
import tempfile
import types
import os
from pathlib import Path

# Stub the Agent SDK before any orchestrator import (house convention).
sdk = types.ModuleType("claude_agent_sdk")


class _Options:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


sdk.ClaudeAgentOptions = _Options
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import backends           # noqa: E402
from orchestrator import gate               # noqa: E402
from orchestrator import agent as agent_mod  # noqa: E402
from orchestrator.agent import run_agent     # noqa: E402
from orchestrator.config import AppConfig   # noqa: E402


def _spy_query_env():
    """Return (spy, captured): a stubbed ``agent.query`` that records the ``options.env`` handed to
    the (would-be) SDK subprocess at spawn time, plus the dict it populates. Yields no messages, so
    ``_run_agent_unrouted`` returns a clean empty AgentRun without any network/model."""
    captured: dict = {}

    def _spy(**kw):
        opts = kw.get("options")
        captured["env"] = dict(getattr(opts, "env", None) or {})
        captured["model"] = getattr(opts, "model", None)

        async def _gen():
            return
            yield  # pragma: no cover — makes this an async generator
        return _gen()

    return _spy, captured

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# Sensitive keys/values planted for the duration of this file, restored at the end — a known-clean
# slate so a real Jira/Telegram token possibly present in the real dev env can't mask a regression.
_SENSITIVE_PLANTED = {
    "JIRA_EMAIL": "roman@example.com",
    "JIRA_API_TOKEN": "jira-test-token-eu255",
    "TELEGRAM_BOT_TOKEN": "telegram-test-token-eu255",
    "TELEGRAM_CHAT_ID": "123456",
    "GENERAL_COCKPIT_PROMOTE": "1",
}
# Non-sensitive vars that MUST survive (model auth + ordinary toolchain env).
_KEEP_PLANTED = {
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-test-token-eu255",
    "ANTHROPIC_API_KEY": "anthropic-test-key-eu255",
}
_SAVED = {k: os.environ.get(k) for k in list(_SENSITIVE_PLANTED) + list(_KEEP_PLANTED)
          + ["GLM_AUTH_TOKEN"]}


def _plant():
    for k, v in _SENSITIVE_PLANTED.items():
        os.environ[k] = v
    for k, v in _KEEP_PLANTED.items():
        os.environ[k] = v


def _restore():
    for k, v in _SAVED.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


_plant()
_environ_snapshot_before = dict(os.environ)

try:
    # ============ 1) officer SDK seam under NATIVE: env handed to the subprocess strips creds ======
    # The scrub lives at ``agent._run_agent_unrouted`` (NOT in ``backends.apply``, which stays a pure
    # no-op — model_selection_test.py pins that). Drive the real seam with a stubbed ``query`` that
    # records the ``options.env`` it would spawn the subprocess with.
    _saved_query = agent_mod.query
    try:
        spy, captured = _spy_query_env()
        agent_mod.query = spy
        _tok = backends.set_backend(backends.NATIVE)
        try:
            run = asyncio.run(run_agent("build the thing", _Options(model="claude-opus-4-8")))
        finally:
            backends.reset_backend(_tok)

        native_env = captured.get("env", {})
        chk("NATIVE: seam ran (query received options)", "env" in captured, captured)
        chk("NATIVE: run labelled as opus/native",
            (run.provider or "").lower() in ("anthropic", "opus", "claude", ""), run.provider)
        chk("NATIVE: subprocess model unchanged", captured.get("model") == "claude-opus-4-8",
            captured.get("model"))
        for k in _SENSITIVE_PLANTED:
            chk(f"NATIVE: subprocess env[{k}] is stripped ('')", native_env.get(k) == "", native_env.get(k))
        for k in _KEEP_PLANTED:
            chk(f"NATIVE: subprocess env does NOT strip model-auth key {k} (inherited from parent)",
                k not in native_env, native_env.get(k))

        # ============ 2) officer SDK seam under GLM: strips the same keys AND keeps the z.ai bearer ==
        _had_glm = os.environ.get("GLM_AUTH_TOKEN")
        os.environ["GLM_AUTH_TOKEN"] = "zai-test-token-eu255"
        spy_glm, captured_glm = _spy_query_env()
        agent_mod.query = spy_glm
        _tok = backends.set_backend(backends.GLM)
        try:
            asyncio.run(run_agent("build the thing", _Options(model="claude-opus-4-8")))
        finally:
            backends.reset_backend(_tok)
            if _had_glm is None:
                os.environ.pop("GLM_AUTH_TOKEN", None)
            else:
                os.environ["GLM_AUTH_TOKEN"] = _had_glm

        glm_env = captured_glm.get("env", {})
        chk("GLM: subprocess env carries the z.ai bearer",
            glm_env.get("ANTHROPIC_AUTH_TOKEN") == "zai-test-token-eu255", glm_env.get("ANTHROPIC_AUTH_TOKEN"))
        chk("GLM: subprocess env carries the z.ai base URL", bool(glm_env.get("ANTHROPIC_BASE_URL")))
        chk("GLM: subprocess model set to the GLM model", captured_glm.get("model") == backends.glm_model(),
            captured_glm.get("model"))
        for k in _SENSITIVE_PLANTED:
            chk(f"GLM: subprocess env[{k}] is ALSO stripped ('')", glm_env.get(k) == "", glm_env.get(k))
    finally:
        agent_mod.query = _saved_query

    # ============ 3) gate._subprocess_env(app): omits sensitive keys, keeps PATH/HOME/model auth ==
    tmp = Path(tempfile.mkdtemp())
    app = AppConfig(name="widgetco", repo_path=str(tmp), gate_env={"NODE_OPTIONS": "--max-old-space-size=2048"})
    genv = gate._subprocess_env(app)

    for k in _SENSITIVE_PLANTED:
        chk(f"gate env: {k} is ABSENT", k not in genv, genv.get(k))
    for k, v in _KEEP_PLANTED.items():
        chk(f"gate env: model-auth key {k} is preserved", genv.get(k) == v, genv.get(k))
    chk("gate env: PATH is preserved", genv.get("PATH") == os.environ.get("PATH"))
    chk("gate env: HOME is preserved", genv.get("HOME") == os.environ.get("HOME"))
    chk("gate env: app.gate_env is overlaid", genv.get("NODE_OPTIONS") == "--max-old-space-size=2048")

    # ---- integration: a REAL subprocess spawned by run_commands never sees the sensitive keys ----
    out = tmp / "seen.txt"
    check_script = (
        "import os,sys;"
        f"present = [k for k in {sorted(_SENSITIVE_PLANTED)!r} if k in os.environ];"
        "open(sys.argv[1],'w').write(repr(present) + '|' + os.environ.get('CLAUDE_CODE_OAUTH_TOKEN',''))"
    )
    res = gate.run_commands(app, [f"{sys.executable} -c \"{check_script}\" {out}"])
    chk("run_commands: the spawned subprocess exits cleanly", res.passed, res.report)
    seen = out.read_text() if out.exists() else "<missing>"
    chk("run_commands: the real child subprocess sees NO sensitive keys at all", seen.startswith("[]|"), seen)
    chk("run_commands: the real child subprocess still sees model auth (CLAUDE_CODE_OAUTH_TOKEN)",
        seen.endswith("|" + _KEEP_PLANTED["CLAUDE_CODE_OAUTH_TOKEN"]), seen)

    # ============ 4) os.environ in THIS process is unchanged by any of the above =====================
    chk("orchestrator-process os.environ is byte-identical after apply()/gate calls "
        "(Jira/Telegram/drain/notify unaffected)",
        dict(os.environ) == _environ_snapshot_before)

finally:
    _restore()


# ============ tally ==============================================================================
passed = sum(1 for _, ok, _ in results if ok)
print("\n========== EU-255 ENV MINIMIZATION QA ==========")
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
