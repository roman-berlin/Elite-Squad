"""Autopilot idle-transition coverage (EU-50, companion to autopilot_idle_then_dark_test.py).

EU-50's fix keys the idle announce on the REASON — (bool(unreachable), frozenset(unreachable boards)) —
and re-announces on ANY transition, not just the first time the queue empties. The sibling test pins the
headline clear→dark case. This one pins the two OTHER transitions the fix promises but that test leaves
unproven, so a future regression to a bare boolean (or to keying on only `bool(unreachable)`, which would
miss a changed set) is caught:

  1. CHANGED dark set: board A dark → boards A+B dark. With a plain `bool(unreachable)` flag both cycles
     read True and the worsening outage stays silent. Keying on the frozenset re-announces.
  2. RECOVERY: dark → reachable again (token rotated). The board going back to a genuinely clear queue
     must push the honest 'queue clear' once more, not stay muted because it already announced 'dark'.

Driven through one real autopilot() run; the _sleep stub doubles as a clock that mutates
LAST_DRAIN_ERRORS between cycles: {A} → {A,B} → {} (recovered)."""
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
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import autopilot
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
# The real autopilot() run below writes its PID file. Keep it off the machine-global
# /tmp/general-autopilot.pid, which is shared with a live daemon and every other checkout's suite.
autopilot._PID_FILE = tmp / "general-autopilot.pid"
cfg = Config(apps=[], audit_path=str(tmp / "audit.jsonl"))

MSG_A = "Jira auth failed for app 'Elite-Unit' - Rotate JIRA_API_TOKEN"
MSG_B = "Jira auth failed for app 'Automatixy' - Rotate JIRA_API_TOKEN"

autopilot.usage.budget_status = lambda c: {"over": False, "alert": False, "used": 0, "cap": 1, "pct": 0.0}
autopilot.intake.from_drain = lambda c, app, n: []          # worklist empty the whole run
autopilot.intake.LAST_DRAIN_ERRORS.clear()
autopilot.intake.LAST_DRAIN_ERRORS["Elite-Unit"] = MSG_A    # cycle 1 starts already dark on board A
async def _ac(c, reports, audit, blocked): return None
autopilot.notify.configured = lambda: False
sent = []
autopilot.notify.send = lambda *a, **k: sent.append(a[0] if a else "")

ev = threading.Event()
sleeps = {"n": 0}
def _sleep_stub(seconds, stop_event=None):
    sleeps["n"] += 1
    if sleeps["n"] == 1:                                     # after cycle 1 (A dark) → outage WORSENS to A+B
        autopilot.intake.LAST_DRAIN_ERRORS["Automatixy"] = MSG_B
    elif sleeps["n"] == 2:                                   # after cycle 2 (A+B dark) → token rotated, RECOVERED
        autopilot.intake.LAST_DRAIN_ERRORS.clear()
    elif sleeps["n"] >= 3:                                   # cycle 3 announced 'queue clear' → stop
        ev.set()
autopilot._sleep = _sleep_stub

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    asyncio.run(autopilot.autopilot(cfg, once=False, interval=1, stop_event=ev))
out = buf.getvalue()

n_unreach = out.count("UNREACHABLE")
n_clear = out.count("queue clear")

chk("a CHANGED dark set re-announces (A → A+B = 2 UNREACHABLE pushes, not 1)", n_unreach == 2,
    f"UNREACHABLE count={n_unreach}")
chk("the worsened outage names the newly-dark board (Automatixy)", "Automatixy" in out, out[:400])
chk("recovery (dark → reachable) re-announces an honest 'queue clear' exactly once", n_clear == 1,
    f"queue clear count={n_clear}")
chk("recovery printed AFTER the outage (clear follows the last UNREACHABLE, not before)",
    out.rfind("UNREACHABLE") < out.find("queue clear"),
    f"last_unreach@{out.rfind('UNREACHABLE')} clear@{out.find('queue clear')}")
chk("each distinct dark set pinged Telegram (2 unreachable alerts)",
    sum(1 for s in sent if "unreachable" in s.lower()) == 2, str(sent)[:300])

print("\n===== AUTOPILOT IDLE-TRANSITION COVERAGE QA (EU-50) =====")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
