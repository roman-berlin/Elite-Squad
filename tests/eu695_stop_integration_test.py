"""EU-695: Verify & close — Fix multi-target stop resolution and wire stop form to a specific run.

Integration regression covering all four ORIGINAL acceptance criteria in one run:
  AC1  With 2+ concurrent runs active, clicking Stop on card A stops ONLY A's stop_event.
  AC2  The stop form always POSTs `app` and `ticket` identifying its own card's run.
  AC3  Posting a stop for a run with no live stop_event shows a visible on-page message.
  AC4  Single-run stop behavior keeps working alongside multi-run (regression coverage).
  AC5  A garbage/unknown `app` does NOT grow the state/lock registries with an orphan entry
       (iteration-2 review: the no-live-run branch must use resolved `key`, not raw posted_app).

This is the LAST piece of the EU-686 epic — runs after siblings EU-692 (form wiring) and
EU-693 (route lookup) have landed.
"""
import sys, types, threading, time
from pathlib import Path
import tempfile

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.server as srv
from orchestrator import cockpit_state
from orchestrator.cockpit_state import get_state
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), str(d)))


# ── Fixture helpers ────────────────────────────────────────────────────────────

started = []
start_lock = threading.Lock()
release = threading.Event()

async def fake_run_loop(rcfg, worklist, audit, stop_event=None):
    with start_lock:
        started.append(worklist)
    release.wait(30)  # safety timeout only
srv.health.summary = lambda c: {"healthy": True, "checks": []}
srv.intake.from_tickets = lambda rcfg, app, keys: [f"wl:{app}:{len(keys)}"]
srv.run_loop = fake_run_loop

def _mkcfg():
    d = Path(tempfile.mkdtemp())
    (d / "audit.jsonl").write_text("")
    return Config(
        apps=[AppConfig(name="alpha", repo_path=str(d), base_branch="DEV",
                        protected_branch="MAIN", backlog_backend="none"),
              AppConfig(name="beta", repo_path=str(d), base_branch="DEV",
                        protected_branch="MAIN", backlog_backend="none")],
        audit_path=str(d / "audit.jsonl"),
        use_worktree=False,
    )

def _start(app, tickets, client):
    client.post("/api/run-selected", data={"app": app, "ticket": tickets})

def _wait_started(n, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with start_lock:
            if len(started) >= n:
                return
        time.sleep(0.05)

def _ev(app):
    return cockpit_state.get_state(app).get("stop_event")

def _drain_and_reset():
    global release
    release.set()
    for _ in range(100):
        if not any(cockpit_state.is_active(a) for a in ("alpha", "beta")):
            break
        time.sleep(0.05)
    cockpit_state.reset_run_state()
    started.clear()
    release = threading.Event()


# ═══════════════════════════════════════════════════════════════════════════════
# AC1: Two concurrent runs → Stop on A does NOT stop B
# ═══════════════════════════════════════════════════════════════════════════════

cockpit_state.reset_run_state()
cockpit_state.set_max_parallel_runs(0)  # unlimited concurrent
client1 = srv.create_app(_mkcfg()).test_client()

_start("alpha", ["EU-1"], client1)
_start("beta", ["EU-7"], client1)
_wait_started(2)

chk("AC1a: two concurrent runs active",
    cockpit_state.is_active("alpha") and cockpit_state.is_active("beta"))
chk("AC1b: distinct stop_events",
    _ev("alpha") is not None and _ev("beta") is not None
    and _ev("alpha") is not _ev("beta"))

# POST stop for alpha via its exact app+ticket (simulates clicking Stop on card A)
r = client1.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-1"})
chk("AC1c: stop(alpha) redirect succeeds", r.status_code == 302)
chk("AC1d: alpha's event IS set", _ev("alpha").is_set())
chk("AC1e: beta's event is NOT set (never touched)", not _ev("beta").is_set())
_drain_and_reset()


# ═══════════════════════════════════════════════════════════════════════════════
# AC2: The stop form posts app+tktidentifying its own card's run
# (End-to-end: verify the server receives and honours them)
# ═══════════════════════════════════════════════════════════════════════════════

cockpit_state.reset_run_state()
client2 = srv.create_app(_mkcfg()).test_client()

_start("alpha", ["EU-420"], client2)
_wait_started(1)

# POST with app AND ticket — must resolve to the correct run's event
client2.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-420"})
chk("AC2a: posted app resolves correctly", _ev("alpha").is_set())

# Reset and test cross-app: posting wrong app should do nothing
_drain_and_reset()
_start("alpha", ["EU-500"], client2)
_start("beta", ["EU-600"], client2)
_wait_started(2)

# POST from a hypothetical "stale" form that mixed up values
client2.post("/api/stop-run", data={"app": "beta", "ticket": "EU-500"})
chk("AC2b: cross-app+ticket mix-up stops NEITHER",
    not _ev("beta").is_set() and not _ev("alpha").is_set(),
    "must be silent-no-op for stale forms")

_drain_and_reset()


# ═══════════════════════════════════════════════════════════════════════════════
# AC3: Posting a stop when NO live stop_event exists → visible on-page message
# ═══════════════════════════════════════════════════════════════════════════════

cockpit_state.reset_run_state()
client3 = srv.create_app(_mkcfg()).test_client()

# Post a stop for an app with NO live run at all
r = client3.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-999"})
chk("AC3a: redirect still happens", r.status_code == 302)

# BUT the warning message was set in per-app state so it displays on the next GET /
# Note: set_last_msg("alpha", ...) writes to get_state("alpha"), NOT the global _state.
msg = get_state("alpha").get("last_msg") or ""
chk("AC3b: visible warning message present",
    msg != "",
    f"expected a warning message, got last_msg={msg!r}")
chk("AC3c: message is meaningful to user",
    "No active run found to stop" in msg,   # EU-706 wording (supersedes EU-695's)
    f"message content: {msg!r}")

# Also test the stale-ticket path: start a run, let it finish, POST old ticket
_drain_and_reset()
_start("alpha", ["EU-10"], client3)
_wait_started(1)
# Simulate the run finishing quickly by setting the event ourselves
cockpit_state.get_state("alpha")["stop_event"].set()
_drain_and_reset()

# After drain, try stopping with the OLD ticket on the fresh app
# (the app slot is empty, no stop_event exists)
cockpit_state.reset_run_state()
cockpit_state.set_max_parallel_runs(0)
client3.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-old"})
msg_stale = get_state("alpha").get("last_msg") or ""
chk("AC3d: stale ticket also sets visible message",
    "No active run found to stop" in msg_stale,   # EU-706 wording (supersedes EU-695's)
    f"message content: {msg_stale!r}")


# ═══════════════════════════════════════════════════════════════════════════════
# AC4: Regression — single-run stop still works alongside multi-run support
# ═══════════════════════════════════════════════════════════════════════════════

cockpit_state.reset_run_state()
client4 = srv.create_app(_mkcfg()).test_client()

# --- Single-run path (today's only tested path pre-EU-692/693) ---
_start("alpha", ["EU-single"], client4)
_wait_started(1)
chk("AC4a: single run active", cockpit_state.is_active("alpha"))

r = client4.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-single"})
chk("AC4b: single-run stop succeeds", r.status_code == 302)
chk("AC4c: single-run event IS set", _ev("alpha").is_set())

# --- Multi-run path side-by-side with single ---
_drain_and_reset()

_start("alpha", ["EU-300"], client4)
_start("beta", ["EU-301"], client4)
_wait_started(2)

# Stop beta specifically
client4.post("/api/stop-run", data={"app": "beta", "ticket": "EU-301"})
chk("AC4d: multi-run stop(beta) works", _ev("beta").is_set())
chk("AC4e: multi-run stop(beta) doesn't touch alpha", not _ev("alpha").is_set())

# Then stop alpha
client4.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-300"})
chk("AC4f: second stop(alpha) works too", _ev("alpha").is_set())

_drain_and_reset()

# Legacy empty POST (no app/ticket) with ONE run still works
_start("alpha", ["EU-leg"], client4)
_wait_started(1)
r = client4.post("/api/stop-run", data={})
chk("AC4g: legacy empty POST stops sole run", r.status_code == 302)
chk("AC4h: legacy event IS set", _ev("alpha").is_set())

_drain_and_reset()


# ═══════════════════════════════════════════════════════════════════════════════
# AC5: Regression — a garbage/unknown `app` must NOT grow the state/lock registries
#      (iteration-2 review: the no-live-run else-branch must call set_last_msg with the
#      already-resolved `key`, not the raw posted_app — otherwise set_last_msg → get_state
#      lazily creates a PERMANENT orphan entry in the registry for an app that doesn't exist)
# ═══════════════════════════════════════════════════════════════════════════════

cockpit_state.reset_run_state()
client5 = srv.create_app(_mkcfg()).test_client()

# reset_run_state leaves only the None default in both registries — snapshot the keys so we can
# prove the garbage POST does not add any.
states_before = set(cockpit_state._states)
locks_before = set(cockpit_state._run_locks)

GARBAGE = "nonexistent-app"   # NOT in the configured app list (alpha, beta)

r = client5.post("/api/stop-run", data={"app": GARBAGE, "ticket": "EU-1"})
chk("AC5a: garbage-app stop still redirects", r.status_code == 302)

chk("AC5b: no orphan entry in the STATE registry",
    GARBAGE not in cockpit_state._states,
    f"states keys={sorted(map(str, cockpit_state._states))}")
chk("AC5c: no orphan entry in the LOCK registry",
    GARBAGE not in cockpit_state._run_locks,
    f"locks keys={sorted(map(str, cockpit_state._run_locks))}")
chk("AC5d: state registry did not grow at all",
    set(cockpit_state._states) == states_before,
    f"before={sorted(map(str, states_before))} after={sorted(map(str, cockpit_state._states))}")
chk("AC5e: lock registry did not grow at all",
    set(cockpit_state._run_locks) == locks_before,
    f"before={sorted(map(str, locks_before))} after={sorted(map(str, cockpit_state._run_locks))}")

# The user still gets a visible message — but on the GLOBAL/default board (key=None), never on a
# lazily-created orphan tab. (Against the pre-fix code this message landed on the orphan instead.)
msg_garbage = get_state(None).get("last_msg") or ""
chk("AC5f: message still surfaced on the default board",
    "No active run found to stop" in msg_garbage,   # EU-706 wording (supersedes EU-695's)
    f"global last_msg={msg_garbage!r}")


# ═══════════════════════════════════════════════════════════════════════════════
# Report
# ═══════════════════════════════════════════════════════════════════════════════

passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"\n{'=' * 60}")
print("  EU-695: Multi-target stop resolution — INTEGRATION TEST")
print(f"  {'─' * 60}")
for name, ok, det in results:
    mark = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"    [{mark}] {name}{extra}")
print(f"  {'─' * 60}")
print(f"  {passed}/{total} checks passed")
failed = total - passed
if failed:
    print(f"  RESULT: {failed} FAILED")
    sys.exit(1)
else:
    print("  RESULT: ALL GREEN")
    sys.exit(0)
