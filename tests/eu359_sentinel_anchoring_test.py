"""EU-359 regression: the SRE post-merge monitor must not false-skip / false-green.

From the 2026-07-16 total audit. Two structural defects in the EU-117 fail-soft (2058e53):

  1. ``_check_required_env_vars`` hardcoded ["TENANT_", "DEV_BASE_URL"] against *any* app with a
     non-empty gate_env. Probed against the live config shape, gate_env={"NODE_OPTIONS": ...}
     returned (False, "TENANT_") -> guard() answered True ("misconfigured — skipping"), i.e. a
     structural false-GREEN: the monitor was dead for every non-two_tenant app while claiming
     misconfiguration.
  2. ``_is_misconfigured_error`` free-text-matched "No such file or directory" / "not found"
     anywhere in the report, so a GENUINE product failure whose output happens to contain those
     words (a FileNotFoundError the diff introduced) was skipped instead of reverting.

What this pins:
  • an app whose gate_env has no TENANT_* still RUNS its suite (no skip)
  • a product failure printing "No such file or directory" at exit 1 still REVERTS
  • a real exit-127 missing command still fail-softs  <- the EU-117 behaviour must survive
  • required env derives from the app's own postmerge_commands ($VAR refs) / postmerge_required_env
"""
import os
import sys, types

# SDK stub for CI environments where claude-agent-sdk is not installed
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k):
        for key, value in k.items():
            setattr(s, key, value)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
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

def app(post=None, env=None, required=None):
    a = AppConfig(name="automatixy", repo_path="/tmp", base_branch="DEV", workdir="/tmp",
                  gate_commands=[], postmerge_commands=list(post or []),
                  gate_timeout_sec=30, gate_env=dict(env or {}))
    if required is not None:
        # setattr, not a constructor kwarg: sentinel reads this key defensively via getattr so the
        # logic fix does not depend on config.py having landed the field yet.
        setattr(a, "postmerge_required_env", list(required))
    return a

cfg = Config(apps=[], audit_path="/tmp/x.jsonl")
sentinel.notify.send = lambda *a, **k: None   # silence Telegram

# --------------------------------------------------------------------------- #
# AC1 — env-shape check derives from the app, not a hardcoded two_tenant shape
# --------------------------------------------------------------------------- #

# The live automatixy shape: gate_env carries a worker cap, no TENANT_* / DEV_BASE_URL anywhere.
ok, missing = sentinel._check_required_env_vars(app(post=["true"], env={"NODE_OPTIONS": "--max-old-space-size=3072"}))
chk("NODE_OPTIONS-only gate_env -> no requirement", ok, f"missing={missing!r}")
chk("NODE_OPTIONS-only gate_env -> nothing reported missing", missing is None)

# ...and end-to-end: the suite RUNS (green), it is not skipped as "misconfigured".
g, au = FakeGit(), Audit()
ok, note = sentinel.guard(cfg, app(post=["true"], env={"NODE_OPTIONS": "--max-old-space-size=3072"}),
                          ns(id="EU-359a", ephemeral=False), g, "sha_a", au)
chk("non-two_tenant app -> guard True", ok)
chk("non-two_tenant app -> suite actually RAN (not skipped)", "misconfigured" not in note.lower(), note)
chk("non-two_tenant app -> audited as a pass", any(k == "sentinel_pass" for k, _ in au.events), au.events)

# A command that references $VAR: the requirement is DERIVED from the command text.
ok, missing = sentinel._check_required_env_vars(app(post=["curl $EU359_FAKE_BASE_URL/health"]))
chk("derived $VAR absent -> flagged", not ok)
chk("derived $VAR absent -> names the var", missing == "EU359_FAKE_BASE_URL", f"missing={missing!r}")

ok, missing = sentinel._check_required_env_vars(
    app(post=["curl $EU359_FAKE_BASE_URL/health"], env={"EU359_FAKE_BASE_URL": "http://dev.example.com"}))
chk("derived $VAR satisfied by gate_env -> pass", ok, f"missing={missing!r}")

ok, missing = sentinel._check_required_env_vars(app(post=["curl ${EU359_FAKE_BASE_URL}/health"]))
chk("derived ${VAR} braces form -> flagged", not ok and missing == "EU359_FAKE_BASE_URL", f"missing={missing!r}")

# ${VAR:-default} carries its own fallback -> NOT a requirement.
ok, missing = sentinel._check_required_env_vars(app(post=["curl ${EU359_FAKE_BASE_URL:-http://localhost}/health"]))
chk("defaulted ${VAR:-x} -> not a requirement", ok, f"missing={missing!r}")

# $(subshell) and positional $1 are shell syntax, not env requirements.
ok, missing = sentinel._check_required_env_vars(app(post=["echo $(date) $1"]))
chk("$(subshell)/$1 -> not env requirements", ok, f"missing={missing!r}")

# A var the gate subprocess will actually inherit from os.environ is satisfied.
os.environ["EU359_PRESENT"] = "1"
ok, missing = sentinel._check_required_env_vars(app(post=["echo $EU359_PRESENT"]))
chk("var present in os.environ -> pass", ok, f"missing={missing!r}")

# An explicitly DECLARED requirement that is absent still skips.
ok, missing = sentinel._check_required_env_vars(app(post=["true"], required=["EU359_DECLARED_ABSENT"]))
chk("declared postmerge_required_env absent -> flagged", not ok and missing == "EU359_DECLARED_ABSENT", f"missing={missing!r}")

g, au = FakeGit(), Audit()
ok, note = sentinel.guard(cfg, app(post=["true"], required=["EU359_DECLARED_ABSENT"]),
                          ns(id="EU-359b", ephemeral=False), g, "sha_b", au)
chk("declared-absent env -> guard True (skip)", ok)
chk("declared-absent env -> no revert", g.reverted is None)
chk("declared-absent env -> misconfigured note", "misconfigured" in note.lower(), note)

# --------------------------------------------------------------------------- #
# AC2 — misconfig detection anchored to the shell's own verdict, not test output
# --------------------------------------------------------------------------- #

# The exact block shape gate.run_commands emits: "$ <cmd>\n(exit <code>)\n<output>".
chk("exit 127 header -> misconfig", sentinel._is_misconfigured_error(
    "$ /opt/two_tenant_smoke.py\n(exit 127)\nsh: /opt/two_tenant_smoke.py: No such file or directory"))
chk("exit 126 header -> misconfig", sentinel._is_misconfigured_error(
    "$ ./smoke.sh\n(exit 126)\nsh: ./smoke.sh: Permission denied"))

# The regression that motivated EU-359: a real product failure whose OUTPUT contains the words.
chk("product FileNotFoundError at exit 1 -> NOT misconfig", not sentinel._is_misconfigured_error(
    "$ pytest e2e/\n(exit 1)\nFAILED test_avatar - FileNotFoundError: [Errno 2] "
    "No such file or directory: '/var/data/avatar.png'"))
chk("product 'not found' assertion at exit 1 -> NOT misconfig", not sentinel._is_misconfigured_error(
    "$ bunx playwright test\n(exit 1)\nError: expected locator 'button' — element not found"))
chk("suite printing 'env var not set' at exit 1 -> NOT misconfig", not sentinel._is_misconfigured_error(
    "$ pytest e2e/\n(exit 1)\nAssertionError: required env var TENANT_1 not set"))

# Mixed report: a genuine red alongside a missing command must still revert.
chk("mixed 127 + genuine exit 1 -> NOT misconfig", not sentinel._is_misconfigured_error(
    "$ /opt/missing.py\n(exit 127)\nsh: /opt/missing.py: not found\n\n"
    "$ pytest e2e/\n(exit 1)\nFAILED test_checkout"))

# Timeouts and runner explosions are red, never a skip.
chk("timeout block -> NOT misconfig", not sentinel._is_misconfigured_error(
    "$ pytest e2e/\n(timed out after 1800s)\n[partial output before the kill]\ncollecting…"))
chk("runner explosion -> NOT misconfig", not sentinel._is_misconfigured_error(
    "sentinel suite could not run: OSError('boom')"))
chk("empty report -> NOT misconfig", not sentinel._is_misconfigured_error(""))

# --------------------------------------------------------------------------- #
# AC3 — end-to-end through the real gate runner
# --------------------------------------------------------------------------- #

# EU-117 must survive: a genuinely missing script (exit 127) still fail-softs.
g, au = FakeGit(), Audit()
ok, note = sentinel.guard(cfg, app(post=["/tmp/eu359_definitely_nonexistent.py"]),
                          ns(id="EU-359c", ephemeral=False), g, "sha_c", au)
chk("EU-117 lives: missing script -> guard True (skip)", ok, note)
chk("EU-117 lives: missing script -> no revert", g.reverted is None)
chk("EU-117 lives: missing script -> misconfigured note", "misconfigured" in note.lower(), note)

# The headline fix: a product failure whose output says "No such file or directory" REVERTS.
g, au = FakeGit(), Audit()
ok, note = sentinel.guard(cfg, app(post=["sh -c 'echo \"FAILED test_avatar - FileNotFoundError: "
                                         "[Errno 2] No such file or directory: /var/data/avatar.png\"; exit 1'"]),
                          ns(id="EU-359d", ephemeral=False), g, "sha_d", au)
chk("product FileNotFoundError -> guard False (revert)", not ok, note)
chk("product FileNotFoundError -> merge reverted", g.reverted == "sha_d")
chk("product FileNotFoundError -> audited as a revert", any(k == "sentinel_revert" for k, _ in au.events), au.events)

# --------------------------------------------------------------------------- #
print("\n================= EU-359 SENTINEL ANCHORING QA =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
