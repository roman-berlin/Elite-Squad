"""Guard test for EU-549: make the 2s board refresh safe.

Checks that orchestrator/warroom.py's inline warroom JS contains all six
fixes described in the design brief:
  1. Focus guard before innerHTML swap
  2. Live runlog rebind (re-query on every call)
  3. Security KPI persistence (secpanel)
  4. Runlog stream reconnect on error
  5. No nested scrollbox
  6. Live header clock via SSE listener
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


def _extract_js_func(name):
    """Extract a JS function body by name using brace-counting."""
    m = re.search(r"function\s+" + re.escape(name) + r"\([^)]*\)\s*\{", SRC)
    if not m:
        return ""
    start = m.start()
    depth = 0
    i = start
    while i < len(SRC):
        if SRC[i] == "{":
            depth += 1
        elif SRC[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return SRC[start : i + 1]


# Isolated sections
ABODY = _extract_js_func("applyBoard")
RLBODY = _extract_js_func("renderRunlogLines")

# SaveUi / ApplyUi id arrays (simple substring search)
save_idx = SRC.find("function saveUi()")
if save_idx >= 0:
    save_end = SRC.find("\n}", save_idx) + 2
    SAVE_BODY = SRC[save_idx:save_end]
else:
    SAVE_BODY = ""

apply_idx = SRC.find("function applyUi()")
if apply_idx >= 0:
    apply_end = SRC.find("\n}", apply_idx) + 2
    APPLY_BODY = SRC[apply_idx:apply_end]
else:
    APPLY_BODY = ""

# SSE board listener line (single line)
BOARD_LISTENER = ""
for ln in SRC.split("\n"):
    if 'addEventListener("board"' in ln:
        BOARD_LISTENER = ln
        break

# Runlog onerror handler snippet
onerr_pos = SRC.find("runlogEs.onerror")
snippet = ""
if onerr_pos >= 0:
    timeout_pos = SRC.find('setTimeout(startRunlogStream,4000);', onerr_pos)
    if timeout_pos >= 0:
        snippet = SRC[onerr_pos : timeout_pos + len("setTimeout(startRunlogStream,4000);")]

# ---------------------------------------------------------------------------
# T1: Focus guard exists between 'function applyBoard' and 'b.innerHTML'
# ---------------------------------------------------------------------------
guard_ok = ABODY
t1_pass = bool(
    "activeElement" in guard_ok
    and "TEXTAREA" in guard_ok
    and "return" in guard_ok
    and guard_ok.index("activeElement") < guard_ok.rindex("b.innerHTML")
)
chk(
    "T1 - focus guard exists between function applyBoard and b.innerHTML",
    t1_pass,
    "missing activeElement check or return before b.innerHTML" if not t1_pass else "",
)

# ---------------------------------------------------------------------------
# T2: Live runlog rebind
# ---------------------------------------------------------------------------
t2a = "getElementById(\"runlog\")" in RLBODY
chk("T2a - renderRunlogLines re-queries getElementById('runlog') on every call", t2a)

# Find the wrapper region and check ordering within it
wrapper_start = SRC.find("// Update on board refresh (log path might change)")
if wrapper_start >= 0:
    wrapper_body = SRC[wrapper_start : wrapper_start + 600]
    t2b = "originalApplyBoard(html)" in wrapper_body and wrapper_body.index("originalApplyBoard(html)") < wrapper_body.index("renderRunlogLines()")
else:
    t2b = False
chk("T2b - wrapper calls renderRunlogLines() after delegating to originalApplyBoard", t2b)

# ---------------------------------------------------------------------------
# T3: Security KPI persistence
# ---------------------------------------------------------------------------
t3a = 'id="secpanel"' in SRC
chk("T3a - security card rendered as <details ... id=\"secpanel\" ...>", t3a)

has_sec_save = '"secpanel"' in SAVE_BODY or "'secpanel'" in SAVE_BODY
chk("T3b - saveUi includes \"secpanel\"", has_sec_save)

has_sec_apply = '"secpanel"' in APPLY_BODY or "'secpanel'" in APPLY_BODY
chk("T3c - applyUi includes \"secpanel\"", has_sec_apply)

# ---------------------------------------------------------------------------
# T4: Runlog reconnect
# ---------------------------------------------------------------------------
t4a = "setTimeout(startRunlogStream,4000)" in snippet
chk("T4a - onerror schedules setTimeout(startRunlogStream, 4000)", t4a)

t4b = "RECONNECT_NOTE" in snippet
chk("T4b - onerror inserts the RECONNECT_NOTE status line", t4b)

t4d = 'RECONNECT_NOTE="· stream lost — reconnecting…"' in SRC
chk(
    "T4d - RECONNECT_NOTE is the dim 'stream lost — reconnecting…' line (leading · → lg-dim class)",
    t4d,
)

t4c = "_runlogDone" in snippet
chk("T4c - done flag (_runlogDone) checked in onerror before reconnect", t4c)

# ---------------------------------------------------------------------------
# T5: No nested scrollbox
# ---------------------------------------------------------------------------
# The inner <pre class=runlog> MUST stay — EU-637's merged guard
# (tests/eu637_runlog_newest_first_test.py) requires renderRunlogLines to render into
# '<pre class=runlog>'. The nested scrollbox is fixed in CSS instead: sizing/scroll live on
# div.runlog alone, and the inner pre is height:auto / overflow:visible.
css_idx = SRC.find("/* EU-200: live run log panel")
css_region = SRC[css_idx:css_idx + 1000] if css_idx >= 0 else ""
t5a = (
    "div.runlog{" in css_region
    and "div.runlog pre.runlog{" in css_region
    and "height:auto" in css_region
    and "overflow:visible" in css_region
)
chk(
    "T5a - CSS scopes height/overflow to div.runlog; inner pre.runlog is height:auto/overflow:visible",
    t5a,
    "div.runlog sizing rule or inner-pre override missing from .runlog CSS block" if not t5a else "",
)

t5b = '"runlog"' in ABODY or "'runlog'" in ABODY
chk("T5b - 'runlog' present in applyBoard keep list [scroll arrays]", t5b)

# ---------------------------------------------------------------------------
# T6: Live header clock
# ---------------------------------------------------------------------------
t6a = "id=gentime" in SRC
chk("T6a - {{GEN}} timestamp wrapped in span with id=gentime", t6a)

t6b = "gentime" in BOARD_LISTENER and "textContent" in BOARD_LISTENER
chk("T6b - SSE board listener updates gentime textContent", t6b)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
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
