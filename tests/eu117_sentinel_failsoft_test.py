"""EU-117 regression: Sentinel must fail-soft on missing script/env errors.

The SRE post-merge gate should detect when the two_tenant_smoke script is missing
or required environment variables are not configured, and skip the gate with a warning
instead of reverting the merge (which would affect EVERY automatixy merge).

Tests:
  • _is_misconfigured_error() detects "No such file or directory"
  • _is_misconfigured_error() detects missing env var messages
  • _check_required_env_vars() detects missing TENANT_* patterns
  • _check_required_env_vars() detects missing DEV_BASE_URL
  • guard() returns (True, "gate misconfigured - skipped") on misconfiguration
  • guard() still reverts on genuine failures (not misconfigured)
"""
import sys, types
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
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

# Pattern: "No such file or directory"
chk("detects missing script", sentinel._is_misconfigured_error(
    "sh: line 1: /tmp/nonexistent.py: No such file or directory"
))

# Pattern: two_tenant_smoke env-missing message
chk("detects TENANT_* not set", sentinel._is_misconfigured_error(
    "Error: required env var TENANT_1 not set"
))

chk("detects DEV_BASE_URL not set", sentinel._is_misconfigured_error(
    "Error: DEV_BASE_URL environment variable not set"
))

# Pattern: generic env missing
chk("detects generic env missing", sentinel._is_misconfigured_error(
    "required environment variable TEST not set"
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

# No gate_env configured -> pass (nothing to check)
ok, missing = sentinel._check_required_env_vars(app(env={}))
chk("no gate_env -> pass", ok)
chk("no gate_env -> no missing var", missing is None)

# Has TENANT_* and DEV_BASE_URL -> pass
ok, missing = sentinel._check_required_env_vars(app(env={
    "TENANT_1_API_KEY": "xxx",
    "TENANT_2_API_KEY": "yyy",
    "DEV_BASE_URL": "http://dev.example.com"
}))
chk("has TENANT_* and DEV_BASE_URL -> pass", ok)
chk("has required -> no missing var", missing is None)

# Missing TENANT_* pattern
ok, missing = sentinel._check_required_env_vars(app(env={
    "DEV_BASE_URL": "http://dev.example.com"
}))
chk("missing TENANT_* -> fail", not ok)
chk("missing TENANT_* -> returns pattern", missing == "TENANT_")

# Missing DEV_BASE_URL pattern
ok, missing = sentinel._check_required_env_vars(app(env={
    "TENANT_1_API_KEY": "xxx"
}))
chk("missing DEV_BASE_URL -> fail", not ok)
chk("missing DEV_BASE_URL -> returns pattern", missing == "DEV_BASE_URL")

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

# Scenario: Missing env var (caught by _check_required_env_vars)
g2, au2 = FakeGit(), Audit()
ok2, note2 = sentinel.guard(cfg, app(env={"OTHER_VAR": "xxx"}, post=["true"]),
                           ns(id="EU-117b", ephemeral=False), g2, "sha_env", au2)
chk("missing required env -> guard True (skip)", ok2)
chk("missing required env -> no revert", g2.reverted is None)
chk("missing required env -> misconfigured note", "misconfigured" in note2.lower())

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
