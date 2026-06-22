"""Autopilot retry-before-park QA (EU-5 / F6).

A transient ERROR must NOT immediately park a ticket: it's retried for a few cycles (with a short
backoff) and only parks after N consecutive ERRORs. The retry counter resets the moment the ticket
makes progress (a success). ESCALATED / PR_OPENED still park immediately.
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
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- constants / immediate-park set sanity ---
chk("ERRORED is NOT in the immediate-park set", Outcome.ERRORED not in autopilot._PARKED)
chk("ESCALATED parks immediately", Outcome.ESCALATED in autopilot._PARKED)
chk("PR_OPENED parks immediately", Outcome.PR_OPENED in autopilot._PARKED)
chk("retry threshold is a couple of passes", autopilot._MAX_TICKET_ERRORS >= 2)

# --- harness: one autopilot cycle with a scripted outcome for one ticket ---
tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[], audit_path=str(tmp / "audit.jsonl"))

_outcome = {"value": Outcome.ERRORED}    # what run_loop returns this cycle
TICKET = types.SimpleNamespace(id="EU-9")

def _run_cycle():
    """Drive a single autopilot cycle (once=True) with stubbed intake / loop / events."""
    autopilot.usage.budget_status = lambda c: {"over": False, "alert": False, "used": 0, "cap": 1, "pct": 0.0}
    autopilot.intake.from_drain = lambda c, app, n: [("app", TICKET)]
    async def _run_loop(c, worklist, audit):
        return [TicketReport(ticket_id=TICKET.id, outcome=_outcome["value"], iterations=1, cost_usd=0.0)]
    autopilot.run_loop = _run_loop
    async def _after_cycle(c, reports, audit, blocked):
        return None
    autopilot.events.after_cycle = _after_cycle
    autopilot.notify.configured = lambda: False
    autopilot.notify.send = lambda *a, **k: None
    asyncio.run(autopilot.autopilot(cfg, once=True))

# Cycle 1: first ERROR -> retried, NOT parked.
_run_cycle()
chk("after 1 ERROR: not parked", TICKET.id not in autopilot.load_blocked(cfg))
chk("after 1 ERROR: counter = 1", autopilot.load_error_counts(cfg).get(TICKET.id) == 1)

# Cycle 2: second consecutive ERROR -> still retried, NOT parked (threshold is 3).
_run_cycle()
chk("after 2 ERRORs: not parked", TICKET.id not in autopilot.load_blocked(cfg))
chk("after 2 ERRORs: counter = 2", autopilot.load_error_counts(cfg).get(TICKET.id) == 2)

# Cycle 3: third consecutive ERROR -> parks, counter cleared.
_run_cycle()
chk("after N ERRORs: parked", TICKET.id in autopilot.load_blocked(cfg))
chk("after park: counter reset", autopilot.load_error_counts(cfg).get(TICKET.id) is None)

# --- counter resets on success ---
autopilot.save_blocked(cfg, set())
autopilot.save_error_counts(cfg, {})
TICKET.id = "EU-10"
_outcome["value"] = Outcome.ERRORED
_run_cycle()
chk("fresh ERROR: counter = 1", autopilot.load_error_counts(cfg).get("EU-10") == 1)
_outcome["value"] = Outcome.MERGED
_run_cycle()
chk("success: counter reset to absent", autopilot.load_error_counts(cfg).get("EU-10") is None)
chk("success: not parked", "EU-10" not in autopilot.load_blocked(cfg))

# A second ERROR AFTER the reset starts a fresh count (not picking up where it left off).
_outcome["value"] = Outcome.ERRORED
_run_cycle()
chk("ERROR after a success: counter restarts at 1", autopilot.load_error_counts(cfg).get("EU-10") == 1)

# --- ESCALATED parks immediately (no retry budget spent) ---
autopilot.save_blocked(cfg, set())
autopilot.save_error_counts(cfg, {})
TICKET.id = "EU-11"
_outcome["value"] = Outcome.ESCALATED
_run_cycle()
chk("ESCALATED: parked on first pass", "EU-11" in autopilot.load_blocked(cfg))

print("\n============ AUTOPILOT RETRY-BEFORE-PARK QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
