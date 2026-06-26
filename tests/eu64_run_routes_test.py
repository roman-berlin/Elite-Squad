"""EU-64: the cockpit run routes + Stop are keyed PER-PROJECT (retire the single global run-lock).

Pins the behaviour of slice 2 (the run routes + SSE) on top of slice 1's per-app state helpers:

  1. Two DIFFERENT projects run truly in parallel — starting beta does NOT wait on alpha's run.
  2. A second run on the SAME project is still refused (F7 idempotent-start, applied per project),
     and the refusal lands a per-project banner on that app's own state.
  3. Stop targets ONLY the named project — alpha's stop Event fires, beta's is untouched.
  4. Each project releases its own slot when its run ends.
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
from orchestrator import cockpit_state
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

d = Path(tempfile.mkdtemp()); (d / "audit.jsonl").write_text("")
def _app(name):
    return AppConfig(name=name, repo_path=str(d), base_branch="DEV",
                     protected_branch="MAIN", backlog_backend="none")
cfg = Config(apps=[_app("alpha"), _app("beta")], audit_path=str(d / "audit.jsonl"), use_worktree=False)

cockpit_state.reset_run_state()
cockpit_state.set_max_parallel_runs(0)   # unlimited — this test is about isolation, not the cap

# --- stub: healthy; a run_loop that BLOCKS until released so both projects stay live at once. ---
started = []
started_lock = threading.Lock()
release = threading.Event()
async def fake_run_loop(rcfg, worklist, audit, stop_event=None):
    with started_lock:
        started.append(worklist)
    release.wait(3)
srv.health.summary = lambda c: {"healthy": True, "checks": []}
srv.intake.from_text = lambda rcfg, app, *a, **k: [f"wl:{app}"]
srv.run_loop = fake_run_loop

app = srv.create_app(cfg)

# 1) two DIFFERENT projects start in parallel (separate clients → separate sessions/tabs).
ca, cb = app.test_client(), app.test_client()
ca.post("/api/run", data={"kind": "task", "text": "a", "app": "alpha"})
cb.post("/api/run", data={"kind": "task", "text": "b", "app": "beta"})
for _ in range(60):
    if len(started) >= 2:
        break
    time.sleep(0.05)
chk("two different projects run in parallel", len(started) == 2, f"started={started}")
chk("alpha run is active", cockpit_state.is_active("alpha"))
chk("beta run is active", cockpit_state.is_active("beta"))

# 2) a SECOND run on the SAME project (alpha) is refused while alpha is live; beta unaffected.
n_before = len(started)
ca.post("/api/run", data={"kind": "task", "text": "a2", "app": "alpha"})
time.sleep(0.2)
chk("second run on the same project is refused", len(started) == n_before, f"started={len(started)}")
st_alpha = cockpit_state.get_state("alpha")
chk("refusal sets a per-project banner",
    "already in progress for this project" in (st_alpha.get("last_msg") or ""), st_alpha.get("last_msg"))

# 3) Stop targets ONLY the named project.
ca.post("/api/stop-run", data={"app": "alpha"})
chk("alpha's stop Event was signalled", cockpit_state.get_state("alpha")["stop_event"].is_set())
chk("beta's stop Event was NOT touched", not cockpit_state.get_state("beta")["stop_event"].is_set())

# 4) each project releases its own slot when its run ends.
release.set()
for _ in range(60):
    if not (cockpit_state.is_active("alpha") or cockpit_state.is_active("beta")):
        break
    time.sleep(0.05)
chk("both projects release their slots when done",
    not cockpit_state.is_active("alpha") and not cockpit_state.is_active("beta"))

print("\n================ EU-64 PER-PROJECT RUN ROUTES QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
