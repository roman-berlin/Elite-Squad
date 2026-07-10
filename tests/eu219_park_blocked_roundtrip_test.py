"""EU-219: Park repeat-ERRORED tickets to Blocked with a reason + round-trip /unblock back to
In Progress.

2026-07-09 forensics: a ticket_exception left the ticket In Progress with no board state change.
The single-run path posts an error comment (12ecb49) but deliberately does not transition (ERRORED
tickets are retried). This closes the other half: once the autopilot's error threshold actually
parks a ticket, the board should show it Blocked with a reason, and /unblock should move it back so
the drain re-picks it.

Pins:
  1. A newly-parked ERRORED ticket gets set_status(Blocked) + a reason comment mentioning /unblock
     and the consecutive-error count.
  2. A newly-parked PARKED-outcome ticket (ESCALATED/PR_OPENED) is NOT re-transitioned by the new
     helper — decisions.add already owns that.
  3. unblock() removes the id from the blocked set AND best-effort transitions the matching
     jira-backed app's ticket back to 'In Progress' via get_task.
  4. Both halves are best-effort: a backlog whose set_status/get_task raises never propagates.

A FakeBacklog records set_status/add_comment/get_task calls; no network, no real models.
"""
import sys, types, tempfile
from pathlib import Path

# Stub the Agent SDK + requests so importing the orchestrator never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import autopilot
from orchestrator.backlog import base as backlog_base
from orchestrator.contracts import Outcome, Ticket
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))
    print(("PASS: " if c else "FAIL: ") + n + (f" - {d}" if d and not c else ""))

ns = types.SimpleNamespace


class FakeBacklog:
    """Records status/comment writes and serves get_task for a fixed ticket. raise_on names a
    method that should raise instead of executing, to exercise the best-effort guards."""
    def __init__(self, ticket=None, raise_on=None):
        self.status = None
        self.comments = []
        self.ticket = ticket
        self.raise_on = raise_on

    def _maybe_raise(self, name):
        if self.raise_on == name:
            raise RuntimeError(f"simulated {name} failure")

    def set_status(self, ticket, status):
        self._maybe_raise("set_status")
        self.status = status

    def add_comment(self, ticket, body):
        self._maybe_raise("add_comment")
        self.comments.append(body)

    def get_task(self, key):
        self._maybe_raise("get_task")
        if self.ticket is None:
            raise RuntimeError("no such ticket")
        return self.ticket


def use(fake):
    """Point every lazy make_backlog() at this fake (autopilot imports it from backlog.base)."""
    backlog_base.make_backlog = lambda a: fake
    return fake


tmp = Path(tempfile.mkdtemp())
app = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev", protected_branch="main",
                backlog_backend="jira", backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})
cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
TKT = Ticket(id="AUTO-1", key="AUTO-1", summary="do thing", description="spec",
             acceptance_criteria=["x"], app="automatixy", ephemeral=False)


# ============================================================================================== #
# AC1: a newly-parked ERRORED ticket -> Blocked + reason comment (/unblock + count)
# ============================================================================================== #
print("\n=== AC1: ERRORED park -> Blocked + reason comment ===")
f1 = use(FakeBacklog(ticket=TKT))
by_id = {"AUTO-1": (app, TKT)}
autopilot._park_errored_on_tracker(cfg, by_id, ["AUTO-1"], {"AUTO-1"})
chk("ERRORED park transitions the ticket to Blocked", f1.status == "Blocked", str(f1.status))
chk("ERRORED park posts a reason comment mentioning /unblock",
    any("/unblock" in c and "AUTO-1" in c for c in f1.comments), str(f1.comments))
chk("ERRORED park's comment cites the consecutive-error count",
    any(str(autopilot._MAX_TICKET_ERRORS) in c for c in f1.comments), str(f1.comments))

# ============================================================================================== #
# AC2: a newly-parked PARKED-outcome ticket (not in `errored`) is left alone by the new helper
# ============================================================================================== #
print("\n=== AC2: PARKED-outcome ticket is NOT re-transitioned by the ERRORED-only helper ===")
f2 = use(FakeBacklog(ticket=TKT))
by_id2 = {"AUTO-2": (app, Ticket(id="AUTO-2", key="AUTO-2", summary="s", description="d",
                                 app="automatixy", ephemeral=False))}
# AUTO-2 is `newly` (just parked) but its report outcome was ESCALATED, not ERRORED -> not in `errored`
autopilot._park_errored_on_tracker(cfg, by_id2, ["AUTO-2"], set())
chk("PARKED (non-ERRORED) ticket gets no Blocked call from the ERRORED-only helper",
    f2.status is None, str(f2.status))
chk("PARKED (non-ERRORED) ticket gets no comment from the ERRORED-only helper",
    f2.comments == [], str(f2.comments))

# ============================================================================================== #
# AC3: unblock() clears the blocked set AND best-effort transitions the ticket to In Progress
# ============================================================================================== #
print("\n=== AC3: /unblock round-trip -> In Progress ===")
autopilot.save_blocked(cfg, {"AUTO-1"})
f3 = use(FakeBacklog(ticket=TKT))
msg = autopilot.unblock(cfg, "AUTO-1")
chk("unblock removes the id from the blocked set", autopilot.load_blocked(cfg) == set())
chk("unblock reports success", "unblocked" in msg.lower() and "AUTO-1" in msg, msg)
chk("unblock best-effort transitions the ticket back to In Progress",
    f3.status == "In Progress", str(f3.status))

# a wrong-project/network miss (get_task raises) must not raise out of unblock()
autopilot.save_blocked(cfg, {"AUTO-9"})
f3b = use(FakeBacklog(raise_on="get_task"))
try:
    msg_b = autopilot.unblock(cfg, "AUTO-9")
    chk("unblock swallows a get_task miss without raising", True)
except Exception as e:  # noqa: BLE001 - the point of this assertion is that this must NOT happen
    chk("unblock swallows a get_task miss without raising", False, f"raised: {e}")
chk("unblock still cleared the blocked set despite the tracker miss",
    autopilot.load_blocked(cfg) == set())

# ============================================================================================== #
# AC4: best-effort guards — a raising backlog never propagates out of either half
# ============================================================================================== #
print("\n=== AC4: best-effort — a raising backlog never crashes the cycle ===")
f4 = use(FakeBacklog(ticket=TKT, raise_on="set_status"))
try:
    autopilot._park_errored_on_tracker(cfg, {"AUTO-1": (app, TKT)}, ["AUTO-1"], {"AUTO-1"})
    chk("_park_errored_on_tracker swallows a set_status failure without raising", True)
except Exception as e:  # noqa: BLE001
    chk("_park_errored_on_tracker swallows a set_status failure without raising", False, f"raised: {e}")

f5 = use(FakeBacklog(ticket=TKT, raise_on="add_comment"))
try:
    autopilot._park_errored_on_tracker(cfg, {"AUTO-1": (app, TKT)}, ["AUTO-1"], {"AUTO-1"})
    chk("_park_errored_on_tracker swallows an add_comment failure without raising", True)
except Exception as e:  # noqa: BLE001
    chk("_park_errored_on_tracker swallows an add_comment failure without raising", False, f"raised: {e}")

autopilot.save_blocked(cfg, {"AUTO-1"})
f6 = use(FakeBacklog(ticket=TKT, raise_on="set_status"))
try:
    autopilot.unblock(cfg, "AUTO-1")
    chk("unblock swallows a set_status failure without raising", True)
except Exception as e:  # noqa: BLE001
    chk("unblock swallows a set_status failure without raising", False, f"raised: {e}")


print("\n================ EU-219 PARK/BLOCKED ROUND-TRIP QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
