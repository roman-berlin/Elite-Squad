"""Idle-log QA: when the queue is clear, the autopilot announces 'queue clear' ONCE per idle stretch —
not the identical line every interval (the dozen-repeat spam Roman saw). It re-announces only after the
queue has had work again."""
import sys, types, tempfile, asyncio, io, contextlib, threading
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import autopilot
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[], audit_path=str(tmp / "audit.jsonl"))
# NEVER touch the machine-global /tmp/general-autopilot.pid — this harness runs the REAL autopilot(),
# which would overwrite (then delete) a live daemon's PID file and flip its cockpit badge OFF mid-run.
autopilot._PID_FILE = tmp / "general-autopilot.pid"

autopilot.usage.budget_status = lambda c: {"over": False, "alert": False, "used": 0, "cap": 1, "pct": 0.0}
autopilot.intake.from_drain = lambda c, app, n: []          # always empty → permanently idle
async def _ac(c, reports, audit, blocked): return None
autopilot.notify.configured = lambda: False
autopilot.notify.send = lambda *a, **k: None

ev = threading.Event()
sleeps = {"n": 0}
def _sleep_stub(seconds, stop_event=None):
    sleeps["n"] += 1
    if sleeps["n"] >= 3:        # let it idle several cycles, then stop
        ev.set()
autopilot._sleep = _sleep_stub

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    asyncio.run(autopilot.autopilot(cfg, once=False, interval=1, stop_event=ev))
out = buf.getvalue()

n_clear = out.count("queue clear")
chk("idled several cycles (≥3 sleeps)", sleeps["n"] >= 3, f"sleeps={sleeps['n']}")
chk("'queue clear' printed exactly ONCE across the idle stretch", n_clear == 1, f"count={n_clear}")
chk("the idle line says it's idling + will auto-pick-up", "idling" in out and "automatically" in out)

print("\n============ IDLE-LOG QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
