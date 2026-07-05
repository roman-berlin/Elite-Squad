"""EU-60 wiring QA — the two acceptance criteria the smoke-module test (eu60_smoke_test.py) can't
reach on its own:

  • CONFIG knobs: `smoke_enabled` is ARMED by default, `AppConfig.smoke_command` defaults to None
    (the framework is inert until an app opts a command in), and `Config.load` round-trips both
    from YAML (top-level toggle + per-app command) — the opt-in surface the PM decision relies on.

  • LOOP integration: after a successful land, `_land` runs the post-merge smoke on the landed
    base; on RED it FLAGS the merge (ticket comment + a "post-merge smoke FAILED" tail on the
    MERGED report) while the merge STILL STANDS — outcome MERGED, never reverted (that's the SRE's
    job). On GREEN it stays quiet (no fail comment, no flag in the notes). An ephemeral ticket is
    flagged in the notes but never gets a backlog comment. This drives the REAL `_land` over fakes
    so a regression in the wiring (drop the call, revert instead of flag, comment on the wrong
    branch) flips a check to FAIL — not a no-op green.

The smoke path here is REAL (real `smoke.should_run` + real `smoke.run`); only its gate command and
Telegram are stubbed, so the loop→smoke handshake is exercised end to end.
"""
import sys, types, tempfile
from pathlib import Path

# House pattern: stub the Agent SDK so importing orchestrator.* is cheap + offline.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

ROOT = Path(__file__).resolve().parent.parent

from orchestrator import loop, smoke, sentinel, dashboard
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import GateResult, Outcome

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace

# ============================================================================ #
# 1) CONFIG knobs — armed by default, opt-in command, YAML round-trip
# ============================================================================ #
chk("Config.smoke_enabled ARMED by default", Config(apps=[]).smoke_enabled is True)
chk("AppConfig.smoke_command defaults to None (inert until opted in)",
    AppConfig(name="a", repo_path="/tmp", base_branch="dev", protected_branch="main").smoke_command is None)

# YAML round-trip: top-level toggle + per-app command both load. repo_path must be a real git repo
# for validate(), so point it at this very repo.
yml = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
yml.write(
    "smoke_enabled: false\n"
    "apps:\n"
    "  - name: automatixy\n"
    f"    repo_path: \"{ROOT}\"\n"
    "    base_branch: dev\n"
    "    protected_branch: main\n"
    "    smoke_command: \"npx playwright test e2e/auth-redirect.smoke.ts\"\n"
)
yml.close()
loaded = Config.load(yml.name)
chk("Config.load reads top-level smoke_enabled from YAML", loaded.smoke_enabled is False)
chk("Config.load reads per-app smoke_command from YAML",
    loaded.app("automatixy").smoke_command == "npx playwright test e2e/auth-redirect.smoke.ts")

# ============================================================================ #
# Shared fakes for the loop-integration cases
# ============================================================================ #
class FakeGit:
    """Just enough of Git for the success land-path in _land. Notably it has NO revert method —
    if the wiring ever tried to roll back on a red smoke, the call would AttributeError and fail."""
    def __init__(s): s.landed = None
    def commit_all(s, msg): return "feat_sha"
    def trial_merge(s, branch, temp, msg): return True
    def changed_paths(s): return []
    def current_sha(s): return "merge_sha"
    def land_trial(s, temp): s.landed = temp
    def delete_local_branch(s, branch): pass

class FakeBacklog:
    def __init__(s): s.comments = []; s.status = None
    def set_status(s, ticket, status): s.status = status
    def add_comment(s, ticket, text): s.comments.append(text)

class Audit:
    def __init__(s): s.events = []
    def record(s, kind, **kw): s.events.append((kind, kw))

# Neutralise everything in _land that isn't the smoke wiring (offline, no real git/gate/notify).
loop.run_gate = lambda app, paths=None, **_: GateResult(passed=True, report="gate green")
loop._notify = lambda *a, **k: None
loop._record_changelog = lambda *a, **k: None
loop._bar = lambda *a, **k: None
loop._test_url = lambda *a, **k: ""
dashboard.brief = lambda text, n=220: (text or "")[:n]
sentinel.should_run = lambda cfg, app: False          # keep the SRE out of the way; smoke is the unit under test
smoke.notify.send = lambda *a, **k: None               # never hit Telegram for real

def app(cmd):
    return ns(name="automatixy", base_branch="DEV", branch_prefix="autodev",
              smoke_command=cmd, workdir="/tmp", repo_path="/tmp", gate_timeout_sec=30, gate_env={})

def cfg():
    return Config(apps=[], audit_path="/tmp/x.jsonl", smoke_enabled=True,
                  mark_done_on_merge=False, sync_base_after_merge=False, dry_run=False, merge_to_dev=True)

def land(cmd, ticket, gate_passed):
    """Drive the REAL _land to a successful merge, then the real smoke runs with a stubbed gate."""
    smoke.gate.run_commands = lambda a, c: GateResult(passed=gate_passed, report="auth-redirect: expected /login, got 500")
    g, bk, au = FakeGit(), FakeBacklog(), Audit()
    build = ns(summary="implemented the thing")
    review = ns(summary="LGTM")
    rep = loop._land(ticket, app(cmd), cfg(), g, bk, au, "autodev/AUTO-7", 1, 0.0, build, review)
    return rep, g, bk, au

# ============================================================================ #
# 2) RED smoke -> FLAG the merge (comment + note), merge STILL STANDS
# ============================================================================ #
rep, g, bk, au = land("npx playwright test", ns(id="AUTO-7", key="AUTO-7", summary="feature", ephemeral=False), gate_passed=False)
chk("red smoke -> outcome is still MERGED (merge stands, not reverted)", rep.outcome == Outcome.MERGED)
chk("red smoke -> land actually happened (trial landed)", g.landed is not None)
chk("red smoke -> MERGED note carries the smoke-FAILED flag", "smoke FAILED" in rep.notes)
chk("red smoke -> a backlog comment flags the failing smoke",
    any("Post-merge smoke FAILED" in c for c in bk.comments))
chk("red smoke -> the flag comment names the landed base (DEV)",
    any("Post-merge smoke FAILED" in c and "DEV" in c for c in bk.comments))
chk("red smoke -> audit recorded smoke_fail", any(k == "smoke_fail" for k, _ in au.events))
chk("red smoke -> ticket still moved to QA (merge not unwound)", bk.status == "QA")

# ============================================================================ #
# 3) GREEN smoke -> quiet: no fail comment, no flag in the notes
# ============================================================================ #
rep2, g2, bk2, au2 = land("npx playwright test", ns(id="AUTO-8", key="AUTO-8", summary="feature", ephemeral=False), gate_passed=True)
chk("green smoke -> outcome MERGED", rep2.outcome == Outcome.MERGED)
chk("green smoke -> notes do NOT mention a smoke failure", "smoke FAILED" not in rep2.notes)
chk("green smoke -> no smoke-fail backlog comment", not any("Post-merge smoke FAILED" in c for c in bk2.comments))
chk("green smoke -> audit recorded smoke_pass", any(k == "smoke_pass" for k, _ in au2.events))

# ============================================================================ #
# 4) RED smoke on an EPHEMERAL ticket -> flagged in notes, but NO backlog comment
# ============================================================================ #
rep3, g3, bk3, au3 = land("npx playwright test", ns(id="EPH-1", key="EPH-1", summary="probe", ephemeral=True), gate_passed=False)
chk("ephemeral red -> still MERGED", rep3.outcome == Outcome.MERGED)
chk("ephemeral red -> notes still carry the smoke flag", "smoke FAILED" in rep3.notes)
chk("ephemeral red -> NO backlog comment (ephemeral tickets are never commented)", bk3.comments == [])

# ============================================================================ #
# 5) Smoke DISABLED -> wiring is a no-op (no smoke run, clean MERGED)
# ============================================================================ #
smoke.gate.run_commands = lambda a, c: GateResult(passed=False, report="should never run")
g5, bk5, au5 = FakeGit(), FakeBacklog(), Audit()
off = Config(apps=[], audit_path="/tmp/x.jsonl", smoke_enabled=False,
             mark_done_on_merge=False, sync_base_after_merge=False, dry_run=False, merge_to_dev=True)
rep5 = loop._land(ns(id="AUTO-9", key="AUTO-9", summary="f", ephemeral=False), app("npx playwright test"), off,
                  g5, bk5, au5, "autodev/AUTO-9", 1, 0.0, ns(summary="b"), ns(summary="r"))
chk("smoke disabled -> MERGED with no smoke flag", rep5.outcome == Outcome.MERGED and "smoke FAILED" not in rep5.notes)
chk("smoke disabled -> no smoke audit event at all", not any(k.startswith("smoke_") for k, _ in au5.events))

print("\n================= EU-60 WIRING QA =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
