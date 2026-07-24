"""Graceful-stop QA: the cockpit offers 'Finish & stop' (let the in-flight ticket land on DEV, then
stand down — take no new tickets) alongside the immediate 'Stop'. The drain action sets the stop signal
but keeps the worker 'on' + 'stopping' so the UI shows it's finishing the current ticket; the worker's
own exit clears it. (max_tickets_per_run=1, and the stop is checked between tickets, so a build is never
killed mid-flight.)

EU-103: autopilot_switch() in warroom.py is now a header roll-up count badge — per-project
start/stop controls moved to the control bar (cockpit_views._control_bar).  The drain/stop API
handlers (server.py /api/autopilot POST) are now PER-APP: they resolve the target project from the
posted `app` field, signal only that app's stop_event from get_state(app), and read/clear that app's
dedicated autopilot_on flag — never a single global autopilot."""
import sys, types, threading, tempfile
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

from orchestrator import warroom, server, sync, cockpit_state
from orchestrator import autopilot as _ap_mod
from orchestrator.config import Config, AppConfig

# get_autopilot_status()'s "on" ORs in a liveness probe of the machine-global autopilot PID file
# (/tmp/general-autopilot.pid). A real daemon on this machine — or another checkout's suite running
# the real autopilot() — flips it True mid-harness, and the immediate-Stop check below reads on=True
# (the 2026-07-06 flake). Probe a per-harness path instead.
_ap_mod._PID_FILE = Path(tempfile.mkdtemp()) / "general-autopilot.pid"

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- autopilot_switch (EU-103): now a roll-up count badge, not per-project controls ---
# When no runs are active: returns empty string (keeps the header clean).
cockpit_state.reset_run_state()
badge_off = warroom.autopilot_switch({}, "*", True)
chk("header roll-up: no active runs → empty string", badge_off == "", repr(badge_off))

# When one run is active: shows '1 project running' badge.
cockpit_state.reset_run_state()
cockpit_state.get_state("automatixy")["active"] = True
badge_one = warroom.autopilot_switch({}, "*", True)
chk("header roll-up: 1 active run → '1 project running' badge", "1" in badge_one and "project" in badge_one, repr(badge_one))
chk("header roll-up: badge has pulsing dot (apdot on)", 'class="apdot on"' in badge_one or "apdot on" in badge_one, repr(badge_one))

# When two runs are active: shows '2 projects running'.
cockpit_state.reset_run_state()
cockpit_state.get_state("automatixy")["active"] = True
cockpit_state.get_state("Elite-Unit")["active"] = True
badge_two = warroom.autopilot_switch({}, "*", True)
chk("header roll-up: 2 active runs → '2 projects running' badge", "2" in badge_two and "projects" in badge_two, repr(badge_two))

# The header roll-up must NOT contain start/stop form controls (those moved to the control bar).
chk("header roll-up: no global Start form", "value=start" not in badge_one)
chk("header roll-up: no global Stop form", "value=stop" not in badge_one)
chk("header roll-up: no global Drain form", "value=drain" not in badge_one)

# --- EU-479: the header badge counts CONCURRENT BUILDS on demand, not distinct active apps ---
# The iter-1 attempt cached per-app counts as a side effect of render_board — which only runs
# for the project whose tab is open, so an unseen same-app double-build collapsed to a floor of
# 1. The corrected design (warroom.total_live_run_count) recomputes the total at badge-render
# time: len(live_runs(cfg, tasks, app, True)) summed over EVERY app in active_runs().
import json as _json
from datetime import datetime as _dt, timedelta as _td
from orchestrator import dashboard as _D


def _eu479_cfg(rows, apps=("automatixy",), max_builders=2):
    """A temp Config whose audit carries *rows*; clears the dashboard caches first."""
    d = Path(tempfile.mkdtemp())
    (d / "audit.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    _D._audit_cache.clear()
    _D._tasks_cache.clear()
    return Config(apps=[AppConfig(name=n, repo_path=str(d), base_branch="dev",
                                  protected_branch="main", backlog_backend="none")
                        for n in apps],
                  audit_path=str(d / "audit.jsonl"),
                  max_concurrent_builders=max_builders)


def _ago(secs):
    return (_dt.now().astimezone() - _td(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%S%z")


# AC1: two concurrent tickets in the SAME app (the EU-444 + EU-443 scenario) → badge shows 2,
# labelled as builds, NOT claimed as 2 distinct projects.
cockpit_state.reset_run_state()
cfg479 = _eu479_cfg([
    dict(event="ticket_start", ticket_id="EU-444", app="automatixy", branch="b", ts=_ago(120)),
    dict(event="build", ticket_id="EU-444", app="automatixy", iteration=1, ts=_ago(110)),
    dict(event="ticket_start", ticket_id="EU-443", app="automatixy", branch="c", ts=_ago(20)),
    dict(event="build", ticket_id="EU-443", app="automatixy", iteration=2, ts=_ago(10)),
])
cockpit_state.get_state("automatixy")["active"] = True
chk("EU-479: same-app double-build → on-demand total is 2",
    warroom.total_live_run_count(cfg479) == 2,
    f"total={warroom.total_live_run_count(cfg479)}")
badge_double = warroom.autopilot_switch({}, "automatixy", True, cfg=cfg479)
chk("EU-479: same-app double-build badge shows '2'", "2" in badge_double, repr(badge_double))
chk("EU-479: same-app double-build badge does NOT claim '2 projects'",
    "2 projects" not in badge_double, repr(badge_double))
chk("EU-479: badge names the real project count ('1 project')",
    "1 project" in badge_double, repr(badge_double))

# AC2: two apps with one run each → still 2 (no cross-app regression), 2 projects.
cockpit_state.reset_run_state()
cfg479b = _eu479_cfg([
    dict(event="ticket_start", ticket_id="A-1", app="appone", branch="b", ts=_ago(30)),
    dict(event="build", ticket_id="A-1", app="appone", iteration=1, ts=_ago(20)),
    dict(event="ticket_start", ticket_id="B-1", app="apptwo", branch="b", ts=_ago(25)),
    dict(event="build", ticket_id="B-1", app="apptwo", iteration=1, ts=_ago(15)),
], apps=("appone", "apptwo"))
cockpit_state.get_state("appone")["active"] = True
cockpit_state.get_state("apptwo")["active"] = True
chk("EU-479: two apps × 1 run → on-demand total is 2 (no regression)",
    warroom.total_live_run_count(cfg479b) == 2,
    f"total={warroom.total_live_run_count(cfg479b)}")
badge_two_apps = warroom.autopilot_switch({}, "*", True, cfg=cfg479b)
chk("EU-479: cross-app badge shows '2' with '2 projects'",
    "2" in badge_two_apps and "2 projects" in badge_two_apps, repr(badge_two_apps))

# AC3: zero active runs → badge absent (even with fresh unclaimed audit activity present).
cockpit_state.reset_run_state()
chk("EU-479: zero active runs → badge absent",
    warroom.autopilot_switch({}, "*", True, cfg=cfg479b) == "")

# THE iter-1 gap: two apps each with an active same-app double-build, but render_board is only
# ever called for ONE of them (operator watching a different project). The on-demand total must
# report the true 4 — not the stale 2+floor(1)=3 a render-side-effect cache produced.
cockpit_state.reset_run_state()
cfg479c = _eu479_cfg([
    dict(event="ticket_start", ticket_id="W-1", app="watched", branch="b", ts=_ago(30)),
    dict(event="ticket_start", ticket_id="W-2", app="watched", branch="c", ts=_ago(20)),
    dict(event="build", ticket_id="W-2", app="watched", iteration=1, ts=_ago(10)),
    dict(event="ticket_start", ticket_id="O-1", app="other", branch="b", ts=_ago(28)),
    dict(event="ticket_start", ticket_id="O-2", app="other", branch="c", ts=_ago(18)),
    dict(event="build", ticket_id="O-2", app="other", iteration=1, ts=_ago(8)),
], apps=("watched", "other"))
cockpit_state.get_state("watched")["active"] = True
cockpit_state.get_state("other")["active"] = True
warroom.render_board(cfg479c, "watched", {"active": True})   # ONLY this tab is ever polled
chk("EU-479: unseen app's double-build counts too — on-demand total is 4",
    warroom.total_live_run_count(cfg479c) == 4,
    f"total={warroom.total_live_run_count(cfg479c)}")
badge_four = warroom.autopilot_switch({}, "watched", True, cfg=cfg479c)
chk("EU-479: badge shows '4' though only 'watched' board was ever rendered",
    "4" in badge_four and "4 projects" not in badge_four, repr(badge_four))
chk("EU-479: badge names the real project count ('2 projects')",
    "2 projects" in badge_four, repr(badge_four))

# cfg-less call sites degrade to the legacy distinct-project count (EU-103 behaviour).
cockpit_state.reset_run_state()
cockpit_state.get_state("solo")["active"] = True
chk("EU-479: cfg=None degrades to distinct-project count",
    warroom.total_live_run_count() == 1,
    f"total={warroom.total_live_run_count()}")

# A released run drops out of the on-demand total immediately (nothing cached to go stale).
cockpit_state.reset_run_state()
cockpit_state.get_state("watched")["active"] = True      # cfg479c's audit: 2 live tickets
chk("EU-479: released app's double-build counted while active",
    warroom.total_live_run_count(cfg479c) == 2,
    f"total={warroom.total_live_run_count(cfg479c)}")
cockpit_state.release_run("watched")
chk("EU-479: after release_run the total drops to 0",
    warroom.total_live_run_count(cfg479c) == 0,
    f"total={warroom.total_live_run_count(cfg479c)}")

cockpit_state.reset_run_state()

# --- the /api/autopilot drain/stop handlers (server-side) — now PER-APP (EU-103) ---
tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV", protected_branch="MAIN",
                             backlog_backend="none"),
                   AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev", protected_branch="main",
                             backlog_backend="none")],
             audit_path=str(tmp / "a.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
sync.can_promote = lambda: False
client = server.create_app(cfg).test_client()

# drain = graceful: signal THIS app's stop_event, but stay on + stopping so the current ticket
# finishes first. The handler resolves the project from the posted `app` and acts on its per-app
# run-state (get_state(app)) — never a single global autopilot.
cockpit_state.reset_run_state()
ev = threading.Event()
st = cockpit_state.get_state("automatixy")
st["active"] = True; st["autopilot_on"] = True; st["stop_event"] = ev
client.post("/api/autopilot", data={"action": "drain", "app": "automatixy"})
chk("drain sets the stop signal", ev.is_set())
_st = cockpit_state.get_autopilot_status("automatixy")
chk("drain keeps on=True + marks stopping (finish current ticket first)",
    _st["on"] is True and _st["stopping"] is True, str(_st))

# immediate stop = flip THIS app's autopilot off now (the in-flight build still finishes in the bg)
ev2 = threading.Event()
st2 = cockpit_state.get_state("automatixy")
st2["active"] = True; st2["autopilot_on"] = True; st2["stop_event"] = ev2
client.post("/api/autopilot", data={"action": "stop", "app": "automatixy"})
chk("immediate Stop flips on=False right away",
    cockpit_state.get_autopilot_status("automatixy")["on"] is False and ev2.is_set())

# per-app isolation: draining automatixy must NOT touch Elite-Unit's independent run.
cockpit_state.reset_run_state()
evA = threading.Event(); evE = threading.Event()
sA = cockpit_state.get_state("automatixy"); sA["active"] = True; sA["autopilot_on"] = True; sA["stop_event"] = evA
sE = cockpit_state.get_state("Elite-Unit"); sE["active"] = True; sE["autopilot_on"] = True; sE["stop_event"] = evE
client.post("/api/autopilot", data={"action": "stop", "app": "automatixy"})
chk("stopping automatixy leaves Elite-Unit running (per-app isolation)",
    cockpit_state.get_autopilot_status("Elite-Unit")["on"] is True
    and not evE.is_set() and evA.is_set(),
    f"A={cockpit_state.get_autopilot_status('automatixy')} E={cockpit_state.get_autopilot_status('Elite-Unit')}")
cockpit_state.reset_run_state()

print("\n============ AUTOPILOT GRACEFUL-STOP QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
