"""Autopilot mid-cycle /unblock re-park QA (EU-48 / F6 generalised lock).

The park write-back must re-read `blocked` straight from disk instead of trusting the snapshot taken at
the top of the loop. The race: a ticket ESCALATES this cycle (so it's about to be parked), and while the
cycle runs the Telegram poller handles `/unblock OTHER` for a *previously* parked ticket — mutating the
on-disk blocked set. With the stale snapshot, the write-back unions the new park into the old set and
re-writes it, silently RESURRECTING the ticket the Commander just unblocked. Re-reading first fixes it.

This harness drives one autopilot cycle (once=True) with stubbed intake/loop/events and simulates the
poller by clearing a parked ticket from inside the stubbed run_loop (i.e. mid-cycle).
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

tmp = Path(tempfile.mkdtemp())
APP = AppConfig(name="eu", repo_path=".", base_branch="dev", protected_branch="main", backlog_backend="none")
cfg = Config(apps=[APP], audit_path=str(tmp / "audit.jsonl"))

# A ticket already parked from an earlier cycle. The Commander will /unblock it WHILE this cycle runs.
autopilot.save_blocked(cfg, {"EU-OLD"})

NEW = types.SimpleNamespace(id="EU-NEW")   # escalates this cycle -> about to be parked

def _run_cycle():
    autopilot.usage.budget_status = lambda c: {"over": False, "alert": False, "used": 0, "cap": 1, "pct": 0.0}
    # worklist excludes already-blocked tickets, so only EU-NEW is offered.
    autopilot.intake.from_drain = lambda c, app, n: [(APP, NEW)]

    async def _run_loop(c, worklist, audit):
        # Simulate the Telegram poller clearing EU-OLD mid-cycle (a real /unblock writes the file now).
        autopilot.unblock(cfg, "EU-OLD")
        return [TicketReport(ticket_id=NEW.id, outcome=Outcome.ESCALATED, iterations=1, cost_usd=0.0)]
    autopilot.run_loop = _run_loop

    async def _after_cycle(c, reports, audit, blocked):
        return None
    autopilot.events.after_cycle = _after_cycle
    autopilot.notify.configured = lambda: False
    autopilot.notify.send = lambda *a, **k: None
    asyncio.run(autopilot.autopilot(cfg, once=True))

_run_cycle()
final = autopilot.load_blocked(cfg)

chk("the newly-escalated ticket IS parked", "EU-NEW" in final, str(sorted(final)))
chk("the mid-cycle /unblock is NOT resurrected by the write-back", "EU-OLD" not in final, str(sorted(final)))

print("\n========= AUTOPILOT MID-CYCLE /UNBLOCK QA =========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
