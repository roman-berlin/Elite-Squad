"""EU-706: visible message when /api/stop-run finds no stop_event.

Acceptance criteria:
  AC1  Posting a stop for an app/ticket with no active stop_event redirects back to
       the cockpit with a visible message (e.g. "No active run found to stop for {app}/{ticket}")
       instead of a silent redirect.
  AC2  Existing successful-stop redirect behavior is unchanged.

The handler (server.py:2073-2078) currently writes:
    "No live run to stop — that request may have arrived after the run already finished."
which contains no app/ticket identifiers.  EU-706 replaces it with:
    "No active run found to stop for {app}/{ticket}"
(and a variant without ticket when only app is posted).
"""
import sys, types, threading, time
from pathlib import Path
import tempfile

# ── Stub claude_agent_sdk (matches the pattern used by sibling tests) ─────────
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


# ── Fixture helpers ────────────────────────────────────────────────────────

started = []
start_lock = threading.Lock()
release = threading.Event()

async def fake_run_loop(rcfg, worklist, audit, stop_event=None):
    with start_lock:
        started.append(worklist)
    release.wait(30)  # safety timeout only


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


# ════════════════════════════════════════════════════════════════════════════
# AC1: POST for app/ticket with NO active stop_event → visible message
# ════════════════════════════════════════════════════════════════════════════

srv.health.summary = lambda c: {"healthy": True, "checks": []}
srv.intake.from_tickets = lambda rcfg, app, keys: [f"wl:{app}:{len(keys)}"]
srv.run_loop = fake_run_loop

cockpit_state.reset_run_state()
cockpit_state.set_max_parallel_runs(0)
client = srv.create_app(_mkcfg()).test_client()

# Case A: app+ticket — never ran
r = client.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-999"})
chk("AC1a: redirect happens", r.status_code == 302)
msg = get_state("alpha").get("last_msg") or ""
chk("AC1b: visible message present (alpha/EU-999)", bool(msg))
chk("AC1c: 'active run found' phrase", "active run found" in msg, f"got: {msg!r}")

# Case B: known app, never started
_drain_and_reset()
_start("alpha", ["EU-1"], client)
_wait_started(1)
cockpit_state.get_state("alpha")["stop_event"].set()  # simulate run finishing
_drain_and_reset()

r = client.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-old"})
chk("AC1d: redirect after finish", r.status_code == 302)
msg2 = get_state("alpha").get("last_msg") or ""
chk("AC1e: 'active run found' after finish", "active run found" in msg2, f"got: {msg2!r}")

# Case C: garbage app + ticket
cockpit_state.reset_run_state()
cockpit_state.set_max_parallel_runs(0)
r = client.post("/api/stop-run", data={"app": "nonexistent", "ticket": "EU-z"})
chk("AC1f: redirect for garbage app", r.status_code == 302)
msg3 = get_state(None).get("last_msg") or ""
chk("AC1g: message for garbage app", "active run found" in msg3, f"got: {msg3!r}")


# ════════════════════════════════════════════════════════════════════════════
# AC2: successful-stop redirect behaviour unchanged
# ════════════════════════════════════════════════════════════════════════════

cockpit_state.reset_run_state()
cockpit_state.set_max_parallel_runs(0)
client2 = srv.create_app(_mkcfg()).test_client()

_start("alpha", ["EU-ok"], client2)
_wait_started(1)
chk("AC2a: single run active", cockpit_state.is_active("alpha"))

r2 = client2.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-ok"})
chk("AC2b: successful stop redirects", r2.status_code == 302)
chk("AC2c: stop_event IS set", _ev("alpha").is_set())

msg_ok = get_state("alpha").get("last_msg") or ""
chk("AC2d: success message distinct from no-event",
    "stopping after the current step" in msg_ok,
    f"got: {msg_ok!r}")

_drain_and_reset()


# ════════════════════════════════════════════════════════════════════════════
# Report
# ════════════════════════════════════════════════════════════════════════════

passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"\n{'=' * 60}")
print("  EU-706: stop no-event visible message")
print(f"  {'─' * 60}")
for name, ok, det in results:
    mark = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"    [{mark}] {name}{extra}")
print(f"  {'─' * 60}")
print(f"  {passed}/{total} checks passed")
if failed := total - passed:
    print(f"  RESULT: {failed} FAILED")
    sys.exit(1)
else:
    print("  RESULT: ALL GREEN")
    sys.exit(0)
