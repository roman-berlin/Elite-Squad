"""EU-463 regression tests: reviewer typing gate must respect CURRENT branch state and the
TYPE_CHECKING / ``from __future__ import annotations`` idiom.

These tests cover six acceptance criteria:

  1. A diff that modifies only a param line inside an existing multi-line signature is NOT
     flagged when the closing '-> RetType:' sits on an unchanged context line outside the
     added-run scan.
  2. A fully-annotated diff using the TYPE_CHECKING idiom passes unchanged (regression).
  3. An import line like ``from typing import TYPE_CHECKING, Any`` alone does NOT trip the
     explicit-Any scan; a genuine annotation ``def f(x: Any) -> None:`` still does (true positive).
  4. A genuinely untyped NEW public def whose complete signature lies entirely within one
     contiguous added run is STILL flagged (no EU-441 regression).
  5. The span-walk does NOT leak across hunks — two @@ headers keep runs independent.
  6. REVIEWER_SYSTEM's typing text contains the TYPE_CHECKING / from __future__ acceptance rule.
  7. REVIEWER_SYSTEM's own prose, scanned as added production lines, does NOT self-trip the
     explicit-typing scan (iteration-1 root cause: the prompt QUOTED the literal erosion patterns
     it was describing, so this ticket's own diff was force-FAILed by the very gate it fixed).

All offline — SDK stubbed; no network, no real models.
"""
from __future__ import annotations

import sys
import types

# ── SDK stub (mirrors tests/eu441_reviewer_hardening_test.py) ────────────────────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a: object, **k: object) -> None: self.__dict__.update(k)
    def __call__(self, *a: object, **k: object) -> _D: return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import reviewer as R       # noqa: E402
from orchestrator.contracts import ReviewResult, Ticket, Verdict  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: object, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def _pass_result() -> ReviewResult:
    """Fresh PASS result — deterministic gates mutate in-place."""
    return ReviewResult(verdict=Verdict.PASS, spec_met=True, spec_gaps=[], summary="ok")


# ════════════════════════════════════════════════════════════════════════════════════════════
# Fixture diffs per AC
# ════════════════════════════════════════════════════════════════════════════════════════════

# AC-1: Modified param inside an existing multi-line signature.
# The def header and ) -> RetType: are unchanged context lines. Only the param line changed.
# Since our scanner ONLY sees added lines, it cannot see the def header or the -> arrow — so
# it must stay SILENT (not flag anything) rather than mis-assemble a pseudo-signature from
# partial added lines.
DIFF_AC1_PARAM_INSIDE_SIG = (
    "diff --git a/src/svc.py b/src/svc.py\n"
    "--- a/src/svc.py\n"
    "+++ b/src/svc.py\n"
    "@@ -3,3 +3,3 @@\n"
    " def load(\n"
    "-    cfg: Any,\n"                    # this was the old untyped-param line
    "+    cfg: Config,\n"                # ← the only change
    " ) -> Config:\n"
)

# AC-2: Fully-typed diff using the TYPE_CHECKING + from __future__ idiom.
# Every public def carries concrete types; there is NO runtime Any.
DIFF_AC2_TYPECHECKING_IDIOM = (
    "diff --git a/src/loader.py b/src/loader.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/loader.py\n"
    "@@ -0,0 +1,14 @@\n"
    "+from __future__ import annotations\n"
    "+\n"
    "+from typing import TYPE_CHECKING\n"
    "+\n"
    "+if TYPE_CHECKING:\n"
    "+    from .config import Config\n"
    "+\n"
    "+\n"
    "+def load(cfg: Config) -> Config:\n"
    "+    \"\"\"Load configuration.\"\"\"\n"
    "+    return cfg\n"
    "+\n"
    "+def dump(cfg: Config) -> None:\n"
    "+    pass\n"
)

# AC-3a: Import line with Any alone — must NOT trigger.
DIFF_AC3_IMPORT_ANY_ONLY = (
    "diff --git a/src/imports.py b/src/imports.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/imports.py\n"
    "@@ -0,0 +1,1 @@\n"
    "+from typing import TYPE_CHECKING, Any\n"
)

# AC-3b: Genuine explicit Any annotation — MUST trigger.
DIFF_AC3_GENUINE_ANY = (
    "diff --git a/src/real_any.py b/src/real_any.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/real_any.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def process(data: Any) -> Any:\n"
    "+    return data\n"
)

# AC-4: Genuinely untyped new public def, complete signature within one contiguous added run.
DIFF_AC4_UNTYPED_DEF = (
    "diff --git a/src/raw.py b/src/raw.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/raw.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+def compute(a, b):\n"
    "+    return a + b\n"
    "+\n"
)

# AC-5: Two hunks; hunk 1 ends mid-signature (unclosed parens); hunk 2 has ) -> int: from
# a DIFFERENT function. The walker must NOT stitch them together.
DIFF_AC5_CROSS_HUNK = (
    "diff --git a/src/multi.py b/src/multi.py\n"
    "--- a/src/multi.py\n"
    "+++ b/src/multi.py\n"
    "@@ -2,3 +2,4 @@\n"         # --- hunk 1 (ends after def line) ---
    " class Calc:\n"
    "+    def foo(\n"             # def on added line, parens unclosed
    "+        pass\n"            # more added content
    "\n"
    "@@ -8,3 +9,4 @@\n"          # --- hunk 2 ---
    "     return x\n"
    "+\n"
    "+    def bar(self, x: int) -> int:\n"   # unrelated def with full annotation
    "+        return x\n"
)

# ════════════════════════════════════════════════════════════════════════════════════════════
# Test functions
# ════════════════════════════════════════════════════════════════════════════════════════════

def ac1_param_inside_sig_not_flagged() -> None:
    """A diff that modifies only a param line inside an existing signature → NOT flagged.

    The added-run contains just ``+    cfg: Config,`` — no def header, no closing paren.
    With hunk-aware scanning, this single added line starts a run of length 1; since there's
    no 'def' keyword in that run, _untyped_public_defs stays silent.  Without hunk awareness,
    an older implementation might accumulate this line across contexts and later match against
    a def it shouldn't see — producing a false-positive flag.
    """
    untyped = R._untyped_public_defs(DIFF_AC1_PARAM_INSIDE_SIG)
    chk("AC1 _untyped_public_defs: modified param line → not flagged",
        untyped == [], str(untyped))

    r = R._enforce_missing_typing(_pass_result(), DIFF_AC1_PARAM_INSIDE_SIG)
    chk("AC1 _enforce_missing_typing: modified param line → verdict stays PASS, no typing issue",
        r.verdict == Verdict.PASS
        and not any(q.area == "typing" for q in r.quality_issues),
        str(r.verdict) + " | " + str([(q.severity, q.area) for q in r.quality_issues]))


def ac2_typechecking_idiom_passes() -> None:
    """Fully-typed diff using TYPE_CHECKING + from __future__ → NOT flagged.

    This is the named EU-444 regression: the Builder annotated everything properly but the
    Reviewer re-flagged because it didn't recognize the idiom as satisfying 'concrete type'.
    The reviewer.py deterministic gate is purely regex/diff-based and never resolves names —
    so the key fix is ensuring the import line doesn't trip the explicit-Any scan.
    """
    untyped = R._untyped_public_defs(DIFF_AC2_TYPECHECKING_IDIOM)
    chk("AC2 _untyped_public_defs: TYPE_CHECKING idiom → not flagged",
        untyped == [], str(untyped))

    r = R._enforce_missing_typing(_pass_result(), DIFF_AC2_TYPECHECKING_IDIOM)
    chk("AC2 _enforce_missing_typing: TYPE_CHECKING idiom → verdict PASS, no typing issues",
        r.verdict == Verdict.PASS
        and not any(q.area == "typing" for q in r.quality_issues),
        str(r.verdict) + " | " + str([(q.severity, q.area) for q in r.quality_issues]))


def ac3_import_line_no_false_positive() -> None:
    """Import line with Any does NOT trip; genuine usage DOES.

    True-negative: ``from typing import TYPE_CHECKING, Any`` is just metadata about what's
    available at runtime — every module can carry this without using Any as a type argument.
    True-positive: ``def process(data: Any) -> Any:`` uses Any as an actual type annotation.
    """
    # True-negative: import only
    untyped_imp = R._untyped_public_defs(DIFF_AC3_IMPORT_ANY_ONLY)
    chk("AC3a _untyped_public_defs: import-only Any → not flagged",
        untyped_imp == [], str(untyped_imp))

    r_imp = R._enforce_missing_typing(_pass_result(), DIFF_AC3_IMPORT_ANY_ONLY)
    chk("AC3a _enforce_missing_typing: import-only Any → verdict PASS, no typing issue",
        r_imp.verdict == Verdict.PASS
        and not any(q.area == "typing" for q in r_imp.quality_issues),
        str(r_imp.verdict) + " | " + str([(q.severity, q.area) for q in r_imp.quality_issues]))

    # True-positive: genuine Any usage
    untyped_genuine = R._untyped_public_defs(DIFF_AC3_GENUINE_ANY)
    chk("AC3b _untyped_public_defs: genuine Any → silently OK (typing enforced via explicit-Any)",
        untyped_genuine == [], str(untyped_genuine))

    r_genuine = R._enforce_missing_typing(_pass_result(), DIFF_AC3_GENUINE_ANY)
    chk("AC3b _enforce_missing_typing: genuine Any → verdict FAIL, typing issue recorded",
        r_genuine.verdict == Verdict.FAIL
        and any(q.area == "typing" for q in r_genuine.quality_issues),
        str(r_genuine.verdict) + " | " + str([(q.severity, q.area, q.detail) for q in r_genuine.quality_issues]))


def ac4_untyped_def_still_flagged() -> None:
    """Genuinely untyped public def whose entire signature is in one contiguous added run → flagged.

    This is the original EU-441 true positive; we must NOT regress it.
    """
    untyped = R._untyped_public_defs(DIFF_AC4_UNTYPED_DEF)
    chk("AC4 _untyped_public_defs: untyped def 'compute' → IS flagged",
        any("compute" in u for u in untyped), str(untyped))

    r = R._enforce_missing_typing(_pass_result(), DIFF_AC4_UNTYPED_DEF)
    chk("AC4 _enforce_missing_typing: untyped def → verdict FAIL, typing block",
        r.verdict == Verdict.FAIL
        and any(q.area == "typing" for q in r.quality_issues),
        str(r.verdict) + " | " + str([(q.severity, q.area) for q in r.quality_issues]))


def ac5_span_walk_no_hunk_leak() -> None:
    """Two @@ hunks must NOT be stitched into one signature-span walk.

    Hunk 1 has ``def foo(\n`` with unclosed parens; hunk 2 (separate @@) has ``) -> int:``
    belonging to ``bar``. If the span-walk ignores hunk boundaries, it would connect these
    into a pseudo-signature and falsely conclude ``foo`` is typed — a dangerous false negative.
    """
    untyped = R._untyped_public_defs(DIFF_AC5_CROSS_HUNK)
    chk("AC5 _untyped_public_defs: cross-hunk def foo → NOT falsely-typed (remains silent)",
        # We expect silence because: hunk 1 starts a def with unclosed parens, and the closing
        # paren isn't in hunk 1. The span-walk correctly stays within the hunk.
        untyped == [], str(untyped))

    r = R._enforce_missing_typing(_pass_result(), DIFF_AC5_CROSS_HUNK)
    chk("AC5 _enforce_missing_typing: cross-hunk def foo → verdict stays PASS",
        r.verdict == Verdict.PASS
        and not any(q.area == "typing" for q in r.quality_issues),
        str(r.verdict) + " | " + str([(q.severity, q.area) for q in r.quality_issues]))


def ac6_reviewer_system_typing_text() -> None:
    """REVIEWER_SYSTEM's typing REQUIRED-CHECKS bullet mentions the TYPE_CHECKING acceptance rule.

    When ``from __future__ import annotations`` guards the module, forward-references inside
    ``if TYPE_CHECKING:`` imports are resolved at static-analysis time and DO count as concrete
    types satisfying the EU-441 requirement. Each typing demand must be checked against the
    CURRENT diff before being re-raised to prevent infinite loops.
    """
    sys_txt = R.REVIEWER_SYSTEM
    chk("AC6 REVIEWER_SYSTEM: TYPE_CHECKING substring present",
        "TYPE_CHECKING" in sys_txt,
        "TYPE_CHECKING not found in REVIEWER_SYSTEM")
    chk("AC6 REVIEWER_SYSTEM: from __future__ import annotations substring present",
        "from __future__ import annotations" in sys_txt,
        "from __future__ import annotations not found in REVIEWER_SYSTEM")


def ac7_reviewer_system_does_not_self_trip() -> None:
    """The reviewer's own system-prompt prose must survive the deterministic typing gate.

    Iteration 1 of EU-463 was REJECTED by the very gate it fixed: the prompt text lives in a
    production .py file (orchestrator/reviewer.py), so every added line of it is scanned — and the
    added bullet QUOTED the literal annotation-erosion patterns it was describing (a quoted
    colon-any token, and two comments quoting a ``from typing import …`` line ending in the
    uppercase Any type). Each quote matched the explicit-typing scan as if it were a real
    annotation, force-FAILing this ticket's own diff with the boilerplate 'annotate every public
    def' demand. The prose must DESCRIBE the rule without quoting a pattern the scanner matches.
    """
    sys_lines = R.REVIEWER_SYSTEM.split("\n")
    added = "".join(f"+{line}\n" for line in sys_lines)
    diff = (
        "diff --git a/orchestrator/reviewer.py b/orchestrator/reviewer.py\n"
        "--- a/orchestrator/reviewer.py\n"
        "+++ b/orchestrator/reviewer.py\n"
        "@@ -21,4 +21,5 @@\n"
        + added
    )
    # Anti-vacuity: the embedding must actually feed every prompt line to the typing cursor —
    # a misparse that drops lines would make the gate pass for the wrong reason.
    yielded = list(R._added_python_production_lines(diff))
    chk("AC7 embedding non-vacuous: every REVIEWER_SYSTEM line reaches the typing cursor",
        len(yielded) == len(sys_lines),
        f"{len(yielded)} yielded vs {len(sys_lines)} prompt lines")

    untyped = R._untyped_public_defs(diff)
    chk("AC7 _untyped_public_defs: REVIEWER_SYSTEM prose as added lines → not flagged",
        untyped == [], str(untyped))

    r = R._enforce_missing_typing(_pass_result(), diff)
    chk("AC7 _enforce_missing_typing: REVIEWER_SYSTEM prose as added lines → verdict stays PASS",
        r.verdict == Verdict.PASS
        and not any(q.area == "typing" for q in r.quality_issues),
        str(r.verdict) + " | " + str([(q.severity, q.area, q.detail) for q in r.quality_issues]))


# ════════════════════════════════════════════════════════════════════════════════════════════
# Run all ACs
# ════════════════════════════════════════════════════════════════════════════════════════════

_ACS = [ac1_param_inside_sig_not_flagged, ac2_typechecking_idiom_passes,
        ac3_import_line_no_false_positive, ac4_untyped_def_still_flagged,
        ac5_span_walk_no_hunk_leak, ac6_reviewer_system_typing_text,
        ac7_reviewer_system_does_not_self_trip]


def main() -> int:
    print("=== EU-463 typing-gate regression tests ===\n")
    for fn in _ACS:
        fn()
        print(f"  ran {fn.__name__}")
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, d) for n, ok, d in results if not ok]
    print(f"\n{'='*56}")
    for n, d in failed:
        print(f"  [FAIL] {n}" + (f"  ({d})" if d else ""))
    if not failed:
        print("  ALL GREEN")
    print(f"  {passed}/{len(results)} passed")
    print(f"{'='*56}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
