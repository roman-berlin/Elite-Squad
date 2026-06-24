"""Graceful-stop QA: the cockpit offers 'Finish & stop' (let the in-flight ticket land on DEV, then
stand down — take no new tickets) alongside the immediate 'Stop'. The drain action sets the stop signal
but keeps the worker 'on' + 'stopping' so the UI shows it's finishing the current ticket; the worker's
own exit clears it. (max_tickets_per_run=1, and the stop is checked between tickets, so a build is never
killed mid-flight.)"""
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

from orchestrator import warroom, server, sync
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- autopilot_switch: the three states ---
on = warroom.autopilot_switch({"autopilot": {"on": True, "app": "all projects"}}, "*", True)
chk("ON offers both 'Finish & stop' (drain) and 'Stop'", "value=drain" in on and "value=stop" in on and "Finish" in on)
stopping = warroom.autopilot_switch({"autopilot": {"on": True, "stopping": True, "app": "x"}}, "*", True)
chk("stopping shows the finishing-current-ticket state", "Stopping" in stopping and "finishing the current ticket" in stopping)
chk("stopping hides the action buttons (already winding down)", "value=drain" not in stopping and "value=stop" not in stopping)
off = warroom.autopilot_switch({"autopilot": {"on": False}}, "*", True)
chk("off offers Start", "value=start" in off and "Start" in off)

# --- the /api/autopilot drain handler ---
tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV", protected_branch="MAIN",
                             backlog_backend="none")], audit_path=str(tmp / "a.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
sync.can_promote = lambda: False
client = server.create_app(cfg).test_client()

# drain = graceful: signal stop, but stay on + stopping so the current ticket finishes first
ev = threading.Event()
server._state["autopilot"] = {"on": True, "stop": ev, "app": "all projects", "stopping": False}
client.post("/api/autopilot", data={"action": "drain"})
chk("drain sets the stop signal", ev.is_set())
chk("drain keeps on=True + marks stopping (finish current ticket first)",
    server._state["autopilot"]["on"] is True and server._state["autopilot"]["stopping"] is True)

# immediate stop = flip off now
ev2 = threading.Event()
server._state["autopilot"] = {"on": True, "stop": ev2, "app": "x", "stopping": False}
client.post("/api/autopilot", data={"action": "stop"})
chk("immediate Stop flips on=False right away", server._state["autopilot"]["on"] is False and ev2.is_set())

print("\n============ AUTOPILOT GRACEFUL-STOP QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
