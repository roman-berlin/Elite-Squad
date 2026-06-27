"""Tests for the EU-72 structured-artifact dataclasses in orchestrator/contracts.py.

Covers:
- Construction and field access for SpecArtifact, BuildArtifact, ReviewVerdict.
- PerTicketArtifactStore.put() routing each artifact to the correct slot.
- put() raises TypeError for unknown types.
- Slots start as None so downstream officers can detect 'not yet produced'.
"""
import sys
sys.path.insert(0, ".")

from orchestrator.contracts import (
    BuildArtifact,
    PerTicketArtifactStore,
    ReviewVerdict,
    SpecArtifact,
    Verdict,
)

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))


# ------------------------------------------------------------------ #
# SpecArtifact
# ------------------------------------------------------------------ #
spec = SpecArtifact(
    acceptance=["users can log in", "session persists across refresh"],
    scope="authentication flow only",
    non_goals=["authorisation / RBAC", "password reset"],
)
check("SpecArtifact.acceptance list", spec.acceptance == ["users can log in", "session persists across refresh"])
check("SpecArtifact.scope string", spec.scope == "authentication flow only")
check("SpecArtifact.non_goals list", spec.non_goals == ["authorisation / RBAC", "password reset"])

# empty lists are valid (e.g. no explicit non-goals)
spec_empty = SpecArtifact(acceptance=[], scope="", non_goals=[])
check("SpecArtifact allows empty lists", spec_empty.acceptance == [] and spec_empty.non_goals == [])


# ------------------------------------------------------------------ #
# BuildArtifact
# ------------------------------------------------------------------ #
build = BuildArtifact(
    files_changed=["orchestrator/contracts.py", "tests/eu72_contracts_artifacts_test.py"],
    diff_digest="Added SpecArtifact, BuildArtifact, ReviewVerdict, PerTicketArtifactStore.",
    decisions=["Used dataclass instead of TypedDict for IDE autocomplete"],
    open_questions=["Should PerTicketArtifactStore be persisted to disk?"],
)
check("BuildArtifact.files_changed", len(build.files_changed) == 2)
check("BuildArtifact.diff_digest non-empty", bool(build.diff_digest))
check("BuildArtifact.decisions list", build.decisions == ["Used dataclass instead of TypedDict for IDE autocomplete"])
check("BuildArtifact.open_questions list", len(build.open_questions) == 1)

# __post_init__ enforces the ≤ 500-char diff_digest ceiling the docstring promises (no second copy
# of the diff). Exactly 500 is allowed; 501 raises so an over-long digest can't slip through.
ok_500 = BuildArtifact(files_changed=[], diff_digest="x" * 500, decisions=[], open_questions=[])
check("BuildArtifact allows a 500-char diff_digest", len(ok_500.diff_digest) == 500)
raised_len = False
try:
    BuildArtifact(files_changed=[], diff_digest="x" * 501, decisions=[], open_questions=[])
except ValueError:
    raised_len = True
check("BuildArtifact rejects a >500-char diff_digest (__post_init__ guard)", raised_len)


# ------------------------------------------------------------------ #
# ReviewVerdict
# ------------------------------------------------------------------ #
verdict_pass = ReviewVerdict(verdict=Verdict.PASS, blocking=[], notes=["minor nit on docstring"])
check("ReviewVerdict PASS verdict (typed Verdict enum)", verdict_pass.verdict is Verdict.PASS)
check("ReviewVerdict no blockers", verdict_pass.blocking == [])
check("ReviewVerdict notes preserved", verdict_pass.notes == ["minor nit on docstring"])

verdict_fail = ReviewVerdict(verdict=Verdict.FAIL, blocking=["missing test coverage"], notes=[])
check("ReviewVerdict FAIL verdict (typed Verdict enum)", verdict_fail.verdict is Verdict.FAIL)
check("ReviewVerdict blocking list", verdict_fail.blocking == ["missing test coverage"])


# ------------------------------------------------------------------ #
# PerTicketArtifactStore — initial state
# ------------------------------------------------------------------ #
store = PerTicketArtifactStore()
check("store.spec starts None", store.spec is None)
check("store.build starts None", store.build is None)
check("store.review starts None", store.review is None)


# ------------------------------------------------------------------ #
# PerTicketArtifactStore.put() — routing
# ------------------------------------------------------------------ #
store.put(spec)
check("put(SpecArtifact) -> store.spec", store.spec is spec)
check("put(SpecArtifact) leaves build None", store.build is None)
check("put(SpecArtifact) leaves review None", store.review is None)

store.put(build)
check("put(BuildArtifact) -> store.build", store.build is build)
check("put(BuildArtifact) leaves review None", store.review is None)

store.put(verdict_fail)
check("put(ReviewVerdict) -> store.review", store.review is verdict_fail)

# all slots now populated
check("all slots filled after three puts",
      store.spec is spec and store.build is build and store.review is verdict_fail)


# ------------------------------------------------------------------ #
# PerTicketArtifactStore.put() — overwrite: re-putting same slot type
# replaces the previous value (no guard; idempotent update is correct)
# ------------------------------------------------------------------ #
spec2 = SpecArtifact(acceptance=["v2 criterion"], scope="v2 scope", non_goals=[])
store.put(spec2)
check("put() overwrites existing spec slot", store.spec is spec2)
check("put() overwrite leaves build untouched", store.build is build)
check("put() overwrite leaves review untouched", store.review is verdict_fail)


# ------------------------------------------------------------------ #
# PerTicketArtifactStore.put() — unknown type raises TypeError
# ------------------------------------------------------------------ #
raised = False
try:
    store.put("not-an-artifact")  # type: ignore[arg-type]
except TypeError:
    raised = True
check("put() raises TypeError for unknown type", raised)


# ------------------------------------------------------------------ #
# Report
# ------------------------------------------------------------------ #
print("\n=========== EU-72 ARTIFACT CONTRACTS QA ===========")
for name, ok, detail in results:
    tag = "PASS" if ok else "FAIL"
    suffix = f"  ({detail})" if detail and not ok else ""
    print(f"  [{tag}] {name}{suffix}")

passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"  {passed}/{total} passed", "✅" if passed == total else "❌")
assert passed == total, f"{total - passed} test(s) failed"
