"""Autopilot idle→dark regression (EU-50): a Jira token that dies AFTER the queue has already idled
clear must still fire the UNREACHABLE alert. The old code gated both the 'queue clear' line and the
'boards UNREACHABLE' alert on one boolean (idle_announced), set True by whichever branch fired and
reset only when work appeared. Sequence that regressed: queue empties → 'queue clear' prints & sets
the flag → later the token expires (empty worklist again, but now LAST_DRAIN_ERRORS is populated) →
flag already True → the whole block is skipped → the 'can't read your backlog' Telegram never fires.
A hung backlog became indistinguishable from an empty one. The fix keys the announce on the REASON
(bool(unreachable), frozenset(unreachable)) and re-announces on any transition, so going dark pushes
once. This harness drives exactly that clear→dark transition in a single run."""
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

AUTH_MSG = ("Jira auth failed for app 'Elite-Unit' (X-Seraph-LoginReason=AUTHENTICATED_FAILED) - the "
            "API token is invalid/expired or its account can't access https://toibis.atlassian.net. "
            "Rotate JIRA_API_TOKEN")

autopilot.usage.budget_status = lambda c: {"over": False, "alert": False, "used": 0, "cap": 1, "pct": 0.0}
autopilot.intake.from_drain = lambda c, app, n: []          # worklist stays empty the whole run...
autopilot.intake.LAST_DRAIN_ERRORS.clear()                  # ...and the board starts REACHABLE (queue truly clear)
async def _ac(c, reports, audit, blocked): return None
autopilot.events.after_cycle = _ac
autopilot.notify.configured = lambda: False
sent = []
autopilot.notify.send = lambda *a, **k: sent.append(a[0] if a else "")

# The sleep stub doubles as a clock: after the FIRST idle cycle (queue announced clear) the Jira token
# dies, so the very next cycle sees an empty worklist again — but now with a drain error recorded.
ev = threading.Event()
sleeps = {"n": 0}
def _sleep_stub(seconds, stop_event=None):
    sleeps["n"] += 1
    if sleeps["n"] == 1:                                     # queue idled clear → NOW the token expires
        autopilot.intake.LAST_DRAIN_ERRORS["Elite-Unit"] = AUTH_MSG
    if sleeps["n"] >= 2:                                     # gave the dark cycle a chance to announce
        ev.set()
autopilot._sleep = _sleep_stub

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    asyncio.run(autopilot.autopilot(cfg, once=False, interval=1, stop_event=ev))
out = buf.getvalue()

clear_i = out.find("queue clear")
unreach_i = out.find("UNREACHABLE")
chk("phase 1: honest 'queue clear' while the board is reachable", clear_i != -1, out[:200])
chk("phase 2: a token death AFTER an idle queue STILL fires UNREACHABLE (the EU-50 regression)",
    unreach_i != -1, out[:300])
chk("the now-dark board is named with its actionable fix",
    "Elite-Unit" in out and "Rotate JIRA_API_TOKEN" in out)
chk("the clear→dark transition pinged Telegram", any("unreachable" in s.lower() for s in sent), str(sent)[:200])
chk("each state announced exactly once (clear x1, unreachable x1 — transition, not spam)",
    out.count("queue clear") == 1 and out.count("UNREACHABLE") == 1,
    f"clear={out.count('queue clear')} unreachable={out.count('UNREACHABLE')}")
chk("the sequence really was clear THEN dark", clear_i != -1 and unreach_i != -1 and clear_i < unreach_i,
    f"clear@{clear_i} unreachable@{unreach_i}")

print("\n========= AUTOPILOT IDLE→DARK REGRESSION QA (EU-50) =========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
