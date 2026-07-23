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
  1. an active run reports stage + elapsed + last-step, not a bare wait — and the STAGE comes
     from the audit, because the run state has no phase field to ask (that was the first version's
     bug: it asked state, got None, and rendered a stage-less "3m 34s elapsed");
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


# A fixture audit whose newest phase-bearing event is a ticket_start. The run state carries NO phase
# field (cockpit_state's blank dict is active/last_activity/run_started/log_path and no more), so the
# label MUST come from the audit — the first version asked state for it, always got None, and rendered
# a stage-less "3m 34s elapsed". The Commander caught it: "if it's planning I'm supposed to see
# 'planning', no?"
import json as _json, tempfile as _tf
AUDIT = pathlib.Path(_tf.mkdtemp(prefix="ph-audit-")) / "audit.jsonl"
AUDIT.write_text("\n".join(_json.dumps(r) for r in [
    {"ts": "2026-07-22T23:03:24", "event": "ticket_start", "ticket_id": "EU-447"},
    {"ts": "2026-07-22T23:03:25", "event": "agent_call"},        # uninformative — must be skipped
]) + "\n", encoding="utf-8")

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

now = time.time()

# 1+2) an active, silent run
html = warroom._runlog_placeholder(
    {"run_started": now - 248, "last_activity": now - 23}, True, str(AUDIT))
chk("(1a) names the stage", "<b>Planning</b>" in html, html[:120])
chk("(1b) reports elapsed time", "4m 08s elapsed" in html, html[:160])
chk("(1c) reports when the process last did anything", "last step 23s ago" in html, html[:160])
chk("(2) explains WHY there is no output yet",
    "prints nothing" in html and "until it returns" in html)
chk("(2b) it is NOT the old bare wait", "Waiting for run output" not in html)

# a long-silent run reports minutes, not a huge second count
html_long = warroom._runlog_placeholder(
    {"run_started": now - 700, "last_activity": now - 300}, True, str(AUDIT))
chk("(1d) a long gap reads in minutes", "last step 5m ago" in html_long, html_long[:160])

# 3) idle
idle = warroom._runlog_placeholder({}, False, str(AUDIT))
chk("(3) an idle panel says 'No active run', not a pending wait",
    "No active run" in idle and "elapsed" not in idle)

# 4) hostile / missing state must not crash
for bad in ({}, {"run_started": "not-a-number", "last_activity": None, "phase": None},
            {"phase": "Build", "run_started": None}, {"last_activity": object()}):
    try:
        out = warroom._runlog_placeholder(bad, True, str(AUDIT))
        ok = isinstance(out, str) and "Traceback" not in out and out.strip() != ""
    except Exception:  # noqa: BLE001
        ok = False
    chk(f"(4) degrades gracefully on {list(bad) or 'empty state'}", ok)

# 5) the JS must not blank it back out
src = pathlib.Path("orchestrator/warroom.py").read_text(encoding="utf-8")
chk("(5a) the panel renders the placeholder server-side",
    "_runlog_placeholder(state, active" in src)
chk("(5b) the JS no longer overwrites it on an empty buffer",
    "runlogPanel.innerHTML='<div class=logempty>Waiting for run output…</div>'" not in src)

# ---- the phase label itself -------------------------------------------------
chk("(6a) a ticket_start with no planner yet reads as Planning",
    warroom._live_phase(str(AUDIT)) == "Planning", warroom._live_phase(str(AUDIT)))
chk("(6b) the rendered panel names it", "Planning" in html, html[:120])

_a2 = AUDIT.parent / "a2.jsonl"
_a2.write_text("\n".join(_json.dumps(r) for r in [
    {"ts": "1", "event": "ticket_start", "ticket_id": "X"},
    {"ts": "2", "event": "planner", "ticket_id": "X"},
    {"ts": "3", "event": "agent_call"},
]) + "\n", encoding="utf-8")
chk("(6c) after the planner returns it reads as Building",
    warroom._live_phase(str(_a2)) == "Building", warroom._live_phase(str(_a2)))

_a3 = AUDIT.parent / "a3.jsonl"
_a3.write_text(_json.dumps({"ts": "1", "event": "review", "ticket_id": "X"}) + "\n", encoding="utf-8")
chk("(6d) a review event reads as Review", warroom._live_phase(str(_a3)) == "Review")

chk("(6e) a missing audit yields no label, never a crash",
    warroom._live_phase(str(AUDIT.parent / "nope.jsonl")) == "")

# (6f) CONCURRENCY: two tickets emit phases into one audit (N=2 drain). The phase must be scoped to
# the panel's OWN ticket — the Commander saw "Gate" over EU-444 while EU-444 was still building and
# the OTHER ticket (EU-443) had reached its gate. Unscoped "newest phase anywhere" is the bug.
_a4 = AUDIT.parent / "a4.jsonl"
_a4.write_text("\n".join(_json.dumps(r) for r in [
    {"ts": "1", "event": "ticket_start", "ticket_id": "EU-444"},
    {"ts": "2", "event": "build", "ticket_id": "EU-444"},        # EU-444 is BUILDING
    {"ts": "3", "event": "gate", "ticket_id": "EU-443"},         # EU-443 reached its GATE, later
]) + "\n", encoding="utf-8")
chk("(6f) scoped to EU-444 → Building (not the other ticket's Gate)",
    warroom._live_phase(str(_a4), "EU-444") == "Building", warroom._live_phase(str(_a4), "EU-444"))
chk("(6g) scoped to EU-443 → Gate (its own phase)",
    warroom._live_phase(str(_a4), "EU-443") == "Gate", warroom._live_phase(str(_a4), "EU-443"))
chk("(6h) unscoped still returns the newest-anywhere (single-run cockpit unchanged)",
    warroom._live_phase(str(_a4)) == "Gate")

print("\n========== LIVE-LOG PLACEHOLDER ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
