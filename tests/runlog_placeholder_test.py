"""The live-log panel shows STATE, not silence, before output arrives (2026-07-22).

Commander, three separate times: "I don't see the logs" / "it's 4 min and still no logs".

Each time the panel was working. The run was simply silent: an officer makes ONE long model call
and prints nothing until it returns. EU-440 is the measured case — ticket_start 14:27:18, first
planner line 14:38:52: **11m30s of legitimate nothing**. A panel that renders a static
"Waiting for run output…" through that window is indistinguishable from a broken one, which is
exactly how it was read.

Everything needed to say something useful was already in the run state and being thrown away. The
placeholder now reports the stage, the elapsed time, and when the process last did anything — so
silence looks like thinking instead of like a bug.

Pins:
  1. an active run reports stage + elapsed + last-step, not a bare wait;
  2. it explains WHY there is no output (the model call prints only when it returns);
  3. an idle panel says so plainly rather than implying a pending run;
  4. missing/garbled state fields degrade gracefully — never a crash, never a raw traceback;
  5. the browser JS no longer overwrites the placeholder when its buffer is empty (that clobber is
     what discarded the information in the first place).
"""
import pathlib
import sys
import time
import types

_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda _n: _D
sys.modules["claude_agent_sdk"] = _sdk
_req = types.ModuleType("requests")
_req.Session = lambda *a, **k: None
_req.RequestException = Exception
sys.modules["requests"] = _req
sys.path.insert(0, ".")

from orchestrator import warroom

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

now = time.time()

# 1+2) an active, silent run
html = warroom._runlog_placeholder(
    {"phase": "Build", "run_started": now - 248, "last_activity": now - 23}, True)
chk("(1a) names the stage", "<b>Build</b>" in html, html[:120])
chk("(1b) reports elapsed time", "4m 08s elapsed" in html, html[:160])
chk("(1c) reports when the process last did anything", "last step 23s ago" in html, html[:160])
chk("(2) explains WHY there is no output yet",
    "prints nothing" in html and "until it returns" in html)
chk("(2b) it is NOT the old bare wait", "Waiting for run output" not in html)

# a long-silent run reports minutes, not a huge second count
html_long = warroom._runlog_placeholder(
    {"phase": "Build", "run_started": now - 700, "last_activity": now - 300}, True)
chk("(1d) a long gap reads in minutes", "last step 5m ago" in html_long, html_long[:160])

# 3) idle
idle = warroom._runlog_placeholder({}, False)
chk("(3) an idle panel says 'No active run', not a pending wait",
    "No active run" in idle and "elapsed" not in idle)

# 4) hostile / missing state must not crash
for bad in ({}, {"run_started": "not-a-number", "last_activity": None, "phase": None},
            {"phase": "Build", "run_started": None}, {"last_activity": object()}):
    try:
        out = warroom._runlog_placeholder(bad, True)
        ok = isinstance(out, str) and "Traceback" not in out and out.strip() != ""
    except Exception:  # noqa: BLE001
        ok = False
    chk(f"(4) degrades gracefully on {list(bad) or 'empty state'}", ok)

# 5) the JS must not blank it back out
src = pathlib.Path("orchestrator/warroom.py").read_text(encoding="utf-8")
chk("(5a) the panel renders the placeholder server-side",
    "_runlog_placeholder(state, active)" in src)
chk("(5b) the JS no longer overwrites it on an empty buffer",
    "runlogPanel.innerHTML='<div class=logempty>Waiting for run output…</div>'" not in src)

print("\n========== LIVE-LOG PLACEHOLDER ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
