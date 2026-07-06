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
