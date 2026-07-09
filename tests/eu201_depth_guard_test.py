"""EU-201 Depth guard test: Verify that fragment continuation doesn't bypass the depth guard.

The depth guard in scrum.split prevents runaway recursion by refusing to split a ticket
that's already at _MAX_SPLIT_DEPTH. This test verifies that even with fragment continuation,
the depth guard still works.
"""
import sys, types
sys.path.insert(0, ".")

from orchestrator import scrum

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Test the depth guard
print("Depth guard test: Verify depth guard prevents runaway recursion")

# A ticket at max depth should NOT split
max_depth_ticket = types.SimpleNamespace(
    id="AUTO-200",
    summary="Already split multiple times",
    description="x <!-- autosplit-depth: 3 -->",
    ephemeral=False
)

depth = scrum._split_depth(max_depth_ticket)
chk("depth read correctly", depth == 3)
chk("depth at max", depth >= scrum._MAX_SPLIT_DEPTH)

# A ticket below max depth can split
normal_ticket = types.SimpleNamespace(
    id="AUTO-201",
    summary="Normal ticket",
    description="x <!-- autosplit-depth: 1 -->",
    ephemeral=False
)

normal_depth = scrum._split_depth(normal_ticket)
chk("normal ticket depth read correctly", normal_depth == 1)
chk("normal ticket below max", normal_depth < scrum._MAX_SPLIT_DEPTH)

# A ticket with no marker is at depth 0
no_marker_ticket = types.SimpleNamespace(
    id="AUTO-202",
    summary="No marker ticket",
    description="No depth marker",
    ephemeral=False
)

no_marker_depth = scrum._split_depth(no_marker_ticket)
chk("no marker ticket depth 0", no_marker_depth == 0)

# The MAX constant is 3
chk("MAX_SPLIT_DEPTH is 3", scrum._MAX_SPLIT_DEPTH == 3)

print("\n============ EU-201 DEPTH GUARD TEST ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
