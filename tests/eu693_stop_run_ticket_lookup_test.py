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
  AC4  EU-694: a stop POST that matches NO live stop_event surfaces a VISIBLE page message
       ("No active run found to stop.") on the redirect target — not a bare silent 302.
       Covers the stale-card miss on a live run, a nonexistent app name, and the legacy
       empty POST with no stoppable run anywhere.
  AC5  EU-694 multi-run end-to-end in ONE concurrent window: stop card A → only A stops
       and the success response carries NO no-match message (explicit negative assertion,
       PM decision on AC2); stop a nonexistent third run → the visible no-match message;
       run B stays active throughout.
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
# EU-694 PM decision: the success path also gets an explicit NEGATIVE assertion — follow
# the redirect and prove the landing page carries no "No active run found to stop" note.
r3 = client.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-1"},
                 follow_redirects=True)
chk("AC3c: the original run is still stoppable by its exact app+ticket", _ev("alpha").is_set())
chk("AC3d: success-path landing page carries no no-match message (EU-694)",
    "No active run found to stop" not in r3.get_data(as_text=True))
_drain_and_reset()


# ═══════════════════════════════════════════════════════════════════════════════════
# AC4: no-match → VISIBLE page message (EU-694). A stop POST that matches no live
#      stop_event must not silently 302 — the page the user LANDS ON after the redirect
#      shows "No active run found to stop." (the control bar's tbnote, rendered from the
#      per-app last_msg the handler now writes; warroom already surfaces that mechanism).
# ═══════════════════════════════════════════════════════════════════════════════════
_start("alpha", ["EU-1"])
_wait_started(1)
chk("AC4a: alpha is active with a stop_event before the miss",
    cockpit_state.is_active("alpha") and _ev("alpha") is not None)

# Stale card: alpha's slot is claimed by EU-1, the form names a ticket it never claimed.
r4 = client.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-999"},
                 follow_redirects=True)
body4 = r4.get_data(as_text=True)
chk("AC4b: unknown ticket stops nothing — event remains unset",
    not _ev("alpha").is_set())
chk("AC4c: the landing page shows the visible no-match message (not a bare 302)",
    r4.status_code == 200 and "No active run found to stop" in body4,
    "the redirect target must render the feedback — AC1")

# Bad card: an app name that exists in no config keys the same feedback on the unit-wide slot.
r4g = client.post("/api/stop-run", data={"app": "ghost", "ticket": "EU-1"})
chk("AC4d: nonexistent app stops nothing and sets the unit-wide no-match message",
    r4g.status_code == 302
    and "No active run found to stop" in (cockpit_state.get_state(None).get("last_msg") or ""))
_drain_and_reset()

# Legacy fallback: an empty POST with NO stoppable run anywhere must also surface the
# message on the landing page (pre-EU-694 it redirected with zero feedback).
r4e = client.post("/api/stop-run", data={}, follow_redirects=True)
chk("AC4e: legacy empty POST with no runs shows the message on the landing page",
    r4e.status_code == 200 and "No active run found to stop" in r4e.get_data(as_text=True))


# ═══════════════════════════════════════════════════════════════════════════════════
# AC5: multi-run end-to-end (EU-694) in ONE concurrent window — run B stays active
#      throughout. Stop card A → only A stops + normal success response with NO
#      no-match message (explicit negative assertion, PM decision on AC2); stop a
#      nonexistent third run → the visible no-match message.
# ═══════════════════════════════════════════════════════════════════════════════════
_start("alpha", ["EU-1"])    # run A
_start("beta", ["EU-7"])     # run B — started last, so it is the active tab
_wait_started(2)
chk("AC5a: both alpha (A) and beta (B) are active",
    cockpit_state.is_active("alpha") and cockpit_state.is_active("beta"))
chk("AC5b: the two runs hold distinct stop_events",
    _ev("alpha") is not None and _ev("beta") is not None and _ev("alpha") is not _ev("beta"))

# Stop card A by its exact app+ticket — the normal success path, unchanged by EU-694.
rA = client.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-1"},
                 follow_redirects=True)
bodyA = rA.get_data(as_text=True)
chk("AC5c: stop(A) sets ONLY alpha's event — beta untouched",
    _ev("alpha").is_set() and not _ev("beta").is_set(),
    "multi-run isolation: stopping A must not affect B")
chk("AC5d: success path keeps today's stopping message on A's slot",
    "stopping after the current step" in (cockpit_state.get_state("alpha").get("last_msg") or ""))
chk("AC5e: success response carries NO no-match message (PM decision)",
    "No active run found to stop" not in bodyA,
    "AC2: no new message may be inserted on the success path")

# Stop a THIRD run that does not exist — a stale card naming beta's app with a ticket
# beta's live run never claimed (the card's old run is gone).
rC = client.post("/api/stop-run", data={"app": "beta", "ticket": "EU-999"},
                 follow_redirects=True)
bodyC = rC.get_data(as_text=True)
chk("AC5f: the nonexistent third stop shows the visible no-match message",
    "No active run found to stop" in bodyC,
    "the page must show feedback, not silently redirect")
chk("AC5g: run B remained active throughout — still live, never signaled",
    cockpit_state.is_active("beta") and not _ev("beta").is_set())
_drain_and_reset()


print("\n================ EU-693 / EU-694 /api/stop-run QA ==================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
