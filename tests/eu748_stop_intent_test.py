"""EU-748 — cockpit Stop / Finish & stop buttons can stop an external autonomous drain.

Fix wires a cross-process stop-intent FILE that the drain polls every cycle, mirroring
the existing drain-intent plumbing (record_drain_intent / clear_drain_intent).

Production shape (modelled exactly): an external daemon makes get_autopilot_status answer
on=True (PID-probe), so the handler's ``ap_on`` branch is what runs — with ``stop_event``
None, because this cockpit never started that process. That ev=None+external slot is where
the intent file is written; launchctl bootout must NOT fire from the cockpit there. The
live drain PEEKS the record (non-destructive) so the keepalive-respawn gate
(honour_pending_stop_intent) still sees it and refuses to resume building. Sections:

  S1 — Round-trip request/peek/consume (unit-test the file plumbing; peek leaves the record).
  S2 — Driving ``autopilot(cfg, app, once=False)`` with a pre-existing stop-intent stands the
       drain down after the current cycle with reason 'cockpit-stop-intent', and the record
       SURVIVES the stand-down (the respawn gate relies on it).
  S3 — POST action=drain with external daemon (on=True, no stop_event): writes the intent
       under the unit-wide "" key, flips the stopping chip, honest last_msg — and does NOT
       call _stop_launchd_daemon (no bootout from the cockpit).
  S4 — POST action=stop on ext daemon (same, mode='stop') + in-process behaviour unchanged.
  S5 — honour_pending_stop_intent: the CLI/keepalive-respawn gate (refuse + self-bootout +
       consume; --force retracts).
"""
import json, sys, types, tempfile, threading, time, asyncio, io, contextlib
from pathlib import Path

# ── Minimal SDK stubs so orchestrator imports don't crash ──────────────────────────────────────
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator.config import Config, AppConfig
results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ===================================================================== Helpers
# =====================================================================
def _make_cfg(audit_suffix="audit.jsonl"):
    """Create config + temp dir; patch PID_FILE off host-global."""
    tmp = Path(tempfile.mkdtemp())
    audit_path = str(tmp / audit_suffix)
    import orchestrator.autopilot as ap
    ap._PID_FILE = tmp / "general-autopilot.pid"
    return Config(apps=[], audit_path=audit_path), tmp


def _sleep_stub(seconds, stop_event=None):
    t = threading.current_thread()
    if hasattr(t, "_on_cycle"):
        t._on_cycle()
    time.sleep(0.02)


def run_drain(cfg, app_name, stop_ev=None, on_cycle=None, once=True, interval=1):
    """Run REAL autopilot() in a thread with _sleep stubbed."""
    import orchestrator.autopilot as ap
    cycles_spy = [0]

    def _inner_sleep(seconds, stop_event=None):
        cycles_spy[0] += 1
        cur = threading.current_thread()
        if hasattr(cur, "_on_cycle"):
            cur._on_cycle()
        time.sleep(0.02)

    old_sleep = ap._sleep
    ap._sleep = _inner_sleep
    t = threading.Thread(target=_run_ap, args=(cfg, app_name, stop_ev, on_cycle, once, interval),
                         daemon=True)
    t.start()
    return t, lambda: cycles_spy[0], lambda: ap._sleep is _inner_sleep


def _run_ap(cfg, app_name, stop_ev, on_cycle_fn, once, interval):
    import orchestrator.autopilot as ap
    cur = threading.current_thread()
    if on_cycle_fn:
        cur._on_cycle = on_cycle_fn
    else:
        cur._on_cycle = lambda: None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(ap.autopilot(cfg, app_name, once=once, interval=interval, stop_event=stop_ev))
    finally:
        ap._sleep = _sleep_stub


# ===================================================================== SECTION 1 — Round-trip
# =====================================================================
def section1_round_trip():
    print("--- Section 1: Round-trip request/peek/consume ---")
    cfg, tmp = _make_cfg("audit1.jsonl")
    import orchestrator.autopilot as ap

    ap.request_drain_stop(cfg, "automatixy", mode="drain")
    peeked = ap.peek_stop_intent(cfg, "automatixy")
    chk("S1: peek returns record carrying mode='drain'",
        peeked is not None and peeked.get("mode") == "drain", f"peeked={peeked}")
    chk("S1: peek is non-destructive (record still pending)",
        ap.peek_stop_intent(cfg, "automatixy") is not None,
        "peek must NOT clear — the respawn gate still needs the record")
    rec = ap.consume_stop_intent(cfg, "automatixy")
    chk("S1: first consume returns record carrying mode='drain'",
        rec is not None and rec.get("mode") == "drain", f"rec={rec}")
    chk("S1: second consume returns None (one-shot)",
        ap.consume_stop_intent(cfg, "automatixy") is None,
        "should be None after one-shot consume")

    ap.request_drain_stop(cfg, "", mode="drain")
    rec_unit = ap.consume_stop_intent(cfg, "anything")
    chk("S1: unit-wide intent (app='') consumed by ANY app key",
        rec_unit is not None and rec_unit.get("mode") == "drain",
        f"rec_unit={rec_unit}")

    intent_file = tmp / "autopilot_stop_intent.json"
    chk("S1: stop-intent file exists on disk",
        intent_file.exists(), f"exists={intent_file.exists()}")
    intent_file.unlink(missing_ok=True)


# ===================================================================== SECTION 2 — Drive autopilot with stop-intent
# =====================================================================
def section2_drive_with_stop_intent():
    print("--- Section 2: Drive autopilot with stop-intent ---")
    cfg, tmp = _make_cfg("audit2.jsonl")
    import orchestrator.autopilot as ap
    ap.intake.from_drain = lambda c, app, n: []
    ap.notify.configured = lambda: False
    ap.notify.send = lambda *a, **k: None
    ap.usage.budget_status = lambda c: {"over": False, "alert": False, "used": 0, "cap": 1, "pct": 0.0}

    import orchestrator.git_ops as git_ops
    orig_reap = git_ops.reap_stale_worktrees
    git_ops.reap_stale_worktrees = lambda c: None
    git_ops.clear_parked_repos = lambda: None

    try:
        ap.request_drain_stop(cfg, "test-app", mode="drain")

        ev = threading.Event()
        t, cycles_fn, alive_fn = run_drain(cfg, "test-app", stop_ev=ev, on_cycle=None,
                                            once=False, interval=1)

        deadline = time.time() + 10
        while t.is_alive() and time.time() < deadline:
            t.join(timeout=0.1)

        chk("S2: drain stands down after detecting stop-intent",
            not t.is_alive(), f"alive={t.is_alive()} cycles={cycles_fn()}")
        chk("S2: drain stops within 3 idle cycles",
            cycles_fn() <= 3, f"cycles={cycles_fn()}")

        intents = ap.load_drain_intent(cfg)
        chk("S2: finally leaves drain-intent in STOPPED state",
            intents.get("test-app", {}).get("state") == "STOPPED",
            f"state={intents.get('test-app', {}).get('state')}")

        events = [json.loads(l) for l in Path(cfg.audit_path).read_text().splitlines() if l.strip()]
        stop_events = [e for e in events if e.get("event") == "autopilot_stop"]
        chk("S2: audit records stop with reason 'cockpit-stop-intent'",
            any(e.get("reason") == "cockpit-stop-intent" for e in stop_events),
            f"reasons={[e.get('reason') for e in stop_events]}")

        chk("S2: stop-intent SURVIVES the stand-down (respawn gate relies on it)",
            ap.peek_stop_intent(cfg, "test-app") is not None,
            "the drain peeks, never consumes — a KeepAlive respawn must still see the record")

    finally:
        ap.intake.from_drain = lambda c, app, n: []
        ap.notify.configured = lambda: False
        ap.notify.send = lambda *a, **k: None
        ap.usage.budget_status = lambda c: {"over": False, "alert": False, "used": 0, "cap": 1, "pct": 0.0}
        git_ops.reap_stale_worktrees = orig_reap
        git_ops.clear_parked_repos = lambda: None


# ===================================================================== SECTION 3 — POST action=drain on external daemon
# =====================================================================
def section3_drain_action_on_external_daemon():
    print("--- Section 3: POST action=drain on external daemon ---")
    from orchestrator import server as srv
    from orchestrator import cockpit_state

    cfg, tmp = _make_cfg("audit3.jsonl")
    APP = "automatixy"
    cfg.app_config = AppConfig(name=APP, repo_path=str(tmp), base_branch="dev",
                               protected_branch="main", backlog_backend="none")
    cfg.apps = [cfg.app_config]
    cfg.detected_auth = lambda: "test"

    # Spy that matches the EXACT signature of the real function
    import orchestrator.autopilot as apmod
    _orig_req = apmod.request_drain_stop
    _orig_di_ext = apmod.daemon_is_external
    _orig_bootout = apmod._stop_launchd_daemon

    def _spy_request(*a, **kw):
        cock_spy["written"] = True
        cock_spy["app"] = a[1] if len(a) > 1 else kw.get("app_name")
        cock_spy["mode"] = kw.get("mode", a[2] if len(a) > 2 else "drain")
        return _orig_req(*a, **kw)

    bootout_calls = []
    apmod.request_drain_stop = _spy_request
    apmod.daemon_is_external = lambda: True
    apmod._stop_launchd_daemon = lambda *a, **k: bootout_calls.append(1) or True
    # PRODUCTION REALITY, computed by the REAL get_autopilot_status (server.py imports it by
    # name, so stubbing cockpit_state.get_autopilot_status would never reach the handler):
    # daemon_running + daemon_is_external → on=True with external=True, while this cockpit
    # holds no stop_event for it. That ev=None+external slot is the branch under test.
    _orig_daemon_running = apmod.daemon_running
    apmod.daemon_running = lambda: True

    # Bypass health gate
    orig_health = srv.health.summary
    srv.health.summary = lambda c: {"healthy": True}

    cock_spy = {"written": False, "app": None, "mode": None}

    try:
        cockpit_state.reset_run_state()
        srv_app = srv.create_app(cfg)
        client = srv_app.test_client()

        result = client.post("/api/autopilot", data={"action": "drain", "app": APP, "mode": "drain"})
        chk("S3: redirect status (HTTP 30x)",
            result.status_code in (302, 301, 303), f"status={result.status_code}")
        chk("S3: request_drain_stop was called (intent written)",
            cock_spy["written"], f"written={cock_spy['written']} mode={cock_spy['mode']}")
        chk("S3: intent written under unit-wide '' key (daemon's app unknown to the cockpit)",
            cock_spy["app"] == "", f"app={cock_spy['app']!r}")
        chk("S3: cockpit did NOT bootout the launchd service (EU-748 removes that lever)",
            len(bootout_calls) == 0, f"bootout_calls={len(bootout_calls)}")
        chk("S3: stopping chip flipped on the tab",
            cockpit_state.get_state(APP).get("stopping") is True,
            f"stopping={cockpit_state.get_state(APP).get('stopping')}")

        resp = client.get("/?app=" + APP)
        body_str = resp.data.decode(errors="replace").lower()
        chk("S3: last_msg says cockpit stays up",
            "cockpit stays up" in body_str,
            f"'cockpit stays up' found={('cockpit stays up' in body_str)} snippet='{body_str[:200]}'")

    finally:
        apmod.request_drain_stop = _orig_req
        apmod.daemon_is_external = _orig_di_ext
        apmod._stop_launchd_daemon = _orig_bootout
        apmod.daemon_running = _orig_daemon_running
        srv.health.summary = orig_health
        cockpit_state.reset_run_state()


# ===================================================================== SECTION 4 — POST action=stop + in-process unchanged
# =====================================================================
def section4_stop_action_and_in_process_unchanged():
    print("--- Section 4: POST action=stop on ext daemon + in-process unchanged ---")
    from orchestrator import server as srv
    from orchestrator import cockpit_state

    cfg, tmp = _make_cfg("audit4.jsonl")
    APP = "automatixy"
    cfg.app_config = AppConfig(name=APP, repo_path=str(tmp), base_branch="dev",
                               protected_branch="main", backlog_backend="none")
    cfg.apps = [cfg.app_config]
    cfg.detected_auth = lambda: "test"

    # Spy matching the real function signature
    import orchestrator.autopilot as apmod
    _orig_req = apmod.request_drain_stop
    _orig_di_ext = apmod.daemon_is_external
    _orig_bootout = apmod._stop_launchd_daemon

    def _spy_request(*a, **kw):
        cock_spy["written"] = True
        cock_spy["app"] = a[1] if len(a) > 1 else kw.get("app_name")
        cock_spy["mode"] = kw.get("mode", a[2] if len(a) > 2 else "stop")
        return _orig_req(*a, **kw)

    bootout_calls = []
    apmod.request_drain_stop = _spy_request
    apmod.daemon_is_external = lambda: True
    apmod._stop_launchd_daemon = lambda *a, **k: bootout_calls.append(1) or True
    # Same production shape as S3 — via the REAL get_autopilot_status (see the S3 note):
    # on=True via the PID probe, no stop_event in this process.
    _orig_daemon_running = apmod.daemon_running
    apmod.daemon_running = lambda: True

    orig_health = srv.health.summary
    srv.health.summary = lambda c: {"healthy": True}

    cock_spy = {"written": False, "app": None, "mode": None}

    try:
        cockpit_state.reset_run_state()
        srv_app = srv.create_app(cfg)
        client = srv_app.test_client()

        result = client.post("/api/autopilot", data={"action": "stop", "app": APP, "mode": "drain"})
        chk("S4a: action=stop on external daemon returns redirect",
            result.status_code in (302, 301, 303), f"status={result.status_code}")
        chk("S4a: request_drain_stop called with mode='stop'",
            cock_spy["written"] and cock_spy["mode"] == "stop",
            f"written={cock_spy['written']} mode={cock_spy['mode']}")
        chk("S4a: intent written under unit-wide '' key",
            cock_spy["app"] == "", f"app={cock_spy['app']!r}")
        chk("S4a: cockpit did NOT bootout the launchd service",
            len(bootout_calls) == 0, f"bootout_calls={len(bootout_calls)}")

    finally:
        apmod.request_drain_stop = _orig_req
        apmod.daemon_is_external = _orig_di_ext
        apmod._stop_launchd_daemon = _orig_bootout
        apmod.daemon_running = _orig_daemon_running
        srv.health.summary = orig_health
        cockpit_state.reset_run_state()

    # ── S4b: in-process behaviour unchanged ───────────────────────────────
    print("  S4b: in-process behaviour unchanged...")
    cockpit_state.reset_run_state()

    cfg4 = Config(apps=[AppConfig(name=APP, repo_path=str(tmp), base_branch="dev",
                                   protected_branch="main", backlog_backend="none")],
                  audit_path=str(tmp / "audit4b.jsonl"), use_worktree=False)
    cfg4.detected_auth = lambda: "test"

    loop_state = {"stopped": False, "ev": None}
    # Holds the fake loop in flight AFTER it sees the stop_event so the test can observe the
    # 'stopping' flag mid-drain. Without it the assertion races release_run (EU-687), which
    # legitimately clears the flag the instant the loop exits — the iteration-1 false-red.
    gate = threading.Event()

    async def fake_autopilot_in_proc(cfg_, app_, once=False, interval=60, stop_event=None):
        cockpit_state.bind_stop_event(app_, stop_event)
        cockpit_state.get_state(app_)["autopilot_on"] = True
        loop_state["ev"] = stop_event
        try:
            for _ in range(600):
                if stop_event is not None and stop_event.is_set():
                    gate.wait(timeout=5)   # held in flight until the test has read the flag
                    loop_state["stopped"] = True
                    return
                time.sleep(0.05)
        finally:
            cockpit_state.get_state(APP)["autopilot_on"] = False
            cockpit_state.unbind_stop_event(app_, stop_event)

    # Stub health
    srv.health.summary = lambda c: {"healthy": True}

    orig_real = apmod.autopilot
    apmod.autopilot = fake_autopilot_in_proc
    apmod.daemon_is_external = lambda: False

    try:
        srv_app2 = srv.create_app(cfg4)
        client = srv_app2.test_client()

        client.post("/api/autopilot", data={"action": "start", "app": APP, "mode": "drain"})

        for _ in range(200):
            if loop_state["ev"] is not None:
                break
            time.sleep(0.05)

        client.post("/api/autopilot", data={"action": "drain", "app": APP})

        # The handler ev.set()s and flips st['stopping'] synchronously BEFORE returning; the
        # fake loop is held at the gate, so release_run cannot have cleared the flag yet.
        # This is the window the flag exists for (the amber 'Stopping…' chip, EU-687).
        st = cockpit_state.get_state(APP)
        chk("S4b: in-process drain flips st['stopping']=True",
            st.get("stopping") is True, f"stopping={st.get('stopping')}")

        gate.set()   # let the drain finish

        for _ in range(200):
            if loop_state["stopped"]:
                break
            time.sleep(0.05)

        chk("S4b: in-process drain uses ev.set()",
            loop_state["ev"] is not None and loop_state["stopped"],
            f"ev={loop_state['ev'] is not None} stopped={loop_state['stopped']}")

        # EU-687 cleanup: the worker's finally → release_run must clear the confirmed-stop chip
        # with the run. Poll is_active (cleared under the SAME lock as 'stopping') so we observe
        # the state AFTER the release, not mid-finally.
        deadline = time.time() + 10
        while time.time() < deadline and cockpit_state.is_active(APP):
            time.sleep(0.05)
        chk("S4b: release_run clears stopping once the drain completes (EU-687)",
            cockpit_state.get_state(APP).get("stopping") is False
            and not cockpit_state.is_active(APP),
            f"stopping={cockpit_state.get_state(APP).get('stopping')} "
            f"active={cockpit_state.is_active(APP)}")

    finally:
        apmod.autopilot = orig_real
        apmod.daemon_is_external = _orig_di_ext
        cockpit_state.reset_run_state()


# ===================================================================== SECTION 5 — respawn gate
# =====================================================================
def section5_cli_respawn_gate():
    print("--- Section 5: honour_pending_stop_intent (CLI / keepalive-respawn gate) ---")
    cfg, tmp = _make_cfg("audit5.jsonl")
    import orchestrator.autopilot as ap

    chk("S5: no pending intent → gate passes (start proceeds)",
        ap.honour_pending_stop_intent(cfg, "automatixy") is False,
        "nothing pending → False")

    ap.request_drain_stop(cfg, "", mode="drain")
    chk("S5: --force retracts a pending intent and proceeds",
        ap.honour_pending_stop_intent(cfg, "automatixy", force=True) is False,
        "force must clear the record and answer False")
    chk("S5: force consumed the intent",
        ap.peek_stop_intent(cfg, "automatixy") is None,
        f"peek={ap.peek_stop_intent(cfg, 'automatixy')}")

    # KeepAlive respawn shape: the intent the live drain PEEKED is still on disk; the gate must
    # refuse, self-bootout the keepalive service (spy), and consume the record.
    ap.request_drain_stop(cfg, "", mode="drain")
    bootout_calls = []
    orig_bootout = ap._stop_launchd_daemon
    ap._stop_launchd_daemon = lambda *a, **k: bootout_calls.append(1) or True
    try:
        chk("S5: respawn gate REFUSES a pending intent (drain stays down)",
            ap.honour_pending_stop_intent(cfg, "automatixy") is True,
            "pending intent without force → True (exit, don't drain)")
    finally:
        ap._stop_launchd_daemon = orig_bootout
    chk("S5: gate attempted the keepalive bootout (ends the respawn cycle)",
        len(bootout_calls) == 1, f"bootout_calls={len(bootout_calls)}")
    chk("S5: gate consumed the intent after refusing",
        ap.peek_stop_intent(cfg, "automatixy") is None,
        f"peek={ap.peek_stop_intent(cfg, 'automatixy')}")


# ===================================================================== Run
# =====================================================================
if __name__ == "__main__":
    print("\n===== EU-748: Cockpit Stop / Finish&stop on external drain =====")

    section1_round_trip()
    section2_drive_with_stop_intent()
    section3_drain_action_on_external_daemon()
    section4_stop_action_and_in_process_unchanged()
    section5_cli_respawn_gate()

    passed = sum(1 for _, ok, _ in results if ok)
    for n, ok, det in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
    print("-" * 60)
    print(f"  {passed}/{len(results)} passed")
    print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
    sys.exit(0 if passed == len(results) else 1)
