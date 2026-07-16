"""EU-353 regression: escalated `ReviewResult.unverifiable_gaps` must reach the Commander exactly
once at the terminal outcome of a ticket attempt (land or max-passes escalate) — not zero times
(today's behaviour: nothing surfaces the field anywhere), and not re-posted on every retry.

Pins the three testable acceptance criteria against the two terminal call sites named in the
design brief:

  1. `loop._land`'s MERGED branch — a clean trial-merge + green post-merge gate — posts exactly
     ONE Jira comment carrying the fixed 'Unverifiable ACs' label and every gap string when
     `review.unverifiable_gaps` is non-empty; zero such comments when it is empty.
  2. `loop._attempt`'s max-passes exhaustion site (`Outcome.ESCALATED`, notes
     "max_iterations reached without a passing review") does the same, using the LAST `review`
     binding the attempt saw.
  3. Both terminal sites fold the escalated gap strings into the returned `TicketReport.notes`.
  4. Calling either terminal path twice for the SAME ticket_id (retries within one attempt,
     sharing one `TicketCommenter` instance) posts the unverifiable-gaps comment only once — the
     second call is suppressed by `TicketCommenter`'s per-instance idempotence guard.

All offline — SDK stubbed, no network, no real models; `jira_adapter.TicketCommenter` is
exercised for REAL (only `backlog.add_comment` is faked) so the formatting/idempotence logic
gets genuine coverage, not just the loop wiring.
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

import orchestrator.loop as loop                                  # noqa: E402
from orchestrator import jira_adapter as jira_commenter            # noqa: E402
from orchestrator.config import Config, AppConfig                  # noqa: E402
from orchestrator.contracts import (                               # noqa: E402
    BuildResult, GateResult, Outcome, QualityIssue, ReviewResult, Ticket, Verdict,
)

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


GAP_1 = "cannot verify pixel alignment on mobile — no browser access"
GAP_2 = "cannot confirm the animation timing matches the mockup"
GAPS = [GAP_1, GAP_2]
LABEL = jira_commenter.TicketCommenter.UNVERIFIABLE_GAPS_LABEL

tmp = Path(tempfile.mkdtemp())


class FakeBacklog:
    """Captures every add_comment call; set_status/attach_pr are no-ops."""
    def __init__(self):
        self.comments: list[str] = []

    def add_comment(self, ticket, body: str) -> None:
        self.comments.append(body)

    def set_status(self, *a, **k): pass
    def attach_pr(self, *a, **k): pass

    def gap_comments(self) -> list[str]:
        return [c for c in self.comments if LABEL in c]


app_cfg = AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev",
                    protected_branch="main", backlog_backend="none")


def mk_ticket(tid: str) -> Ticket:
    return Ticket(id=tid, key=tid, summary="s", description="d", ephemeral=False, app="Elite-Unit")


# ══════════════════════════════════════════════════════════════════════════════
# AC 1 + 3 + 4 — loop._land MERGED branch
# ══════════════════════════════════════════════════════════════════════════════
class _GitMerge:
    def commit_all(self, *a, **k): pass
    def trial_merge(self, *a, **k): return True
    def changed_paths(self, *a, **k): return []
    def current_sha(self, *a, **k): return "deadbeef"
    def land_trial(self, *a, **k): pass
    def abandon_trial(self, *a, **k): pass
    def delete_local_branch(self, *a, **k): pass
    def delete_remote_branch(self, *a, **k): pass
    def sync_main_base(self, *a, **k): return ""


class _CapAudit:
    def record(self, *a, **k): pass


bld = BuildResult(ok=True, summary="build did the thing", cost_usd=0.0, num_turns=1, raw="")

orig_gate, orig_notify, orig_changelog = loop.run_gate, loop._notify, loop._record_changelog
loop.run_gate = lambda *a, **k: GateResult(passed=True, report="ok")
loop._notify = lambda *a, **k: None
loop._record_changelog = lambda *a, **k: None
try:
    live_cfg = Config(apps=[app_cfg], audit_path=str(tmp / "audit-land.jsonl"), dry_run=False)

    # --- non-empty unverifiable_gaps: exactly one gap comment, notes carry the gaps ---
    rev_gaps = ReviewResult(verdict=Verdict.PASS, spec_met=True, unverifiable_gaps=list(GAPS))
    bl1 = FakeBacklog()
    commenter1 = jira_commenter.TicketCommenter(live_cfg, dry_run=False, no_comment=False)
    tkt1 = mk_ticket("EU-353A")

    rep1 = loop._land(tkt1, app_cfg, live_cfg, _GitMerge(), bl1, _CapAudit(),
                      branch="autodev/EU-353A", iteration=1, cost=0.0, build=bld, review=rev_gaps,
                      commenter=commenter1)

    chk("AC1: MERGED land with non-empty unverifiable_gaps posts exactly ONE gap comment",
        len(bl1.gap_comments()) == 1, str(bl1.comments))
    chk("AC1: the posted comment carries the fixed label",
        bl1.gap_comments() and LABEL in bl1.gap_comments()[0], str(bl1.gap_comments()))
    chk("AC1: the posted comment carries every gap string",
        bl1.gap_comments() and all(g in bl1.gap_comments()[0] for g in GAPS), str(bl1.gap_comments()))
    chk("AC3: TicketReport.notes carries every escalated gap string",
        rep1.outcome == Outcome.MERGED and all(g in rep1.notes for g in GAPS), rep1.notes)

    # --- idempotence: a second terminal call for the SAME ticket_id (same commenter) posts nothing more ---
    rep1b = loop._land(tkt1, app_cfg, live_cfg, _GitMerge(), bl1, _CapAudit(),
                       branch="autodev/EU-353A", iteration=2, cost=0.0, build=bld, review=rev_gaps,
                       commenter=commenter1)
    chk("AC4: a second _land call for the same ticket_id does NOT re-post the gap comment",
        len(bl1.gap_comments()) == 1, str(bl1.comments))
    chk("AC4: the second call's report still carries the gaps in notes (visibility unaffected by the guard)",
        all(g in rep1b.notes for g in GAPS), rep1b.notes)

    # --- empty unverifiable_gaps: zero gap comments ---
    rev_empty = ReviewResult(verdict=Verdict.PASS, spec_met=True, unverifiable_gaps=[])
    bl2 = FakeBacklog()
    commenter2 = jira_commenter.TicketCommenter(live_cfg, dry_run=False, no_comment=False)
    tkt2 = mk_ticket("EU-353B")

    rep2 = loop._land(tkt2, app_cfg, live_cfg, _GitMerge(), bl2, _CapAudit(),
                      branch="autodev/EU-353B", iteration=1, cost=0.0, build=bld, review=rev_empty,
                      commenter=commenter2)
    chk("AC1: MERGED land with EMPTY unverifiable_gaps posts ZERO gap comments",
        len(bl2.gap_comments()) == 0, str(bl2.comments))
    chk("AC1 sanity: empty-gaps land still reports MERGED", rep2.outcome == Outcome.MERGED, str(rep2.outcome))
finally:
    loop.run_gate, loop._notify, loop._record_changelog = orig_gate, orig_notify, orig_changelog


# ══════════════════════════════════════════════════════════════════════════════
# AC 2 + 3 + 4 — loop._attempt's max-passes exhaustion site
# ══════════════════════════════════════════════════════════════════════════════
class StubGit:
    def has_changes(self): return True
    def diff_against_base(self): return "--- a\n+++ b\n@@ stub diff @@"
    def changed_paths(self): return []


class StubBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "stub")

    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        return BuildResult(ok=True, summary="stub build", cost_usd=0.0, num_turns=1, raw="stub build")


def _mkcfg(tag: str) -> Config:
    # dry_run=False: _attempt's max-passes escalation path itself gates its Jira comment on
    # `not cfg.dry_run` (mirrors the live land path) — a dry_run cfg would make every assertion
    # below about REAL posted comments vacuous.
    return Config(apps=[app_cfg], audit_path=str(tmp / f"audit-{tag}.jsonl"), dry_run=False,
                 max_iterations=1, pm_enabled=False, use_worktree=False)


async def _stub_review_unverifiable(diff, ticket, app, cfg, iteration=1, *, store=None,
                                    build_artifact=None, already_bounced=None):
    # verdict FAIL / not ship-ready, no quality_issues (keeps the PM-findings-triage side path a
    # no-op) — unverifiable_gaps is populated directly, independent of quality_issues, exactly as
    # `_enforce_bounce_once` (EU-351) would leave it on a demoted finding.
    return ReviewResult(
        verdict=Verdict.FAIL, spec_met=False, quality_issues=[],
        required_changes=["fix the widget spacing"],
        unverifiable_gaps=list(GAPS),
        summary="stub reviewer fail", needs_human=False, raw="",
    )


orig_notify2, orig_builder, orig_review = loop._notify, loop.builder_mod, loop.reviewer_mod.review
orig_decisions_add, orig_decisions_load = loop.decisions.add, loop.decisions.load
orig_decisions_hint, orig_decision_brief = loop.decisions.reply_hint, loop._decision_brief
loop.run_gate = lambda *a, **k: GateResult(passed=True, report="")
loop._notify = lambda *a, **k: None
loop.builder_mod = StubBuilder
loop.reviewer_mod.review = _stub_review_unverifiable
loop.decisions.add = lambda cfg, ticket, app_name, question, entry_id=None, **kw: entry_id or ticket.id
loop.decisions.load = lambda cfg: []
loop.decisions.reply_hint = lambda ticket_id: ""


async def _stub_decision_brief(cfg, ticket_id, raw):
    return "(brief)"


loop._decision_brief = _stub_decision_brief
try:
    cfg_a = _mkcfg("attempt-a")
    bl3 = FakeBacklog()
    commenter3 = jira_commenter.TicketCommenter(cfg_a, dry_run=cfg_a.dry_run, no_comment=False)
    tkt3 = mk_ticket("EU-353C")

    rep3 = asyncio.run(loop._attempt(tkt3, app_cfg, cfg_a, StubGit(), bl3, _CapAudit(), loop.Budget(0),
                                     "autodev/EU-353C", commenter=commenter3))

    chk("AC2 sanity: max-passes exhaustion actually reaches Outcome.ESCALATED via the "
        "'max_iterations reached' site",
        rep3.outcome == Outcome.ESCALATED
        and rep3.notes.startswith("max_iterations reached without a passing review"),
        f"outcome={rep3.outcome} notes={rep3.notes!r}")
    chk("AC2: max-passes ESCALATED with non-empty unverifiable_gaps posts exactly ONE gap comment",
        len(bl3.gap_comments()) == 1, str(bl3.comments))
    chk("AC2: the posted comment carries the fixed label and every gap string",
        bl3.gap_comments() and LABEL in bl3.gap_comments()[0]
        and all(g in bl3.gap_comments()[0] for g in GAPS), str(bl3.gap_comments()))
    chk("AC3: TicketReport.notes carries every escalated gap string",
        all(g in rep3.notes for g in GAPS), rep3.notes)

    # --- idempotence: re-invoke the same terminal path for the same ticket_id/commenter ---
    tkt3b = mk_ticket("EU-353C")   # same id/key as tkt3 — simulates a retry within the attempt
    rep3b = asyncio.run(loop._attempt(tkt3b, app_cfg, cfg_a, StubGit(), bl3, _CapAudit(), loop.Budget(0),
                                      "autodev/EU-353C", commenter=commenter3))
    chk("AC4: re-invoking the max-passes path for the same ticket_id does NOT re-post the gap comment",
        len(bl3.gap_comments()) == 1, str(bl3.comments))
    chk("AC4: the second call's report still carries the gaps in notes",
        all(g in rep3b.notes for g in GAPS), rep3b.notes)

    # --- zero comments when unverifiable_gaps is empty ---
    async def _stub_review_no_gaps(diff, ticket, app, cfg, iteration=1, *, store=None,
                                   build_artifact=None, already_bounced=None):
        return ReviewResult(
            verdict=Verdict.FAIL, spec_met=False, quality_issues=[],
            required_changes=["fix the widget spacing"], unverifiable_gaps=[],
            summary="stub reviewer fail", needs_human=False, raw="",
        )

    loop.reviewer_mod.review = _stub_review_no_gaps
    cfg_b = _mkcfg("attempt-b")
    bl4 = FakeBacklog()
    commenter4 = jira_commenter.TicketCommenter(cfg_b, dry_run=cfg_b.dry_run, no_comment=False)
    tkt4 = mk_ticket("EU-353D")

    rep4 = asyncio.run(loop._attempt(tkt4, app_cfg, cfg_b, StubGit(), bl4, _CapAudit(), loop.Budget(0),
                                     "autodev/EU-353D", commenter=commenter4))
    chk("AC2: max-passes ESCALATED with EMPTY unverifiable_gaps posts ZERO gap comments",
        len(bl4.gap_comments()) == 0, str(bl4.comments))
    chk("AC2 sanity: empty-gaps escalation still reaches Outcome.ESCALATED", rep4.outcome == Outcome.ESCALATED,
        str(rep4.outcome))
finally:
    loop._notify = orig_notify2
    loop.builder_mod = orig_builder
    loop.reviewer_mod.review = orig_review
    loop.decisions.add = orig_decisions_add
    loop.decisions.load = orig_decisions_load
    loop.decisions.reply_hint = orig_decisions_hint
    loop._decision_brief = orig_decision_brief
    loop.run_gate = orig_gate


print("\n========== EU-353 UNVERIFIABLE-GAPS ESCALATION QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
