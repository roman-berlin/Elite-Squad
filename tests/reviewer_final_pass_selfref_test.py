"""Regression: two reviewer non-convergence leaks (2026-07-28).

FIX A — the escalate-once demotion reaches the FINAL pass.
`_enforce_bounce_once` only demoted an unverifiable finding whose EXACT fingerprint was already in
`already_bounced`. The 2026-07-05 audit recorded on `config.escalate_effort_on_retry` measured
135/135 round-≥2 reviewer objections as textually NEW, so that fingerprint match effectively never
hit and 29 of 128 escalations were still "max passes — PM escalated". On the final pass (and ONLY
there) a FRESH unverifiable finding is now demoted too:

  A1. `final_pass=True` + empty `already_bounced` -> a fresh unverifiable BLOCKER is demoted to
      `unverifiable_gaps`, and the verdict recomputes to PASS.
  A2. same via the `spec_gaps` channel -> demoted, `spec_met` recomputed True, verdict PASS.
  A3. `final_pass=False` (the default) is byte-identical to before: a fresh unverifiable finding
      still blocks and the verdict stays FAIL.
  A4. an ORDINARY logic finding (classifier returns None) still FAILs on the final pass.
  A5. an EXECUTION-GATE spec gap is NEVER relieved by `final_pass`, even when its quoted AC text
      classifies as unverifiable — `_enforce_execution_gate` keeps its own escalate-once contract.
  A6. `review()` end-to-end honours `final_pass`, and the loop threads it from
      `iteration >= max_passes` at BOTH reviewer call sites.

FIX B — a deterministic enforcer must not fire on a diff that edits it.
The four `_enforce_*` backstops decide by pattern-matching diff/handoff TEXT, so a ticket whose whole
purpose is one of those detectors trips it by construction (EU-518 and EU-520 both escalated exactly
this way, ~80% done, gate 501/502 green). Each enforcer is now skipped when the diff modifies the
source file implementing it — and only then:

  B1. `_self_referential_enforcer_skips` names all four enforcers for a diff touching
      `orchestrator/reviewer.py`, and NOTHING for an ordinary product diff.
  B2. `review()` on a reviewer.py diff with no test file does NOT force the EU-441 missing-tests
      FAIL, and records a skip note on `unverifiable_gaps`.
  B3. the byte-identical diff shape on ANOTHER production file still forces that FAIL (not weakened
      for any other ticket).
  B4. same asymmetry for the EU-249 admitted-red-tests enforcer, which reads the Builder handoff.
  B5. the skip note is inert for the EU-351/EU-352 bounce machinery (classifier returns None, so
      `collect_unverifiable_fingerprints` never picks it up as a finding).

All offline — SDK and agents stubbed; no network, no real models.
"""
import asyncio
import json
import sys
import types
from pathlib import Path

# ── SDK stub (no real model calls) — mirrors tests/reviewer_bounce_once_test.py ───────────────────
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

ROOT = Path(__file__).resolve().parent.parent

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# ══════════════════════════════════════════════════════════════════════════════
# FIX A — final-pass demotion of a FRESH unverifiable finding
# ══════════════════════════════════════════════════════════════════════════════
FRESH_UNVERIFIABLE = "the spacing renders incorrectly under a narrow viewport"
chk("sanity: FRESH_UNVERIFIABLE classifies as unverifiable",
    reviewer_mod._classify_unverifiable_finding(FRESH_UNVERIFIABLE) is not None,
    str(reviewer_mod._classify_unverifiable_finding(FRESH_UNVERIFIABLE)))

# A1 — quality_issues channel, nothing bounced before.
a1 = ReviewResult(verdict=Verdict.FAIL, spec_met=True,
                  quality_issues=[QualityIssue(severity="blocker", area="ui",
                                               detail=FRESH_UNVERIFIABLE)])
a1 = reviewer_mod._enforce_bounce_once(a1, set(), final_pass=True)
chk("A1: final pass demotes a FRESH unverifiable blocker out of blocking_issues",
    a1.blocking_issues == [], str(a1.blocking_issues))
chk("A1: demoted detail lands in unverifiable_gaps",
    FRESH_UNVERIFIABLE in a1.unverifiable_gaps, str(a1.unverifiable_gaps))
chk("A1: verdict recomputed to PASS", a1.verdict == Verdict.PASS, str(a1.verdict))
chk("A1: is_ship_ready() True", a1.is_ship_ready() is True, str(a1.is_ship_ready()))

# A2 — spec_gaps channel: the spec_met recompute must fire for a final-pass demotion too.
a2 = ReviewResult(verdict=Verdict.FAIL, spec_met=False, spec_gaps=[FRESH_UNVERIFIABLE])
a2 = reviewer_mod._enforce_bounce_once(a2, set(), final_pass=True)
chk("A2: final pass demotes a FRESH unverifiable spec_gap",
    a2.spec_gaps == [] and FRESH_UNVERIFIABLE in a2.unverifiable_gaps,
    f"gaps={a2.spec_gaps} unver={a2.unverifiable_gaps}")
chk("A2: spec_met recomputed True", a2.spec_met is True, str(a2.spec_met))
chk("A2: verdict reaches PASS", a2.verdict == Verdict.PASS, str(a2.verdict))

# A3 — NOT the final pass: unchanged behaviour, the fresh finding still blocks.
a3 = ReviewResult(verdict=Verdict.FAIL, spec_met=True,
                  quality_issues=[QualityIssue(severity="blocker", area="ui",
                                               detail=FRESH_UNVERIFIABLE)])
a3 = reviewer_mod._enforce_bounce_once(a3, set())
chk("A3: non-final pass leaves a FRESH unverifiable blocker in place",
    any(q.detail == FRESH_UNVERIFIABLE for q in a3.blocking_issues), str(a3.blocking_issues))
chk("A3: non-final pass verdict stays FAIL", a3.verdict == Verdict.FAIL, str(a3.verdict))
chk("A3: non-final pass records nothing in unverifiable_gaps",
    a3.unverifiable_gaps == [], str(a3.unverifiable_gaps))

# A4 — an ordinary logic finding is untouched even on the final pass (scope stays tight).
ORDINARY = "missing null check on user_id before the update query"
chk("sanity: ORDINARY does not classify as unverifiable",
    reviewer_mod._classify_unverifiable_finding(ORDINARY) is None,
    str(reviewer_mod._classify_unverifiable_finding(ORDINARY)))
a4 = ReviewResult(verdict=Verdict.FAIL, spec_met=True,
                  quality_issues=[QualityIssue(severity="blocker", area="logic", detail=ORDINARY)],
                  spec_gaps=["acceptance criterion 3 (audit row written) is not implemented"])
a4 = reviewer_mod._enforce_bounce_once(a4, set(), final_pass=True)
chk("A4: ordinary blocker survives the final pass",
    any(q.detail == ORDINARY for q in a4.blocking_issues), str(a4.blocking_issues))
chk("A4: ordinary spec_gap survives the final pass",
    a4.spec_gaps == ["acceptance criterion 3 (audit row written) is not implemented"],
    str(a4.spec_gaps))
chk("A4: ordinary findings keep the verdict FAIL", a4.verdict == Verdict.FAIL, str(a4.verdict))
chk("A4: ordinary findings never enter unverifiable_gaps",
    a4.unverifiable_gaps == [], str(a4.unverifiable_gaps))

# A4b — the canned blockers the OTHER deterministic enforcers force are not demotable either: none
# of them classifies as an unverifiable surface, so the final-pass widening can never launder a
# forced EU-249/EU-441/EU-446 FAIL into an advisory gap.
_FORCED_BLOCKERS = {
    "EU-249 admitted-red-tests": (
        "Builder's own handoff admits a failing/skipped/broken test: \"one test is failing\" — a "
        "self-reported red or skipped test is an automatic blocking finding (EU-249) and cannot "
        "ship PASS."),
    "EU-441 missing-tests": (
        "Production code changed (>= 5 added lines) but no test file accompanies the diff — missing "
        "tests for the new behaviour. 'Tests with the code' is a blocking convention (EU-441): add a "
        "*_test.py / *.test.* / *.spec.* file covering it."),
    "EU-441 missing-typing": (
        "New Python code is missing return annotations or erodes typing with explicit Any (EU-441): "
        "orchestrator/x.py:foo. Add '-> <ReturnType>' to every public def and replace Any with a "
        "concrete type."),
    "EU-446 tenant-query": (
        "Supabase query on a user-scoped table with no tenant scoping signal in the diff (EU-446): "
        "supabase.from('users').select('*'). Confirm an explicit tenant filter (.eq('tenant_id', "
        "...) / SQL 'where tenant_id =') or a Supabase row-level-security policy guards this query, "
        "otherwise it is a cross-tenant data-leak."),
}
for _label, _detail in _FORCED_BLOCKERS.items():
    _r = ReviewResult(verdict=Verdict.FAIL, spec_met=True,
                      quality_issues=[QualityIssue(severity="blocker", area="x", detail=_detail)])
    _r = reviewer_mod._enforce_bounce_once(_r, set(), final_pass=True)
    chk(f"A4b: the {_label} forced blocker survives the final pass",
        any(q.detail == _detail for q in _r.blocking_issues) and _r.verdict == Verdict.FAIL,
        f"{_r.verdict} / {_r.unverifiable_gaps}")

# A5 — the execution gate is never relieved by final_pass, even when the quoted AC classifies.
EXEC_GAP = (f"{reviewer_mod._EXEC_GATE_GAP_PREFIX}/gate report attached — "
            "unverified: \"the responsive grid tests pass\"")
chk("sanity: EXEC_GAP would classify as unverifiable (so the guard is load-bearing)",
    reviewer_mod._classify_unverifiable_finding(EXEC_GAP) is not None,
    str(reviewer_mod._classify_unverifiable_finding(EXEC_GAP)))
a5 = ReviewResult(verdict=Verdict.FAIL, spec_met=False, spec_gaps=[EXEC_GAP])
a5 = reviewer_mod._enforce_bounce_once(a5, set(), final_pass=True)
chk("A5: execution-gate gap survives the final-pass demotion",
    a5.spec_gaps == [EXEC_GAP], str(a5.spec_gaps))
chk("A5: execution-gate gap not demoted into unverifiable_gaps",
    EXEC_GAP not in a5.unverifiable_gaps, str(a5.unverifiable_gaps))
chk("A5: execution-gate gap keeps spec_met False", a5.spec_met is False, str(a5.spec_met))
chk("A5: execution-gate gap keeps the verdict FAIL", a5.verdict == Verdict.FAIL, str(a5.verdict))

# A5b — the PRIOR contract still holds: a repeat exec-gate fingerprint demotes via already_bounced
# (that relief lives in _enforce_execution_gate; this only pins that final_pass didn't disturb it).
a5b = ReviewResult(verdict=Verdict.FAIL, spec_met=False, spec_gaps=[EXEC_GAP])
_exec_lens = reviewer_mod._classify_unverifiable_finding(EXEC_GAP)
a5b = reviewer_mod._enforce_bounce_once(
    a5b, {reviewer_mod._finding_fingerprint(_exec_lens, EXEC_GAP)}, final_pass=False)
chk("A5b: pre-existing repeat-fingerprint demotion is unchanged",
    a5b.spec_gaps == [] and EXEC_GAP in a5b.unverifiable_gaps,
    f"gaps={a5b.spec_gaps} unver={a5b.unverifiable_gaps}")


# ══════════════════════════════════════════════════════════════════════════════
# FIX B — self-referential enforcer skip (pure helpers)
# ══════════════════════════════════════════════════════════════════════════════
def _diff_for(path: str, added: int = 8) -> str:
    """A production diff touching `path` with `added` added code lines and NO test file — the exact
    shape the EU-441 missing-tests enforcer forces a FAIL on."""
    body = "\n".join(f"+    x{i} = compute({i})" for i in range(added))
    return (f"diff --git a/{path} b/{path}\n"
            f"index 111..222 333\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"@@ -1,3 +1,{added + 3} @@\n"
            f" def existing() -> int:\n{body}\n")


def _diff_with_test(path: str) -> str:
    """`_diff_for(path)` plus an accompanying test file, so the EU-441 missing-tests enforcer is
    satisfied and only the enforcer under examination is in play."""
    return _diff_for(path) + _diff_for("tests/some_test.py", added=6)


REVIEWER_DIFF = _diff_for("orchestrator/reviewer.py")
OTHER_DIFF = _diff_for("orchestrator/gate.py")
PRODUCT_DIFF = _diff_for("src/components/Button.tsx")
PRODUCT_DIFF_WITH_TEST = _diff_with_test("src/components/Button.tsx")

# The expected keys are spelled out (not derived from _ENFORCER_SOURCE_FILES) so that dropping an
# enforcer from the map fails here instead of silently redefining what the test asserts.
_ALL_ENFORCERS = {"admitted-red-tests", "missing-tests", "missing-typing", "tenant-query"}
_skips_reviewer = reviewer_mod._self_referential_enforcer_skips(REVIEWER_DIFF)
chk("B1: a reviewer.py diff skips all four deterministic enforcers",
    set(_skips_reviewer) == _ALL_ENFORCERS, str(_skips_reviewer))
chk("B1: the enforcer→source map covers exactly those four",
    set(reviewer_mod._ENFORCER_SOURCE_FILES) == _ALL_ENFORCERS,
    str(sorted(reviewer_mod._ENFORCER_SOURCE_FILES)))
# gate.py carries the OTHER half of the red-admission parser (gate._red_test_admission /
# gate_vs_builder_verdict, importing reviewer.py's regexes) — so it relieves that ONE enforcer and
# no other. EU-520 was exactly a gate.py verdict-mismatch ticket.
chk("B1: an orchestrator/gate.py diff skips admitted-red-tests and ONLY that",
    set(reviewer_mod._self_referential_enforcer_skips(OTHER_DIFF)) == {"admitted-red-tests"},
    str(reviewer_mod._self_referential_enforcer_skips(OTHER_DIFF)))
chk("B1: a product diff skips nothing",
    reviewer_mod._self_referential_enforcer_skips(PRODUCT_DIFF) == [],
    str(reviewer_mod._self_referential_enforcer_skips(PRODUCT_DIFF)))
chk("B1: an empty diff skips nothing",
    reviewer_mod._self_referential_enforcer_skips("") == [],
    str(reviewer_mod._self_referential_enforcer_skips("")))
# The match is an exact repo-relative path compare, so a same-named file in some other tree does
# NOT unlock the relief for a product ticket.
chk("B1: a look-alike path (vendor/orchestrator/reviewer.py) does NOT skip",
    reviewer_mod._self_referential_enforcer_skips(_diff_for("vendor/orchestrator/reviewer.py")) == [],
    str(reviewer_mod._self_referential_enforcer_skips(_diff_for("vendor/orchestrator/reviewer.py"))))

# B5 — the recorded note must be inert for the EU-351/EU-352 bounce machinery.
_note = reviewer_mod._enforcer_skip_note("missing-tests")
chk("B5: the skip note does not classify as an unverifiable FINDING",
    reviewer_mod._classify_unverifiable_finding(_note) is None,
    str(reviewer_mod._classify_unverifiable_finding(_note)))
chk("B5: a result carrying only the skip note contributes no bounce fingerprints",
    reviewer_mod.collect_unverifiable_fingerprints(
        ReviewResult(verdict=Verdict.PASS, spec_met=True, unverifiable_gaps=[_note])) == set(),
    "")


# ══════════════════════════════════════════════════════════════════════════════
# review() end-to-end — A6 (final_pass) and B2/B3/B4 (self-referential skip)
# ══════════════════════════════════════════════════════════════════════════════
class _RR:
    def __init__(self, t):
        (self.final, self.text, self.is_error, self.cost_usd, self.num_turns, self.tools,
         self.provider, self.model_version) = t, t, False, 0.0, 1, [], "Anthropic", "claude-sonnet-4-6"
        self.input_tokens = self.output_tokens = 0


_PASS_JSON = "```json\n" + json.dumps({
    "verdict": "PASS",
    "spec_conformance": {"met": True, "gaps": []},
    "quality": {"issues": []},
    "required_changes": [],
    "summary": "clean",
}) + "\n```"

_UNVERIFIABLE_FAIL_JSON = "```json\n" + json.dumps({
    "verdict": "FAIL",
    "spec_conformance": {"met": True, "gaps": []},
    "quality": {"issues": [{"severity": "blocker", "area": "ui", "detail": FRESH_UNVERIFIABLE}]},
    "required_changes": ["fix the spacing"],
    "summary": "visual concern",
}) + "\n```"

_STUB_REPLY = [_PASS_JSON]


async def _fake_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None,
                          cfg=None, routing_tier=None):   # EU-258: mirror the real signature
    return _RR(_STUB_REPLY[0])


_orig_run_agent_fb = reviewer_mod.run_agent_with_fallback
reviewer_mod.run_agent_with_fallback = _fake_run_agent

_cfg = Config(apps=[AppConfig(name="general", repo_path=".", base_branch="dev",
                              protected_branch="main", backlog_backend="none")],
              audit_path="/tmp/final-pass-selfref-audit.jsonl", use_worktree=False, auto_model=False)
_app = _cfg.app("general")
_ticket = Ticket(id="EU-999", key="EU-999", summary="Reviewer non-convergence",
                 description="d", acceptance_criteria=["The demotion reaches the final pass"])
_artifact = BuildArtifact(files_changed=["orchestrator/reviewer.py"],
                          diff_digest="widened the demotion", decisions=[], open_questions=[])

# ── A6: final_pass threaded through review() ─────────────────────────────────────────────────────
_STUB_REPLY[0] = _UNVERIFIABLE_FAIL_JSON
_r_nonfinal = asyncio.run(reviewer_mod.review(PRODUCT_DIFF_WITH_TEST, _ticket, _app, _cfg, 1,
                                              build_artifact=_artifact))
chk("A6: review() default (final_pass omitted) -> fresh unverifiable finding still blocks",
    any(q.detail == FRESH_UNVERIFIABLE for q in _r_nonfinal.blocking_issues),
    str(_r_nonfinal.blocking_issues))
chk("A6: review() default -> verdict stays FAIL",
    _r_nonfinal.verdict == Verdict.FAIL, str(_r_nonfinal.verdict))

_r_final = asyncio.run(reviewer_mod.review(PRODUCT_DIFF_WITH_TEST, _ticket, _app, _cfg, 3,
                                           build_artifact=_artifact, final_pass=True))
chk("A6: review(final_pass=True) -> fresh unverifiable finding demoted",
    _r_final.blocking_issues == [] and FRESH_UNVERIFIABLE in _r_final.unverifiable_gaps,
    f"blocking={_r_final.blocking_issues} unver={_r_final.unverifiable_gaps}")
chk("A6: review(final_pass=True) -> verdict reaches PASS",
    _r_final.verdict == Verdict.PASS, str(_r_final.verdict))

# ── B2/B3: the EU-441 missing-tests enforcer ─────────────────────────────────────────────────────
_STUB_REPLY[0] = _PASS_JSON
_r_self = asyncio.run(reviewer_mod.review(REVIEWER_DIFF, _ticket, _app, _cfg, 1,
                                          build_artifact=_artifact))
chk("B2: reviewer.py diff without a test file -> missing-tests enforcer does NOT force FAIL",
    _r_self.verdict == Verdict.PASS, f"{_r_self.verdict} / {[q.detail for q in _r_self.quality_issues]}")
chk("B2: reviewer.py diff -> no forced 'tests' blocker recorded",
    not any(q.area == "tests" for q in _r_self.quality_issues),
    str([(q.area, q.detail) for q in _r_self.quality_issues]))
chk("B2: reviewer.py diff -> each skip is recorded on unverifiable_gaps",
    all(reviewer_mod._enforcer_skip_note(k) in _r_self.unverifiable_gaps
        for k in reviewer_mod._ENFORCER_SOURCE_FILES),
    str(_r_self.unverifiable_gaps))

_r_product = asyncio.run(reviewer_mod.review(PRODUCT_DIFF, _ticket, _app, _cfg, 1,
                                             build_artifact=_artifact))
chk("B3: the SAME diff shape on a product file still forces the missing-tests FAIL",
    _r_product.verdict == Verdict.FAIL, str(_r_product.verdict))
chk("B3: ...with the 'tests' blocker recorded",
    any(q.area == "tests" and q.severity == "blocker" for q in _r_product.quality_issues),
    str([(q.area, q.severity) for q in _r_product.quality_issues]))
chk("B3: ...and no skip note (the relief is self-referential only)",
    _r_product.unverifiable_gaps == [], str(_r_product.unverifiable_gaps))
# gate.py relieves ONLY admitted-red-tests — the missing-tests gate still bites there.
_r_gate = asyncio.run(reviewer_mod.review(OTHER_DIFF, _ticket, _app, _cfg, 1,
                                          build_artifact=_artifact))
chk("B3: a gate.py diff still forces the missing-tests FAIL (its skip is scoped to one enforcer)",
    _r_gate.verdict == Verdict.FAIL
    and any(q.area == "tests" and q.severity == "blocker" for q in _r_gate.quality_issues),
    f"{_r_gate.verdict} / {[(q.area, q.severity) for q in _r_gate.quality_issues]}")

# ── B4: the EU-249 admitted-red-tests enforcer reads the HANDOFF, same asymmetry ─────────────────
_red_artifact = BuildArtifact(
    files_changed=["orchestrator/reviewer.py"],
    diff_digest="Added detection for a handoff that admits a failing test; one test is failing.",
    decisions=[], open_questions=[])
chk("sanity: the handoff admits a red test (the enforcer would fire)",
    reviewer_mod._admitted_red_test_note(_red_artifact) is not None,
    str(reviewer_mod._admitted_red_test_note(_red_artifact)))

# Use a diff WITH a test file so only the admitted-red-tests enforcer is in play on both sides.
_r_red_self = asyncio.run(reviewer_mod.review(_diff_with_test("orchestrator/reviewer.py"),
                                              _ticket, _app, _cfg, 1, build_artifact=_red_artifact))
chk("B4: reviewer.py diff -> admitted-red-tests enforcer skipped, verdict stays PASS",
    _r_red_self.verdict == Verdict.PASS, str(_r_red_self.verdict))
# gate.py holds the other half of the red-admission parser (the EU-442 verdict-mismatch check, whose
# docstring cites EU-520 — the ticket that escalated this way), so it relieves this enforcer too.
_r_red_gate = asyncio.run(reviewer_mod.review(_diff_with_test("orchestrator/gate.py"),
                                              _ticket, _app, _cfg, 1, build_artifact=_red_artifact))
chk("B4: gate.py diff (the EU-520 shape) -> admitted-red-tests enforcer skipped, verdict PASS",
    _r_red_gate.verdict == Verdict.PASS, str(_r_red_gate.verdict))
# A product ticket with the identical handoff is NOT relieved — the enforcer is intact everywhere else.
_r_red_product = asyncio.run(reviewer_mod.review(_diff_with_test("src/components/Button.tsx"),
                                                 _ticket, _app, _cfg, 1, build_artifact=_red_artifact))
chk("B4: a product diff with the same handoff -> admitted-red-tests still forces FAIL",
    _r_red_product.verdict == Verdict.FAIL, str(_r_red_product.verdict))
chk("B4: ...with the EU-249 blocker recorded",
    any(q.area == "tests" and q.severity == "blocker" for q in _r_red_product.quality_issues),
    str([(q.area, q.severity) for q in _r_red_product.quality_issues]))

reviewer_mod.run_agent_with_fallback = _orig_run_agent_fb


# ══════════════════════════════════════════════════════════════════════════════
# A6 (loop wiring) — the loop computes final_pass and hands it to BOTH review calls
# ══════════════════════════════════════════════════════════════════════════════
_loop_src = (ROOT / "orchestrator" / "loop.py").read_text()
_fp_calc = _loop_src.find("_final_pass = iteration >= max_passes")
_first_review = _loop_src.find("reviewer_mod.review(")
chk("A6(loop): _final_pass is computed from iteration >= max_passes",
    _fp_calc != -1, str(_fp_calc))
chk("A6(loop): it is computed BEFORE the first reviewer_mod.review() call",
    _fp_calc != -1 and _first_review != -1 and _fp_calc < _first_review,
    f"calc={_fp_calc} review={_first_review}")
chk("A6(loop): every reviewer_mod.review() call passes final_pass=_final_pass",
    _loop_src.count("final_pass=_final_pass") == _loop_src.count("reviewer_mod.review("),
    f"passes={_loop_src.count('final_pass=_final_pass')} "
    f"calls={_loop_src.count('reviewer_mod.review(')}")

print("\n=========== REVIEWER FINAL-PASS DEMOTION + SELF-REFERENTIAL ENFORCER SKIP ===========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
