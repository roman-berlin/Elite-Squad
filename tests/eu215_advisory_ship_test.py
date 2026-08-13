"""EU-215: ship on advisory-only blocking.

2026-07-09 forensics (the AUTO-98 class, 2/14 "max passes — PM escalated" strandings): a reviewer
verdict of PASS + spec_met=True with only an advisory/hygiene "blocking" item (severity "major", e.g.
"update tenant-isolation doc") still failed ``review.is_ship_ready()`` (contracts.py counts "major" as
blocking) and burned passes until escalation. A done deliverable must not strand on advisory items.

Pins (orchestrator/loop.py, immediately before the ``if review.is_ship_ready():`` gate):

  1. PASS + spec_met=True + only minor/major leftover quality_issues (no "blocker") → the loop LANDS
     the ticket (``_land`` is called, outcome MERGED) AND files every leftover issue as a backlog
     ticket via ``filing.file_findings``.
  2. PASS + spec_met=True + at least one "blocker" quality_issue → the loop does NOT land — it falls
     through to the existing retry/escalate path exactly as today (no ``_land`` call).
  3. When the advisory-ship path fires, exactly one ``shipped_with_advisories`` audit event is recorded
     and its ``filed`` field equals the filed + deduped ticket keys the filing call returned.
  4. PASS + spec_met=True + zero quality_issues still takes the plain ``is_ship_ready()`` land path and
     never emits ``shipped_with_advisories``.

Import / stub pattern matches gate_flake_confirm_test.py / eu90_findings_triage_test.py — no network,
no real models, ``_land``/``run_gate``/the builder/reviewer are all stubbed.
"""
import sys
import types
import asyncio
import json
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Stub the Agent SDK so importing the orchestrator never reaches the network.
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------
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
    @staticmethod
    def effort_plan(cfg, it, ticket):
        return ("low", "sized")

    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        return BuildResult(ok=True, summary="did it", cost_usd=0.0, num_turns=1, raw="did it", tools=[])


loop.builder_mod = FakeBuilder

# the review returned this pass — swapped per test
_review = [None]


async def _fake_review(diff, ticket, app, cfg, iteration=1, **_):
    return _review[0]


reviewer_mod.review = _fake_review

# capture _land calls (never actually merge git in this harness)
land_calls: list[int] = []


def _fake_land(ticket, app, cfg, git, backlog, audit, branch, iteration, cost, build, review, commenter=None):
    land_calls.append(1)
    return TicketReport(ticket.id, Outcome.MERGED, iteration, cost, app.name, branch)


loop._land = _fake_land

# capture filing.file_findings calls; return canned filed + deduped keys so the audit assertion
# (filed == filing_result.filed + filing_result.deduped) has something concrete to check.
filing_calls: list[dict] = []


def _fake_file_findings(app, officer_label, report, audit=None):
    filing_calls.append({"app": app.name, "label": officer_label, "report": report,
                         "audit_threaded": audit is not None})
    return FilingResult(filed=["EU-9001"], deduped=["EU-9002"], lines=["✓ EU-9001 filed"])


filing_mod.file_findings = _fake_file_findings

# escalate-path stubs (only exercised by the blocker case, test 2)
loop.decisions.add = lambda *a, **k: f"{a[1].id}#escalate"
loop.decisions.reply_hint = lambda ticket_id: ""


async def _stub_decision_brief(cfg, ticket_id, raw):
    return ""


loop._decision_brief = _stub_decision_brief


def _run(review: ReviewResult, *, ephemeral: bool, dry_run: bool) -> tuple[TicketReport, Audit, Backlog]:
    """Drive one _attempt() call with the given canned review; returns (report, audit, backlog)."""
    land_calls.clear()
    filing_calls.clear()
    _review[0] = review

    tmp = Path(tempfile.mkdtemp())
    cfg = Config(
        apps=[AppConfig(name="Elite-Unit", repo_path=".", base_branch="dev",
                        protected_branch="main", backlog_backend="none")],
        audit_path=str(tmp / "audit.jsonl"),
        dry_run=dry_run,
        use_worktree=False,
        pm_enabled=False,        # keep the exhaustion path's mock surface clean (blocker case only)
        max_iterations=1,
    )
    app = cfg.app("Elite-Unit")
    ticket = Ticket(id="EU-215", key="EU-215", summary="ship on advisory-only blocking",
                    description="d", ephemeral=ephemeral, app="Elite-Unit")
    audit = Audit()
    backlog = Backlog()
    report = asyncio.run(
        loop._attempt(ticket, app, cfg, Git(), backlog, audit, loop.Budget(0), "autodev/EU-215")
    )
    return report, audit, backlog


# ===========================================================================
# Test 1 — PASS + spec_met + only major/minor (no blocker) → lands AND files
#           each leftover issue as a backlog ticket.
# ===========================================================================
review_advisory = ReviewResult(
    verdict=Verdict.PASS,
    spec_met=True,
    quality_issues=[
        QualityIssue(severity="major", area="docs", detail="update tenant-isolation doc"),
        QualityIssue(severity="minor", area="style", detail="rename local var for clarity"),
    ],
    summary="looks good, a couple of hygiene notes",
)
chk("sanity: this review is NOT is_ship_ready() (major counts as blocking today)",
    not review_advisory.is_ship_ready())
chk("sanity: this review has zero blocker-severity issues",
    not any(q.severity == "blocker" for q in review_advisory.quality_issues))

report1, audit1, bl1 = _run(review_advisory, ephemeral=False, dry_run=False)

chk("advisory-only: the ticket LANDS (outcome MERGED)", report1.outcome == Outcome.MERGED,
    report1.outcome)
chk("advisory-only: _land was actually invoked", land_calls == [1], land_calls)
chk("advisory-only: filing.file_findings was called exactly once", len(filing_calls) == 1,
    filing_calls)
chk("advisory-only: the loop's audit object is threaded into file_findings (EU-589)",
    bool(filing_calls) and filing_calls[0].get("audit_threaded") is True, filing_calls)
if filing_calls:
    _proposals = json.loads(
        filing_calls[0]["report"].split("===TICKETS===\n", 1)[1].rsplit("\n===END===", 1)[0]
    )
    chk("advisory-only: both leftover issues were proposed for filing", len(_proposals) == 2,
        _proposals)
    chk("advisory-only: the major issue's detail made it into the filed block",
        any("update tenant-isolation doc" in p.get("body", "") for p in _proposals), _proposals)
    chk("advisory-only: the minor issue's detail made it into the filed block",
        any("rename local var for clarity" in p.get("body", "") for p in _proposals), _proposals)

ship_events1 = [e for e in audit1.ev if e["event"] == "shipped_with_advisories"]
chk("advisory-only: exactly one shipped_with_advisories audit event", len(ship_events1) == 1,
    audit1.ev)
if ship_events1:
    chk("advisory-only: the audit event's filed field equals filed+deduped from the filing call",
        sorted(ship_events1[0].get("filed", [])) == sorted(["EU-9001", "EU-9002"]),
        ship_events1[0])


# ===========================================================================
# Test 2 — PASS + spec_met but WITH a blocker → does NOT land; behaves exactly
#           as today (falls through to the existing retry/escalate path).
# ===========================================================================
review_blocker = ReviewResult(
    verdict=Verdict.PASS,
    spec_met=True,
    quality_issues=[
        QualityIssue(severity="blocker", area="security", detail="SQL built via string concat"),
        QualityIssue(severity="minor", area="style", detail="unrelated nit"),
    ],
    summary="one real blocker present",
)
chk("sanity: a blocker-bearing review is NOT is_ship_ready()", not review_blocker.is_ship_ready())

report2, audit2, bl2 = _run(review_blocker, ephemeral=True, dry_run=True)

chk("blocker-present: the ticket does NOT land (outcome != MERGED)",
    report2.outcome != Outcome.MERGED, report2.outcome)
chk("blocker-present: _land was NEVER invoked", land_calls == [], land_calls)
chk("blocker-present: filing.file_findings was NEVER invoked via the advisory path",
    filing_calls == [], filing_calls)
chk("blocker-present: no shipped_with_advisories audit event was recorded",
    not any(e["event"] == "shipped_with_advisories" for e in audit2.ev), audit2.ev)
chk("blocker-present: it escalates exactly as today (needs_human audit event, max passes reason)",
    any(e["event"] == "needs_human" and "max passes" in (e.get("reason") or "") for e in audit2.ev),
    audit2.ev)


# ===========================================================================
# Test 3 — PASS + spec_met + zero quality_issues → the plain is_ship_ready()
#           path lands; shipped_with_advisories must NEVER fire here.
# ===========================================================================
review_clean = ReviewResult(
    verdict=Verdict.PASS,
    spec_met=True,
    quality_issues=[],
    summary="clean pass, nothing to report",
)
chk("sanity: a clean PASS review IS is_ship_ready()", review_clean.is_ship_ready())

report3, audit3, bl3 = _run(review_clean, ephemeral=False, dry_run=False)

chk("clean pass: the ticket lands via the plain is_ship_ready() path", report3.outcome == Outcome.MERGED,
    report3.outcome)
chk("clean pass: _land was invoked", land_calls == [1], land_calls)
chk("clean pass: filing.file_findings was NOT called (nothing to file)", filing_calls == [],
    filing_calls)
chk("clean pass: no shipped_with_advisories audit event",
    not any(e["event"] == "shipped_with_advisories" for e in audit3.ev), audit3.ev)


# ===========================================================================
# Results
# ===========================================================================
passed = sum(1 for _, ok, _ in results if ok)
print("\n========== EU-215 ADVISORY-ONLY SHIP QA ==========")
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
