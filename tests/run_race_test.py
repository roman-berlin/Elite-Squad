"""EU-13 (F13): the cockpit active check-then-set is a TOCTOU race.

Two rapid run POSTs must start EXACTLY one run — a threading.Lock makes the
`if _state["active"]` check-and-set atomic so two near-simultaneous requests can't
both pass the guard and launch two run_loops.
"""
import sys, types, tempfile, threading, time
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.server as srv
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

d = Path(tempfile.mkdtemp()); (d / "audit.jsonl").write_text("")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)

# --- stubs: healthy; count how many run_loops actually start; hold each "run" open so the
#     active flag stays set while the other concurrent requests are deciding. ---
starts = []
release = threading.Event()
starts_lock = threading.Lock()
async def fake_run_loop(rcfg, worklist, audit, stop_event=None):
    with starts_lock:
        starts.append(1)
    release.wait(3)            # keep _state["active"] True for the duration of the race

srv.health.summary = lambda c: {"healthy": True, "checks": []}
# widen the pre-spawn window so that WITHOUT the lock every concurrent request would slip
# past the guard before any background thread sets active.
def slow_from_text(*a, **k):
    time.sleep(0.05)
    return ["wl"]
srv.intake.from_text = slow_from_text
srv.run_loop = fake_run_loop

app = srv.create_app(cfg)

# Fire N near-simultaneous POSTs to /api/run from separate threads.
N = 6
barrier = threading.Barrier(N)
def fire(i):
    c = app.test_client()
    barrier.wait()            # release all requests at once
    c.post("/api/run", data={"kind": "task", "text": f"do {i}"})

threads = [threading.Thread(target=fire, args=(i,)) for i in range(N)]
for t in threads: t.start()
for t in threads: t.join(5)

# Give the single accepted run a moment to enter run_loop, then let it finish.
time.sleep(0.2)
release.set()
for _ in range(60):
    if not srv._state["active"]:
        break
    time.sleep(0.05)

chk("two+ rapid run POSTs start EXACTLY one run", len(starts) == 1, f"starts={len(starts)}")
chk("active flag cleared after the run ends", srv._state["active"] is False, str(srv._state.get("active")))

# A request that arrives while a run is active is rejected WITH feedback (deterministic).
srv._state["active"] = True
try:
    app.test_client().post("/api/run", data={"kind": "task", "text": "busy"})
    chk("busy request gets feedback", "already in progress" in (srv._state.get("last_msg") or ""),
        srv._state.get("last_msg"))
finally:
    srv._state["active"] = False

print("\n================ COCKPIT RUN-RACE QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
