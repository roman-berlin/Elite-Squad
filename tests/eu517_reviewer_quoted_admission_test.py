"""EU-517 regression: wire `_strip_quoted_context` into `_admitted_red_test_note`.

Before this ticket, quoted/example context (backtick-enclosed text, fenced blocks) still tripped
the admitted-red-test scanner as if it were a genuine admission. After wiring `_strip_quoted_context`
into the scanner, a field that merely *quotes* red-admission phrasing as a fixture/example must no
longer produce a false FAIL — while genuine unquoted first-person admissions still do.

AC 1: backticked example in diff_digest → None
AC 2: fenced block quote → None
AC 3: genuine unquoted STRONG admission → non-None (original text returned)
AC 4: WEAK signal whose only "fixed" wording is inside backticks → still non-None (quoting can't
      clear; _RESOLVED_CTX_RE also runs on the stripped text)
AC 5: empty-after-strip → continue (skip the field entirely)
"""
import sys
sys.path.insert(0, ".")

from orchestrator.contracts import BuildArtifact
from orchestrator import reviewer as R

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# ── AC 1: backticked example in diff_digest should NOT trip ────────────────
def ac1_backticked_example() -> None:
    ba = BuildArtifact(
        files_changed=[],
        diff_digest="Added a fixture where the summary reads `the test suite is still red` and verified it is ignored",
        decisions=[], open_questions=[], caveats=[],
    )
    chk("AC1: backticked phrase in diff_digest → None",
        R._admitted_red_test_note(ba) is None,
        f"got {R._admitted_red_test_note(ba)!r}")


# ── AC 2: fenced block quote should NOT trip ──────────────────────────────
def ac2_fenced_block() -> None:
    caveats = [
        "Example handoff:\n```\ncouldn't get the localStorage tests to pass\n```\nverified the scanner ignores it",
    ]
    ba = BuildArtifact(
        files_changed=[],
        diff_digest="",
        decisions=[], open_questions=[], caveats=caveats,
    )
    chk("AC2: fenced block with 'couldn't … tests' in caveats → None",
        R._admitted_red_test_note(ba) is None,
        f"got {R._admitted_red_test_note(ba)!r}")


# ── AC 3: genuine unquoted STRONG admission MUST fire ─────────────────────
def ac3_genuine_strong() -> None:
    caveats = ["tests are still failing on CI"]
    ba = BuildArtifact(
        files_changed=[],
        diff_digest="",
        decisions=[], open_questions=[], caveats=caveats,
    )
    note = R._admitted_red_test_note(ba)
    chk("AC3: genuine unquoted STRONG admission → non-None",
        note is not None,
        f"got {note!r} — expected a note")
    if note is not None:
        chk("AC3: returned note contains original text 'still failing'",
            "still failing" in note,
            f"got {note!r}")


# ── AC 4: weak signal with resolution wording INSIDE backticks → still fires ──
def ac4_weak_inside_quotes() -> None:
    """A WEAK pattern ('failing … test') whose ONLY 'fixed' marker is inside backticks
    must STILL return non-None because after stripping the backtick span, the word 'fixed'
    disappears and _RESOLVED_CTX_RE no longer clears the weak signal."""
    ba = BuildArtifact(
        files_changed=[],
        diff_digest="the failing dropdown test remains; docs say `fixed` elsewhere",
        decisions=[], open_questions=[], caveats=[],
    )
    note = R._admitted_red_test_note(ba)
    chk("AC4: WEAK signal + 'fixed' inside backticks → still non-None",
        note is not None,
        f"got {note!r}")


# ── AC 5: empty-after-strip → skip, don't fire ────────────────────────────
def ac5_empty_after_strip() -> None:
    """If a field consists entirely of quoted content (fences/backticks/blockquotes),
    stripped text becomes '' → the field is skipped."""
    ba = BuildArtifact(
        files_changed=[],
        diff_digest='```\nthe test suite is still red\n```',
        decisions=[], open_questions=[], caveats=[],
    )
    chk("AC5: field that is purely a quoted block → None",
        R._admitted_red_test_note(ba) is None,
        f"got {R._admitted_red_test_note(ba)!r}")


# ── All check functions ────────────────────────────────────────────────────
_CHECKS = (ac1_backticked_example, ac2_fenced_block, ac3_genuine_strong,
           ac4_weak_inside_quotes, ac5_empty_after_strip)


def main() -> int:
    for fn in _CHECKS:
        fn()
    passed = sum(1 for _, ok, _ in results if ok)
    print("\n================ EU-517 QUOTED ADMISSION HANDLING ===================")
    for n, ok, det in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
    print("---------------------------------------------------------------------")
    print(f"  {passed}/{len(results)} passed")
    print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
