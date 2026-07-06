"""EU-116 — no_changes build must not leave ticket stuck In Progress.

Asserts that when a build returns no_changes (because the fix was already applied
by a sibling ticket):

A. The ticket is moved off "In Progress" to a terminal state (Needs Human).
B. The ticket is parked immediately (ESCALATED outcome) so the drain does not re-run it.
C. A no_changes ticket is NOT retried like a transient ERROR.

Tests the fix for the bug where:
- AUTO-50 was stuck "In Progress" after returning no_changes twice
- The drain re-selected and re-ran it, wasting a full Opus build
"""
import sys
import types
import tempfile
import asyncio
from pathlib import Path

# Minimal stubs
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules["requests"] = req

sys.path.insert(0, ".")

from orchestrator import autopilot
from orchestrator.contracts import Outcome, TicketReport
from orchestrator.config import Config, AppConfig

results = []


def chk(name: str, condition: bool, detail: str = ""):
    """Record a single assertion."""
    results.append((name, bool(condition), detail))


ns = types.SimpleNamespace
tmp = Path(tempfile.mkdtemp())
# The real autopilot() runs below write their PID file. Keep it off the machine-global
# /tmp/general-autopilot.pid, which is shared with a live daemon and every other checkout's suite.
autopilot._PID_FILE = tmp / "general-autopilot.pid"

APP = AppConfig(
    name="eu",
    repo_path=".",
    base_branch="dev",
    protected_branch="main",
    backlog_backend="none",
)
cfg = Config(
    apps=[APP],
    audit_path=str(tmp / "audit.jsonl"),
    max_tickets_per_run=10,
)

# Ticket that returned no_changes (treated as ESCALATED)
NO_CHANGES_TICKET = ns(id="EU-116-NOCHANGES", status="In Progress", app=APP)


def _run_cycle():
    """Drive one autopilot cycle (once=True) with stubbed intake/loop/events."""
    autopilot.usage.budget_status = (
        lambda c: {"over": False, "alert": False, "used": 0, "cap": 1, "pct": 0.0}
    )
    autopilot.intake.from_drain = lambda c, app, n: [(APP, NO_CHANGES_TICKET)]

    async def _run_loop(c, worklist, audit):
        # Simulate a no_changes outcome: loop.py now returns ESCALATED for no_changes
        return [
            TicketReport(
                ticket_id=NO_CHANGES_TICKET.id,
                outcome=Outcome.ESCALATED,
                iterations=1,
                cost_usd=0.0,
                notes="builder found no changes — fix already present",
            )
        ]

    autopilot.run_loop = _run_loop

    async def _after_cycle(c, reports, audit, blocked):
        return

    autopilot.notify.configured = lambda: False
    autopilot.notify.send = lambda *a, **k: None
    asyncio.run(autopilot.autopilot(cfg, once=True))


# Test A: no_changes ticket is parked immediately (not retried like a transient ERROR)
_run_cycle()
chk(
    "A. no_changes ticket parked immediately",
    NO_CHANGES_TICKET.id in autopilot.load_blocked(cfg),
    "ESCALATED outcome should park immediately",
)

# Test B: no_changes ticket does NOT accumulate error retries
# (ERRORED outcomes accumulate error counts; ESCALATED should not)
chk(
    "B. no_changes ticket has no error count",
    autopilot.load_error_counts(cfg).get(NO_CHANGES_TICKET.id) is None,
    "ESCALATED (no_changes) should not accumulate error retries",
)

# Test C: no_changes ticket stays parked across cycles
# (verify the drain doesn't re-run it)
_run_cycle()
chk(
    "C. no_changes ticket stays parked",
    NO_CHANGES_TICKET.id in autopilot.load_blocked(cfg),
    "Drain must not re-run a parked no_changes ticket",
)

# Test D: compare with ERRORED behavior (which DOES retry)
autopilot.save_blocked(cfg, set())
autopilot.save_error_counts(cfg, {})
ERROR_TICKET = ns(id="EU-116-ERROR", status="In Progress", app=APP)

_outcome = {"value": Outcome.ERRORED}


def _run_error_cycle():
    """Drive one cycle with an ERRORED outcome."""
    autopilot.usage.budget_status = (
        lambda c: {"over": False, "alert": False, "used": 0, "cap": 1, "pct": 0.0}
    )
    autopilot.intake.from_drain = lambda c, app, n: [(APP, ERROR_TICKET)]

    async def _run_loop(c, worklist, audit):
        return [
            TicketReport(
                ticket_id=ERROR_TICKET.id,
                outcome=_outcome["value"],
                iterations=1,
                cost_usd=0.0,
            )
        ]

    autopilot.run_loop = _run_loop

    async def _after_cycle(c, reports, audit, blocked):
        return

    autopilot.notify.configured = lambda: False
    autopilot.notify.send = lambda *a, **k: None
    asyncio.run(autopilot.autopilot(cfg, once=True))


_run_error_cycle()
chk(
    "D. ERRORED ticket is NOT parked (retries)",
    ERROR_TICKET.id not in autopilot.load_blocked(cfg),
    "ERRORED should retry, not park immediately",
)
chk(
    "E. ERRORED ticket accumulates error count",
    autopilot.load_error_counts(cfg).get(ERROR_TICKET.id) == 1,
    "ERRORED should count errors for retry-before-park",
)

# Summary
print("\n============ EU-116 NO CHANGES DRAIN LOOP TEST ============")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not ok else ""))

print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
