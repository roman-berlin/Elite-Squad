"""Guard test for EU-637: live-run-log panel is terminal-style newest-first.

orchestrator/warroom.py's cockpit JS appends each SSE 'log' line to the END of
runlogBuffer with push() and scrolls to bottom — oldest-on-top, newest-at-bottom.
EU-637 flips this: prepend with unshift(), drop the tail (slice(0,1000)), and
stop force-scrolling (rendered lines now sit at the top without scrolling).

This is a static-assertion guard (no JS runtime available), mirroring the style
of tests/eu243_runlog_render_test.py: isolate function bodies from the source
and scan for required / forbidden patterns.
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


# ---- isolate the startRunlogStream function body ----
m_stream = re.search(
    r"function startRunlogStream\(\)\{.*?\n  \}\n",
    SRC,
    re.DOTALL,
)
chk("startRunlogStream function found in warroom.py", m_stream is not None)
stream_body = m_stream.group(0) if m_stream else ""

# ---- isolate the renderRunlogLines function body ----
m_render = re.search(
    r"function renderRunlogLines\(\)\{.*?\n  \}\n",
    SRC,
    re.DOTALL,
)
chk("renderRunlogLines function found in warroom.py", m_render is not None)
render_body = m_render.group(0) if m_render else ""


# ===== CHECK 1: prepend-newest (unshift, NOT push) =====
chk("runlogBuffer.unshift(e.data) present in startRunlogStream",
    "runlogBuffer.unshift(e.data)" in stream_body)
chk("old runlogBuffer.push(e.data) is REMOVED from startRunlogStream",
    "runlogBuffer.push(e.data)" not in stream_body)


# ===== CHECK 2: cap trim keeps NEWEST 1000 (slice(0,1000), NOT slice(-1000)) =====
chk("runlogBuffer=runlogBuffer.slice(0,1000) present in startRunlogStream",
    "runlogBuffer=runlogBuffer.slice(0,1000)" in stream_body)
chk("old slice(-1000) is REMOVED from warroom.py entirely",
    "slice(-1000)" not in SRC)


# ===== CHECK 3: no force-scroll in renderRunlogLines =====
chk("no runlogPanel.scrollTop in renderRunlogLines",
    "runlogPanel.scrollTop" not in render_body)


# ===== CHECK 4: innerHTML target still correct =====
chk("runlogPanel.innerHTML='<pre class=runlog>'... present",
    "'<pre class=runlog>'" in render_body and "+linesHtml" in render_body)


# ===== CHECK 5: empty-buffer early-return preserved (server placeholder intact) =====
chk("empty-buffer early-return still present (returns before touching innerHTML)",
    "if(runlogBuffer.length===0)" in render_body and "return;" in render_body)


# ===== CHECK 6: 'log' listener calls renderRunlogLines + 'done' closes EventSource =====
chk("'log' listener calls renderRunlogLines()",
    "renderRunlogLines();" in stream_body)
chk("'done' listener calls runlogEs.close()",
    "runlogEs.close()" in stream_body)

# ---- report ----
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
