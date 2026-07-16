"""EU-356 — the cockpit's Stop must reach, and actually stop, the loop that is running.

THE INCIDENT (2026-07-15, live). A drain was ordered to stop from the cockpit at ~22:50. It reported
back ``{"on": true, "stopping": true}`` — and then picked four MORE tickets (EU-350 23:12, EU-351
23:30, EU-352 00:04, EU-353 00:31), standing down only at 01:53. Audit-log forensics against the SHA
that ran (05a7e98) proved the root cause was NOT a mis-bound event: the drain set the RIGHT event and
the loop itself honoured it at 01:53 (``autopilot_stop reason=cockpit-stop``). The killer was that
``autopilot()`` called ``run_loop(cfg, worklist, audit)`` WITHOUT the event, so loop.py's ticket-
boundary stop check was disarmed — and one run_loop call IS a whole cycle, whose worklist EU-201
fragment injection extends IN PLACE (EU-321 split into EU-350..353 mid-run), so the "one more cycle"
the stop waited for took 3h13m. Zero ``run_stopped`` events in the whole window is the smoking gun.

TWO FIXES land together, and this harness pins both:

  A. THE INCIDENT FIX — thread the drain's event into run_loop as ``stop_between_tickets``, the
     ticket-boundary-only channel (NOT ``stop_event``, which also arms the pre-build / pre-merge
     aborts and would kill or abandon the in-flight build, breaking the drain's "let the in-flight
     ticket land, then stand down" promise). Sections 5 and 6.

  B. THE ADJACENT BINDING HOLE — ``claim_run`` binds the caller's event only on a SUCCESSFUL claim;
     ``autopilot()`` deliberately survives a failed claim (``owns_run_state=False``) polling a local
     Event the state never learned about, while ``st["stop_event"]`` keeps a previous owner's Event:

         cockpit Stop  ──set()──▶  st["stop_event"]  (a DEAD Event nobody polls)
         live loop     ──poll───▶  its own Event     (which nothing can reach)

     Not the live incident's path (that binding was correct), but demonstrably reachable — the
     fail-first control below reproduces the unstoppable-drain shape through it. Fixed by
     ``bind_stop_event`` on entry (unconditional) + identity-checked ``unbind_stop_event`` on exit,
     and ``stopping`` gated on ``internal_on`` (a local Event can never stop another process's
     daemon). Sections 1-4.

THE INVARIANT (sections 1-3): *while ``autopilot_on`` is true for an app, the event reachable at
``st["stop_event"]`` IS the event that app's live loop polls.* Sections 1/2 drive the REAL
``autopilot()`` (SDK stubbed, no network, no models) with ``_sleep`` as the injection point — the
operator pressing Stop mid-drain, THROUGH the state, exactly as the cockpit route does. Section 3
drives the real cockpit HTTP route end-to-end. Section 7 is the FAIL-FIRST CONTROL for section 1's
shape (the slot pre-held, so claim_run's success-branch bind never runs and no-opping bind_stop_event
reproduces the pre-fix code exactly); sections 5/6 carry their own fail-first legs inline.
"""
import sys, types, tempfile, asyncio, io, contextlib, json, threading, time
from pathlib import Path

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

from orchestrator import autopilot, cockpit_state
from orchestrator import loop as loop_mod
from orchestrator.audit import AuditLog
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Outcome, Ticket, TicketReport
import orchestrator.backlog.base as backlog_base

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
# The real autopilot() runs below write a PID file. Keep it off the machine-global
# /tmp/general-autopilot.pid, which is shared with a live daemon and every other checkout's suite —
# otherwise daemon_running() reads a foreign process and get_autopilot_status().on flips True mid-harness.
autopilot._PID_FILE = tmp / "general-autopilot.pid"
cfg = Config(apps=[], audit_path=str(tmp / "audit.jsonl"))
APP = "Elite-Unit"

autopilot.usage.budget_status = lambda c: {"over": False, "alert": False, "used": 0, "cap": 1, "pct": 0.0}
autopilot.intake.from_drain = lambda c, app, n: []      # empty worklist → the loop idles through _sleep
autopilot.notify.configured = lambda: False
autopilot.notify.send = lambda *a, **k: None


def run_drain(live_ev, on_cycle, app_name=APP):
    """Run the REAL autopilot() in a thread; call ``on_cycle(n)`` from inside the live loop each cycle.

    ``on_cycle`` fires from the ``_sleep`` stub — i.e. from INSIDE a running drain, which is precisely
    where the cockpit operator hits Stop. Returns (thread, cycles) where ``cycles`` also records
    whether the emergency backstop had to fire — a check that relies on the loop having STOPPED must
    also assert the backstop stayed cold, or an unstoppable loop reads as a pass."""
    cycles = {"n": 0, "backstop_fired": False}
    def _sleep_stub(seconds, stop_event=None):
        cycles["n"] += 1
        on_cycle(cycles["n"])
        if cycles["n"] > 12:               # backstop: never hang the suite if the loop won't exit
            cycles["backstop_fired"] = True
            live_ev.set()
    autopilot._sleep = _sleep_stub
    def _target():
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(autopilot.autopilot(cfg, app_name, once=False, interval=1, stop_event=live_ev))
    t = threading.Thread(target=_target, daemon=True)
    t.start()
    return t, cycles


# ── 1. A drain whose claim_run FAILS still has ITS OWN event reachable from the state ───────────────
# The binding hole's shape: the slot is already held by someone else (a leaked claim / a manual run),
# carrying a FOREIGN event. autopilot() runs anyway (owns_run_state=False) — and must still be stoppable.
cockpit_state.reset_run_state()
foreign_ev = threading.Event()
cockpit_state.claim_run(APP, stop_event=foreign_ev)        # slot pre-held → the drain's claim will FAIL
chk("precondition: the slot is pre-held, so the drain's own claim_run fails",
    cockpit_state.claim_run(APP, stop_event=threading.Event()) is False)

live_ev = threading.Event()
seen = {"bound_is_live": None}
def _cycle1(n):
    st = cockpit_state.get_state(APP)
    if n == 1:
        # The invariant, observed from INSIDE the live drain.
        seen["bound_is_live"] = st.get("stop_event") is live_ev
        # Now do EXACTLY what server.py's Stop/drain branch does — reach the event THROUGH the state.
        ev = st.get("stop_event")
        if ev is not None:
            ev.set()
t1, cycles1 = run_drain(live_ev, _cycle1)
t1.join(timeout=30)

chk("a failed claim_run still leaves the LIVE loop's event reachable at st['stop_event']",
    seen["bound_is_live"] is True, f"bound_is_live={seen['bound_is_live']}")
chk("the foreign slot-holder's event is NOT what the cockpit would reach",
    seen["bound_is_live"] is True and not foreign_ev.is_set(), f"foreign_set={foreign_ev.is_set()}")
chk("setting the state's event stops the drain within one cycle (not 4 tickets later)",
    not t1.is_alive() and cycles1["n"] <= 2, f"alive={t1.is_alive()} cycles={cycles1['n']}")
chk("the loop was stopped by the cockpit path, NOT the harness backstop",
    not t1.is_alive() and cycles1["backstop_fired"] is False,
    f"backstop_fired={cycles1['backstop_fired']}")


# ── 2. A stale event from a PRIOR stopped run can never report stopping:true for a NEW live drain ───
# Drain #1 runs and is stopped; its Event stays .set() forever. Drain #2 then goes live. The badge must
# describe drain #2 — not drain #1's corpse.
cockpit_state.reset_run_state()
stale_ev = threading.Event()
t2a, _ = run_drain(stale_ev, lambda n: stale_ev.set())      # drain #1: stop it immediately
t2a.join(timeout=30)
chk("drain #1 stood down, leaving its event permanently set", not t2a.is_alive() and stale_ev.is_set())

# Simulate the leak that kept the corpse reachable: a slot still held, carrying the stale Event.
cockpit_state.claim_run(APP, stop_event=stale_ev)
new_ev = threading.Event()
status_while_live = {}
def _cycle2(n):
    if n == 1:
        status_while_live.update(cockpit_state.get_autopilot_status(APP))
        new_ev.set()                                        # let drain #2 exit
t2b, _ = run_drain(new_ev, _cycle2)
t2b.join(timeout=30)

chk("a NEW live drain is never reported 'stopping' because of a PRIOR run's stale event",
    status_while_live.get("on") is True and status_while_live.get("stopping") is False,
    f"status={status_while_live}")
chk("after a drain exits, its event is retracted — no corpse left bound",
    cockpit_state.get_state(APP).get("stop_event") is not new_ev,
    f"still bound={cockpit_state.get_state(APP).get('stop_event') is new_ev}")

# POSITIVE leg — 'stopping' must still be reportable at all, or the two negatives above pass against
# a get_autopilot_status that simply never says stopping (e.g. the term typo'd away). Deterministic,
# direct-state: a live in-process loop's own set event IS a genuine stopping state.
cockpit_state.reset_run_state()
_pos = threading.Event()
cockpit_state.bind_stop_event(APP, _pos)
cockpit_state.get_state(APP)["autopilot_on"] = True
_pos.set()
chk("a live loop's own set event DOES report stopping:true (the badge still works)",
    cockpit_state.get_autopilot_status(APP)["stopping"] is True,
    f"status={cockpit_state.get_autopilot_status(APP)}")
cockpit_state.reset_run_state()

# Identity contract of unbind — the docstring's declared hazard, pinned: a SUPERSEDED loop exiting
# must not blank the newer live loop's binding (unconditional blanking would leave Stop a silent
# no-op with ev=None while 'on' stays true — the incident class, minus the badge).
ev_a, ev_b = threading.Event(), threading.Event()
cockpit_state.bind_stop_event(APP, ev_a)
cockpit_state.bind_stop_event(APP, ev_b)                    # a newer drain rebinds
chk("unbind by a superseded loop leaves the newer binding alone (identity check)",
    cockpit_state.unbind_stop_event(APP, ev_a) is False
    and cockpit_state.get_state(APP).get("stop_event") is ev_b)
chk("unbind by the live loop itself clears its own binding",
    cockpit_state.unbind_stop_event(APP, ev_b) is True
    and cockpit_state.get_state(APP).get("stop_event") is None)
cockpit_state.reset_run_state()


# ── 3. Regression, end-to-end: the real cockpit ROUTE stops a running per-app loop ──────────────────
# The headline symptom, driven through the actual HTTP surface: POST /api/autopilot action=drain.
from orchestrator import server as srv

cockpit_state.reset_run_state()
# The route resolves its target via _scope(), which only honours a CONFIGURED app name — with apps=[]
# the key would collapse to None and the test would silently drive the unit-wide slot, not the per-app one.
srv_cfg = Config(apps=[AppConfig(name=APP, repo_path=str(tmp), base_branch="dev", protected_branch="main",
                                 backlog_backend="none")],
                 audit_path=str(tmp / "audit2.jsonl"), use_worktree=False)
srv_cfg.detected_auth = lambda: "test"
srv.health.summary = lambda c: {"healthy": True}
loop_state = {"stopped": False, "ev": None}

async def fake_autopilot(cfg_, app_, once=False, interval=60, stop_event=None):
    """A faithful stand-in for the real loop: bind own event, then poll it — autopilot()'s contract.
    (Route-wiring regression only; the fix itself is exercised by sections 1/2/5/6 on the real code.)"""
    cockpit_state.bind_stop_event(app_, stop_event)
    cockpit_state.get_state(app_)["autopilot_on"] = True
    loop_state["ev"] = stop_event
    try:
        for _ in range(600):                                # 30s cap — the loop polls, like the real one
            if stop_event is not None and stop_event.is_set():
                loop_state["stopped"] = True
                return
            time.sleep(0.05)
    finally:
        cockpit_state.get_state(APP)["autopilot_on"] = False
        cockpit_state.unbind_stop_event(app_, stop_event)

srv_app = srv.create_app(srv_cfg)
import orchestrator.autopilot as _apmod
_real = _apmod.autopilot
_apmod.autopilot = fake_autopilot
try:
    client = srv_app.test_client()
    client.post("/api/autopilot", data={"action": "start", "app": APP, "mode": "drain"})
    for _ in range(200):                                 # wait for the loop to go live
        if loop_state["ev"] is not None:
            break
        time.sleep(0.05)
    live_before = cockpit_state.get_autopilot_status(APP)
    client.post("/api/autopilot", data={"action": "drain", "app": APP})
    for _ in range(200):                                 # the loop must notice within a cycle
        if loop_state["stopped"]:
            break
        time.sleep(0.05)
    chk("the route started a live drain that the state can reach",
        live_before.get("on") is True and loop_state["ev"] is not None, f"{live_before}")
    chk("POST /api/autopilot action=drain STOPS the running per-app loop end-to-end",
        loop_state["stopped"] is True, "the drain POST never reached the live loop")
    # EU-356: the stop REQUEST itself must land in the audit trail (the live forensics couldn't place
    # the operator's click closer than a 3-hour window because ev.set() left no trace).
    _audit2 = Path(str(tmp / "audit2.jsonl"))
    _stop_reqs = ([json.loads(l) for l in _audit2.read_text().splitlines() if l.strip()]
                  if _audit2.exists() else [])
    _stop_reqs = [r for r in _stop_reqs if r.get("event") == "autopilot_stop_requested"]
    chk("the drain POST records an autopilot_stop_requested audit event (the click leaves a trail)",
        any(r.get("action") == "drain" and r.get("app") == APP and r.get("event_reachable") is True
            for r in _stop_reqs), f"stop_requested events={_stop_reqs}")
    # The loop returning is NOT the end of the run: server.py's _bg finally still has to clear
    # autopilot_on and release_run(). Wait for the slot to actually drop before handing the state to
    # the next section — otherwise that late release_run() lands mid-setup and silently un-holds the
    # slot the control below depends on (which is exactly what it caught on the first run).
    for _ in range(200):
        if not cockpit_state.is_active(APP):
            break
        time.sleep(0.05)
    chk("the run slot is released once the drain stands down", not cockpit_state.is_active(APP))
finally:
    _apmod.autopilot = _real


# ── 5. The drain's polled event reaches run_loop's ticket-boundary channel (the incident fix) ───────
# One REAL autopilot cycle with a non-empty worklist: the event the cockpit reaches through the state
# must be the SAME object the cycle hands run_loop as stop_between_tickets — cockpit ▸ state ▸ loop ▸
# run_loop, one Event end to end. Fail-first: pre-fix autopilot called run_loop(cfg, worklist, audit)
# bare, so `captured["between"]` stays None and both checks go red against today's-before code.
cockpit_state.reset_run_state()
import orchestrator.git_ops as git_ops
_orig_reap = git_ops.reap_stale_worktrees
git_ops.reap_stale_worktrees = lambda c: None            # never side-effect real worktrees from a test
_ap5 = AppConfig(name=APP, repo_path=str(tmp), base_branch="dev", protected_branch="main",
                 backlog_backend="none")
cfg5 = Config(apps=[_ap5], audit_path=str(tmp / "audit5.jsonl"), use_worktree=False)
_tk5 = types.SimpleNamespace(id="EU-CYCLE", ephemeral=True)
autopilot.intake.from_drain = lambda c, app, n: [(_ap5, _tk5)]
captured = {"between": None, "stop": "unset"}
async def _capturing_run_loop(c, worklist, audit, stop_event=None, stop_between_tickets=None):
    captured["between"] = stop_between_tickets
    captured["stop"] = stop_event
    return [TicketReport(_tk5.id, Outcome.MERGED, 1, 0.0, _ap5.name)]
_orig_rl = autopilot.run_loop
autopilot.run_loop = _capturing_run_loop
ev5 = threading.Event()
try:
    with contextlib.redirect_stdout(io.StringIO()):
        asyncio.run(autopilot.autopilot(cfg5, APP, once=True, stop_event=ev5))
finally:
    autopilot.run_loop = _orig_rl
    autopilot.intake.from_drain = lambda c, app, n: []
    git_ops.reap_stale_worktrees = _orig_reap

chk("the cycle passes its own polled event to run_loop as stop_between_tickets",
    captured["between"] is ev5, f"captured={captured['between']!r}")
chk("…and NOT as stop_event (mid-ticket aborts stay disarmed — the drain finishes in-flight work)",
    captured["stop"] is None, f"stop_event={captured['stop']!r}")


# ── 6. INCIDENT REPLAY on the real loop: fragment injection + a mid-cycle stop ───────────────────────
# The live shape, in miniature: a one-ticket worklist (EU-321) whose build splits it into fragments
# that EU-201 injects IN PLACE into the live worklist; the operator's stop lands DURING the parent's
# build (the 22:50 click). Pre-fix, the disarmed loop built every fragment (3 more hours); fixed, the
# ticket boundary honours the drain and no fragment builds.
d6 = Path(tempfile.mkdtemp())
(d6 / "audit.jsonl").write_text("")
app6 = AppConfig(name="automatixy", repo_path=str(d6), base_branch="DEV", protected_branch="MAIN",
                 backlog_backend="jira", backlog={"base_url": "x", "project_key": "AUTO"})
cfg6 = Config(apps=[app6], audit_path=str(d6 / "audit.jsonl"), use_worktree=False, dry_run=True,
              max_iterations=1)
def _mk6(key):
    # ephemeral=True → the run uses NoneBacklog for the ticket itself (no tracker creds), while the
    # fragment RESOLUTION still goes through backlog_base.make_backlog (a local import inside
    # _fetch_fragments_to_worklist), which the stub below intercepts — same shape as the EU-201 harness.
    return Ticket(id=key, key=key, summary=f"Ticket {key}", description="d",
                  acceptance_criteria=["AC1"], app="automatixy", ephemeral=True)
parent, frag1, frag2 = _mk6("AUTO-321"), _mk6("AUTO-350"), _mk6("AUTO-351")

class _FakeGit:
    def ensure_clean(self):
        return None

_orig_make_backlog = backlog_base.make_backlog
_orig_process = loop_mod.process_ticket
_orig_make_git = loop_mod._make_git

class _Backlog6:
    def get_ready_tasks(self, limit): return []
    def get_task(self, key): return {"AUTO-350": frag1, "AUTO-351": frag2}[key]
    def set_status(self, ticket, status): return None
    def add_comment(self, ticket, body): return None

def _replay(stop_mid_parent: bool):
    """Drive real loop.run over [parent]; the parent 'splits' into two fragments. When
    ``stop_mid_parent``, the drain's boundary event is set DURING the parent's build."""
    drain_ev = threading.Event()
    built = []
    async def _process(ticket, app, c, git, backlog, audit, budget, stop_event=None):
        built.append(ticket.id)
        if ticket.id == parent.id:
            if stop_mid_parent:
                drain_ev.set()                       # the operator's click, mid-build
            return TicketReport(parent.id, Outcome.REQUEUED, 1, 0.0, app.name,
                                notes="too big — Scrum Master split into AUTO-350, AUTO-351")
        return TicketReport(ticket.id, Outcome.MERGED, 1, 0.0, app.name)
    backlog_base.make_backlog = lambda app: _Backlog6()
    loop_mod.process_ticket = _process
    loop_mod._make_git = lambda c, app: _FakeGit()
    audit6 = AuditLog(str(d6 / "audit.jsonl"))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(loop_mod.run(cfg6, [(app6, parent)], audit6,
                                     stop_between_tickets=drain_ev))
        return built
    finally:
        backlog_base.make_backlog = _orig_make_backlog
        loop_mod.process_ticket = _orig_process
        loop_mod._make_git = _orig_make_git

built_ctrl = _replay(stop_mid_parent=False)
chk("CONTROL: with no stop, the injected fragments DO build (the replay is faithful to EU-201)",
    built_ctrl == ["AUTO-321", "AUTO-350", "AUTO-351"], f"built={built_ctrl}")

(d6 / "audit.jsonl").write_text("")
built_stop = _replay(stop_mid_parent=True)
_ev6 = [json.loads(l) for l in (d6 / "audit.jsonl").read_text().splitlines() if l.strip()]
chk("a stop ordered mid-parent halts at the NEXT ticket boundary — no injected fragment builds",
    built_stop == ["AUTO-321"], f"built={built_stop}")
chk("…and the boundary stop is recorded (run_stopped — zero of these existed live)",
    any(e.get("event") == "run_stopped" for e in _ev6),
    f"events={[e.get('event') for e in _ev6]}")


# ── 7. FAIL-FIRST CONTROL: prove sections 1-2's checks go RED against the pre-fix binding ───────────
# Faithful for section 1's shape: the slot is pre-held, so claim_run's success-branch bind never runs
# and no-opping bind_stop_event reproduces the pre-fix module exactly. (Sections 5/6 carry their own
# controls inline; section 3's route wiring predates the fix.)
cockpit_state.reset_run_state()
_real_bind = cockpit_state.bind_stop_event
cockpit_state.bind_stop_event = lambda app, ev: None        # ← pre-EU-356 behaviour
try:
    prefix_foreign = threading.Event()
    cockpit_state.claim_run(APP, stop_event=prefix_foreign)  # slot pre-held → claim fails → event dropped
    prefix_live = threading.Event()
    prefix = {"bound_is_live": None, "kept_running": False, "status_while_running": None}
    def _cycle7(n):
        st = cockpit_state.get_state(APP)
        if n == 1:
            prefix["bound_is_live"] = st.get("stop_event") is prefix_live
            ev = st.get("stop_event")                        # the cockpit's Stop — sets the WRONG event
            if ev is not None:
                ev.set()
        if n >= 3:
            prefix["kept_running"] = True                    # still draining, 2 cycles after Stop
            # The badge, read while the drain demonstrably runs on: this is the live lie.
            prefix["status_while_running"] = cockpit_state.get_autopilot_status(APP)
            prefix_live.set()                                # release it so the suite doesn't hang
    t7, cycles7 = run_drain(prefix_live, _cycle7)
    t7.join(timeout=30)
    chk("CONTROL: pre-fix, the live loop's event was NOT reachable from the state",
        prefix["bound_is_live"] is False, f"bound_is_live={prefix['bound_is_live']}")
    chk("CONTROL: pre-fix, the cockpit's Stop did NOT stop the drain (the unstoppable shape)",
        prefix["kept_running"] is True, "the pre-fix drain stopped — the control proves nothing")
    chk("CONTROL: pre-fix, get_autopilot_status reported stopping:true WHILE the drain ran on",
        (prefix["status_while_running"] or {}).get("stopping") is True
        and (prefix["status_while_running"] or {}).get("on") is True,
        f"status={prefix['status_while_running']}")
finally:
    cockpit_state.bind_stop_event = _real_bind
    cockpit_state.reset_run_state()

print("\n===== AUTOPILOT STOP-PATH QA (EU-356: binding + ticket-boundary drain) =====")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
