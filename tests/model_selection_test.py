"""EU-189 QA — cockpit model-backend selection (Opus vs GLM/Z.ai).

Offline harness: stubs claude_agent_sdk (no network, no real models) and a spy `query`, then pins
the behaviour that matters:
  • apply() writes GLM's z.ai endpoint + bearer into options.env (per-call), never os.environ;
  • apply() FAILS CLOSED to Opus when GLM_AUTH_TOKEN is absent (never aims a subprocess at z.ai
    with an empty bearer — the credential-leak guard);
  • native (Opus) is a pure no-op;
  • the provider label is derived from the applied backend;
  • run_agent_with_fallback makes NO native-Opus cap probe under a GLM run.
"""
import sys, types, os, asyncio, copy
from dataclasses import fields as _dc_fields

# ── Rich SDK stub (installed BEFORE importing orchestrator) ────────────────────────────────────
sdk = types.ModuleType("claude_agent_sdk")


class ClaudeAgentOptions:
    def __init__(self, **kw):
        self.model = kw.get("model")
        self.env = dict(kw.get("env") or {})
        for k, v in kw.items():
            if k not in ("model", "env"):
                setattr(self, k, v)


class AssistantMessage:
    def __init__(self, content=None, error=None):
        self.content = content or []
        self.error = error


class ResultMessage:
    def __init__(self, result="", total_cost_usd=0.0, num_turns=1, is_error=False, usage=None):
        self.result = result
        self.total_cost_usd = total_cost_usd
        self.num_turns = num_turns
        self.is_error = is_error
        self.usage = usage or {"input_tokens": 1, "output_tokens": 1}


class TextBlock:
    def __init__(self, text=""):
        self.text = text


class ToolUseBlock:
    def __init__(self, name="", input=None):
        self.name = name
        self.input = input


# Spy query — records each call's (model, env) and yields a controllable result.
CALLS = []
RETURN_CAP = False


async def _spy_query(prompt=None, options=None, **kw):
    CALLS.append({
        "model": getattr(options, "model", None),
        "env": dict(getattr(options, "env", {}) or {}),
    })
    if RETURN_CAP:
        yield AssistantMessage(content=[TextBlock("capped")], error="Claude usage limit reached")
        yield ResultMessage(result="capped", is_error=True)
    else:
        yield AssistantMessage(content=[TextBlock("ok")])
        yield ResultMessage(result="ok", is_error=False)


sdk.ClaudeAgentOptions = ClaudeAgentOptions
sdk.AssistantMessage = AssistantMessage
sdk.ResultMessage = ResultMessage
sdk.TextBlock = TextBlock
sdk.ToolUseBlock = ToolUseBlock
sdk.query = _spy_query
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import agent, backends, provider           # noqa: E402
from orchestrator.config import Config                        # noqa: E402

agent.query = _spy_query          # agent bound `query` at import — repoint it at the spy
agent._TRANSIENT_RETRY_BACKOFF_S = 0.0

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


class _Cfg:
    """Minimal cfg stand-in for run_agent_with_fallback (only .model_backend is read)."""
    def __init__(self, model_backend="opus"):
        self.model_backend = model_backend


FAKE_TOKEN = "zai-test-tokenAAA111"


def _with_glm_token():
    os.environ["GLM_AUTH_TOKEN"] = FAKE_TOKEN


def _without_glm_token():
    os.environ.pop("GLM_AUTH_TOKEN", None)


# Keep provider detection deterministic: no ambient z.ai endpoint.
os.environ.pop("ANTHROPIC_BASE_URL", None)

# ── 1. normalize() — unknown degrades SAFELY to native, never to GLM ───────────────────────────
for v in ("opus", "native", "anthropic", "claude", "", None, "sonnet-ish", "gpt"):
    chk(f"normalize({v!r}) -> opus", backends.normalize(v) == backends.NATIVE)
for v in ("glm", "GLM", "zai", "z.ai"):
    chk(f"normalize({v!r}) -> glm", backends.normalize(v) == backends.GLM)

# ── 2. apply(): native is a pure no-op ─────────────────────────────────────────────────────────
_without_glm_token()
opts = ClaudeAgentOptions(model="claude-opus-4-8", env={})
eff = backends.apply(opts, backend="opus")
chk("native apply -> 'opus'", eff == backends.NATIVE)
chk("native apply: env untouched ({})", opts.env == {})
chk("native apply: model unchanged", opts.model == "claude-opus-4-8")

# ── 3. apply(): GLM configured — z.ai endpoint + bearer into options.env, subscription creds blanked
_with_glm_token()
env_before = dict(os.environ)
opts = ClaudeAgentOptions(model="claude-opus-4-8", env={})
eff = backends.apply(opts, backend="glm")
chk("glm apply -> 'glm'", eff == backends.GLM)
chk("glm apply: base_url is z.ai", "z.ai" in (opts.env.get("ANTHROPIC_BASE_URL", "")).lower())
chk("glm apply: bearer = GLM_AUTH_TOKEN", opts.env.get("ANTHROPIC_AUTH_TOKEN") == FAKE_TOKEN)
chk("glm apply: ANTHROPIC_API_KEY blanked", opts.env.get("ANTHROPIC_API_KEY") == "")
chk("glm apply: CLAUDE_CODE_OAUTH_TOKEN blanked", opts.env.get("CLAUDE_CODE_OAUTH_TOKEN") == "")
chk("glm apply: model overridden to glm-4.6", opts.model == "glm-4.6")
chk("glm apply: small-fast model = glm-4.5-air",
    opts.env.get("ANTHROPIC_SMALL_FAST_MODEL") == "glm-4.5-air")
chk("glm apply: os.environ NOT mutated", dict(os.environ) == env_before)

# fresh dict per call (no shared singleton)
o1, o2 = ClaudeAgentOptions(env={}), ClaudeAgentOptions(env={})
backends.apply(o1, backend="glm"); backends.apply(o2, backend="glm")
chk("glm apply: fresh env dict per call", o1.env is not o2.env)

# ── 4. apply(): GLM selected but NO token — FAIL CLOSED to native (the credential-leak guard) ───
_without_glm_token()
opts = ClaudeAgentOptions(model="claude-opus-4-8", env={})
eff = backends.apply(opts, backend="glm")
chk("glm apply w/o token -> FAIL CLOSED to 'opus'", eff == backends.NATIVE)
chk("glm apply w/o token: env untouched (no z.ai)", opts.env == {})
chk("glm apply w/o token: model unchanged", opts.model == "claude-opus-4-8")
chk("available('glm') false w/o token", backends.available("glm") is False)
_with_glm_token()
chk("available('glm') true w/ token", backends.available("glm") is True)
chk("available('opus') always true", backends.available("opus") is True)

# ── 5. provider labeling derives from the applied backend ──────────────────────────────────────
chk("provider(glm-4.6, backend=glm) -> GLM", provider.get_provider_info("glm-4.6", backend="glm")[0] == "GLM")
chk("provider(claude-opus, no backend) -> Anthropic",
    provider.get_provider_info("claude-opus-4-8")[0] == "Anthropic")

# ── 6. Config field declared (so _known_only keeps it) with the right default ──────────────────
_names = {f.name for f in _dc_fields(Config)}
chk("Config declares model_backend", "model_backend" in _names)
_default = next(f for f in _dc_fields(Config) if f.name == "model_backend").default
chk("Config.model_backend default == 'opus'", _default == "opus")

# ── 7. End-to-end: GLM run applies GLM at the SDK seam, and makes NO Opus cap-probe ────────────
async def _drive(backend, cfg, cap):
    global RETURN_CAP
    RETURN_CAP = cap
    CALLS.clear()
    tok = backends.set_backend(backend)
    try:
        # Sonnet model on purpose: without the GLM guard this would trigger the Sonnet-cap Opus probe.
        opts = ClaudeAgentOptions(model="claude-sonnet-5", env={})
        return await agent.run_agent_with_fallback("p", opts, tag="t", cfg=cfg)
    finally:
        backends.reset_backend(tok)

# GLM, success: exactly ONE query call, and it was aimed at GLM (model overridden, z.ai env).
_with_glm_token()
asyncio.run(_drive("glm", _Cfg("glm"), cap=False))
chk("GLM run: exactly ONE SDK call (no Opus probe)", len(CALLS) == 1)
chk("GLM run: served model was glm-4.6", CALLS and CALLS[0]["model"] == "glm-4.6")
chk("GLM run: call aimed at z.ai", CALLS and "z.ai" in CALLS[0]["env"].get("ANTHROPIC_BASE_URL", "").lower())

# GLM, cap error: STILL exactly ONE call — the guard suppresses the native-Opus probe.
asyncio.run(_drive("glm", _Cfg("glm"), cap=True))
chk("GLM run under cap: still ONE call (guard suppresses Opus probe)", len(CALLS) == 1)

# Control — native Sonnet under a cap DOES probe Opus: TWO calls. Proves the guard is what differs.
asyncio.run(_drive("opus", _Cfg("opus"), cap=True))
chk("native Sonnet+cap: TWO calls (Sonnet + Opus probe)", len(CALLS) == 2)
chk("native probe: 2nd call ran Opus", len(CALLS) == 2 and "opus" in (CALLS[1]["model"] or "").lower())
chk("native run: os.environ has no z.ai leak", "z.ai" not in os.environ.get("ANTHROPIC_BASE_URL", "").lower())

_without_glm_token()

# ── 8. backend_pref persistence (EU-190 sticky selection) — isolated to a tmp file ──────────────
import tempfile
from pathlib import Path
from orchestrator import backend_pref

_tmp = tempfile.mkdtemp()
backend_pref._file = lambda: Path(_tmp) / "model_backend.json"   # isolate from the repo root (one patch, all importers)

_without_glm_token()
chk("pref: unset -> get() is None", backend_pref.get() is None)
chk("pref: active() falls back to opus", backend_pref.active(_Cfg("opus")) == "opus")
chk("pref: active() honours cfg default when unset", backend_pref.active(_Cfg("glm")) == "glm")
backend_pref.set_active("glm")
chk("pref: set_active('glm') persists", backend_pref.get() == "glm")
chk("pref: active() overrides cfg with persisted pref", backend_pref.active(_Cfg("opus")) == "glm")
backend_pref.set_active("garbage")
chk("pref: set_active normalizes unknown -> opus", backend_pref.get() == "opus")
backend_pref.set_active("opus")

# ── 9. server._resolve_run_backend: applies pref + BLOCKS unconfigured GLM (no silent fallback) ──
import orchestrator.server as srv

class _RC:
    def __init__(self, mb="opus"): self.model_backend = mb

backend_pref.set_active("opus")
_rc = _RC("glm")                                  # cfg default glm, but pref opus -> pref wins
_err = srv._resolve_run_backend(_rc)
chk("resolve: pref(opus) overrides cfg(glm)", _rc.model_backend == "opus" and _err is None)

_with_glm_token()
backend_pref.set_active("glm")
_rc = _RC("opus")
_err = srv._resolve_run_backend(_rc)
chk("resolve: GLM configured -> applied, no block", _rc.model_backend == "glm" and _err is None)

_without_glm_token()
_rc = _RC("opus")
_err = srv._resolve_run_backend(_rc)              # pref=glm but token now missing
chk("resolve: GLM unconfigured -> BLOCK (no silent fallback)", _rc.model_backend == "glm" and bool(_err))
backend_pref.set_active("opus")

# ── 9b. GLM config validation + live connection test (EU-190 alerting: 'what to fix') ───────────
_without_glm_token()
chk("config_issues: no token flagged", any("GLM_AUTH_TOKEN" in x for x in backends.glm_config_issues()))
_with_glm_token()
os.environ["GLM_BASE_URL"] = "not-a-url"
chk("config_issues: bad url flagged", any("GLM_BASE_URL" in x for x in backends.glm_config_issues()))
os.environ.pop("GLM_BASE_URL", None)
chk("config_issues: token + default url -> clean", backends.glm_config_issues() == [])

# glm_test_connection maps HTTP statuses / network errors to actionable messages (fake `requests`)
_real_requests = sys.modules.get("requests")
_fake_rq = types.ModuleType("requests")
class _RQExc:
    class ConnectionError(Exception): pass
    class Timeout(Exception): pass
_fake_rq.exceptions = _RQExc
class _RQResp:
    def __init__(self, code): self.status_code = code
def _rq_post(code=None, exc=None):
    def _p(*a, **k):
        if exc: raise exc()
        return _RQResp(code)
    return _p
sys.modules["requests"] = _fake_rq
_with_glm_token()
_fake_rq.post = _rq_post(code=200)
chk("test_connection: 200 -> OK", backends.glm_test_connection()[0] is True)
_fake_rq.post = _rq_post(code=401)
_r = backends.glm_test_connection(); chk("test_connection: 401 -> bad token", _r[0] is False and "token" in _r[1].lower())
_fake_rq.post = _rq_post(code=404)
_r = backends.glm_test_connection(); chk("test_connection: 404 -> bad URL", _r[0] is False and "url" in _r[1].lower())
_fake_rq.post = _rq_post(exc=_RQExc.ConnectionError)
_r = backends.glm_test_connection(); chk("test_connection: conn error -> unreachable", _r[0] is False and "reach" in _r[1].lower())
_without_glm_token()
chk("test_connection: no token -> static issue (no network)", backends.glm_test_connection()[0] is False)
if _real_requests is not None:
    sys.modules["requests"] = _real_requests
else:
    sys.modules.pop("requests", None)

# ── 10. cockpit set/get via the Flask test client (AC: 'cockpit set/get switches the backend') ───
from orchestrator.config import Config, AppConfig
srv.health.summary = lambda c: {"healthy": True, "checks": []}
_appdir = tempfile.mkdtemp()
Path(_appdir, "audit.jsonl").write_text("", encoding="utf-8")
_cfg = Config(apps=[AppConfig(name="alpha", repo_path=_appdir, base_branch="DEV",
                              protected_branch="MAIN", backlog_backend="none")],
              audit_path=str(Path(_appdir, "audit.jsonl")), use_worktree=False)
_client = srv.create_app(_cfg).test_client()

backend_pref.set_active("opus")
_with_glm_token()
backends.glm_test_connection = lambda timeout=8.0: (True, "OK")     # stub: no live network in tests
_client.post("/api/model", data={"backend": "glm"})
chk("cockpit set: glm (connection OK) -> persists glm", backend_pref.get() == "glm")
_client.post("/api/model", data={"backend": "opus"})
chk("cockpit set: opus -> persists opus", backend_pref.get() == "opus")
backends.glm_test_connection = lambda timeout=8.0: (False, "token rejected (incorrect GLM_AUTH_TOKEN)")
srv._state.pop("model_alert", None)
_client.post("/api/model", data={"backend": "glm"})   # connection test fails -> alert, not persisted
chk("cockpit set: glm (bad connection) -> NOT persisted", backend_pref.get() == "opus")
chk("cockpit set: bad connection -> model_alert surfaced", "GLM not enabled" in (srv._state.get("model_alert") or ""))
_without_glm_token()

# ── 11. CLI: --model override, `model` subcommand, block-on-missing-key ──────────────────────────
from orchestrator import main as M
_p = M.build_parser()
chk("CLI: --model glm parses on task", M.build_parser().parse_args(["--model", "glm", "task", "a", "x"]).model == "glm")
_ma = _p.parse_args(["model", "glm"])
chk("CLI: `model` subcommand parses", _ma.command == "model" and _ma.backend == "glm")

_ccfg = _Cfg("opus")
M._apply_overrides(_ccfg, _p.parse_args(["--model", "glm", "task", "a", "x"]))
chk("CLI: --model glm overrides cfg.model_backend", _ccfg.model_backend == "glm")
backend_pref.set_active("glm")
_ccfg2 = _Cfg("opus")
M._apply_overrides(_ccfg2, _p.parse_args(["task", "a", "x"]))
chk("CLI: task honours persisted pref when no --model", _ccfg2.model_backend == "glm")
backend_pref.set_active("opus")

_without_glm_token()
chk("CLI: _run_work blocks unconfigured GLM (returns 2)", asyncio.run(M._run_work(_Cfg("glm"), [("a", "t")])) == 2)

# ── tally ──────────────────────────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok, _ in results if ok)
print("\n=========== MODEL SELECTION QA (EU-189 + EU-190) ===========")
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
