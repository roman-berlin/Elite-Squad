"""EU-49 (F7) regression — pin the two sharp edges the run-guard fix closes that the happy-path
harness (``autopilot_run_guard_test.py``) doesn't isolate:

  1. The ``action != "start"`` guard in the autopilot toggle handler. BEFORE the fix a redundant
     "Start" while autopilot was live could fall through to the immediate-stop ``elif`` and call
     ``cur["stop"].set()`` — silently STOPPING the running loop (or, with the old overwrite, orphaning
     its Event so "Stop" did nothing). This harness fires a SEQUENTIAL second Start at an already-live
     autopilot and asserts the running loop's stop Event is NOT set and the stored Event is unchanged.
     Remove ``action != "start"`` (or the idempotency guard) and check #2/#3 below go red.

  2. The decision-resume exemption invoked the way production actually calls it: ``handle_reply`` at
     ``decisions.py:143`` calls ``_run_bg(cfg, audit, worklist)`` with NO keyword — i.e. the DEFAULT
     ``refuse_if_busy=False``. That default must stay exempt: a resume started while the guard is held
     must still run (its pending decision was already consumed; dropping it loses the Commander's answer).
"""
import sys, types, tempfile, threading, time
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

import orchestrator.server as srv
import orchestrator.autopilot as ap_mod
import orchestrator.decisions as decisions
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

d = Path(tempfile.mkdtemp()); (d / "audit.jsonl").write_text("")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)
srv.health.summary = lambda c: {"healthy": True, "checks": []}

release = threading.Event()
started_evs = []
async def fake_autopilot(cfg, app_name=None, once=False, interval=60, stop_event=None):
    started_evs.append(stop_event)
    release.wait(3)
ap_mod.autopilot = fake_autopilot

app = srv.create_app(cfg)

# --- Start ONE autopilot loop and let it enter the (held-open) fake loop. ---
app.test_client().post("/api/autopilot", data={"action": "start"})
for _ in range(60):
    if started_evs:
        break
    time.sleep(0.05)
chk("a single Start launches the loop", len(started_evs) == 1, f"started={len(started_evs)}")
# EU-103: a no-app Start targets the unit-wide None key (== the default _state). The stop Event and
# the autopilot-on signal now live in the per-app run-state, not the retired _state["autopilot"] dict.
running_ev = srv.get_state(None).get("stop_event")
chk("the running loop's stop Event is stored", running_ev is started_evs[0])
chk("the running loop's stop Event is NOT set yet", running_ev is not None and not running_ev.is_set())

# --- THE REGRESSION: a sequential, redundant second "Start" while live. ---
# A sequential redundant Start is caught by `want_on and not cur.get('on')` (False, already on) and
# is a harmless silent no-op — it never reaches the in-lock "already running" branch (that fires only
# in the concurrent race, covered by autopilot_run_guard_test.py). The contract here is idempotency =
# DO NO HARM: it must not stop the loop, orphan/overwrite the Event, or spawn a second loop.
srv._state["last_msg"] = ""
app.test_client().post("/api/autopilot", data={"action": "start"})
chk("the redundant Start is a harmless no-op (no error banner)",
    "error" not in (srv._state.get("last_msg") or "").lower(), srv._state.get("last_msg"))
chk("the redundant Start did NOT set the running loop's stop Event (no silent stop)",
    not running_ev.is_set())
chk("the stored stop Event is still the original running one (not orphaned/overwritten)",
    srv.get_state(None).get("stop_event") is running_ev)
chk("autopilot is still marked on after the redundant Start",
    srv.get_autopilot_status(None)["on"] is True)
chk("no second loop was launched", len(started_evs) == 1, f"started={len(started_evs)}")

# --- #2: the production resume call shape — _run_bg with the DEFAULT arg — stays exempt. ---
decisions.notify.send = lambda *a, **k: None
ran = []
async def fake_run_loop(cfg, worklist, audit):
    ran.append(1)
import orchestrator.loop as loop_mod
loop_mod.run = fake_run_loop

chk("the guard is held by live autopilot before the resume", srv._state["active"] is True)
started_default = decisions._run_bg(cfg, None, ["wl"])     # <-- exactly how handle_reply calls it
chk("a resume via the DEFAULT _run_bg call still runs while the guard is held",
    started_default is True, f"returned {started_default!r}")

# --- release & confirm the genuine stop path still works (a real toggle-off DOES set the Event). ---
real_stop_ev = srv.get_state(None).get("stop_event")
app.test_client().post("/api/autopilot", data={"action": "stop"})
chk("a genuine stop DOES set the running loop's stop Event", real_stop_ev.is_set())
release.set()
for _ in range(60):
    if srv._state["active"] is False:
        break
    time.sleep(0.05)
chk("the guard clears once the loop stands down", srv._state["active"] is False)

print("\n============ EU-49 STOP-BRANCH / RESUME-DEFAULT REGRESSION ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
