"""Fail-first doctrine QA (2026-07-06) — the Elite Unit's OWN builds must produce tests with teeth.

The Builder writes tests today (Planner testable_ac -> Builder writes tests -> deterministic gate
runs them), but a test written AFTER the code can be green for the wrong reason (vacuous). Roman
adopted the fail-first / mutation-check discipline this session, so it is baked into the Builder's
doctrine (BUILDER_SYSTEM) and reinforced through the Planner's testable_ac contract. This harness
PINS that wording so a future edit can't silently strip the mandate — it asserts the doctrine says:
per acceptance criterion, write the test FIRST, watch it FAIL for the right reason, THEN implement;
and, for genuine test-after, MUTATION-CHECK (perturb the code and confirm the test catches it).
"""
import sys, types

REPO = "."
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, REPO)

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator import builder, planner

P = builder.BUILDER_SYSTEM
low = P.lower()

# ---- the fail-first mandate is present in the Builder doctrine ----
check("doctrine names the fail-first discipline", "fail-first" in low)
check("mandate: write the test FIRST", "write the test first" in low)
check("test is run against the UNCHANGED code first",
      "unchanged code" in low or "unchanged, failing" in low)
check("failure must be for the RIGHT reason (not an import error / typo)",
      "fails for the right reason" in low or "fail for the right reason" in low)
check("names the failure mode it prevents: a vacuous / wrong-reason green",
      "vacuous" in low)
check("a test that can't be made to fail has NO TEETH", "no teeth" in low)

# ---- the test-after escape hatch is the mutation-check ----
check("mutation-check is mandated for genuine test-after / touching an existing test",
      "mutation-check" in low or "mutation check" in low)
check("mutation-check says: perturb the code and confirm the test goes RED",
      "goes red" in low or "go red" in low)

# ---- the mandate lives on the ordered TEST step, not buried in prose ----
# (guard both substrings BEFORE .index() so a renamed/removed heading is a clean named FAIL,
#  never a ValueError that aborts the harness before the summary — the very teeth this file is about)
check("the fail-first mandate is attached to the TEST step",
      "fail-first" in low and "pre-submit gates" in low
      and low.index("fail-first") < low.index("pre-submit gates"))

# ---- the hard TESTS gate cross-references fail-first so the Reviewer half also enforces it ----
gate_region = low.split("tests + coverage", 1)
check("TESTS+COVERAGE gate references fail-first",
      len(gate_region) == 2 and "fail-first" in gate_region[1][:600])

# ---- Planner reinforcement: testable_ac ARE the fail-first contract ----
PS = planner.PLANNER_SYSTEM.lower()
check("Planner doctrine frames testable_ac as the fail-first contract", "fail-first" in PS)
check("Planner asks for AC phrased so a test can go RED when the behaviour is absent",
      "go red" in PS or "goes red" in PS)

# the brief the Builder actually reads must carry the fail-first instruction inline with the ACs
brief = planner.PlannerResult(
    verdict="BUILD", approach="do the thing",
    testable_ac=["given X, calling f() returns Y"],
    in_scope_files=["orchestrator/x.py"],
).as_builder_brief()
bl = brief.lower()
check("rendered Builder brief tells the Builder to write a FAILING test for each AC first",
      "failing test" in bl)
check("rendered brief still lists the testable AC", "given x" in bl)

# a non-BUILD verdict still renders no brief (unchanged contract)
empty = planner.PlannerResult(verdict="ANSWER", answer="already done").as_builder_brief()
check("non-BUILD verdict renders no brief (contract preserved)", empty == "")

print("\n============ FAIL-FIRST DOCTRINE QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
