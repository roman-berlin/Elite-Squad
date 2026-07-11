"""EU-87: three-tier drain order in autopilot — In Progress → answered/unblocked → To Do.

Verifies that a single autopilot cycle assembles the worklist in the correct priority order:
  1. Tier-1 (In Progress): tickets already in flight on the board, regardless of Rank.
  2. Tier-2 (answered):    parked tickets the Commander has answered directly on Jira.
  3. Tier-3 (To Do):       ready tickets waiting to be picked up, in board-Rank order.

Also checks that unanswered blocked tickets are excluded from the worklist entirely.
"""
import sys, types, asyncio, tempfile, threading
from pathlib import Path

# --- stub heavy deps so modules import without them ---------------------
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import autopilot, intake
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _ticket(tid, status):
    """Build a minimal Ticket with the given id and Jira status."""
    return Ticket(id=tid, key=tid, summary=f"ticket {tid}",
                  description="", acceptance_criteria=[], status=status)


def _app():
    return AppConfig(name="automatixy", repo_path="/tmp", base_branch="DEV",
                     protected_branch="MAIN", backlog_backend="jira",
                     backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})


# --------------------------------------------------------------------------- #
# Unit-level worklist-assembly tests (exercising the split logic directly)
# --------------------------------------------------------------------------- #

def _run_cycle(drain_tickets, resumed_map, blocked_set, cap=5):
    """Simulate the EU-87/EU-252 worklist-assembly code with the same logic as autopilot.py.
    EU-252: `cap` bounds only the To Do (tier-3) slice — it is applied BEFORE the tiers are
    joined, not to the combined list, so In Progress/answered work is never truncated by it."""
    app = _app()
    raw = [(app, t) for t in drain_tickets if t.id not in blocked_set]

    in_progress = [(a, t) for (a, t) in raw
                   if t.status and "progress" in t.status.lower()]
    to_do = [(a, t) for (a, t) in raw
             if not (t.status and "progress" in t.status.lower())][:cap]

    in_drain = {t.id for _, t in raw}
    answered_items = [v for k, v in resumed_map.items() if k not in in_drain]

    worklist = in_progress + answered_items + to_do
    return [t.id for _, t in worklist]


app = _app()

# ---- basic three-tier ordering ----
ip1 = _ticket("AUTO-1", "In Progress")
ip2 = _ticket("AUTO-2", "In Progress")
td1 = _ticket("AUTO-3", "To Do")
td2 = _ticket("AUTO-4", "To Do")
ans = _ticket("AUTO-5", None)   # resumed from blocked — no status from drain

resumed_map = {"AUTO-5": (app, ans)}

order = _run_cycle([ip1, ip2, td1, td2], resumed_map=resumed_map, blocked_set=set())
chk("In Progress tickets come before answered tier",
    order.index("AUTO-1") < order.index("AUTO-5") and
    order.index("AUTO-2") < order.index("AUTO-5"),
    str(order))
chk("answered ticket comes before To Do tickets",
    order.index("AUTO-5") < order.index("AUTO-3") and
    order.index("AUTO-5") < order.index("AUTO-4"),
    str(order))
chk("To Do tickets are last", order[-2:] == ["AUTO-3", "AUTO-4"], str(order))
chk("all five tickets present (no loss)", len(order) == 5, str(order))

# ---- unanswered blocked ticket must NOT appear in the worklist ----
blocked_ticket = _ticket("AUTO-99", "In Progress")   # blocked AND In Progress
order_b = _run_cycle([ip1, blocked_ticket, td1], resumed_map={}, blocked_set={"AUTO-99"})
chk("unanswered blocked ticket excluded from worklist", "AUTO-99" not in order_b, str(order_b))
chk("non-blocked In Progress still included", "AUTO-1" in order_b, str(order_b))

# ---- an answered ticket whose drain-status is In Progress goes via tier-1, not tier-2 ----
ip_answered = _ticket("AUTO-7", "In Progress")   # still In Progress on the board
resumed_overlap = {"AUTO-7": (app, ip_answered)}
order_ov = _run_cycle([ip1, ip_answered, td1], resumed_map=resumed_overlap, blocked_set=set())
chk("answered In Progress ticket stays in tier-1 (not duplicated via tier-2)",
    order_ov.count("AUTO-7") == 1, str(order_ov))
chk("answered In Progress ticket appears before To Do",
    order_ov.index("AUTO-7") < order_ov.index("AUTO-3"), str(order_ov))

# ---- EU-252: cap bounds ONLY the To Do slice — In Progress rides through uncapped, never cut
# by a full To Do tier (a cap on the combined list is what starved EU-233..237) ----
many_todo = [_ticket(f"AUTO-{10+i}", "To Do") for i in range(4)]
order_cap = _run_cycle([ip1, ip2] + many_todo, resumed_map={}, blocked_set=set(), cap=3)
chk("cap applied after tier ordering — both In Progress tickets survive",
    "AUTO-1" in order_cap and "AUTO-2" in order_cap, str(order_cap))
chk("cap bounds only To Do: total is N(In Progress) + cap, not cap alone",
    len(order_cap) == 5, str(order_cap))
todo_ids = {t.id for t in many_todo}
chk("cap bounds only To Do: exactly `cap` To Do tickets admitted",
    sum(1 for k in order_cap if k in todo_ids) == 3, str(order_cap))

# ---- empty answered tier (no resumed tickets) still works ----
order_no_ans = _run_cycle([ip1, td1, td2], resumed_map={}, blocked_set=set())
chk("no answered tier: In Progress before To Do",
    order_no_ans.index("AUTO-1") < order_no_ans.index("AUTO-3"), str(order_no_ans))

# ---- status field case-insensitivity (e.g. "in progress" lowercase from custom Jira) ----
ip_lower = _ticket("AUTO-8", "in progress")
order_case = _run_cycle([ip_lower, td1], resumed_map={}, blocked_set=set())
chk("status 'in progress' (lowercase) still classified as tier-1",
    order_case.index("AUTO-8") < order_case.index("AUTO-3"), str(order_case))

# ---- Ticket.status field exists and is populated (contract check) ----
t_with_status = Ticket(id="T-1", key="T-1", summary="s", description="d",
                       acceptance_criteria=[], status="In Progress")
chk("Ticket.status field accepted", t_with_status.status == "In Progress")
t_no_status = Ticket(id="T-2", key="T-2", summary="s", description="d",
                     acceptance_criteria=[])
chk("Ticket.status defaults to None", t_no_status.status is None)

# --------------------------------------------------------------------------- #
# Integration: run one full autopilot cycle and assert the order seen by the loop
# --------------------------------------------------------------------------- #
tmp = Path(tempfile.mkdtemp())
# The real autopilot() run below writes its PID file. Keep it off the machine-global
# /tmp/general-autopilot.pid, which is shared with a live daemon and every other checkout's suite.
autopilot._PID_FILE = tmp / "general-autopilot.pid"
cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="jira",
                    backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})],
    audit_path=str(tmp / "audit.jsonl"),
    max_tickets_per_run=10,   # allow all fixture tickets through the cap
)

# Drain returns In Progress first (AUTO-10), then To Do (AUTO-11, AUTO-12)
DRAIN = [_ticket("AUTO-10", "In Progress"), _ticket("AUTO-11", "To Do"), _ticket("AUTO-12", "To Do")]
# AUTO-13 is a parked ticket the Commander answered → tier-2
ANSWERED_TICKET = _ticket("AUTO-13", None)

autopilot.usage.budget_status = lambda c: {"over": False, "alert": False, "used": 0, "cap": 999, "pct": 0.0}
autopilot.notify.configured = lambda: False
autopilot.notify.send = lambda *a, **k: None
autopilot._commander_mid_git = lambda c: None

# Stub _resumable_answered to return AUTO-13 as an answered ticket
autopilot._resumable_answered = lambda cfg, app, blocked: (
    {"AUTO-13": (cfg.apps[0], ANSWERED_TICKET)} if "AUTO-13" in blocked else {}
)
# Seed blocked with AUTO-13 (the answered one) and AUTO-99 (unanswered, must stay out)
autopilot.load_blocked = lambda cfg: {"AUTO-13", "AUTO-99"}
autopilot.save_blocked = lambda cfg, s: None
autopilot.load_error_counts = lambda cfg: {}
autopilot.save_error_counts = lambda cfg, d: None

# Stub drain to return our fixture tickets (AUTO-99 not in drain — it's still blocked)
intake.from_drain = lambda cfg, app, n: [(cfg.apps[0], t) for t in DRAIN]
intake.LAST_DRAIN_ERRORS.clear()

worklist_seen = []
async def _fake_loop(cfg, wl, audit):
    worklist_seen.extend(t.id for _, t in wl)
    return []

loop_mod = types.ModuleType("orchestrator.loop")
loop_mod.run = _fake_loop
import orchestrator.loop as _real_loop  # noqa: F401 — we patch via the autopilot module namespace
autopilot.run_loop = _fake_loop

async def _ac(c, reports, audit, blocked): return None

asyncio.run(autopilot.autopilot(cfg, once=True))

chk("integration: Auto-10 (In Progress) runs first",
    worklist_seen and worklist_seen[0] == "AUTO-10", str(worklist_seen))
chk("integration: Auto-13 (answered) runs second (before To Do)",
    "AUTO-13" in worklist_seen and
    worklist_seen.index("AUTO-13") == 1, str(worklist_seen))
chk("integration: To Do tickets (AUTO-11, AUTO-12) follow answered",
    worklist_seen.index("AUTO-11") > worklist_seen.index("AUTO-13") and
    worklist_seen.index("AUTO-12") > worklist_seen.index("AUTO-13"), str(worklist_seen))
chk("integration: AUTO-99 (unanswered blocked) never enters worklist",
    "AUTO-99" not in worklist_seen, str(worklist_seen))

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
print("\n===== EU-87 AUTOPILOT DRAIN ORDER QA =====")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-" * 48)
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
