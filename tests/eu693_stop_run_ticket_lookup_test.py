"""EU-693: /api/stop-run resolves by EXACT app+ticket, not the single-run heuristic.

Checks:
  AC1  two concurrent run-selected runs on DIFFERENT apps with distinct tickets + distinct
       stop_events — POST stop(app=A, ticket=A) sets ONLY A's event; B is untouched.
       Also: a multi-ticket run is stoppable by its SECOND ticket, and a stale/mismatched
       ticket stops nothing (no cross-run ticket search that could kill the wrong run).
  AC2  POST with NO app/ticket (legacy) keeps the EXACT pre-EU-693 resolution: the active
       tab's run is stopped first, and the "exactly one stoppable run" heuristic only fires
       when that tab holds no run.
  AC3  regression coverage: app-only stop keeps working, and a REFUSED run-selected claim
       (same app already running) returns a redirect instead of crashing (the iteration-1
       gate failure in eu646_per_app_message_test.py was a NoneType assignment before the
       claim's None check).
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
cfg = Config(apps=[_app("alpha"), _app("beta"), _app("gamma")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)

cockpit_state.reset_run_state()
cockpit_state.set_max_parallel_runs(0)   # unlimited — we WANT concurrent runs

# --- stubs: healthy gate; a run_loop that BLOCKS until released so runs stay live at once. ---
started = []
started_lock = threading.Lock()
release = threading.Event()
async def fake_run_loop(rcfg, worklist, audit, stop_event=None):
    with started_lock:
        started.append(worklist)
    release.wait(20)   # safety timeout only — every phase drains via release.set()
srv.health.summary = lambda c: {"healthy": True, "checks": []}
srv.intake.from_tickets = lambda rcfg, app, keys: [f"wl:{app}:{len(keys)}"]
srv.run_loop = fake_run_loop

client = srv.create_app(cfg).test_client()

def _start(app, tickets):
    client.post("/api/run-selected", data={"app": app, "ticket": tickets})

def _wait_started(n):
    for _ in range(100):
        with started_lock:
            if len(started) >= n:
                return
        time.sleep(0.05)

def _ev(app):
    return cockpit_state.get_state(app).get("stop_event")

def _drain_and_reset():
    global release
    release.set()
    for _ in range(100):
        if not any(cockpit_state.is_active(a) for a in ("alpha", "beta", "gamma")):
            break
        time.sleep(0.05)
    cockpit_state.reset_run_state()
    started.clear()
    release = threading.Event()


# ═══════════════════════════════════════════════════════════════════════════════════
# AC1: two concurrent runs, distinct stop_events — stop by exact app+ticket hits ONLY
# that run. Stale / cross-app tickets stop nothing.
# ═══════════════════════════════════════════════════════════════════════════════════
_start("alpha", ["EU-1", "EU-2"])     # multi-ticket worklist
_start("beta", ["EU-7"])
_wait_started(2)
chk("AC1a: two concurrent runs active",
    cockpit_state.is_active("alpha") and cockpit_state.is_active("beta"))
chk("AC1b: the two runs hold DISTINCT stop_events",
    _ev("alpha") is not None and _ev("beta") is not None and _ev("alpha") is not _ev("beta"))

# Stale form: beta's card carrying ALPHA's ticket must not stop anything.
client.post("/api/stop-run", data={"app": "beta", "ticket": "EU-1"})
chk("AC1c: cross-app ticket on beta's form stops NEITHER run",
    not _ev("beta").is_set() and not _ev("alpha").is_set(),
    "a stale ticket must not hunt down another run's event")

# Stale form: ticket that belongs to no run at all.
client.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-999"})
chk("AC1d: unknown ticket on alpha's form stops nothing", not _ev("alpha").is_set())

# The exact key: alpha's run claimed EU-2 as its second ticket — stop by it.
client.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-2"})
chk("AC1e: stop(app=alpha, ticket=EU-2) sets ONLY alpha's event",
    _ev("alpha").is_set() and not _ev("beta").is_set())
_drain_and_reset()


# ═══════════════════════════════════════════════════════════════════════════════════
# AC2: legacy POST with NO app/ticket → "exactly one stoppable run" heuristic, unchanged.
# ═══════════════════════════════════════════════════════════════════════════════════
_start("gamma", ["EU-5"])
_wait_started(1)
client.post("/api/stop-run", data={})
chk("AC2a: legacy empty POST stops the SOLE stoppable run",
    _ev("gamma") is not None and _ev("gamma").is_set())
_drain_and_reset()

# With TWO stoppable runs the pre-EU-693 handler resolved the active tab FIRST (the
# exactly-one heuristic only fired when that tab held no run) — EU-693 keeps that exact
# behavior. Every run POST focuses the tab it names (_scope), so starting alpha LAST makes
# it the active tab.
_start("beta", ["EU-7"])
_start("alpha", ["EU-1"])
_wait_started(2)
client.post("/api/stop-run", data={})
chk("AC2b: legacy empty POST with two stoppable runs still resolves the active tab (alpha)",
    _ev("alpha").is_set() and not _ev("beta").is_set(),
    "pre-EU-693 semantics: active tab first, heuristic only when that tab has no run")

# App-only POST (pre-EU-693 cockpit path) still resolves and stops that app's run.
client.post("/api/stop-run", data={"app": "beta"})
chk("AC2c: app-only POST stops beta too", _ev("beta").is_set())
_drain_and_reset()


# ═══════════════════════════════════════════════════════════════════════════════════
# AC3: refusal guard — a run-selected claim on an already-running app must redirect,
# not crash (iteration 1 wrote to the None claim result → TypeError → eu646 gate red).
# ═══════════════════════════════════════════════════════════════════════════════════
_start("alpha", ["EU-1"])
_wait_started(1)
r2 = client.post("/api/run-selected", data={"app": "alpha", "ticket": ["EU-2"]})
chk("AC3a: refused duplicate run-selected claim returns a redirect (no crash)",
    r2.status_code == 302)
chk("AC3b: the original run still owns the slot, unstopped",
    cockpit_state.is_active("alpha") and not _ev("alpha").is_set())
client.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-1"})
chk("AC3c: the original run is still stoppable by its exact app+ticket", _ev("alpha").is_set())
_drain_and_reset()


print("\n================ EU-693 /api/stop-run LOOKUP QA ===================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
