"""EU-352 regression: the per-ticket "already-bounced" unverifiable-finding set threads through
_attempt()'s multi-pass build<->review retry loop.

Wires `reviewer.collect_unverifiable_fingerprints(review)` (EU-352, sitting alongside EU-351's
`_enforce_bounce_once`/`_classify_unverifiable_finding`/`_finding_fingerprint`) into a new
per-attempt local, `bounced_unverifiable: set[str]`, declared with the SAME single-ticket,
in-memory lifetime as `recent_reject_sigs`/`last_in_scope_sig`. Threaded into BOTH
`reviewer_mod.review(...)` call sites (the initial call and the `review.parse_failed` parse-retry
call) via the `already_bounced=` kwarg, and folded forward after each pass settles:

  1. Pass-1 review returns FAIL with an unverifiable blocking finding -> pass-2's stubbed
     reviewer receives that finding's fingerprint in `already_bounced`.
  2. Both review() call sites receive the CURRENT accumulated set: the initial call for a pass,
     AND — when that initial result is `parse_failed=True` — the parse-retry call too (same set,
     unmodified, since folding only happens once the pass is fully settled).
  3. An ordinary (non-unverifiable) blocking finding never contributes a fingerprint —
     `collect_unverifiable_fingerprints(result)` is empty for it, classifier-only and minor-severity
     findings are excluded — purely additive, no accidental over-collection.
  4. Purely additive threading: `HARD_MAX_PASSES`/`HARD_MAX_PASSES_WEAK` are unchanged constants;
     `python3 tests/run_all.py` is asserted green separately (this file is one of its tests).

All offline — SDK stubbed; no network, no real models.
"""
import sys
import types
import asyncio

# ── SDK stub (no real model calls) — mirrors tests/review_parse_retry_test.py ──────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop                          # noqa: E402
from orchestrator import reviewer as reviewer_mod          # noqa: E402
from orchestrator.config import Config, AppConfig          # noqa: E402
from orchestrator.contracts import (                       # noqa: E402
    Ticket, BuildResult, ReviewResult, GateResult, QualityIssue, Verdict, Outcome, TicketReport,
)

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


UNVERIFIABLE_DETAIL = "the button looks off"
ORDINARY_DETAIL = "missing null check on user_id before the query"

# ══════════════════════════════════════════════════════════════════════════════
# 3. collect_unverifiable_fingerprints — direct unit checks (classification correctness)
# ══════════════════════════════════════════════════════════════════════════════
_lens = reviewer_mod._classify_unverifiable_finding(UNVERIFIABLE_DETAIL)
chk("sanity: UNVERIFIABLE_DETAIL classifies as unverifiable", _lens is not None, str(_lens))
_fp = reviewer_mod._finding_fingerprint(_lens, UNVERIFIABLE_DETAIL)

_rr_unverifiable = ReviewResult(
    verdict=Verdict.FAIL, spec_met=True,
    quality_issues=[QualityIssue(severity="blocker", area="ui", detail=UNVERIFIABLE_DETAIL)],
)
chk("collect_unverifiable_fingerprints: picks up a blocker unverifiable finding",
    reviewer_mod.collect_unverifiable_fingerprints(_rr_unverifiable) == {_fp},
    str(reviewer_mod.collect_unverifiable_fingerprints(_rr_unverifiable)))

chk("sanity: ORDINARY_DETAIL does not classify as unverifiable",
    reviewer_mod._classify_unverifiable_finding(ORDINARY_DETAIL) is None)
_rr_ordinary = ReviewResult(
    verdict=Verdict.FAIL, spec_met=True,
    quality_issues=[QualityIssue(severity="blocker", area="logic", detail=ORDINARY_DETAIL)],
)
chk("collect_unverifiable_fingerprints: ordinary blocking finding contributes NOTHING",
    reviewer_mod.collect_unverifiable_fingerprints(_rr_ordinary) == set(),
    str(reviewer_mod.collect_unverifiable_fingerprints(_rr_ordinary)))

_rr_mixed = ReviewResult(
    verdict=Verdict.FAIL, spec_met=True,
    quality_issues=[
        QualityIssue(severity="blocker", area="ui", detail=UNVERIFIABLE_DETAIL),
        QualityIssue(severity="blocker", area="logic", detail=ORDINARY_DETAIL),
        QualityIssue(severity="minor", area="ui", detail="the button looks off (minor)"),
    ],
)
chk("collect_unverifiable_fingerprints: minor-severity findings excluded (only blocker/major)",
    reviewer_mod.collect_unverifiable_fingerprints(_rr_mixed) == {_fp},
    str(reviewer_mod.collect_unverifiable_fingerprints(_rr_mixed)))

_rr_already_demoted = ReviewResult(
    verdict=Verdict.PASS, spec_met=True, unverifiable_gaps=[UNVERIFIABLE_DETAIL],
)
chk("collect_unverifiable_fingerprints: also recognises already-demoted unverifiable_gaps",
    reviewer_mod.collect_unverifiable_fingerprints(_rr_already_demoted) == {_fp},
    str(reviewer_mod.collect_unverifiable_fingerprints(_rr_already_demoted)))

# ══════════════════════════════════════════════════════════════════════════════
# 4. Purely additive — the hard pass ceilings are untouched by this ticket
# ══════════════════════════════════════════════════════════════════════════════
chk("HARD_MAX_PASSES unchanged (2)", loop.HARD_MAX_PASSES == 2, str(loop.HARD_MAX_PASSES))
chk("HARD_MAX_PASSES_WEAK unchanged (3)", loop.HARD_MAX_PASSES_WEAK == 3, str(loop.HARD_MAX_PASSES_WEAK))

# ══════════════════════════════════════════════════════════════════════════════
# Shared _attempt() harness plumbing (mirrors tests/review_parse_retry_test.py)
# ══════════════════════════════════════════════════════════════════════════════
class Audit:
    def __init__(s): s.ev = []
    def record(s, e, **k): s.ev.append({"event": e, **k})


class Git:
    def has_changes(s): return True
    def diff_against_base(s): return "diff --git a/x b/x\n+change"
    def changed_paths(s): return ["apps/automatixy/x.ts"]


loop._notify = lambda c, t: None
loop.run_gate = lambda app, changed_paths=None, **_: GateResult(passed=True, report="")
loop._land = lambda *a, **k: TicketReport("EU-352", Outcome.MERGED, 1, 0.0, "automatixy", "b")


class FakeBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")

    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        return BuildResult(ok=True, summary="did it", cost_usd=0.0, num_turns=1, raw="did it", tools=[])


loop.builder_mod = FakeBuilder
_orig_review = reviewer_mod.review


def mkcfg(**kw):
    return Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path="/tmp/eu352-audit.jsonl", use_worktree=False, max_iterations=3, **kw)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Pass-1 unverifiable finding -> pass-2 review receives its fingerprint in already_bounced
# ══════════════════════════════════════════════════════════════════════════════
calls_a: list[dict] = []


async def fake_review_a(diff, ticket, app, cfg, iteration=1, *, store=None, build_artifact=None,
                        already_bounced=None):
    calls_a.append({"iteration": iteration, "already_bounced": set(already_bounced or set())})
    if len(calls_a) == 1:
        return ReviewResult(
            verdict=Verdict.FAIL, spec_met=True,
            quality_issues=[QualityIssue(severity="blocker", area="ui", detail=UNVERIFIABLE_DETAIL)],
            cost_usd=0.0,
        )
    return ReviewResult(verdict=Verdict.PASS, spec_met=True, cost_usd=0.0)


reviewer_mod.review = fake_review_a

cfg_a = mkcfg()
app_a = cfg_a.app("automatixy")
ticket_a = Ticket(id="EU-352A", key="EU-352A", summary="s", description="d", ephemeral=False, app="automatixy")
rep_a = asyncio.run(loop._attempt(ticket_a, app_a, cfg_a, Git(), None, Audit(), loop.Budget(0), "autodev/EU-352A"))

chk("scenario 1: exactly 2 review calls (pass 1 fail, pass 2 ship)", len(calls_a) == 2, str(calls_a))
chk("scenario 1: pass 1 sees an EMPTY already_bounced (nothing bounced yet)",
    calls_a[0]["already_bounced"] == set(), str(calls_a[0]))
chk("scenario 1: pass 2 receives pass-1's unverifiable fingerprint in already_bounced",
    calls_a[1]["already_bounced"] == {_fp}, str(calls_a[1]))
chk("scenario 1: ticket ships (MERGED) once pass 2 passes", rep_a.outcome == Outcome.MERGED, str(rep_a.outcome))

reviewer_mod.review = _orig_review

# ══════════════════════════════════════════════════════════════════════════════
# 2. The parse-retry call site ALSO receives the accumulated set (not just the initial call)
# ══════════════════════════════════════════════════════════════════════════════
calls_b: list[dict] = []


async def fake_review_b(diff, ticket, app, cfg, iteration=1, *, store=None, build_artifact=None,
                        already_bounced=None):
    calls_b.append({"iteration": iteration, "already_bounced": set(already_bounced or set())})
    if len(calls_b) == 1:
        # pass 1: unverifiable blocking finding -> bounced_unverifiable accumulates its fingerprint
        return ReviewResult(
            verdict=Verdict.FAIL, spec_met=True,
            quality_issues=[QualityIssue(severity="blocker", area="ui", detail=UNVERIFIABLE_DETAIL)],
            cost_usd=0.0,
        )
    if len(calls_b) == 2:
        # pass 2's INITIAL call: unparseable -> triggers the parse-retry call site (EU-11/EU-52)
        return ReviewResult(verdict=Verdict.FAIL, spec_met=False,
                            required_changes=["unparseable"], parse_failed=True, cost_usd=0.0)
    # pass 2's parse-retry call: parses cleanly and ships
    return ReviewResult(verdict=Verdict.PASS, spec_met=True, cost_usd=0.0)


reviewer_mod.review = fake_review_b

cfg_b = mkcfg()
app_b = cfg_b.app("automatixy")
ticket_b = Ticket(id="EU-352B", key="EU-352B", summary="s", description="d", ephemeral=False, app="automatixy")
rep_b = asyncio.run(loop._attempt(ticket_b, app_b, cfg_b, Git(), None, Audit(), loop.Budget(0), "autodev/EU-352B"))

chk("scenario 2: exactly 3 review calls (pass1, pass2-initial, pass2-parse-retry)",
    len(calls_b) == 3, str(calls_b))
chk("scenario 2: pass2-initial call receives already_bounced with pass-1's fingerprint",
    calls_b[1]["already_bounced"] == {_fp}, str(calls_b[1]))
chk("scenario 2: pass2 PARSE-RETRY call ALSO receives already_bounced with the same fingerprint",
    calls_b[2]["already_bounced"] == {_fp}, str(calls_b[2]))
chk("scenario 2: pass2-initial/parse-retry iterations are 2 and 3 (EU-52 escalation unchanged)",
    [calls_b[1]["iteration"], calls_b[2]["iteration"]] == [2, 3], str(calls_b))
chk("scenario 2: ticket ships (MERGED) once the parse-retry parses and passes",
    rep_b.outcome == Outcome.MERGED, str(rep_b.outcome))

reviewer_mod.review = _orig_review

# ══════════════════════════════════════════════════════════════════════════════
# 3b. An ordinary blocking finding on pass 1 is NEVER added to the set pass 2 receives
# ══════════════════════════════════════════════════════════════════════════════
calls_c: list[dict] = []


async def fake_review_c(diff, ticket, app, cfg, iteration=1, *, store=None, build_artifact=None,
                        already_bounced=None):
    calls_c.append({"iteration": iteration, "already_bounced": set(already_bounced or set())})
    if len(calls_c) == 1:
        return ReviewResult(
            verdict=Verdict.FAIL, spec_met=True,
            quality_issues=[QualityIssue(severity="blocker", area="logic", detail=ORDINARY_DETAIL)],
            cost_usd=0.0,
        )
    return ReviewResult(verdict=Verdict.PASS, spec_met=True, cost_usd=0.0)


reviewer_mod.review = fake_review_c

cfg_c = mkcfg()
app_c = cfg_c.app("automatixy")
ticket_c = Ticket(id="EU-352C", key="EU-352C", summary="s", description="d", ephemeral=False, app="automatixy")
rep_c = asyncio.run(loop._attempt(ticket_c, app_c, cfg_c, Git(), None, Audit(), loop.Budget(0), "autodev/EU-352C"))

chk("scenario 3: exactly 2 review calls", len(calls_c) == 2, str(calls_c))
chk("scenario 3: pass 2 receives an EMPTY already_bounced — ordinary finding never bounced",
    calls_c[1]["already_bounced"] == set(), str(calls_c[1]))
chk("scenario 3: ticket ships (MERGED)", rep_c.outcome == Outcome.MERGED, str(rep_c.outcome))

reviewer_mod.review = _orig_review

print("\n========== EU-352 LOOP BOUNCE-TRACKING QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed", "✅" if passed == len(results) else "❌")
assert passed == len(results)
