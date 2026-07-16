"""EU-230: route review.needs_human and the EU-90 pm-findings 'decisions' bucket through the
automode PM BEFORE parking on the Commander — mirroring the builder-halt path's decide-first
machinery (loop.py: _consult_pm / _pm_decide_before_park), instead of parking straight on Roman.

Fail-first coverage of the six testable acceptance criteria:
  1. review.needs_human + auto_mode on + PM DECIDE -> injects the decision into ticket.description,
     re-builds (continues), records `pm_decided` (automode=True), posts the '🤖 Automode' Jira
     comment; the first occurrence does NOT park (no pm-findings/needs_human decisions.add yet).
  2. review.needs_human + PM ESCALATE (explicit WHY) -> parks (Outcome.ESCALATED), `needs_human`
     audited, and the parked proposal begins with the WHY-CANNOT-RESOLVE line. Same for PM
     unavailable (returns None), minus the WHY line.
  3. EU-90 pm-findings 'decisions' bucket + PM DECIDE -> does NOT call decisions.add/notify for the
     pm-findings entry_id; injects + re-builds instead.
  4. EU-90 pm-findings 'decisions' bucket + PM ESCALATE -> still pages the Commander (unchanged
     behaviour: decisions.add with the pm-findings entry_id + a bulleted Needs-you notify).
  5. The PM is consulted AT MOST ONCE per ticket across the halt/needs_human/pm-findings paths
     (shared pm_used flag) -- a needs_human retry does not re-invoke the PM a second time.
  6. The audit trail distinguishes PM-decided (`pm_decided`, automode flag) from Commander-parked
     (`needs_human`) -- both event kinds are asserted directly off the audit log.

Import / mock pattern matches pm_loop_test.py and eu90_findings_triage_test.py (stub the Agent SDK,
drive loop._attempt directly with fakes -- no git/network/LLM calls).
"""
import sys
import types
import asyncio

sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop
from orchestrator import pm as pm_mod
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import (
    BuildResult, GateResult, Outcome, QualityIssue, ReviewResult, Ticket, Verdict,
)
from orchestrator.pm import FindingsTriage

results = []


def chk(name, cond, detail=""):
    results.append((name, bool(cond), detail))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
app_cfg = AppConfig(name="Elite-Unit", repo_path=".", base_branch="dev",
                     protected_branch="main", backlog_backend="none")

ticket = Ticket(id="EU-230", key="EU-230", summary="route needs_human through the PM",
                description="orig spec", ephemeral=False, app="Elite-Unit")

BRANCH = "autodev/EU-230-test"


def mkcfg(**kw):
    kw.setdefault("max_iterations", 2)
    kw.setdefault("pm_enabled", False)   # exhaustion-triage PM off (cleaner mock surface)
    return Config(apps=[app_cfg], audit_path="/tmp/eu230_audit.jsonl", use_worktree=False, **kw)


class Audit:
    def __init__(s): s.ev = []
    def record(s, e, **k): s.ev.append({"event": e, **k})


class Git:
    """Always reports changes so the loop proceeds past build into gate/review."""
    def has_changes(s): return True
    def diff_against_base(s): return "--- a\n+++ b\n@@ stub diff @@"
    def changed_paths(s): return []


class Backlog:
    def __init__(s): s.comments = []
    def add_comment(s, t, b): s.comments.append(b)
    def set_status(s, *a): pass
    def find_open_by_summary(s, *a, **k): return None
    def create_task(s, *a, **k): return "EU-999"


# ---------------------------------------------------------------------------
# Permanent stubs shared by every scenario
# ---------------------------------------------------------------------------
loop.run_gate = lambda app, paths=None, **_: GateResult(passed=True, report="")
loop.run_deterministic_checks = lambda app, paths, diff, **_: GateResult(passed=True, report="")


async def _stub_decision_brief(cfg, ticket_id, raw):
    # EU-337: the reviewer-findings ping now routes through _decision_brief. Echo the raw notes so
    # this harness (which checks the ping carries the finding detail) still exercises that content;
    # the real distillation preserves the substance while dropping the code identifiers.
    return raw


loop._decision_brief = _stub_decision_brief
loop.decisions.reply_hint = lambda ticket_id: ""

notify_calls = []
loop._notify = lambda cfg, text: notify_calls.append(text)

dec_calls = []


def _capture_decisions_add(cfg, ticket, app_name, question, entry_id=None, **kw):
    dec_calls.append({"question": question, "entry_id": entry_id})
    return entry_id or ticket.id


loop.decisions.add = _capture_decisions_add
loop.decisions.load = lambda cfg: []

built = []


class FakeBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")

    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        built.append(req.ticket.description or "")
        return BuildResult(ok=True, summary="built", cost_usd=0.0, num_turns=1, raw="built", tools=[])


loop.builder_mod = FakeBuilder

# PM exhaustion triage (the post-loop path) — irrelevant here (pm_enabled=False keeps it a no-op),
# but stub it anyway in case a scenario runs to iteration exhaustion.
async def _stub_pm_triage(cfg, app_name, ticket_id, **_):
    return {"action": "ESCALATE", "text": "stub escalate", "raw": ""}


pm_mod.triage = _stub_pm_triage


def _reset():
    built.clear()
    dec_calls.clear()
    notify_calls.clear()


# ===========================================================================
# AC1 + AC5 + AC6 — review.needs_human + PM DECIDE (auto_mode on):
#   inject + continue, pm_decided audited (automode), Automode comment posted, PM consulted once
#   even though needs_human fires again on the re-built pass (pm_used already spent).
# ===========================================================================
QUESTION = "Which billing tier is default — Premium or Free?"

need_human_review = ReviewResult(
    verdict=Verdict.FAIL, spec_met=False, quality_issues=[], summary="needs a product call",
    needs_human=True, question=QUESTION,
)

_reset()
pm_decide_calls = []


async def pm_decide(cfg, tkt, app, audit, halt_report):
    pm_decide_calls.append(halt_report)
    return {"verdict": "DECIDE", "body": "Default to Premium — matches current live behaviour."}


async def _stub_review_needs_human(diff, ticket, app, cfg, iteration, store=None, build_artifact=None,
                                   already_bounced=None):
    return need_human_review


loop.reviewer_mod.review = _stub_review_needs_human
loop._consult_pm = pm_decide

cfg1 = mkcfg(auto_mode=True)
au1 = Audit()
bl1 = Backlog()
rep1 = asyncio.run(loop._attempt(ticket, app_cfg, cfg1, Git(), bl1, au1, loop.Budget(0), BRANCH))

chk("AC1: needs_human + PM DECIDE -> pm_decided audited with automode=True",
    any(e["event"] == "pm_decided" and e.get("automode") is True for e in au1.ev), str(au1.ev))
chk("AC1: needs_human + PM DECIDE -> re-built with the PM decision injected into ticket.description",
    any("Default to Premium" in d for d in built), f"built={built}")
chk("AC1: needs_human + PM DECIDE -> the durable '🤖 Automode' Jira comment is posted",
    any("🤖 Automode" in c for c in bl1.comments), f"comments={bl1.comments}")
chk("AC1: needs_human + PM DECIDE -> Roman is NOT paged for the DECIDEd occurrence "
    "(only one park total, from the SECOND needs_human hit after pm_used was spent)",
    len(dec_calls) == 1, f"dec_calls={dec_calls}")
chk("AC5: the PM is consulted AT MOST ONCE across the run (a needs_human retry does not re-invoke it)",
    len(pm_decide_calls) == 1, f"pm_decide_calls={pm_decide_calls}")
chk("AC6: the audit trail carries BOTH a pm_decided (PM-decided) and a needs_human (Commander-parked) "
    "event -- distinct routes for the same run",
    any(e["event"] == "pm_decided" for e in au1.ev) and any(e["event"] == "needs_human" for e in au1.ev),
    str(au1.ev))
chk("needs_human + PM decided once, then a second identical needs_human hit (pm already used) parks",
    rep1.outcome == Outcome.ESCALATED, str(rep1.outcome))


# ===========================================================================
# AC2 — review.needs_human + PM ESCALATE (explicit WHY line): parks, needs_human audited, the
#        parked proposal is prefixed with the WHY-CANNOT-RESOLVE line, no pm_decided is recorded.
# ===========================================================================
_reset()


async def pm_escalate(cfg, tkt, app, audit, halt_report):
    return {
        "verdict": "ESCALATE",
        "body": "Recommend keeping Premium as default (billing-critical, policy ambiguous).",
        "why": "Both tiers are documented as default in different places; no code signal disambiguates.",
    }


loop._consult_pm = pm_escalate
cfg2 = mkcfg(auto_mode=False, max_iterations=1)
au2 = Audit()
bl2 = Backlog()
rep2 = asyncio.run(loop._attempt(ticket, app_cfg, cfg2, Git(), bl2, au2, loop.Budget(0), BRANCH))

chk("AC2: needs_human + PM ESCALATE -> parks (Outcome.ESCALATED)",
    rep2.outcome == Outcome.ESCALATED, str(rep2.outcome))
chk("AC2: needs_human + PM ESCALATE -> needs_human audited",
    any(e["event"] == "needs_human" for e in au2.ev), str(au2.ev))
chk("AC2: needs_human + PM ESCALATE -> the parked proposal begins with the WHY-CANNOT-RESOLVE line",
    any(e["event"] == "needs_human"
        and str(e.get("question", "")).startswith("WHY PM CANNOT RESOLVE:")
        for e in au2.ev),
    str(au2.ev))
chk("AC2: needs_human + PM ESCALATE -> no pm_decided is recorded (the PM did not decide)",
    not any(e["event"] == "pm_decided" for e in au2.ev), str(au2.ev))

# ---- PM unavailable (returns None) also parks, without a WHY line (the PM gave none) ----
_reset()


async def pm_none(cfg, tkt, app, audit, halt_report):
    return None


loop._consult_pm = pm_none
cfg2b = mkcfg(auto_mode=False, max_iterations=1)
au2b = Audit()
bl2b = Backlog()
rep2b = asyncio.run(loop._attempt(ticket, app_cfg, cfg2b, Git(), bl2b, au2b, loop.Budget(0), BRANCH))

chk("AC2: needs_human + PM unavailable (None) -> parks (Outcome.ESCALATED)",
    rep2b.outcome == Outcome.ESCALATED, str(rep2b.outcome))
chk("AC2: needs_human + PM unavailable -> needs_human audited, no WHY line (PM gave nothing)",
    any(e["event"] == "needs_human" and QUESTION in str(e.get("question", ""))
        and not str(e.get("question", "")).startswith("WHY PM CANNOT RESOLVE:")
        for e in au2b.ev),
    str(au2b.ev))


# ===========================================================================
# AC3 — EU-90 pm-findings 'decisions' bucket + PM DECIDE: does NOT call decisions.add/notify for
#        the pm-findings entry_id; injects the decision and re-builds instead.
# ===========================================================================
_reset()

DECISION_ISSUE = QualityIssue(severity="major", area="product",
                               detail="Should the free tier include basic analytics?")

fail_with_decision_finding = ReviewResult(
    verdict=Verdict.FAIL, spec_met=False,
    quality_issues=[DECISION_ISSUE], required_changes=["settle the product question"],
    summary="reviewer flagged a product ambiguity", needs_human=False,
)
# The re-built pass (after the PM injects its decision) comes back clean of quality_issues, so the
# pm-findings triage block does not re-trigger — proving the PM decision was actually incorporated
# rather than merely re-asking the same question forever.
fail_no_issues = ReviewResult(
    verdict=Verdict.FAIL, spec_met=False, quality_issues=[],
    required_changes=["polish per the injected decision"],
    summary="reviewer wants a small follow-up, no product ambiguity left", needs_human=False,
)


async def _stub_review_findings(diff, ticket, app, cfg, iteration, store=None, build_artifact=None,
                                already_bounced=None):
    return fail_with_decision_finding if iteration == 1 else fail_no_issues


async def _stub_triage_decision(review, tkt, cfg, diff="", files_changed=None):
    return FindingsTriage(in_scope=[], out_of_scope=[], decisions=[DECISION_ISSUE])


loop.reviewer_mod.review = _stub_review_findings
pm_mod.triage_findings = _stub_triage_decision

pm_findings_decide_calls = []


async def pm_findings_decide(cfg, tkt, app, audit, halt_report):
    pm_findings_decide_calls.append(halt_report)
    return {"verdict": "DECIDE", "body": "Yes — include basic analytics in the free tier."}


loop._consult_pm = pm_findings_decide
cfg3 = mkcfg(auto_mode=True, max_iterations=2)   # 2nd pass observes the re-build with the injected decision
au3 = Audit()
bl3 = Backlog()
rep3 = asyncio.run(loop._attempt(ticket, app_cfg, cfg3, Git(), bl3, au3, loop.Budget(0), BRANCH))

pmf_dec_calls = [c for c in dec_calls if c.get("entry_id") == f"{ticket.id}#pm-findings-decisions"]
pmf_notify = [t for t in notify_calls if "needs YOUR decision" in t]

chk("AC3: pm-findings decisions + PM DECIDE -> decisions.add is NOT called with the pm-findings entry_id",
    pmf_dec_calls == [], f"pmf_dec_calls={pmf_dec_calls}")
chk("AC3: pm-findings decisions + PM DECIDE -> the Commander is NOT paged with the bulleted brief",
    pmf_notify == [], f"pmf_notify={pmf_notify}")
chk("AC3: pm-findings decisions + PM DECIDE -> pm_decided audited",
    any(e["event"] == "pm_decided" for e in au3.ev), str(au3.ev))
chk("AC3: pm-findings decisions + PM DECIDE -> re-built with the PM decision injected",
    any("include basic analytics" in d for d in built), f"built={built}")
chk("AC5 (pm-findings route): the PM is consulted exactly once for this run",
    len(pm_findings_decide_calls) == 1, f"calls={pm_findings_decide_calls}")


# ===========================================================================
# AC4 — EU-90 pm-findings 'decisions' bucket + PM ESCALATE: unchanged behaviour — decisions.add
#        with the pm-findings entry_id, and the Commander is paged with the bulleted brief.
# ===========================================================================
_reset()


async def pm_findings_escalate(cfg, tkt, app, audit, halt_report):
    return {"verdict": "ESCALATE", "body": "Needs your call.", "why": "Pricing policy — only you set it."}


loop._consult_pm = pm_findings_escalate
cfg4 = mkcfg(auto_mode=False, max_iterations=1)
au4 = Audit()
bl4 = Backlog()
rep4 = asyncio.run(loop._attempt(ticket, app_cfg, cfg4, Git(), bl4, au4, loop.Budget(0), BRANCH))

pmf_dec_calls4 = [c for c in dec_calls if c.get("entry_id") == f"{ticket.id}#pm-findings-decisions"]
pmf_notify4 = [t for t in notify_calls if "needs YOUR decision" in t]

chk("AC4: pm-findings decisions + PM ESCALATE -> decisions.add IS called with the pm-findings entry_id",
    len(pmf_dec_calls4) == 1, f"pmf_dec_calls4={pmf_dec_calls4}")
chk("AC4: pm-findings decisions + PM ESCALATE -> the parked question carries the WHY line",
    bool(pmf_dec_calls4) and pmf_dec_calls4[0]["question"].startswith("WHY PM CANNOT RESOLVE:"),
    f"pmf_dec_calls4={pmf_dec_calls4}")
chk("AC4: pm-findings decisions + PM ESCALATE -> the Commander IS paged with the bulleted brief",
    len(pmf_notify4) == 1 and "•" in pmf_notify4[0] and DECISION_ISSUE.detail in pmf_notify4[0],
    f"pmf_notify4={pmf_notify4}")
chk("AC4: pm-findings decisions + PM ESCALATE -> no pm_decided is recorded",
    not any(e["event"] == "pm_decided" for e in au4.ev), str(au4.ev))


# ===========================================================================
# Restore + report
# ===========================================================================
loop.reviewer_mod.review = _stub_review_findings  # leave in a known state; no further tests run

passed = sum(1 for _, ok, _ in results if ok)
print("\n================ EU-230 QA ================")
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
