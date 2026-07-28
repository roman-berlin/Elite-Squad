"""EU-738 — boot_reconcile must actually find stranded In-Progress tickets, and must record on
EVERY boot (2026-07-28).

The live gap: six EU tickets sat In Progress against max_concurrent_builders=2 while the newest
`boot_reconcile` audit line was days old. Root cause, in boot_reconcile:

    active_ids = {t.id for app, t in in_progress if app.name in (active_apps or set())}

`cockpit_state.active_runs()` returns app NAMES, not ticket ids, so `app.name in active_apps` was
true for EVERY In-Progress ticket of any app holding a run slot. `resume_armed_drains` claims that
slot SYNCHRONOUSLY (claim_run) before main.py calls boot_reconcile, so on a normal boot the entire
In-Progress column read as "genuinely running", nothing was ever dangling, and the reconcile did
nothing at all. And because it recorded an audit event only `if resumed or parked`, four days of a
structurally-broken reconcile looked byte-identical to four days of nothing-to-do.

The fix keeps the EXPLICIT `active_apps` seam verbatim (it is the documented test override, pinned
by eu398 §3) and changes only the SELF-DERIVED path: per-TICKET audit recency, with the window
derived from the configured agent budget rather than guessed. Two live measurements forced that:
a 15-minute window classified two genuinely-live tickets as abandoned (18 min silent inside one
planner call), and EU-743's builder call ran 42.9 minutes silent between its `planner` and `build`
audit lines on 2026-07-28.

Pins:
  §1 PURE CORE (_recently_active_ids / _live_silence_window_s):
     - a ticket with recent audit activity is live; one silent past the window is not
     - a 25-minute silent stretch (longer than the 18 min observed live) is still LIVE
     - an unparseable ts never counts as "now" (can't mask an abandoned run)
     - an id outside the In-Progress set is never returned
     - the window is DERIVED from cfg.builder_timeout_s (raise it -> the window grows)
  §2 I/O — THE DEFECT: an app holding a live run slot no longer exempts its idle stranded ticket
  §3 I/O — a ticket mid long silent agent call (25 min gap) is NOT touched: no comment, no strike
  §4 I/O — an EXPLICIT active_apps still overrides verbatim (the eu398 §3 seam)
  §5 I/O — a NO-OP boot still records exactly one boot_reconcile audit event

Offline — the SDK + requests are stubbed; make_backlog is faked; no network, no real models.
"""
import json
import os
import sys
import tempfile
import time
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
from orchestrator import cockpit_state
from orchestrator.backlog import base as backlog_base
from orchestrator.config import AppConfig, Config
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))
    print(("PASS: " if c else "FAIL: ") + n + (f" - {d}" if d and not c else ""))


MIN = 60.0
HOUR = 3600.0


def _ts(seconds_ago: float) -> str:
    """An audit stamp `seconds_ago` in the past, in audit.py's exact `%Y-%m-%dT%H:%M:%S%z` shape."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - seconds_ago))


def _ev(event, tid, seconds_ago):
    return {"event": event, "ticket_id": tid, "ts": _ts(seconds_ago)}


def _write_audit(path, rows):
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


def _cfg_with_app(tickets):
    """A one-jira-app Config whose make_backlog serves `tickets`, with a fresh audit path."""
    tmp = Path(tempfile.mkdtemp())
    app = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev",
                    protected_branch="main", backlog_backend="jira",
                    backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})
    cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
    fake = FakeBacklog(tickets)
    backlog_base.make_backlog = lambda a: fake
    return cfg, fake


def _in_progress(tid):
    return Ticket(id=tid, key=tid, summary="s", description="spec",
                  acceptance_criteria=["x"], app="automatixy", status="In Progress")


def _boot_events(path):
    out = []
    try:
        for ln in Path(path).read_text().splitlines():
            if not ln.strip():
                continue
            try:
                ev = json.loads(ln)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("event") == "boot_reconcile":
                out.append(ev)
    except OSError:
        pass
    return out


# ============================================================================================ #
# §1 PURE CORE: _recently_active_ids / _live_silence_window_s
# ============================================================================================ #
print("\n=== §1 pure core: per-ticket liveness ===")
WINDOW = 90 * MIN
rows1 = [
    _ev("agent_call", "EU-1", 5 * MIN),        # a run wrote about it 5 minutes ago -> live
    _ev("planner", "EU-2", 25 * MIN),          # mid a long silent agent call -> still live
    _ev("ticket_start", "EU-3", 6 * HOUR),     # stranded hours ago -> not live
    {"event": "agent_call", "ticket_id": "EU-4", "ts": "not-a-timestamp"},   # unreadable stamp
    _ev("agent_call", "EU-9", 1 * MIN),        # recent, but NOT In Progress -> never returned
]
ip1 = {"EU-1", "EU-2", "EU-3", "EU-4"}
live1 = ap._recently_active_ids(ip1, rows1, window_s=WINDOW)
chk("a ticket written about 5 min ago is live", "EU-1" in live1, str(live1))
chk("a 25-minute silent stretch is STILL live (18 min was observed on a healthy run)",
    "EU-2" in live1, str(live1))
chk("a ticket silent for 6h is NOT live", "EU-3" not in live1, str(live1))
chk("an unparseable ts never counts as 'now'", "EU-4" not in live1, str(live1))
chk("an id outside the In-Progress set is never returned", "EU-9" not in live1, str(live1))
chk("live set is exactly {EU-1, EU-2}", live1 == {"EU-1", "EU-2"}, str(live1))

# The window is DERIVED from the configured agent budget, not a literal.
cfg_w, _ = _cfg_with_app([])
default_window = ap._live_silence_window_s(cfg_w)
chk("the default window comfortably exceeds the 42.9-min silent builder call measured live",
    default_window > 43 * MIN, f"{default_window}s")
cfg_w.builder_timeout_s = 7200
chk("raising builder_timeout_s widens the window with it",
    ap._live_silence_window_s(cfg_w) > default_window,
    f"{ap._live_silence_window_s(cfg_w)}s vs {default_window}s")


# ============================================================================================ #
# §2 I/O — THE DEFECT: a live run slot on the APP must not exempt its idle stranded ticket
# ============================================================================================ #
print("\n=== §2 I/O: an app holding a run slot no longer exempts its stranded ticket ===")
# The shape that produced the leak: the app IS active (a drain resumed at boot and claimed the run
# slot), and a DIFFERENT ticket of that same app has been stranded for hours with no terminal.
_real_active_runs = cockpit_state.active_runs
cockpit_state.active_runs = lambda: ["automatixy"]
try:
    t_stranded = _in_progress("EU-553")
    cfg2, fake2 = _cfg_with_app([t_stranded])
    _write_audit(cfg2.audit_path, [
        _ev("ticket_start", "EU-553", 8 * HOUR),
        _ev("token_burn_report", "EU-553", 5 * HOUR),   # last sign of life: 5h ago, no terminal
    ])
    out2 = ap.boot_reconcile(cfg2)          # self-derived path (active_apps=None)
finally:
    cockpit_state.active_runs = _real_active_runs
chk("the stranded ticket IS reconciled even though its app holds a live run slot",
    out2.get("resumed") == ["EU-553"], str(out2))
chk("it is NOT listed as skipped_active", out2.get("skipped_active") == [], str(out2))
chk("it gets the 'unclean stop' comment",
    len(fake2.comments) == 1 and "unclean stop" in fake2.comments[0][1].lower(),
    str(fake2.comments))
chk("re-queue leaves it In Progress (no Blocked transition)", fake2.statuses == [],
    str(fake2.statuses))


# ============================================================================================ #
# §3 I/O — a ticket mid a long SILENT agent call is not touched
# ============================================================================================ #
print("\n=== §3 I/O: a 25-minute silent agent call is left alone ===")
t_live = _in_progress("EU-743")
cfg3, fake3 = _cfg_with_app([t_live])
_write_audit(cfg3.audit_path, [
    _ev("ticket_start", "EU-743", 3 * HOUR),
    _ev("planner", "EU-743", 25 * MIN),      # >= 20 min of silence: a normal builder stretch
])
out3 = ap.boot_reconcile(cfg3)
chk("a genuinely-live ticket 25 min into a silent call is NOT resumed",
    out3.get("resumed") == [], str(out3))
chk("...nor parked", out3.get("parked") == [], str(out3))
chk("...and is reported as skipped_active", out3.get("skipped_active") == ["EU-743"], str(out3))
chk("no 'unclean stop' comment is posted onto a healthy run", fake3.comments == [],
    str(fake3.comments))
chk("no durable strike is charged to a healthy run", ap.load_error_counts(cfg3) == {},
    str(ap.load_error_counts(cfg3)))


# ============================================================================================ #
# §4 I/O — an EXPLICIT active_apps still overrides VERBATIM (the eu398 §3 seam)
# ============================================================================================ #
print("\n=== §4 I/O: explicit active_apps still overrides verbatim ===")
t_old = _in_progress("EU-597")
cfg4, fake4 = _cfg_with_app([t_old])
_write_audit(cfg4.audit_path, [_ev("ticket_start", "EU-597", 50 * HOUR)])   # silent for days
out4 = ap.boot_reconcile(cfg4, active_apps={"automatixy"})
chk("an explicitly-named active app exempts its ticket however stale the audit is",
    out4.get("resumed") == [] and out4.get("parked") == [], str(out4))
chk("the exempted ticket is reported in skipped_active",
    out4.get("skipped_active") == ["EU-597"], str(out4))
chk("no comment on the explicitly-active ticket", fake4.comments == [], str(fake4.comments))


# ============================================================================================ #
# §5 I/O — a NO-OP boot still records exactly one boot_reconcile audit event
# ============================================================================================ #
print("\n=== §5 I/O: a no-op boot is provable ===")
cfg5, fake5 = _cfg_with_app([])          # nothing In Progress at all
_write_audit(cfg5.audit_path, [])
out5 = ap.boot_reconcile(cfg5)
ev5 = _boot_events(cfg5.audit_path)
chk("a no-op boot records exactly one boot_reconcile event", len(ev5) == 1, str(ev5))
chk("the no-op event says nothing was resumed or parked",
    len(ev5) == 1 and ev5[0].get("resumed") == [] and ev5[0].get("parked") == [], str(ev5))
chk("the no-op event carries the In-Progress count it looked at",
    len(ev5) == 1 and ev5[0].get("in_progress_count") == 0, str(ev5))
chk("a no-op boot reports nothing resumed", out5.get("resumed") == [], str(out5))

# ...and a boot where everything is genuinely live records too (nothing touched, still provable).
cfg6, fake6 = _cfg_with_app([_in_progress("EU-743")])
_write_audit(cfg6.audit_path, [_ev("agent_call", "EU-743", 2 * MIN)])
ap.boot_reconcile(cfg6)
ev6 = _boot_events(cfg6.audit_path)
chk("an all-live boot records a boot_reconcile event too",
    len(ev6) == 1 and ev6[0].get("skipped_active") == ["EU-743"], str(ev6))


# ============================================================================================ #
passed = [n for n, ok, _ in results if ok]
failed = [(n, d) for n, ok, d in results if not ok]
print(f"\neu738_boot_reconcile_liveness_test: {len(passed)}/{len(results)} passed")
for n, d in failed:
    print(f"  FAIL: {n}" + (f" — {d}" if d else ""))
if failed:
    sys.exit(1)
