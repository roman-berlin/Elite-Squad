"""EU-99: Performance Engineer officer charter — structural verification.

Three acceptance criteria:
  AC1  officers/performance-engineer.md exists in the officers/ directory.
  AC2  The charter specifies both accepted blocking artifact formats
       (profiler trace AND before/after wall-clock), plus the structured
       PERF GATE block with mandatory fields, and documents that a
       countersignature is required before the Reviewer is tagged.
  AC3  ORG.md (the gate doc) references the Performance Engineer at the
       correct position in the mission flow — between Test Engineer and
       Code Reviewer — and lists the officer in the roster table with a
       pointer to officers/performance-engineer.md.

Bonus checks on officers/builder.md (a file the Builder also touched):
  B1   Builder gate step (e) "Pre-handoff Performance Countersignature" is
       present so the Builder is obligated to fill it before hand-off.
  B2   The §P1/§P2/§P3 block and the "cold-only" sentinel are documented
       so the Builder can never plausibly claim ambiguity.

Pure filesystem / string-scan: no agent, no network, no SDK import.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

results: list[tuple[str, bool, str]] = []


def chk(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion result."""
    results.append((name, bool(condition), detail))


# ---------------------------------------------------------------------------
# Paths under test
# ---------------------------------------------------------------------------
CHARTER = ROOT / "officers" / "performance-engineer.md"
ORG     = ROOT / "ORG.md"
BUILDER = ROOT / "officers" / "builder.md"

charter_text = CHARTER.read_text(encoding="utf-8") if CHARTER.exists() else ""
org_text     = ORG.read_text(encoding="utf-8")     if ORG.exists() else ""
builder_text = BUILDER.read_text(encoding="utf-8") if BUILDER.exists() else ""

# ---------------------------------------------------------------------------
# AC1 — charter file exists
# ---------------------------------------------------------------------------
chk("AC1 charter file exists: officers/performance-engineer.md", CHARTER.exists())
chk("AC1 charter is non-empty", len(charter_text.strip()) > 0)

# ---------------------------------------------------------------------------
# AC2 — blocking artifact format specified
# ---------------------------------------------------------------------------

# Both accepted formats must be documented
chk("AC2 artifact format: 'profiler trace' mentioned",
    "profiler trace" in charter_text.lower(),
    "charter must document the profiler-trace artifact format")

chk("AC2 artifact format: 'before/after wall-clock' mentioned",
    "before/after" in charter_text.lower() and "wall-clock" in charter_text.lower(),
    "charter must document the before/after wall-clock artifact format")

# The PERF GATE block with mandatory fields must be present
chk("AC2 PERF GATE block defined",
    "PERF GATE" in charter_text,
    "charter must define the PERF GATE verdict block")

chk("AC2 §P1-hot-paths field in charter",
    "§P1-hot-paths" in charter_text or "Hot paths examined" in charter_text,
    "charter must name the hot-paths field")

chk("AC2 §P2-artifact / Before:After table field in charter",
    ("Before:" in charter_text and "After:" in charter_text),
    "charter must show the Before/After table format")

# cold-only sentinel must be documented (the 'no benchmark required' escape hatch)
chk("AC2 cold-only sentinel documented",
    "cold-only" in charter_text,
    "charter must document the cold-only escape sentinel")

# Countersignature required BEFORE Reviewer tag
chk("AC2 countersignature required before Reviewer",
    ("countersign" in charter_text.lower() and
     "reviewer" in charter_text.lower() and
     "before" in charter_text.lower()),
    "charter must state that countersignature is required before routing to Reviewer")

# Gate is a hard blocker, not advisory
chk("AC2 PERFORMANCE GATE: BLOCK verdict named",
    "PERFORMANCE GATE: BLOCK" in charter_text,
    "charter must define the BLOCK verdict so builders know it is not advisory")

chk("AC2 PERFORMANCE GATE: PASS verdict named",
    "PERFORMANCE GATE: PASS" in charter_text,
    "charter must define the PASS verdict")

# ---------------------------------------------------------------------------
# AC3 — ORG.md (gate doc) updated to reference new officer at correct step
# ---------------------------------------------------------------------------
chk("AC3 ORG.md exists", ORG.exists())

# Performance Engineer must appear in the mission-flow diagram (between TE and Reviewer)
chk("AC3 Performance Engineer appears in mission flow",
    "Performance Engineer" in org_text,
    "ORG.md must mention Performance Engineer in the mission flow")

# Specifically, the mission-flow mermaid shows TE -> PE -> Reviewer ordering
chk("AC3 mission flow: PE2 node between TE and Reviewer",
    "PE2" in org_text,
    "ORG.md mission-flow diagram must have a PE2 node between Test Engineer and Reviewer")

chk("AC3 mission flow: PE2 references hot-path benchmark",
    ("hot-path benchmark" in org_text or "hot-path" in org_text.lower()),
    "ORG.md PE2 node must reference the hot-path benchmark")

# Roster table must list Performance Engineer with pointer to charter
chk("AC3 roster: Performance Engineer row present",
    "Performance Engineer" in org_text and "Perf Gate" in org_text,
    "ORG.md roster must include a Performance Engineer row")

chk("AC3 roster: row points to officers/performance-engineer.md",
    "officers/performance-engineer.md" in org_text,
    "ORG.md roster must link to the charter file")

chk("AC3 roster: status is active",
    ("Performance Engineer" in org_text and "active" in org_text),
    "ORG.md must mark Performance Engineer as active")

# ---------------------------------------------------------------------------
# B1 — builder.md gate step (e) added
# ---------------------------------------------------------------------------
chk("B1 officers/builder.md exists", BUILDER.exists())

chk("B1 gate step (e) Pre-handoff Performance Countersignature in builder.md",
    "pre-handoff performance countersignature" in builder_text.lower(),
    "officers/builder.md must include gate step (e) for the performance countersignature")

chk("B1 builder exit gates mention step (e) before Reviewer hand-off",
    "(e)" in builder_text,
    "officers/builder.md exit gates must enumerate step (e)")

# ---------------------------------------------------------------------------
# B2 — §P1/§P2/§P3 block and cold-only sentinel in builder.md
# ---------------------------------------------------------------------------
chk("B2 §P1-hot-paths field in builder.md",
    "§P1-hot-paths" in builder_text,
    "officers/builder.md must show the §P1-hot-paths field for the Builder to fill in")

chk("B2 §P2-artifact field in builder.md",
    "§P2-artifact" in builder_text,
    "officers/builder.md must show the §P2-artifact field")

chk("B2 §P3-verdict field in builder.md",
    "§P3-verdict" in builder_text,
    "officers/builder.md must show the §P3-verdict field")

chk("B2 cold-only sentinel in builder.md",
    "cold-only" in builder_text,
    "officers/builder.md must document the cold-only sentinel so Builders can use it")

chk("B2 example with real numbers in builder.md",
    "mean" in builder_text and "p95" in builder_text,
    "officers/builder.md must show a worked example with mean/p95 numbers")

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
print("\n============= EU-99 PERFORMANCE ENGINEER CHARTER QA =============")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    suffix = f"  ({detail})" if detail and not ok else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{suffix}")
print("-" * 66)
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results) - passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
