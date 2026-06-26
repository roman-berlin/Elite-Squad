"""EU-51 land-path audit-event WIRING QA.

EU-51 made the `Outcome` enum the single source of truth for run outcomes: `loop._land` now records
its terminal audit events via `Outcome.<X>.audit_event` instead of hand-typed string literals
("merged"/"dryrun_land"/"pr_opened"). `outcome_audit_mapping_test.py` locks the enum→string contract
in isolation, but nothing pins that each `_land` CALL SITE uses the RIGHT enum member — a refactor
that wrote `Outcome.SKIPPED.audit_event` in the live-merge branch would pass every other harness while
silently mis-classifying every merged run in audit.jsonl/the dashboard.

This harness drives `loop._land` end-to-end (with fakes, no git/network) through its three terminal
paths and asserts the audit event string ACTUALLY recorded equals the recorded TicketReport's
Outcome.audit_event — so outcome and audit trail can never diverge at the source."""
import sys, types, tempfile
from pathlib import Path
from types import SimpleNamespace

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import loop
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, Outcome

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
app = AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev",
                protected_branch="main", backlog_backend="none")  # no postmerge_commands -> SRE skipped
live = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), dry_run=False)
dry = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), dry_run=True)
tkt = Ticket(id="EU-51", key="EU-51", summary="single-source outcomes", description="")


class _FakeBacklog:
    def set_status(self, *a, **k): pass
    def add_comment(self, *a, **k): pass
    def attach_pr(self, *a, **k): pass

class _CapAudit:
    """Captures every (event, kwargs) audit.record call so we can assert the exact event string."""
    def __init__(self): self.events = []
    def record(self, event, **k): self.events.append((event, k))

class _GitMerge:        # trial merges cleanly -> the live-merge (MERGED) path
    def commit_all(self, *a, **k): pass
    def trial_merge(self, *a, **k): return True
    def changed_paths(self, *a, **k): return []
    def current_sha(self, *a, **k): return "deadbeef"
    def land_trial(self, *a, **k): pass
    def abandon_trial(self, *a, **k): pass
    def delete_local_branch(self, *a, **k): pass
    def sync_main_base(self, *a, **k): return ""

class _GitConflict(_GitMerge):   # trial merge fails -> the PR_OPENED path
    def trial_merge(self, *a, **k): return False
    def push(self, *a, **k): pass
    def open_pr(self, *a, **k): return None

bld = SimpleNamespace(summary="build did the thing")
rev = SimpleNamespace(summary="review confirmed it")

orig_gate, orig_notify, orig_changelog = loop.run_gate, loop._notify, loop._record_changelog
loop.run_gate = lambda *a, **k: SimpleNamespace(passed=True, report="ok")
loop._notify = lambda *a, **k: None
loop._record_changelog = lambda *a, **k: None
try:
    # 1) LIVE clean merge -> Outcome.MERGED, recorded under its canonical "merged" event.
    a1 = _CapAudit()
    rep1 = loop._land(tkt, app, live, _GitMerge(), _FakeBacklog(), a1,
                      branch="autodev/EU-51", iteration=1, cost=0.0, build=bld, review=rev)
    events1 = [e for e, _ in a1.events]
    chk("live merge reports Outcome.MERGED", rep1.outcome == Outcome.MERGED, str(rep1.outcome))
    chk("live merge records the 'merged' audit event", "merged" in events1, str(events1))
    chk("merged event string == Outcome.MERGED.audit_event (single source)",
        Outcome.MERGED.audit_event in events1, str(events1))
    chk("recorded event matches the report's outcome (no source drift)",
        rep1.outcome.audit_event in events1, f"{rep1.outcome.audit_event} not in {events1}")

    # 2) DRY-RUN -> Outcome.SKIPPED, recorded under "dryrun_land".
    a2 = _CapAudit()
    rep2 = loop._land(tkt, app, dry, _GitMerge(), _FakeBacklog(), a2,
                      branch="autodev/EU-51", iteration=1, cost=0.0, build=bld, review=rev)
    events2 = [e for e, _ in a2.events]
    chk("dry-run reports Outcome.SKIPPED", rep2.outcome == Outcome.SKIPPED, str(rep2.outcome))
    chk("dry-run records 'dryrun_land' == Outcome.SKIPPED.audit_event",
        Outcome.SKIPPED.audit_event in events2 and "dryrun_land" in events2, str(events2))
    chk("dry-run event matches the report's outcome", rep2.outcome.audit_event in events2, str(events2))

    # 3) LIVE but unmergeable -> Outcome.PR_OPENED, recorded under "pr_opened".
    a3 = _CapAudit()
    rep3 = loop._land(tkt, app, live, _GitConflict(), _FakeBacklog(), a3,
                      branch="autodev/EU-51", iteration=1, cost=0.0, build=bld, review=rev)
    events3 = [e for e, _ in a3.events]
    chk("unmergeable land reports Outcome.PR_OPENED", rep3.outcome == Outcome.PR_OPENED, str(rep3.outcome))
    chk("PR path records 'pr_opened' == Outcome.PR_OPENED.audit_event",
        Outcome.PR_OPENED.audit_event in events3 and "pr_opened" in events3, str(events3))
    chk("PR event matches the report's outcome", rep3.outcome.audit_event in events3, str(events3))

    # cross-cut: the three terminal paths record three DISTINCT events (no accidental collision).
    chk("the three land paths record distinct audit events",
        len({"merged", "dryrun_land", "pr_opened"} & (set(events1) | set(events2) | set(events3))) == 3,
        f"{events1} | {events2} | {events3}")
finally:
    loop.run_gate, loop._notify, loop._record_changelog = orig_gate, orig_notify, orig_changelog

print("\n=========== EU-51 LAND-PATH AUDIT-EVENT WIRING QA ===========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
