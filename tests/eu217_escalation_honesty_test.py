"""EU-217: escalation-path honesty.

Two focused fixes, each pinned here:

  (1) Gate-flake requeue: when the loop exhausts its passes because a gate failed
      IDENTICALLY twice in a row (last_changes[0].startswith("Verification failed")) and the
      PM's triage on that exhaustion says RESOLVE, the ticket requeues ONCE
      (Outcome.REQUEUED) instead of parking on the Commander — the PM diagnosed a
      pre-existing flake / already-done deliverable, not a genuine spec disagreement.
      `_already_pm_triaged` caps this at one triage per ticket, so it cannot loop.
      The Phase-2 retirement is otherwise UNCHANGED: a RESOLVE reached via a reviewer
      rejection (last_changes NOT starting with "Verification failed") still escalates.

  (2) Failure-evidence extraction: `gate.extract_failure_evidence()` replaces the naive
      `report[:2500]` slice in the loop's gate/deterministic-gate audit events — a red gate
      report where green suites fill the first 2500 chars must still surface the failing
      suite name, which a plain head-slice would silently drop (the EU-204/EU-206 symptom).

Import / mock pattern matches eu92_pm_auto_resolve_test.py (stub the Agent SDK, drive
loop._attempt directly with fakes — no git/network/LLM calls).
"""
import asyncio
import sys
import tempfile
import types
from pathlib import Path

# ---------------------------------------------------------------------------
# Stub the Agent SDK so importing the orchestrator never reaches the network.
# ---------------------------------------------------------------------------
sdk = types.ModuleType("claude_agent_sdk")

class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self

sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop
from orchestrator import pm as pm_mod
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import (
    BuildResult, GateResult, Outcome, QualityIssue, ReviewResult, Ticket, Verdict,
)
from orchestrator.gate import extract_failure_evidence

# ---------------------------------------------------------------------------
# Test result tracking
# ---------------------------------------------------------------------------
results = []

def chk(name, cond, detail=""):
    results.append((name, bool(cond), detail))


# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------
tmp = Path(tempfile.mkdtemp())

app_cfg = AppConfig(
    name="Elite-Unit",
    repo_path=str(tmp),
    base_branch="dev",
    protected_branch="main",
    backlog_backend="none",
)

ticket = Ticket(
    id="EU-217",
    key="EU-217",
    summary="gate-flake requeue honesty test",
    description="test fixture",
    ephemeral=False,
    app="Elite-Unit",
)

BRANCH = "autodev/EU-217-test"


class StubGit:
    def has_changes(self): return True
    def diff_against_base(self): return "--- a\n+++ b\n@@ stub diff @@"
    def changed_paths(self): return []
    def branch(self): return BRANCH


class StubBacklog:
    def __init__(self):
        self.comments: list[tuple[object, str]] = []

    def add_comment(self, ticket, body: str) -> None:
        self.comments.append((ticket, body))

    def set_status(self, *a, **k): pass
    def find_open_by_summary(self, *a, **k): return None
    def create_task(self, *a, **k): return "EU-999"


class StubAudit:
    def __init__(self):
        self.events: list[dict] = []

    def record(self, event, **kw):
        self.events.append({"event": event, **kw})

    @staticmethod
    def diff_hash(*a):
        return "aabbcc"


class FakeCommenter:
    """Never touches the network — the gate-failure path below calls this for real."""
    def summarize_gate_event(self, *a, **k): return None
    def post_comment(self, *a, **k): return True


class StubBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "stub")

    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        return BuildResult(
            ok=True, summary="stub: changed one file",
            cost_usd=0.0, num_turns=1, raw="stub: changed one file", tools=[],
        )


def _make_cfg(max_iterations=2, pm_enabled=True, tag="") -> Config:
    return Config(
        apps=[app_cfg],
        audit_path=str(tmp / f"audit-{tag}.jsonl"),
        dry_run=True,
        max_iterations=max_iterations,
        pm_enabled=pm_enabled,
        use_worktree=False,
    )


notify_calls: list[str] = []
dec_calls: list[dict] = []


def _install_loop_stubs():
    """Reset shared state and wire capturing stubs into the loop module."""
    notify_calls.clear()
    dec_calls.clear()

    def _capture_decisions_add(cfg, ticket, app_name, question, entry_id=None, **kw):
        dec_calls.append({"question": question})
        return entry_id or ticket.id

    loop.decisions.add = _capture_decisions_add
    loop.decisions.load = lambda cfg: []
    loop.decisions.reply_hint = lambda ticket_id: ""
    loop._notify = lambda cfg, text: notify_calls.append(text)
    loop.builder_mod = StubBuilder

    async def _stub_decision_brief(cfg, ticket_id, raw):
        return "(brief)"

    loop._decision_brief = _stub_decision_brief


# ===========================================================================
# AC1 — gate-exhaustion + PM RESOLVE -> requeue exactly once (Outcome.REQUEUED)
# ===========================================================================

# A red gate report with a stable failure signature (a FAILED-matching line), identical
# across passes so run_gate's confirmation re-run + the next pass's gate both reproduce the
# SAME gate_fingerprint -> the "identically two gates running" stuck-breaker trips.
RED_GATE_REPORT = "$ tests/run_all.py\n(exit 1)\n  ✗ eu217_stub_test.py   FAILED: stub failure\n"


async def _stub_triage_resolve(cfg, app_name, ticket_id, **_):
    return {"action": "RESOLVE", "text": "Pre-existing flake — deliverable is done.", "raw": ""}


def _run_gate_exhaustion_resolve():
    _install_loop_stubs()
    loop.run_gate = lambda app, paths=None, **_: GateResult(passed=False, report=RED_GATE_REPORT)
    pm_mod.triage = _stub_triage_resolve

    bl = StubBacklog()
    audit = StubAudit()
    cfg = _make_cfg(max_iterations=2, pm_enabled=True, tag="gate-resolve")
    budget = loop.Budget(0)

    report = asyncio.run(loop._attempt(ticket, app_cfg, cfg, StubGit(), bl, audit, budget, BRANCH,
                                       commenter=FakeCommenter()))
    return report, audit, bl


report1, audit1, bl1 = _run_gate_exhaustion_resolve()

chk(
    "AC1: gate-exhaustion + PM RESOLVE -> outcome is REQUEUED (requeued once)",
    report1.outcome == Outcome.REQUEUED,
    f"outcome={report1.outcome} notes={report1.notes!r}",
)
chk(
    "AC1: the canonical REQUEUED audit event (pm_triage) was recorded",
    any(e["event"] == Outcome.REQUEUED.audit_event for e in audit1.events),
    str([e["event"] for e in audit1.events]),
)
chk(
    "AC1: no Commander escalation was raised for the gate-flake requeue",
    len(dec_calls) == 0,
    str(dec_calls),
)
chk(
    "AC1: the fingerprint-stuck breaker actually fired (sanity — proves the gate path, not "
    "some other exhaustion route, drove this requeue)",
    any(e["event"] == "gate_fingerprint_stuck" for e in audit1.events),
    str([e["event"] for e in audit1.events]),
)


# ===========================================================================
# AC2 — RESOLVE off the gate path (a reviewer rejection) still escalates
#       (Phase-2 retirement preserved when last_changes is NOT a gate-exhaustion line)
# ===========================================================================

async def _stub_review_fail(diff, ticket, app, cfg, iteration, store=None, build_artifact=None):
    return ReviewResult(
        verdict=Verdict.FAIL, spec_met=False,
        quality_issues=[QualityIssue(severity="minor", area="style", detail="stub fail")],
        required_changes=["fix the style issue"],
        summary="stub reviewer fail",
        needs_human=False,
        raw="",
    )


async def _stub_triage_resolve_reviewer(cfg, app_name, ticket_id, **_):
    return {"action": "RESOLVE", "text": "Re-run the builder with a narrower scope.", "raw": ""}


def _run_reviewer_rejection_resolve():
    _install_loop_stubs()
    loop.run_gate = lambda app, paths=None, **_: GateResult(passed=True, report="")
    loop.reviewer_mod.review = _stub_review_fail

    async def _stub_triage_findings(review, tkt, cfg, diff="", files_changed=None):
        from orchestrator.pm import FindingsTriage
        return FindingsTriage(in_scope=[
            QualityIssue(severity="minor", area="style", detail="stub fail")
        ], out_of_scope=[], decisions=[])

    pm_mod.triage_findings = _stub_triage_findings
    pm_mod.triage = _stub_triage_resolve_reviewer

    bl = StubBacklog()
    audit = StubAudit()
    cfg = _make_cfg(max_iterations=1, pm_enabled=True, tag="reviewer-resolve")
    budget = loop.Budget(0)

    report = asyncio.run(loop._attempt(ticket, app_cfg, cfg, StubGit(), bl, audit, budget, BRANCH,
                                       commenter=FakeCommenter()))
    return report, audit, bl


report2, audit2, bl2 = _run_reviewer_rejection_resolve()

chk(
    "AC2: reviewer-rejection + PM RESOLVE -> outcome is ESCALATED (NOT requeued)",
    report2.outcome == Outcome.ESCALATED,
    f"outcome={report2.outcome} notes={report2.notes!r}",
)
chk(
    "AC2: the retirement event (pm_resolve_retired) fired, and NO requeue event was recorded",
    any(e["event"] == "pm_resolve_retired" for e in audit2.events)
    and not any(e["event"] == Outcome.REQUEUED.audit_event for e in audit2.events),
    str([e["event"] for e in audit2.events]),
)
chk(
    "AC2: the ticket still escalates to the Commander (decisions.add IS called)",
    len(dec_calls) == 1,
    str(dec_calls),
)


# ===========================================================================
# AC3 — extract_failure_evidence(): a failing-suite line buried past char 2500 behind
#       green suites survives, whereas a naive report[:2500] would drop it.
# ===========================================================================

_green_lines = "\n".join(f"  ✓ suite_{i:03d}_test.py           3/3 passed" for i in range(60))
_failing_line = "  ✗ eu204_regression_test.py     FAILED: assertion mismatch on line 42"
_run_all_tail = (
    "=" * 64 + "\n"
    "  HARNESSES: 60 passed / 61     TOTAL CHECKS: 612\n"
    "  FAILED: eu204_regression_test.py\n"
    + "=" * 64
)
BIG_REPORT = _green_lines + "\n" + _failing_line + "\n" + _run_all_tail

chk(
    "AC3 setup sanity: the naive report[:2500] slice actually DROPS the failing line "
    "(the bug this ticket fixes)",
    "eu204_regression_test.py" not in BIG_REPORT[:2500],
    f"len(BIG_REPORT)={len(BIG_REPORT)}",
)

evidence = extract_failure_evidence(BIG_REPORT, limit=2500)

chk(
    "AC3: extract_failure_evidence() output is capped at the limit",
    len(evidence) <= 2500,
    f"len(evidence)={len(evidence)}",
)
chk(
    "AC3: extract_failure_evidence() output INCLUDES the failing-suite line",
    "eu204_regression_test.py" in evidence and "FAILED:" in evidence,
    evidence[:300],
)
chk(
    "AC3: extract_failure_evidence() output INCLUDES the run_all summary tail (FAILED: line)",
    "FAILED: eu204_regression_test.py" in evidence,
    evidence[-300:],
)
chk(
    "AC3: a short report (under the limit) passes through unchanged",
    extract_failure_evidence("all green", limit=2500) == "all green",
)
chk(
    "AC3: a long report with NO failure signal at all falls back to a plain head slice "
    "(never raises, never returns something longer than the limit)",
    len(extract_failure_evidence("x" * 5000, limit=2500)) == 2500,
)
chk(
    "AC3: a 'timed out' report (no FAILED/✗/ERROR token) still counts as failure signal "
    "(gate.py's own timeout report reads '(timed out after Ns)')",
    "timed out after 30s" in extract_failure_evidence(
        _green_lines + "\n$ slow-cmd\n(timed out after 30s)\n" + _run_all_tail, limit=2500),
)


# ===========================================================================
# Results
# ===========================================================================
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if not ok and detail else ""))
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
