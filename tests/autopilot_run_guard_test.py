"""EU-49 (F7): Autopilot Start + the Telegram/answer resume threads must respect the F13 run-guard.

Three paths used to start a run/loop without taking ``_run_lock`` / setting ``_state["active"]``:
the autopilot Start handler, ``decisions._run_bg`` (Telegram/answer resume), and (via it) the answer
box. Two consequences this harness pins down:

  1. A double "Start autopilot" (Flask is threaded=True) used to overwrite ``_state["autopilot"]`` and
     ORPHAN the first loop's stop Event — "Stop" in the War Room then silently did nothing. Start must
     now be idempotent: exactly one loop starts and the stored stop Event is the running one's.
  2. While autopilot (or any run) holds the guard, a manual cockpit run and a Telegram /run|/drain must
     REFUSE rather than start an overlapping run_loop — but a decision RESUME stays exempt (it is
     flock-safe and its pending decision was already consumed, so it must not be dropped).
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

# Slow health check widens the pre-claim window so all the concurrent Starts pass the top-level
# `not on` gate before any one claims the lock — i.e. it forces the real TOCTOU race the guard closes.
def slow_healthy(c):
    time.sleep(0.05)
    return {"healthy": True, "checks": []}
srv.health.summary = slow_healthy

# --- stub the autopilot loop: record each loop that actually starts, and hold it open so the
#     guard (_state["active"]) stays set while the concurrent Start requests are deciding. ---
starts = []
starts_lock = threading.Lock()
release = threading.Event()
async def fake_autopilot(cfg, app_name=None, once=False, interval=60, stop_event=None):
    with starts_lock:
        starts.append(stop_event)
    release.wait(3)
ap_mod.autopilot = fake_autopilot

app = srv.create_app(cfg)

# Fire N near-simultaneous "Start autopilot" POSTs from separate threads.
N = 6
barrier = threading.Barrier(N)
def fire(i):
    c = app.test_client()
    barrier.wait()
    c.post("/api/autopilot", data={"action": "start"})
threads = [threading.Thread(target=fire, args=(i,)) for i in range(N)]
for t in threads: t.start()
for t in threads: t.join(5)

for _ in range(60):      # let the single accepted loop enter fake_autopilot
    if starts:
        break
    time.sleep(0.05)

chk("a double Start starts EXACTLY one autopilot loop", len(starts) == 1, f"starts={len(starts)}")
chk("autopilot holds the run-guard (_state['active'])", srv._state["active"] is True)
stored = (srv._state.get("autopilot") or {}).get("stop")
chk("the stored stop Event is the RUNNING loop's (not orphaned)", starts and stored is starts[0])
_msg = srv._state.get("last_msg") or ""
chk("the extra Starts are refused with feedback",
    "already running" in _msg or "already in progress" in _msg, _msg)

# A manual cockpit run must refuse while autopilot holds the guard.
srv._state["last_msg"] = ""
app.test_client().post("/api/run", data={"kind": "task", "text": "overlap"})
chk("a manual cockpit run refuses while autopilot is live",
    "already in progress" in (srv._state.get("last_msg") or ""), srv._state.get("last_msg"))

# --- decisions._run_bg: the guard, with the resume exemption ---
decisions.notify.send = lambda *a, **k: None
ran = []
async def fake_run_loop(cfg, worklist, audit):
    ran.append(1)
import orchestrator.loop as loop_mod
loop_mod.run = fake_run_loop

# refuse_if_busy=True (Telegram /run|/drain) is refused while the guard is held.
started = decisions._run_bg(cfg, None, ["wl"], refuse_if_busy=True)
chk("Telegram /run|/drain refuses while a run/autopilot is active", started is False)

# refuse_if_busy=False (decision resume) proceeds anyway — flock-safe, must not drop the answer.
started_resume = decisions._run_bg(cfg, None, ["wl"], refuse_if_busy=False)
chk("a decision resume still runs while autopilot is live", started_resume is True)

# Release autopilot; the guard must clear when the loop exits.
release.set()
for _ in range(60):
    if srv._state["active"] is False:
        break
    time.sleep(0.05)
chk("the run-guard clears once autopilot stands down", srv._state["active"] is False)

# With the guard free, even a /run|/drain starts and owns+clears the flag.
started_free = decisions._run_bg(cfg, None, ["wl"], refuse_if_busy=True)
chk("/run|/drain starts when the guard is free", started_free is True)
for _ in range(60):
    if srv._state["active"] is False:
        break
    time.sleep(0.05)
chk("_run_bg releases the guard it claimed", srv._state["active"] is False)

print("\n============ AUTOPILOT / RESUME RUN-GUARD QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
