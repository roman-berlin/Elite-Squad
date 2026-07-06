"""EU-61 Part A — autopilot integration: a Blocked-with-answer ticket is pulled into the develop queue.

`autopilot._resumable_answered` (the detection half) is unit-pinned in eu61_decision_roundtrip_test.py.
THIS harness pins the loop WIRING that consumes it — the part that turns a detected resume into actual
work, which the unit test does not exercise:

  * the resumed ticket is LIFTED OUT of the on-disk blocked set (so /unblock isn't needed), and
  * it is PREPENDED to the worklist handed to run_loop (resume before taking new work), de-duped
    against whatever the drain already returned.

Drives one real autopilot cycle (once=True) with stubbed intake/run_loop/events, mirroring
autopilot_unblock_reread_test.py. No network, no models.
"""
import sys, types, tempfile, asyncio
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
from orchestrator.contracts import Outcome, TicketReport
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace
tmp = Path(tempfile.mkdtemp())
# The real autopilot() run below writes its PID file. Keep it off the machine-global
# /tmp/general-autopilot.pid, which is shared with a live daemon and every other checkout's suite.
autopilot._PID_FILE = tmp / "general-autopilot.pid"
APP = AppConfig(name="eu", repo_path=".", base_branch="dev", protected_branch="main", backlog_backend="none")
cfg = Config(apps=[APP], audit_path=str(tmp / "audit.jsonl"))

# AUTO-1 is parked from an earlier cycle; the Commander answered it on Jira, so it should auto-resume.
# AUTO-2 is a normal new ticket the drain offers this cycle.
autopilot.save_blocked(cfg, {"AUTO-1"})
RESUMED = ns(id="AUTO-1")
NEW = ns(id="AUTO-2")

seen = {}

def _run_cycle():
    autopilot.usage.budget_status = lambda c: {"over": False, "alert": False, "used": 0, "cap": 1, "pct": 0.0}
    # Detection is unit-tested elsewhere — stub it so this harness isolates the consume/prepend wiring.
    autopilot._resumable_answered = lambda c, app, blocked: (
        {"AUTO-1": (APP, RESUMED)} if "AUTO-1" in blocked else {})
    autopilot.intake.from_drain = lambda c, app, n: [(APP, NEW)]
    autopilot.intake.LAST_DRAIN_ERRORS = {}

    async def _run_loop(c, worklist, audit):
        seen["worklist_ids"] = [t.id for _, t in worklist]
        return [TicketReport(ticket_id=t.id, outcome=Outcome.MERGED, iterations=1, cost_usd=0.0)
                for _, t in worklist]
    autopilot.run_loop = _run_loop

    async def _after_cycle(c, reports, audit, blocked):
        return None
    autopilot.notify.configured = lambda: False
    autopilot.notify.send = lambda *a, **k: None
    asyncio.run(autopilot.autopilot(cfg, once=True))

_run_cycle()

wl = seen.get("worklist_ids", [])
chk("the Jira-answered parked ticket reaches the develop queue", "AUTO-1" in wl, str(wl))
chk("it is PREPENDED — resumed before new work", wl[:1] == ["AUTO-1"], str(wl))
chk("the new drained ticket still follows it", "AUTO-2" in wl, str(wl))
chk("the resumed ticket appears exactly once (de-duped)", wl.count("AUTO-1") == 1, str(wl))

final = autopilot.load_blocked(cfg)
chk("the resumed ticket is lifted out of the blocked set", "AUTO-1" not in final, str(sorted(final)))

print("\n========= EU-61 AUTOPILOT RESUME-QUEUE QA =========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
