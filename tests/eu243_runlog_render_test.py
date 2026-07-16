"""Guard test for EU-243: renderRunlogLines must not use JS `in` on string primitives.

orchestrator/warroom.py's cockpit JS function renderRunlogLines colors each streamed
log line. It used to test `"merged" in low || "✓" in line || " pass" in low || "ready"
in low` — the JS `in` operator requires an object RHS; against a string primitive it
throws a TypeError inside the `.map()`, before `runlogPanel.innerHTML` is ever
assigned. That leaves the run-log panel stuck on "Waiting for run output…" and no
streamed SSE 'log' line ever renders (defeats EU-157's live-terminal acceptance).

This is a static-assertion guard (no JS runtime available), mirroring the style of
tests/warroom_triage_html_test.py: it isolates the renderRunlogLines function body
from the source and scans it for the buggy `"<literal>" in low` / `"<literal>" in
line` pattern, and asserts the corrected `.includes(` substring checks are present.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "orchestrator" / "warroom.py").read_text()

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ---- isolate the renderRunlogLines function body ----
m = re.search(
    r"function renderRunlogLines\(\)\{.*?\n  \}\n",
    SRC,
    re.DOTALL,
)
chk("renderRunlogLines function found in warroom.py", m is not None)
body = m.group(0) if m else ""

# ---- (1) no JS-`in`-on-string pattern ----
bad_pattern = re.compile(r'"\s*in\s+(low|line)\b')
bad_matches = bad_pattern.findall(body)
chk(
    "no `\"...\" in low` / `\"...\" in line` (JS-in-on-string) pattern in renderRunlogLines",
    len(bad_matches) == 0,
    f"found matches: {bad_matches}",
)

# ---- (2) corrected substring checks are present ----
chk("low.includes('merged') present", "low.includes('merged')" in body)
chk("line.includes('✓') present", "line.includes('✓')" in body)
chk("low.includes(' pass') present", "low.includes(' pass')" in body)
chk("low.includes('ready') present", "low.includes('ready')" in body)

# ---- (3) the already-correct regex/dim branches are untouched ----
chk("error/fail regex branch untouched", "/error|fail|park|block|✗|reject/.test(low)" in body)
chk("dim branch untouched", "/^·/.test(line) || /builder:|reviewer:/.test(low)" in body)

passed = sum(1 for _, c, _ in results if c)
total = len(results)
for n, c, d in results:
    status = "PASS" if c else "FAIL"
    print(f"[{status}] {n}" + (f" -- {d}" if d and not c else ""))

print(f"\n{passed}/{total} passed")
if passed != total:
    print("RESULT: FAIL")
    sys.exit(1)
print("RESULT: PASS")
