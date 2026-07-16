"""EU-351 regression: the Reviewer must not re-block an already-bounced unverifiable lens.

Wires the EU-350 classifier/fingerprinter (`_classify_unverifiable_finding` /
`_finding_fingerprint`) into `review()` as an escalate-once bounce gate
(`_enforce_bounce_once`), mirroring the existing deterministic backstops
`_enforce_admitted_red_tests`/`_enforce_execution_gate`:

  1. `review()` called WITHOUT `already_bounced` (existing signature) — unchanged behavior: a
     first-pass unverifiable-surface blocking finding still blocks, verdict can be FAIL.
  2. `_enforce_bounce_once(result, already_bounced)` with the finding's fingerprint present in
     `already_bounced` — removes it from `blocking_issues`/`spec_gaps` and appends it to
     `unverifiable_gaps`; the detail never re-appears in blocking_issues/spec_gaps.
  3. A `ReviewResult` whose only remaining blockers are previously-bounced unverifiable findings
     reaches `verdict == Verdict.PASS` after `_enforce_bounce_once` (spec_met true, no other
     blockers).
  4. An ordinary logic/test/data blocking finding (classifier returns None) is left in
     `blocking_issues`/`spec_gaps` unchanged even when its fingerprint is passed in
     `already_bounced` — never lands in `unverifiable_gaps`.
  5. `ReviewResult.unverifiable_gaps` defaults to `[]` and is excluded from `blocking_issues` /
     `is_ship_ready()`.

All offline — SDK and agents stubbed; no network, no real models.
"""
import asyncio
import json
import sys
import types

# ── SDK stub (no real model calls) — mirrors tests/reviewer_unverifiable_classifier_test.py ───────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): self.__dict__.update(k)
    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import reviewer as reviewer_mod        # noqa: E402
from orchestrator.config import Config, AppConfig        # noqa: E402
from orchestrator.contracts import (                      # noqa: E402
    BuildArtifact, QualityIssue, ReviewResult, Ticket, Verdict,
)

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# ══════════════════════════════════════════════════════════════════════════════
# 5. ReviewResult.unverifiable_gaps — defaults empty, excluded from blocking_issues/is_ship_ready
# ══════════════════════════════════════════════════════════════════════════════
_rr_default = ReviewResult(verdict=Verdict.PASS, spec_met=True)
chk("ReviewResult: unverifiable_gaps defaults to []",
    _rr_default.unverifiable_gaps == [], str(_rr_default.unverifiable_gaps))

_rr_with_gap = ReviewResult(verdict=Verdict.PASS, spec_met=True,
                            unverifiable_gaps=["the button looks off"])
chk("ReviewResult: unverifiable_gaps excluded from blocking_issues",
    _rr_with_gap.blocking_issues == [], str(_rr_with_gap.blocking_issues))
chk("ReviewResult: unverifiable_gaps alone does not block is_ship_ready()",
    _rr_with_gap.is_ship_ready() is True, str(_rr_with_gap.is_ship_ready()))

# ══════════════════════════════════════════════════════════════════════════════
# 2. _enforce_bounce_once — bounced fingerprint demotes quality_issues -> unverifiable_gaps
# ══════════════════════════════════════════════════════════════════════════════
UNVERIFIABLE_DETAIL = "the button looks misaligned on the settings page"
lens = reviewer_mod._classify_unverifiable_finding(UNVERIFIABLE_DETAIL)
chk("sanity: UNVERIFIABLE_DETAIL classifies as unverifiable", lens is not None, str(lens))
fp = reviewer_mod._finding_fingerprint(lens, UNVERIFIABLE_DETAIL)

result_2 = ReviewResult(
    verdict=Verdict.FAIL, spec_met=True,
    quality_issues=[QualityIssue(severity="blocker", area="ui", detail=UNVERIFIABLE_DETAIL)],
)
result_2 = reviewer_mod._enforce_bounce_once(result_2, {fp})
chk("_enforce_bounce_once: bounced finding removed from quality_issues/blocking_issues",
    result_2.blocking_issues == [], str(result_2.blocking_issues))
chk("_enforce_bounce_once: bounced finding's detail appended to unverifiable_gaps",
    UNVERIFIABLE_DETAIL in result_2.unverifiable_gaps, str(result_2.unverifiable_gaps))
chk("_enforce_bounce_once: demoted detail never reappears in blocking_issues",
    all(q.detail != UNVERIFIABLE_DETAIL for q in result_2.quality_issues),
    str(result_2.quality_issues))

# Same demotion, but the unverifiable finding lives in spec_gaps instead of quality_issues AND is
# the ONLY blocker with spec_met=False — after demotion the spec channel must be recomputed clear so
# the ticket reaches PASS + is_ship_ready() (EU-351 iteration-2: the spec_gaps channel must reach
# ship-ready, not just relabel a permanent FAIL).
result_2b = ReviewResult(verdict=Verdict.FAIL, spec_met=False, spec_gaps=[UNVERIFIABLE_DETAIL])
result_2b = reviewer_mod._enforce_bounce_once(result_2b, {fp})
chk("_enforce_bounce_once: bounced finding removed from spec_gaps",
    UNVERIFIABLE_DETAIL not in result_2b.spec_gaps, str(result_2b.spec_gaps))
chk("_enforce_bounce_once: bounced spec_gap's detail appended to unverifiable_gaps",
    UNVERIFIABLE_DETAIL in result_2b.unverifiable_gaps, str(result_2b.unverifiable_gaps))
chk("_enforce_bounce_once: spec_gap-only bounced ticket -> spec_met recomputed True",
    result_2b.spec_met is True, str(result_2b.spec_met))
chk("_enforce_bounce_once: spec_gap-only bounced ticket -> verdict reaches PASS",
    result_2b.verdict == Verdict.PASS, str(result_2b.verdict))
chk("_enforce_bounce_once: spec_gap-only bounced ticket -> is_ship_ready() True",
    result_2b.is_ship_ready() is True, str(result_2b.is_ship_ready()))

# ══════════════════════════════════════════════════════════════════════════════
# 3. A ticket whose ONLY remaining blockers are previously-bounced unverifiable findings -> PASS
# ══════════════════════════════════════════════════════════════════════════════
result_3 = ReviewResult(
    verdict=Verdict.FAIL, spec_met=True,
    quality_issues=[QualityIssue(severity="blocker", area="ui", detail=UNVERIFIABLE_DETAIL)],
    spec_gaps=[],
)
result_3 = reviewer_mod._enforce_bounce_once(result_3, {fp})
chk("_enforce_bounce_once: only-previously-bounced blockers -> verdict recomputed to PASS",
    result_3.verdict == Verdict.PASS, str(result_3.verdict))
chk("_enforce_bounce_once: only-previously-bounced blockers -> is_ship_ready() True",
    result_3.is_ship_ready() is True, str(result_3.is_ship_ready()))

# Sanity: if spec_met is False (a genuine other reason it's not ready), demotion alone must not force PASS.
result_3b = ReviewResult(
    verdict=Verdict.FAIL, spec_met=False,
    quality_issues=[QualityIssue(severity="blocker", area="ui", detail=UNVERIFIABLE_DETAIL)],
)
result_3b = reviewer_mod._enforce_bounce_once(result_3b, {fp})
chk("_enforce_bounce_once: spec_met False -> verdict stays FAIL despite demotion",
    result_3b.verdict == Verdict.FAIL, str(result_3b.verdict))

# Test 3b (distinct case): an INDEPENDENT non-demoted spec_gap (ordinary, not unverifiable) survives
# demotion and keeps the ticket FAIL. The spec_met recompute must fire ONLY when every removed gap was
# a bounced-unverifiable repeat AND none survive — never when a real gap (an ordinary spec gap, or an
# execution-gate gap, neither of which classifies as unverifiable) remains. This distinguishes the
# spec_gap-only PASS case (result_2b) from a genuine surviving-gap FAIL.
INDEP_GAP = "acceptance criterion 2 (server-side rate limiting) is not implemented"
chk("sanity: INDEP_GAP does not classify as unverifiable",
    reviewer_mod._classify_unverifiable_finding(INDEP_GAP) is None,
    str(reviewer_mod._classify_unverifiable_finding(INDEP_GAP)))
result_3c = ReviewResult(verdict=Verdict.FAIL, spec_met=False,
                         spec_gaps=[UNVERIFIABLE_DETAIL, INDEP_GAP])
result_3c = reviewer_mod._enforce_bounce_once(result_3c, {fp})
chk("3b: independent ordinary spec_gap survives demotion",
    result_3c.spec_gaps == [INDEP_GAP], str(result_3c.spec_gaps))
chk("3b: unverifiable spec_gap still demoted alongside it",
    UNVERIFIABLE_DETAIL in result_3c.unverifiable_gaps, str(result_3c.unverifiable_gaps))
chk("3b: spec_met stays False while a real gap survives",
    result_3c.spec_met is False, str(result_3c.spec_met))
chk("3b: verdict stays FAIL", result_3c.verdict == Verdict.FAIL, str(result_3c.verdict))
chk("3b: is_ship_ready() False", result_3c.is_ship_ready() is False, str(result_3c.is_ship_ready()))

# ══════════════════════════════════════════════════════════════════════════════
# 4. Ordinary (non-unverifiable) blocking finding is completely unaffected, even if its
#    fingerprint (computed the same way) is passed in already_bounced.
# ══════════════════════════════════════════════════════════════════════════════
ORDINARY_DETAIL = "missing null check on user_id before the query"
ordinary_lens = reviewer_mod._classify_unverifiable_finding(ORDINARY_DETAIL)
chk("sanity: ORDINARY_DETAIL does not classify as unverifiable", ordinary_lens is None, str(ordinary_lens))
# Fingerprint it anyway (as if some other code mistakenly bounced it) using a placeholder lens —
# _enforce_bounce_once must never even compute a fingerprint for a None-classified finding, so this
# entry in already_bounced must have zero effect on it.
ordinary_fp = reviewer_mod._finding_fingerprint("ui_visual", ORDINARY_DETAIL)

result_4 = ReviewResult(
    verdict=Verdict.FAIL, spec_met=True,
    quality_issues=[QualityIssue(severity="blocker", area="logic", detail=ORDINARY_DETAIL)],
)
result_4 = reviewer_mod._enforce_bounce_once(result_4, {ordinary_fp})
chk("_enforce_bounce_once: ordinary finding stays in blocking_issues unchanged",
    any(q.detail == ORDINARY_DETAIL for q in result_4.blocking_issues), str(result_4.blocking_issues))
chk("_enforce_bounce_once: ordinary finding never lands in unverifiable_gaps",
    ORDINARY_DETAIL not in result_4.unverifiable_gaps, str(result_4.unverifiable_gaps))
chk("_enforce_bounce_once: ordinary-finding verdict stays FAIL",
    result_4.verdict == Verdict.FAIL, str(result_4.verdict))

# No-op when already_bounced is empty — an unverifiable first-timer must be left completely alone.
result_4b = ReviewResult(
    verdict=Verdict.FAIL, spec_met=True,
    quality_issues=[QualityIssue(severity="blocker", area="ui", detail=UNVERIFIABLE_DETAIL)],
)
result_4b = reviewer_mod._enforce_bounce_once(result_4b, set())
chk("_enforce_bounce_once: empty already_bounced -> first-time unverifiable finding untouched",
    any(q.detail == UNVERIFIABLE_DETAIL for q in result_4b.quality_issues), str(result_4b.quality_issues))
chk("_enforce_bounce_once: empty already_bounced -> verdict stays FAIL (unchanged behavior)",
    result_4b.verdict == Verdict.FAIL, str(result_4b.verdict))

# ══════════════════════════════════════════════════════════════════════════════
# 1. review() end-to-end — WITHOUT already_bounced (existing signature): a first-pass
#    unverifiable-surface blocking finding still blocks, verdict can be FAIL. Mirrors the
#    reviewer_unverifiable_classifier_test.py EU-268 fixture harness style.
# ══════════════════════════════════════════════════════════════════════════════
class _RR:
    def __init__(self, t):
        (self.final, self.text, self.is_error, self.cost_usd, self.num_turns, self.tools,
         self.provider, self.model_version) = t, t, False, 0.0, 1, [], "Anthropic", "claude-sonnet-4-6"
        self.input_tokens = self.output_tokens = 0


_UNVERIFIABLE_BLOCK_JSON = "```json\n" + json.dumps({
    "verdict": "FAIL",
    "spec_conformance": {"met": True, "gaps": []},
    "quality": {"issues": [{"severity": "blocker", "area": "ui", "detail": UNVERIFIABLE_DETAIL}]},
    "required_changes": ["fix the alignment"],
    "summary": "visual issue",
}) + "\n```"


async def _fake_run_agent_pass1(prompt, options, tag="", cfg=None, routing_tier=None):
    return _RR(_UNVERIFIABLE_BLOCK_JSON)


_orig_run_agent_fb = reviewer_mod.run_agent_with_fallback
reviewer_mod.run_agent_with_fallback = _fake_run_agent_pass1

_rcfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                               protected_branch="MAIN", backlog_backend="none")],
              audit_path="/tmp/eu351-audit.jsonl", use_worktree=False, auto_model=False)
_rapp = _rcfg.app("automatixy")
_rtk = Ticket(id="AUTO-200", key="AUTO-200", summary="Fix button alignment",
             description="d", acceptance_criteria=["Button is aligned"])
_ba = BuildArtifact(files_changed=["src/Button.tsx"], diff_digest="aligned the button",
                    decisions=[], open_questions=[])

_pass1_result = asyncio.run(reviewer_mod.review(
    "diff --git a/src/Button.tsx b/src/Button.tsx\n+// align\n",
    _rtk, _rapp, _rcfg, build_artifact=_ba))
chk("review(): pass 1, no already_bounced kwarg -> unverifiable finding still blocks (unchanged)",
    any(q.detail == UNVERIFIABLE_DETAIL for q in _pass1_result.blocking_issues),
    str(_pass1_result.blocking_issues))
chk("review(): pass 1, no already_bounced kwarg -> verdict can still be FAIL",
    _pass1_result.verdict == Verdict.FAIL, str(_pass1_result.verdict))
chk("review(): pass 1 -> unverifiable_gaps stays empty (nothing bounced yet)",
    _pass1_result.unverifiable_gaps == [], str(_pass1_result.unverifiable_gaps))

# Pass 2: same finding, its fingerprint now supplied as already_bounced -> must reach PASS.
_pass1_lens = reviewer_mod._classify_unverifiable_finding(UNVERIFIABLE_DETAIL)
_pass1_fp = reviewer_mod._finding_fingerprint(_pass1_lens, UNVERIFIABLE_DETAIL)

_pass2_result = asyncio.run(reviewer_mod.review(
    "diff --git a/src/Button.tsx b/src/Button.tsx\n+// align\n",
    _rtk, _rapp, _rcfg, build_artifact=_ba, already_bounced={_pass1_fp}))
chk("review(): pass 2, fingerprint in already_bounced -> no longer in blocking_issues",
    _pass2_result.blocking_issues == [], str(_pass2_result.blocking_issues))
chk("review(): pass 2, fingerprint in already_bounced -> detail lands in unverifiable_gaps",
    UNVERIFIABLE_DETAIL in _pass2_result.unverifiable_gaps, str(_pass2_result.unverifiable_gaps))
chk("review(): pass 2, only previously-bounced blockers left -> verdict reaches PASS",
    _pass2_result.verdict == Verdict.PASS, str(_pass2_result.verdict))

reviewer_mod.run_agent_with_fallback = _orig_run_agent_fb

print("\n================= EU-351 REVIEWER BOUNCE-ONCE GATE =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
