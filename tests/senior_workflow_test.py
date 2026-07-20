"""2026-07-19 Commander order — "make the unit work like a strong smart DEV team, not juniors."
Pins the three senior behaviors added that day, all evidence-driven (AUTO-198/AUTO-178/AUTO-15x):

  A. Review verdict reconciliation (loop.py, after the review audit record):
     (A1) FAIL + spec_met + minors-only (no blocker/major) → coerced to PASS → lands (the reviewer
          contradicted its own contract; a done deliverable must not churn);
     (A2) FAIL + spec_met + majors-no-blocker on the FINAL pass → coerced → advisory-ship: lands
          AND files the leftovers via filing.file_findings ("review_verdict_reconciled" audited);
     (A3) FAIL with a blocker NEVER reconciles (the reviewer doing its job);
     (A4) needs_human FAILs never reconcile (a product question still routes to PM/Commander).

  B. Execution-gate escalate-once (reviewer.py _enforce_execution_gate):
     (B1) first raise of an execution AC gap → FAIL + spec gap (unchanged EU-268 behavior);
     (B2) the SAME gap with its fingerprint in already_bounced → demoted to unverifiable_gaps,
          verdict left to the LLM (the AUTO-198 permanent-FAIL class);
     (B3) collect_unverifiable_fingerprints carries exec-gate gaps so pass N+1 sees the bounce.

  C. Turn-limit senior ladder (loop.py + builder.py):
     (C1) turn-limit + split declined → REQUEUED once ("turn_limit_retry" audited, marker persisted);
     (C2) second blow-out (marker set) → decision park with the honest depth-cap note, NOT a bare
          ERRORED, and the note no longer tells the Commander to split by hand;
     (C3) effort_plan boosts one level when the retry marker is set (turns_for gets real headroom);
     (C4) mark_turn_retry persist failure → NO retry (fail-closed, parks instead of looping);
     (C5) _try_scrum_split records "scrum_split_failed" with the refusal reason.
"""
import sys
import types
import asyncio
import tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        s.__dict__.update(k)

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop
import orchestrator.builder as builder_real
import orchestrator.filing as filing_mod
from orchestrator import reviewer as reviewer_mod
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import (
    BuildResult, GateResult, Outcome, QualityIssue, ReviewResult, Ticket, TicketReport, Verdict,
)
from orchestrator.filing import FilingResult

results = []


def chk(name, cond, detail=""):
    results.append((name, bool(cond), detail))


class Audit:
    def __init__(s):
        s.ev = []

    def record(s, event, **kw):
        s.ev.append({"event": event, **kw})

    @staticmethod
    def diff_hash(*a):
        return "aabbcc"


class Git:
    def has_changes(s):
        return True

    def diff_against_base(s):
        return "diff --git a/x b/x\n+change"

    def changed_paths(s):
        return ["orchestrator/loop.py"]


class Backlog:
    def __init__(s):
        s.comments = []

    def add_comment(s, ticket, body):
        s.comments.append(body)

    def set_status(s, *a, **k):
        pass


loop._notify = lambda cfg, text: None
loop.run_gate = lambda app, paths=None, **_: GateResult(passed=True, report="")
loop.run_deterministic_checks = lambda app, paths, diff: GateResult(passed=True, report="")


class FakeBuilder:
    turn_calls = []

    @staticmethod
    def effort_plan(cfg, it, ticket):
        return ("low", "sized")

    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        return BuildResult(ok=True, summary="did it", cost_usd=0.0, num_turns=1, raw="did it", tools=[])

    # the senior turn-limit ladder reads these off builder_mod — delegate to the real persisted impl
    turn_retry_count = staticmethod(builder_real.turn_retry_count)
    mark_turn_retry = staticmethod(builder_real.mark_turn_retry)
    turns_for = staticmethod(builder_real.turns_for)


loop.builder_mod = FakeBuilder

_review = [None]


async def _fake_review(diff, ticket, app, cfg, iteration=1, **_):
    return _review[0]


reviewer_mod.review = _fake_review

land_calls: list[int] = []


def _fake_land(ticket, app, cfg, git, backlog, audit, branch, iteration, cost, build, review, commenter=None):
    land_calls.append(1)
    return TicketReport(ticket.id, Outcome.MERGED, iteration, cost, app.name, branch)


loop._land = _fake_land

filing_calls: list[dict] = []


def _fake_file_findings(app, officer_label, report):
    filing_calls.append({"app": app.name, "label": officer_label, "report": report})
    return FilingResult(filed=["EU-9001"], deduped=[], lines=[])


filing_mod.file_findings = _fake_file_findings
loop.decisions.add = lambda *a, **k: f"{a[1].id}#escalate"
loop.decisions.reply_hint = lambda ticket_id: ""


async def _stub_decision_brief(cfg, ticket_id, raw):
    return ""


loop._decision_brief = _stub_decision_brief


def _cfg(tmp):
    return Config(
        apps=[AppConfig(name="Elite-Unit", repo_path=".", base_branch="dev",
                        protected_branch="main", backlog_backend="none")],
        audit_path=str(tmp / "audit.jsonl"),
        dry_run=False, use_worktree=False, pm_enabled=False, max_iterations=1,
    )


def _run(review: ReviewResult):
    land_calls.clear()
    filing_calls.clear()
    _review[0] = review
    tmp = Path(tempfile.mkdtemp())
    cfg = _cfg(tmp)
    app = cfg.app("Elite-Unit")
    ticket = Ticket(id="EU-1", key="EU-1", summary="s", description="d", app="Elite-Unit")
    audit = Audit()
    report = asyncio.run(
        loop._attempt(ticket, app, cfg, Git(), Backlog(), audit, loop.Budget(0), "autodev/EU-1"))
    return report, audit


# =========================================================================== A
r1, a1 = _run(ReviewResult(verdict=Verdict.FAIL, spec_met=True, quality_issues=[
    QualityIssue(severity="minor", area="tests", detail="selector could be more robust")],
    summary="criteria fully met, two minor concerns"))
chk("(A1) FAIL + spec_met + minors-only → reconciled and LANDS", r1.outcome == Outcome.MERGED, r1.outcome)
chk("(A1) reconciliation audited",
    any(e["event"] == "review_verdict_reconciled" and e["reason"] == "fail-with-minors-only" for e in a1.ev))

r2, a2 = _run(ReviewResult(verdict=Verdict.FAIL, spec_met=True, quality_issues=[
    QualityIssue(severity="major", area="tests", detail="coverage breadth below wish")],
    summary="criteria met; wants more tests"))
chk("(A2) final-pass FAIL + spec_met + majors-no-blocker → advisory-ships (lands)",
    r2.outcome == Outcome.MERGED, r2.outcome)
chk("(A2) the major got FILED as a backlog ticket", len(filing_calls) == 1, filing_calls)
chk("(A2) reconciled with the final-pass reason",
    any(e["event"] == "review_verdict_reconciled" and e["reason"] == "final-pass-criteria-met-majors"
        for e in a2.ev))
chk("(A2) advisory ship audited", any(e["event"] == "shipped_with_advisories" for e in a2.ev))

r3, a3 = _run(ReviewResult(verdict=Verdict.FAIL, spec_met=True, quality_issues=[
    QualityIssue(severity="blocker", area="security", detail="SQL via string concat")],
    summary="real blocker"))
chk("(A3) FAIL with a blocker NEVER reconciles (no land)", r3.outcome != Outcome.MERGED, r3.outcome)
chk("(A3) no reconciliation event", not any(e["event"] == "review_verdict_reconciled" for e in a3.ev))

r4, a4 = _run(ReviewResult(verdict=Verdict.FAIL, spec_met=True, needs_human=True,
                           question="which currency should refunds use?", quality_issues=[],
                           summary="product question"))
chk("(A4) needs_human FAIL never reconciles", not any(e["event"] == "review_verdict_reconciled" for e in a4.ev))

# =========================================================================== B
_t = Ticket(id="EU-2", key="EU-2", summary="s", description="d", app="Elite-Unit",
            acceptance_criteria=["All 6 sync tests pass on Mobile Safari"])
_r = ReviewResult(verdict=Verdict.PASS, spec_met=True, quality_issues=[], summary="ok")
out1 = reviewer_mod._enforce_execution_gate(_r, _t, None, "", gate_evidence="", already_bounced=set())
chk("(B1) first raise → forced FAIL with a spec gap (EU-268 unchanged)",
    out1.verdict == Verdict.FAIL and not out1.spec_met and len(out1.spec_gaps) == 1, vars(out1))
fps = reviewer_mod.collect_unverifiable_fingerprints(out1)
chk("(B3) exec-gate gap fingerprint collected for the next pass", len(fps) >= 1, fps)

_r2 = ReviewResult(verdict=Verdict.PASS, spec_met=True, quality_issues=[], summary="ok")
out2 = reviewer_mod._enforce_execution_gate(_r2, _t, None, "", gate_evidence="", already_bounced=fps)
chk("(B2) repeat of the same gap → demoted to unverifiable_gaps, LLM verdict stands",
    out2.verdict == Verdict.PASS and out2.spec_met and not out2.spec_gaps
    and len(out2.unverifiable_gaps) == 1, vars(out2))

# =========================================================================== C
async def _fail(*a, **k):
    return {"ok": False, "keys": [], "error": "max auto-split depth (3) reached"}


import orchestrator.scrum as scrum_mod
_orig_split = scrum_mod.split
scrum_mod.split = _fail

tmp = Path(tempfile.mkdtemp())
cfg = _cfg(tmp)
app = cfg.app("Elite-Unit")
tk = Ticket(id="EU-3", key="EU-3", summary="big", description="d", app="Elite-Unit")
aud = Audit()

rep1 = asyncio.run(loop._exception_report(cfg, tk, app, RuntimeError(
    "Claude Code run failed: reached the maximum number of turns"), aud))
chk("(C1) turn-limit + split declined → REQUEUED once", rep1.outcome == Outcome.REQUEUED, rep1.outcome)
chk("(C1) turn_limit_retry audited", any(e["event"] == "turn_limit_retry" for e in aud.ev))
chk("(C5) scrum_split_failed audited with the refusal reason",
    any(e["event"] == "scrum_split_failed" and "depth" in str(e.get("error", "")) for e in aud.ev))
chk("(C3) effort_plan boosts one level while the marker is set",
    "turn-limit retry" in builder_real.effort_plan(cfg, 1, tk)[1])

rep2 = asyncio.run(loop._exception_report(cfg, tk, app, RuntimeError(
    "Claude Code run failed: reached the maximum number of turns"), aud))
chk("(C2) second blow-out → ESCALATED decision park, not ERRORED",
    rep2.outcome == Outcome.ESCALATED, rep2.outcome)
_q = next((e for e in aud.ev if e["event"] == "needs_human"), {})
chk("(C2) the park note is honest (depth cap named, no 'split it yourself')",
    "depth cap" in _q.get("question", "") and "Split it into smaller tickets, or raise" not in _q.get("question", ""))

_orig_mark = builder_real.mark_turn_retry
builder_real.mark_turn_retry = FakeBuilder.mark_turn_retry = lambda cfg, tid: False
tk4 = Ticket(id="EU-4", key="EU-4", summary="big", description="d", app="Elite-Unit")
aud4 = Audit()
rep4 = asyncio.run(loop._exception_report(cfg, tk4, app, RuntimeError(
    "reached the maximum number of turns"), aud4))
builder_real.mark_turn_retry = FakeBuilder.mark_turn_retry = _orig_mark
chk("(C4) marker persist failure → fail-closed park (no retry loop)",
    rep4.outcome == Outcome.ESCALATED and not any(e["event"] == "turn_limit_retry" for e in aud4.ev),
    rep4.outcome)

scrum_mod.split = _orig_split

# =========================================================================== D
# 2026-07-20 Commander order: a land needing MANUAL testing carries the exact steps and goes to
# the Blocked column ('Needs Human'), not QA. Pinned on the _manual_test_block helper + source.
_b_manual = BuildResult(ok=True, cost_usd=0.0, num_turns=1, tools=[], raw="",
                        summary="did it\n\nMANUAL TEST:\n1. Open /trips on iPhone Safari\n"
                                "2. Pick a trip May 1-7\n3. Every day 1..7 shows the highlight\n\n"
                                "TEST: /trips")
_r_clean = ReviewResult(verdict=Verdict.PASS, spec_met=True, quality_issues=[], summary="ok")
blk = loop._manual_test_block(_b_manual, _r_clean)
chk("(D1) the Builder's MANUAL TEST block is extracted verbatim",
    blk is not None and "iPhone Safari" in blk and "May 1-7" in blk and "TEST: /trips" not in blk, blk)
_b_clean = BuildResult(ok=True, summary="did it\nTEST: /x", cost_usd=0.0, num_turns=1, raw="", tools=[])
_r_gaps = ReviewResult(verdict=Verdict.PASS, spec_met=True, quality_issues=[], summary="ok",
                       unverifiable_gaps=["Mobile Safari matrix not runnable by the gate"])
blk2 = loop._manual_test_block(_b_clean, _r_gaps)
chk("(D2) unverifiable review gaps synthesize a numbered manual checklist",
    blk2 is not None and "1. Verify by hand" in blk2 and "Mobile Safari" in blk2, blk2)
chk("(D3) a fully-verified land has NO manual block (stays on the QA path)",
    loop._manual_test_block(_b_clean, _r_clean) is None)
_lsrc2 = Path("orchestrator/loop.py").read_text(encoding="utf-8")
chk("(D4) the land reroutes manual-test tickets to 'Needs Human' (the Blocked column) with the steps",
    'backlog.set_status(ticket, "Needs Human")' in _lsrc2
    and "MANUAL TEST STEPS:" in _lsrc2 and 'audit.record("manual_test_required"' in _lsrc2)
_bsrc2 = Path("orchestrator/builder.py").read_text(encoding="utf-8")
chk("(D5) the Builder contract demands exact numbered steps for anything it could not verify",
    "MANUAL TEST contract" in _bsrc2 and "NUMBERED, exact steps" in _bsrc2)

print("\n========== SENIOR WORKFLOW QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
