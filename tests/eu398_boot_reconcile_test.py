"""EU-398 — boot reconcile: In Progress tickets left dangling by a killed/crashed run get
resumed or honestly parked at serve boot (2026-07-19 senior-workflow lifecycle finding).

The live gap (the AUTO-177 kill, 2026-07-19): a killed/crashed serve process leaves its ticket
In Progress with no comment, no transition, no audit — the board shows work happening on a DEAD
run. Recovery today relies on the next drain happening to resume In Progress first via
queue_statuses. This fix adds a boot-time reconcile: list In Progress tickets; any with NO active
run AND no terminal audit event after their last `ticket_start` get a "resumed after an unclean
stop" comment + a re-queue (or an honest park on recurrence), audited as `boot_reconcile`.

Pins:
  §1 PURE CORE (_dangling_in_progress):
     - killed-mid-run (ticket_start, no terminal after) → dangling
     - a terminal AFTER the last start (merged/needs_human/...) → NOT dangling
     - a ticket_start with a terminal, THEN a fresh start with no terminal → dangling (2nd run killed)
     - In Progress but no ticket_start in the window → NOT dangling (can't classify; leave it)
     - genuinely-running (in active_ids) → NOT dangling (untouched)
  §2 I/O (boot_reconcile):
     - killed-mid-run ticket → "resumed after an unclean stop" comment, left In Progress,
       one boot_reconcile audit event with resumed=[id]
     - a genuinely-running ticket (its app active) → untouched, listed in skipped_active, no comment
     - a terminal-after-start ticket → not dangling, not touched
     - a RECURRING kill (prior boot_reconcile already resumed it, still dangling) → honest park:
       set_status(Blocked) + park comment, audited as parked=[id]
     - dry_run → no side-effects, still returns the dangling ids

Offline — the SDK + requests are stubbed; make_backlog is faked; no network, no real models.
"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

# Stub the Agent SDK + requests so importing the orchestrator never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

# Hermetic even when run standalone (run_all already scrubs these from harness env).
for _k in list(os.environ):
    if _k.startswith(("TELEGRAM_", "JIRA_")):
        os.environ.pop(_k, None)

from orchestrator import autopilot as ap
from orchestrator.backlog import base as backlog_base
from orchestrator.config import AppConfig, Config
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))
    print(("PASS: " if c else "FAIL: ") + n + (f" - {d}" if d and not c else ""))


# ── helpers ─────────────────────────────────────────────────────────────────── #
def _ev(event, tid, ts):
    return {"event": event, "ticket_id": tid, "ts": ts}


def _write_audit(path, rows):
    """Write controlled-order JSONL rows straight to the audit file (deterministic ts ordering)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class FakeBacklog:
    """Serves a fixed ticket list and records every comment / set_status call."""
    def __init__(self, tickets):
        self._tickets = list(tickets)
        self.comments = []        # [(key, body)]
        self.statuses = []        # [(key, status)]

    def get_ready_tasks(self, limit):
        return list(self._tickets)

    def add_comment(self, ticket, body):
        self.comments.append((ticket.key, body))

    def set_status(self, ticket, status):
        self.statuses.append((ticket.key, status))


def _cfg_with_app(tmp, tickets):
    """A one-jira-app Config whose make_backlog serves `tickets`, with a fresh audit path."""
    app = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev",
                    protected_branch="main", backlog_backend="jira",
                    backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})
    cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
    fake = FakeBacklog(tickets)
    backlog_base.make_backlog = lambda a: fake
    return cfg, fake


def _audit_events(path):
    try:
        return [json.loads(ln) for ln in Path(path).read_text().splitlines() if ln.strip()]
    except OSError:
        return []


# ============================================================================================ #
# §1 PURE CORE: _dangling_in_progress
# ============================================================================================ #
print("\n=== §1 pure core: _dangling_in_progress ===")
T_KILL = "AUTO-177"      # killed mid-run: start, no terminal
T_DONE = "AUTO-200"      # finished: start then merged
T_NEVER = "AUTO-201"     # In Progress but never started in the window
T_TWICE = "AUTO-202"     # start -> merged -> start (2nd run killed, no terminal)
rows = [
    _ev("ticket_start", T_KILL, "2026-07-19T13:00:00+0000"),
    _ev("ticket_start", T_DONE, "2026-07-19T13:00:00+0000"),
    _ev("merged", T_DONE, "2026-07-19T13:05:00+0000"),      # terminal AFTER start -> done
    _ev("ticket_start", T_TWICE, "2026-07-19T13:00:00+0000"),
    _ev("merged", T_TWICE, "2026-07-19T13:05:00+0000"),     # 1st run finished...
    _ev("ticket_start", T_TWICE, "2026-07-19T14:00:00+0000"),  # ...2nd run started...
    # ...and never reached a terminal (killed) -> dangling
]
ip = {T_KILL, T_DONE, T_NEVER, T_TWICE}
dangling = ap._dangling_in_progress(ip, rows, active_ids=set())
chk("killed-mid-run (start, no terminal) is dangling", T_KILL in dangling, str(dangling))
chk("terminal-after-last-start is NOT dangling (run finished)", T_DONE not in dangling, str(dangling))
chk("a 2nd run killed after a finished 1st run IS dangling", T_TWICE in dangling, str(dangling))
chk("In Progress but no ticket_start in window is NOT dangling (can't classify)",
    T_NEVER not in dangling, str(dangling))
chk("dangling set is exactly {T_KILL, T_TWICE}",
    set(dangling) == {T_KILL, T_TWICE}, str(dangling))

# genuinely-running -> untouched (active_ids excludes it)
dangling_active = ap._dangling_in_progress({T_KILL}, rows, active_ids={T_KILL})
chk("genuinely-running ticket (in active_ids) is NOT dangling",
    T_KILL not in dangling_active, str(dangling_active))

# terminal aliases (needs_human / ticket_exception / no_changes / pm_triage / scrum_split) all close a run
for term in ("needs_human", "ticket_exception", "no_changes", "pm_triage", "scrum_split", "pr_opened"):
    r = [_ev("ticket_start", "X-1", "2026-07-19T13:00:00+0000"),
         _ev(term, "X-1", "2026-07-19T13:05:00+0000")]
    d = ap._dangling_in_progress({"X-1"}, r, active_ids=set())
    chk(f"terminal alias '{term}' closes the run (not dangling)", "X-1" not in d, str(d))


# ============================================================================================ #
# §2 I/O: boot_reconcile — killed-mid-run resumes
# ============================================================================================ #
print("\n=== §2 I/O: killed-mid-run resumes ===")
tmp1 = Path(tempfile.mkdtemp())
t_kill = Ticket(id="AUTO-177", key="AUTO-177", summary="do thing", description="spec",
                acceptance_criteria=["x"], app="automatixy", status="In Progress")
cfg1, fake1 = _cfg_with_app(tmp1, [t_kill])
_write_audit(cfg1.audit_path, [_ev("ticket_start", "AUTO-177", "2026-07-19T13:00:00+0000")])
out1 = ap.boot_reconcile(cfg1)
chk("boot_reconcile resumes the killed-mid-run ticket", out1.get("resumed") == ["AUTO-177"], str(out1))
chk("resume posts exactly one comment", len(fake1.comments) == 1, str(fake1.comments))
chk("resume comment says 'unclean stop'",
    "unclean stop" in fake1.comments[0][1].lower(), str(fake1.comments))
chk("resume does NOT transition to Blocked (re-queue leaves it In Progress)",
    fake1.statuses == [], str(fake1.statuses))
ev1 = [e for e in _audit_events(cfg1.audit_path) if e.get("event") == "boot_reconcile"]
chk("one boot_reconcile audit event recorded with resumed=[AUTO-177]",
    len(ev1) == 1 and ev1[0].get("resumed") == ["AUTO-177"], str(ev1))


# ============================================================================================ #
# §3 I/O: a genuinely-running ticket is untouched
# ============================================================================================ #
print("\n=== §3 I/O: genuinely-running ticket untouched ===")
tmp2 = Path(tempfile.mkdtemp())
t_run = Ticket(id="AUTO-188", key="AUTO-188", summary="running now", description="spec",
               acceptance_criteria=["x"], app="automatixy", status="In Progress")
cfg2, fake2 = _cfg_with_app(tmp2, [t_run])
_write_audit(cfg2.audit_path, [_ev("ticket_start", "AUTO-188", "2026-07-19T13:00:00+0000")])
out2 = ap.boot_reconcile(cfg2, active_apps={"automatixy"})   # the app has a LIVE run right now
chk("genuinely-running ticket is NOT resumed", out2.get("resumed") == [], str(out2))
chk("genuinely-running ticket is NOT parked", out2.get("parked") == [], str(out2))
chk("genuinely-running ticket is listed in skipped_active",
    out2.get("skipped_active") == ["AUTO-188"], str(out2))
chk("genuinely-running ticket gets NO comment", fake2.comments == [], str(fake2.comments))
chk("genuinely-running ticket gets NO status transition", fake2.statuses == [], str(fake2.statuses))


# ============================================================================================ #
# §4 I/O: a terminal-after-start ticket is not dangling -> untouched
# ============================================================================================ #
print("\n=== §4 I/O: terminal-after-start ticket untouched ===")
tmp3 = Path(tempfile.mkdtemp())
t_done = Ticket(id="AUTO-300", key="AUTO-300", summary="already merged", description="spec",
                acceptance_criteria=["x"], app="automatixy", status="In Progress")
cfg3, fake3 = _cfg_with_app(tmp3, [t_done])
_write_audit(cfg3.audit_path, [
    _ev("ticket_start", "AUTO-300", "2026-07-19T13:00:00+0000"),
    _ev("merged", "AUTO-300", "2026-07-19T13:05:00+0000"),   # finished after start -> not dangling
])
out3 = ap.boot_reconcile(cfg3)
chk("terminal-after-start ticket is neither resumed nor parked",
    out3.get("resumed") == [] and out3.get("parked") == [], str(out3))
chk("terminal-after-start ticket gets NO comment", fake3.comments == [], str(fake3.comments))


# ============================================================================================ #
# §5 I/O: a RECURRING kill honest-parks (prior boot_reconcile already resumed it; still dangling)
# ============================================================================================ #
print("\n=== §5 I/O: recurring kill -> honest park ===")
tmp4 = Path(tempfile.mkdtemp())
t_rec = Ticket(id="AUTO-404", key="AUTO-404", summary="keeps dying", description="spec",
               acceptance_criteria=["x"], app="automatixy", status="In Progress")
cfg4, fake4 = _cfg_with_app(tmp4, [t_rec])
_write_audit(cfg4.audit_path, [
    _ev("ticket_start", "AUTO-404", "2026-07-19T13:00:00+0000"),
    # a PREVIOUS boot already resumed this ticket once...
    {"ts": "2026-07-19T13:01:00+0000", "event": "boot_reconcile", "resumed": ["AUTO-404"],
     "parked": [], "skipped_active": []},
    _ev("ticket_start", "AUTO-404", "2026-07-19T14:00:00+0000"),  # ...it ran again and was killed
])
out4 = ap.boot_reconcile(cfg4)
chk("recurring kill is honest-PARKED (not resumed again)",
    out4.get("parked") == ["AUTO-404"] and out4.get("resumed") == [], str(out4))
chk("park transitions the ticket to Blocked",
    ("AUTO-404", "Blocked") in fake4.statuses, str(fake4.statuses))
chk("park comment mentions /unblock (the honest hand-off)",
    any("/unblock" in body for _k, body in fake4.comments), str(fake4.comments))
ev4 = [e for e in _audit_events(cfg4.audit_path) if e.get("event") == "boot_reconcile"]
# two boot_reconcile events now: the seeded prior + this run's park
chk("this run's boot_reconcile records parked=[AUTO-404]",
    any(e.get("parked") == ["AUTO-404"] for e in ev4), str(ev4))


# ============================================================================================ #
# §6 I/O: dry_run -> no side-effects, but dangling is still computed
# ============================================================================================ #
print("\n=== §6 I/O: dry_run computes but writes nothing ===")
tmp5 = Path(tempfile.mkdtemp())
t_dry = Ticket(id="AUTO-500", key="AUTO-500", summary="dry", description="spec",
               acceptance_criteria=["x"], app="automatixy", status="In Progress")
cfg5, fake5 = _cfg_with_app(tmp5, [t_dry])
cfg5.dry_run = True
_write_audit(cfg5.audit_path, [_ev("ticket_start", "AUTO-500", "2026-07-19T13:00:00+0000")])
out5 = ap.boot_reconcile(cfg5)
chk("dry_run still reports the dangling id", out5.get("dangling") == ["AUTO-500"], str(out5))
chk("dry_run posts NO comment", fake5.comments == [], str(fake5.comments))
chk("dry_run transitions NOTHING", fake5.statuses == [], str(fake5.statuses))


# ============================================================================================ #
passed = [n for n, ok, _ in results if ok]
failed = [(n, d) for n, ok, d in results if not ok]
print(f"\neu398_boot_reconcile_test: {len(passed)}/{len(results)} passed")
for n, d in failed:
    print(f"  FAIL: {n}" + (f" — {d}" if d else ""))
if failed:
    sys.exit(1)
