"""EU-87 — three-tier drain order: In Progress → answered/unblocked → To Do (by rank).

Asserts the worklist assembled by one autopilot cycle honours the priority ordering:

  1. Tier-1 – In Progress tickets come first (a ticket we already started must never be
              delayed by fresh To Do items).
  2. Tier-2 – Answered/unblocked tickets (parked but Commander replied on Jira) sit between
              In Progress and To Do so a replied-to ticket is never left behind new work.
  3. Tier-3 – To Do tickets fill the rest of the cap, in board-Rank order.

Three assertions:
  A. Full order: [In Progress, answered-Blocked, To-Do-rank-1, To-Do-rank-2]
  B. Unanswered blocked ticket must NOT appear in the worklist.
  C. If both In Progress and answered tickets exist, In Progress is always first.

Drives one real autopilot cycle (once=True) with stubbed intake / _resumable_answered /
run_loop / events — no network, no models. Follows the pattern of
eu61_autopilot_resume_queue_test.py and autopilot_retry_test.py.
"""
import sys
import types
import tempfile
import asyncio
from pathlib import Path

# ---------------------------------------------------------------------------
# Minimal stubs for heavyweight SDK / requests imports the module pulls in.
# ---------------------------------------------------------------------------
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _D  # noqa: E731
sys.modules["claude_agent_sdk"] = sdk

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules["requests"] = req

sys.path.insert(0, ".")

from orchestrator import autopilot  # noqa: E402
from orchestrator.contracts import Outcome, TicketReport  # noqa: E402
from orchestrator.config import Config  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
results: list[tuple[str, bool, str]] = []


def chk(name: str, condition: bool, detail: str = "") -> None:
    """Record a single assertion."""
    results.append((name, bool(condition), detail))


ns = types.SimpleNamespace
tmp = Path(tempfile.mkdtemp())

# Need room for 4 tickets; the default cap is 1, so raise it explicitly.
cfg = Config(apps=[], audit_path=str(tmp / "audit.jsonl"), max_tickets_per_run=10)

# ---------------------------------------------------------------------------
# Tickets (plain namespaces — autopilot uses getattr(..., "status", None)).
# ---------------------------------------------------------------------------
IP_TICKET   = ns(id="EU-87-IP",    status="In Progress")   # Tier-1
ANS_TICKET  = ns(id="EU-87-ANS")                            # Tier-2 (blocked + answered)
TODO_RANK1  = ns(id="EU-87-TODO1")                          # Tier-3, rank 1
TODO_RANK2  = ns(id="EU-87-TODO2")                          # Tier-3, rank 2
UNANS_TICKET = ns(id="EU-87-UNANS")                         # blocked + NOT answered → must stay out

# Initial blocked set: the answered ticket and the unanswered ticket are both parked.
autopilot.save_blocked(cfg, {ANS_TICKET.id, UNANS_TICKET.id})

# Track what run_loop actually receives.
seen: dict = {}


def _run_cycle() -> None:
    """Drive one autopilot cycle (once=True) with all external calls stubbed out."""
    # Budget check — never paused.
    autopilot.usage.budget_status = lambda c: {
        "over": False, "alert": False, "used": 0, "cap": 1, "pct": 0.0
    }

    # _resumable_answered: ANS_TICKET is answered; UNANS_TICKET is not.
    # Returns {ticket_id: (app, ticket)} for tickets the Commander replied to on Jira.
    def _resumable_answered(c, app, blocked):  # noqa: ANN001
        """Stub: only EU-87-ANS is treated as answered on Jira."""
        return {ANS_TICKET.id: ("app", ANS_TICKET)} if ANS_TICKET.id in blocked else {}

    autopilot._resumable_answered = _resumable_answered

    # Drain returns: In Progress, unanswered-blocked (should be filtered), then two To Do.
    # The unanswered blocked ticket appears in the drain to prove it is excluded by the blocked filter.
    # from_drain is called AFTER _resumable_answered removes ANS_TICKET from blocked, so ANS_TICKET
    # is NOT in the drain — it arrives only via answered_items (Tier-2).
    autopilot.intake.from_drain = lambda c, app, n: [
        ("app", IP_TICKET),      # Tier-1 candidate
        ("app", UNANS_TICKET),   # still blocked → filtered out
        ("app", TODO_RANK1),     # Tier-3, rank 1
        ("app", TODO_RANK2),     # Tier-3, rank 2
    ]
    autopilot.intake.LAST_DRAIN_ERRORS = {}

    # Capture the worklist; return a success report for every ticket.
    async def _run_loop(c, worklist, audit):  # noqa: ANN001
        seen["worklist_ids"] = [t.id for _, t in worklist]
        return [
            TicketReport(ticket_id=t.id, outcome=Outcome.MERGED, iterations=1, cost_usd=0.0)
            for _, t in worklist
        ]

    autopilot.run_loop = _run_loop

    async def _after_cycle(c, reports, audit, blocked):  # noqa: ANN001
        return None

    autopilot.events.after_cycle = _after_cycle
    autopilot.notify.configured = lambda: False
    autopilot.notify.send = lambda *a, **k: None

    asyncio.run(autopilot.autopilot(cfg, once=True))


_run_cycle()

wl = seen.get("worklist_ids", [])

# ---------------------------------------------------------------------------
# Assertion A: full three-tier order
# ---------------------------------------------------------------------------
chk(
    "In Progress ticket is first in the worklist",
    wl and wl[0] == IP_TICKET.id,
    str(wl),
)
chk(
    "Answered/unblocked ticket is second (after In Progress)",
    len(wl) > 1 and wl[1] == ANS_TICKET.id,
    str(wl),
)
chk(
    "To-Do rank-1 ticket is third",
    len(wl) > 2 and wl[2] == TODO_RANK1.id,
    str(wl),
)
chk(
    "To-Do rank-2 ticket is fourth",
    len(wl) > 3 and wl[3] == TODO_RANK2.id,
    str(wl),
)
chk(
    "Full order is [In Progress, answered-Blocked, To-Do-rank-1, To-Do-rank-2]",
    wl[:4] == [IP_TICKET.id, ANS_TICKET.id, TODO_RANK1.id, TODO_RANK2.id],
    str(wl),
)

# ---------------------------------------------------------------------------
# Assertion B: unanswered blocked ticket must NOT appear
# ---------------------------------------------------------------------------
chk(
    "Unanswered blocked ticket is absent from the worklist",
    UNANS_TICKET.id not in wl,
    str(wl),
)

# ---------------------------------------------------------------------------
# Assertion C: In Progress is always before answered when both exist
# ---------------------------------------------------------------------------
both_present = IP_TICKET.id in wl and ANS_TICKET.id in wl
if both_present:
    ip_pos  = wl.index(IP_TICKET.id)
    ans_pos = wl.index(ANS_TICKET.id)
    chk(
        "In Progress appears before answered-Blocked when both are present",
        ip_pos < ans_pos,
        f"In-Progress pos={ip_pos}, answered pos={ans_pos} in {wl}",
    )
else:
    chk(
        "In Progress appears before answered-Blocked when both are present",
        False,
        f"One or both missing: IP={IP_TICKET.id in wl}, ANS={ANS_TICKET.id in wl}, wl={wl}",
    )

# ---------------------------------------------------------------------------
# Print summary
# ---------------------------------------------------------------------------
print("\n========= EU-87 DRAIN ORDER QA =========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    marker = "PASS" if ok else "FAIL"
    suffix = f"  ({det})" if det and not ok else ""
    print(f"  [{marker}] {name}{suffix}")
print("-----------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
