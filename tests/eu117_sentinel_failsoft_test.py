"""EU-117 regression: Sentinel must fail-soft on missing script/env errors.

The SRE post-merge gate should detect when the two_tenant_smoke script is missing
or required environment variables are not configured, and skip the gate with a warning
instead of reverting the merge (which would affect EVERY automatixy merge).

EU-359 CONTRACT CHANGE (2026-07-16 total audit) — the fail-soft INTENT below is unchanged and
still pinned end-to-end; the MECHANISM behind it changed, so the unit-level assertions moved:

  • _is_misconfigured_error is now anchored to the shell's own exit code (126/127) in the block
    header, not free text anywhere in the report. Matching "No such file or directory" / "not set"
    in the OUTPUT BODY was the false-skip vector: a genuine product failure printing those words
    was waved onto DEV instead of reverted. Assertions that fed bare free-text strings now feed the
    real "$ <cmd>\\n(exit <code>)\\n<tail>" block gate.run_commands emits.
  • _check_required_env_vars no longer demands a hardcoded TENANT_*/DEV_BASE_URL shape from every
    app with a gate_env — requirements derive from the app's own postmerge_commands /
    postmerge_required_env. Assertions pinning the two_tenant shape as *intended* are gone; the
    two_tenant app still gets the same protection, now because it declares/references those vars.

See tests/eu359_sentinel_anchoring_test.py for the new contract in full.

Tests:
  • _is_misconfigured_error() detects a shell-rejected command (exit 127 / 126)
  • _is_misconfigured_error() does NOT fire on product output that merely says "not found"
  • _check_required_env_vars() detects a missing env var the suite actually references
  • guard() returns (True, "gate misconfigured - skipped") on misconfiguration
  • guard() still reverts on genuine failures (not misconfigured)
"""
import os
import sys, types

# SDK stub for CI environments where claude-agent-sdk is not installed
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k):
        # Store keyword args as attributes so they can be read later
        for key, value in k.items():
            setattr(s, key, value)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

# Add SDK classes that orchestrator modules import
sdk.ClaudeAgentOptions = _D
sdk.AssistantMessage = _D
sdk.ToolUseBlock = _D
sdk.HookMatcher = _D
sdk.ResultMessage = _D
sdk.TextBlock = _D
sdk.query = _D()
sys.path.insert(0, ".")

from orchestrator import sentinel
from orchestrator.config import AppConfig, Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace

class Audit:
    def __init__(s): s.events = []
    def record(s, kind, **kw): s.events.append((kind, kw))

class FakeGit:
    def __init__(s): s.reverted = None
    def revert_merge_on_base(s, sha): s.reverted = sha; return True

def app(cmds=None, post=None, timeout=30, env=None):
    return AppConfig(name="automatixy", repo_path="/tmp", base_branch="DEV", workdir="/tmp",
                     gate_commands=list(cmds or []), postmerge_commands=list(post or []),
                     gate_timeout_sec=timeout, gate_env=dict(env or {}))

cfg = Config(apps=[], audit_path="/tmp/x.jsonl")

# --------------------------------------------------------------------------- #
# Test _is_misconfigured_error() pattern detection
# ---------------------------------------------------------------------------

# Missing script: the shell rejects it with 127. Same EU-117 scenario, now fed the real block
# shape gate.run_commands emits instead of a bare free-text line (EU-359).
chk("detects missing script", sentinel._is_misconfigured_error(
    "$ /tmp/nonexistent.py\n(exit 127)\nsh: line 1: /tmp/nonexistent.py: No such file or directory"
))

# The two_tenant_smoke script being unrunnable (present but not +x) is also a never-ran.
chk("detects non-executable script", sentinel._is_misconfigured_error(
    "$ ./two_tenant_smoke.py\n(exit 126)\nsh: ./two_tenant_smoke.py: Permission denied"
))

# EU-359: the same words in a SUITE'S OUTPUT are a real red — the suite ran and returned a verdict.
# These four used to return True (skip); that was the defect, not the contract.
chk("env-missing text at exit 1 is a real red", not sentinel._is_misconfigured_error(
    "$ ./two_tenant_smoke.py\n(exit 1)\nError: required env var TENANT_1 not set"
))

chk("DEV_BASE_URL text at exit 1 is a real red", not sentinel._is_misconfigured_error(
    "$ ./two_tenant_smoke.py\n(exit 1)\nError: DEV_BASE_URL environment variable not set"
))

chk("product FileNotFoundError is a real red", not sentinel._is_misconfigured_error(
    "$ pytest e2e/\n(exit 1)\nFAILED test_avatar - FileNotFoundError: No such file or directory"
))

# Should NOT match normal failures
chk("ignores normal exit code", not sentinel._is_misconfigured_error(
    "exit 1"
))

chk("ignores test failure", not sentinel._is_misconfigured_error(
    "FAIL: TestSomething failed"
))

chk("ignores empty report", not sentinel._is_misconfigured_error(""))

# --------------------------------------------------------------------------- #
# Test _check_required_env_vars()
# ---------------------------------------------------------------------------

# Hermetic: the check consults the real env the suite would inherit, so make sure the ambient
# shell isn't quietly satisfying these.
for _k in ("TENANT_1_API_KEY", "TENANT_2_API_KEY", "DEV_BASE_URL"):
    os.environ.pop(_k, None)

# The two_tenant suite as it actually reaches the shell — it REFERENCES the vars it needs, which is
# where the requirement now comes from (EU-359), instead of a hardcoded pattern list in sentinel.py.
TWO_TENANT = ["./two_tenant_smoke.py --tenant $TENANT_1_API_KEY --other $TENANT_2_API_KEY --base $DEV_BASE_URL"]

# No gate_env and no suite -> pass (nothing to check)
ok, missing = sentinel._check_required_env_vars(app(env={}))
chk("no gate_env -> pass", ok)
chk("no gate_env -> no missing var", missing is None)

# Has TENANT_* and DEV_BASE_URL -> pass
ok, missing = sentinel._check_required_env_vars(app(post=TWO_TENANT, env={
    "TENANT_1_API_KEY": "xxx",
    "TENANT_2_API_KEY": "yyy",
    "DEV_BASE_URL": "http://dev.example.com"
}))
chk("has TENANT_* and DEV_BASE_URL -> pass", ok, f"missing={missing!r}")
chk("has required -> no missing var", missing is None)

# Missing TENANT_* -> still caught, now by name rather than by pattern
ok, missing = sentinel._check_required_env_vars(app(post=TWO_TENANT, env={
    "DEV_BASE_URL": "http://dev.example.com"
}))
chk("missing TENANT_* -> fail", not ok)
chk("missing TENANT_* -> returns the var", missing == "TENANT_1_API_KEY", f"missing={missing!r}")

# Missing DEV_BASE_URL -> still caught
ok, missing = sentinel._check_required_env_vars(app(post=TWO_TENANT, env={
    "TENANT_1_API_KEY": "xxx",
    "TENANT_2_API_KEY": "yyy",
}))
chk("missing DEV_BASE_URL -> fail", not ok)
chk("missing DEV_BASE_URL -> returns the var", missing == "DEV_BASE_URL", f"missing={missing!r}")

# EU-359 regression: an app whose gate_env has NO TENANT_* is not "misconfigured" — it just has a
# different suite. The old hardcoded check returned (False, "TENANT_") here and skipped the gate,
# leaving the monitor structurally dead for every non-two_tenant app.
ok, missing = sentinel._check_required_env_vars(app(post=["bunx playwright test"], env={
    "NODE_OPTIONS": "--max-old-space-size=3072"
}))
chk("non-two_tenant gate_env -> pass (no false skip)", ok, f"missing={missing!r}")

# --------------------------------------------------------------------------- #
# Test guard() fail-soft behavior on misconfiguration
# ---------------------------------------------------------------------------

sentinel.notify.send = lambda *a, **k: None   # silence Telegram

# Scenario: Missing script -> should skip, not revert
g, au = FakeGit(), Audit()
ok, note = sentinel.guard(cfg, app(post=["/tmp/nonexistent.py"]),
                          ns(id="EU-117a", ephemeral=False), g, "sha_missing", au)
chk("missing script -> guard True (skip)", ok)
chk("missing script -> no revert", g.reverted is None)
chk("missing script -> misconfigured note", "misconfigured" in note.lower())

# Scenario: Missing env var (caught by _check_required_env_vars before the suite runs).
# EU-359: the suite must REFERENCE the var for it to be required — an app carrying an unrelated
# gate_env ({"OTHER_VAR": ...}) is not misconfigured, and pre-EU-359 this scenario skipped for the
# wrong reason (hardcoded TENANT_ miss) rather than for a genuinely absent requirement.
g2, au2 = FakeGit(), Audit()
ok2, note2 = sentinel.guard(cfg, app(env={"OTHER_VAR": "xxx"}, post=["./smoke.sh --base $EU117_ABSENT_BASE_URL"]),
                           ns(id="EU-117b", ephemeral=False), g2, "sha_env", au2)
chk("missing required env -> guard True (skip)", ok2)
chk("missing required env -> no revert", g2.reverted is None)
chk("missing required env -> misconfigured note", "misconfigured" in note2.lower(), note2)
chk("missing required env -> names the absent var", "EU117_ABSENT_BASE_URL" in note2, note2)

# Scenario: Genuine failure (not misconfigured) -> should revert as before
g3, au3 = FakeGit(), Audit()
ok3, note3 = sentinel.guard(cfg, app(post=["sh -c 'exit 1'"]),
                           ns(id="EU-117c", ephemeral=False), g3, "sha_fail", au3)
chk("genuine failure -> guard False (revert)", not ok3)
chk("genuine failure -> reverts merge", g3.reverted == "sha_fail")
chk("genuine failure -> not marked misconfigured", "misconfigured" not in note3.lower())

# Scenario: Green run -> should pass as before
g4, au4 = FakeGit(), Audit()
ok4, note4 = sentinel.guard(cfg, app(post=["true"]),
                           ns(id="EU-117d", ephemeral=False), g4, "sha_ok", au4)
chk("green run -> guard True (pass)", ok4)
chk("green run -> no revert", g4.reverted is None)

# --------------------------------------------------------------------------- #
# Print results
# --------------------------------------------------------------------------- #
print("\n================= EU-117 SENTINEL FAIL-SOFT QA =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
