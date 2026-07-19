"""EU-372 — the two decision-ordering crash windows: a killed process must not eat the Commander.

Both defects are "durable side effect committed BEFORE the work it guards actually started", so a
process death (SIGKILL, host reboot, the 5h plan-limit stop) in the gap loses the Commander's input
permanently and silently. From the 2026-07-16 total audit, re-verified on dev 2026-07-17:

  1. resolve() popped the pending decision from the store, and only THEN did handle_reply build the
     worklist and start the run. A crash in that gap (which spans _comment_answer's Jira round-trip)
     left the ticket parked on 'Blocked' with NO pending entry: the question is gone from 'Needs you'
     and no run is coming. Nothing re-raises it — the ticket is stuck forever.
  2. poll_once advanced the Telegram offset BEFORE routing the message. Telegram's getUpdates is a
     single-consumer queue: once the offset is past an update it is never redelivered. A crash inside
     route_message therefore dropped the Commander's message with no trace.

Pinned here:
  A1. handle_reply whose run-start dies leaves the decision RECOVERABLE in the store (the fix).
  A2. …but an in-flight claim is still exclusive — a second racing reply gets nothing (the EU-48
      "resolved exactly once" guarantee must survive the two-phase claim).
  A3. …and a claim stranded by a dead process is reaped, so the question becomes answerable again
      rather than wedging forever behind a tombstone.
  A4. The happy path still fully drains the store — the claim must not leak a half-resolved entry
      into 'Needs you'.
  A5. Bare resolve() STILL pops in one shot. loop._resume_from_jira_answer (loop.py:727) and the
      cockpit's dismiss (server.py:3147) both depend on the pop, as does
      eu48_state_writers_concurrency_test:96. The claim is opt-in; this is the contract guard.
  B1. The offset is NOT advanced while route_message is still running (the crash window itself).
  B2. A process-level kill mid-route leaves the offset unadvanced → Telegram redelivers on restart.
  B3. A deterministic exception in route_message consumes the update anyway, so one poison message
      cannot wedge the queue behind it forever (poll_loop swallows and retries every 5s).
  B4. The happy path advances the offset once routing succeeded.

Real temp store + offset files; Telegram/Jira/SDK all stubbed; no network, no models.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import types

# Stub the Agent SDK + requests so importing the orchestrator never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import decisions, notify   # noqa: E402
from orchestrator.contracts import Ticket    # noqa: E402

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


class _Cfg:
    """Minimal stand-in for Config: only what the decision store touches."""
    def __init__(self, tmp_dir):
        self.audit_path = os.path.join(tmp_dir, "audit.jsonl")
        self.dry_run = True          # keeps _comment_answer off the (stubbed) tracker
        self.telegram_poller_host = "server"

    def app(self, name):
        return types.SimpleNamespace(name=name or "app", backlog_backend="none")


class _Audit:
    def __init__(self):
        self.events = []

    def record(self, kind, **kw):
        self.events.append((kind, kw))


def _ticket(tid="AUTO-1"):
    return Ticket(id=tid, key=tid, summary=f"sum {tid}", description=f"desc {tid}",
                  acceptance_criteria=[], ephemeral=True)


class _Boom(BaseException):
    """A BaseException, not an Exception: stands in for the process dying (SIGKILL / plan-limit
    stop) rather than a routine error, so it slips past every `except Exception` fail-soft in the
    path — which is exactly what a real crash does."""


_orig_run_bg = decisions._run_bg
_orig_route_message = decisions.route_message
_orig_get_updates = notify.get_updates
_orig_configured = notify.configured

try:
    # ===================================================================================== #
    # A. resolve()/handle_reply — the pop-before-run-start window
    # ===================================================================================== #
    with tempfile.TemporaryDirectory() as d1:
        cfg = _Cfg(d1)
        audit = _Audit()

        # --- A1: the run-start dies → the decision must survive, not vanish ---------------
        decisions.add(cfg, _ticket("AUTO-1"), "app", "Which date format?")
        decisions._run_bg = lambda *a, **k: (_ for _ in ()).throw(_Boom("process died"))
        try:
            decisions.handle_reply(cfg, audit, "AUTO-1: use DD/MM")
        except _Boom:
            pass   # the process "died" exactly in the gap the ticket describes
        survived = decisions.load(cfg)
        chk("A1 crash before the run starts leaves the decision recoverable (not lost)",
            len(survived) == 1 and survived[0].get("id") == "AUTO-1", str(survived))

        # --- A2: the stranded claim is still EXCLUSIVE -------------------------------------
        # A second reply arriving while the first is genuinely in flight must not resolve the
        # same decision twice (EU-48's guarantee). The entry is claimed, so it is not on offer.
        second = decisions.resolve(cfg, "a different answer", "AUTO-1")
        chk("A2 an in-flight claim is not handed to a second racing reply",
            second is None, str(second))

        # --- A3: a claim stranded by a dead process is reaped ------------------------------
        # Backdate the claim past the TTL — i.e. no live process can still be working it.
        stale = decisions.load(cfg)
        stale[0]["claimed_at"] = time.time() - (decisions._CLAIM_TTL_SEC + 60)
        decisions._save(cfg, stale)
        reaped = decisions.resolve(cfg, "answered again after the crash", "AUTO-1")
        chk("A3 a claim stranded by a dead process is reaped → answerable again",
            reaped is not None and reaped.get("id") == "AUTO-1", str(reaped))
        chk("A3 the reaped-then-resolved decision leaves the store drained",
            decisions.load(cfg) == [], str(decisions.load(cfg)))

    # --- A4: the happy path drains the store (no in_flight leak into 'Needs you') ----------
    with tempfile.TemporaryDirectory() as d2:
        cfg = _Cfg(d2)
        audit = _Audit()
        started = []
        decisions._run_bg = lambda cfg, audit, worklist, **k: (started.append(worklist) or True)
        decisions.add(cfg, _ticket("AUTO-2"), "app", "Which date format?")
        ok = decisions.handle_reply(cfg, audit, "AUTO-2: use DD/MM")
        chk("A4 happy path resumes the ticket", ok is True and len(started) == 1)
        chk("A4 happy path fully drains the store once the run has started",
            decisions.load(cfg) == [], str(decisions.load(cfg)))
        chk("A4 the answer reached the worklist",
            bool(started) and "use DD/MM" in started[0][0][1].description, str(started))

    # --- A5: bare resolve() still pops in ONE shot (loop.py:727 / server.py:3147) ----------
    with tempfile.TemporaryDirectory() as d3:
        cfg = _Cfg(d3)
        decisions.add(cfg, _ticket("AUTO-3"), "app", "Which date format?")
        got = decisions.resolve(cfg, "from a Jira comment", "AUTO-3", comment=False)
        chk("A5 bare resolve() still returns the entry", got is not None and got.get("id") == "AUTO-3")
        chk("A5 bare resolve() still pops immediately — no commit() needed by existing callers",
            decisions.load(cfg) == [], str(decisions.load(cfg)))

    # ===================================================================================== #
    # B. poll_once — the offset-before-routing window
    # ===================================================================================== #
    notify.configured = lambda: True
    os.environ.pop("TELEGRAM_CHAT_ID", None)

    def _one_update(uid=7, text="AUTO-9: yes"):
        def _get(offset=None, timeout=0):
            # Telegram's single-consumer queue: once the caller asks past uid, it is gone forever.
            if offset is not None and offset > uid:
                return []
            return [{"update_id": uid, "message": {"text": text, "chat": {"id": ""}}}]
        return _get

    # --- B1/B2: a kill mid-route must not consume the update -------------------------------
    with tempfile.TemporaryDirectory() as d4:
        cfg = _Cfg(d4)
        seen_offset = {}

        def _dying_route(cfg_, audit_, text):
            # Snapshot the offset AS ROUTING RUNS — this is the crash window. If the offset is
            # already at the update here, a kill on the next line eats the message for good.
            seen_offset["during"] = decisions._read_offset(cfg_)
            raise _Boom("process died mid-route")

        notify.get_updates = _one_update()
        decisions.route_message = _dying_route
        try:
            decisions.poll_once(cfg, _Audit())
        except _Boom:
            pass
        chk("B1 the offset is NOT advanced while route_message is still running",
            seen_offset.get("during") is None, f"offset during route={seen_offset.get('during')}")
        chk("B2 a kill mid-route leaves the update unconsumed → Telegram redelivers",
            decisions._read_offset(cfg) is None, str(decisions._read_offset(cfg)))

        # Prove the redelivery actually recovers the message on the next poll.
        redelivered = []
        decisions.route_message = lambda cfg_, audit_, text: (redelivered.append(text) or True)
        decisions.poll_once(cfg, _Audit())
        chk("B2 the redelivered update is routed on the next poll (message recovered)",
            redelivered == ["AUTO-9: yes"], str(redelivered))
        chk("B2 the recovered update is consumed once routing succeeded",
            decisions._read_offset(cfg) == 7, str(decisions._read_offset(cfg)))

    # --- B3: a poison message must not wedge the queue behind it ---------------------------
    with tempfile.TemporaryDirectory() as d5:
        cfg = _Cfg(d5)
        audit = _Audit()
        attempts = {"n": 0}

        def _poison(cfg_, audit_, text):
            attempts["n"] += 1
            raise ValueError("deterministic bug on this message")

        notify.get_updates = _one_update(uid=11, text="a message that always explodes")
        decisions.route_message = _poison
        decisions.poll_once(cfg, audit)
        decisions.poll_once(cfg, audit)   # a wedged queue would re-serve the same update forever
        chk("B3 a deterministically failing message is attempted once, not retried forever",
            attempts["n"] == 1, f"attempts={attempts['n']}")
        chk("B3 the poison update is consumed so later messages can get through",
            decisions._read_offset(cfg) == 11, str(decisions._read_offset(cfg)))
        chk("B3 the dropped message is reported, not silently swallowed",
            any(k == "telegram_route_failed" for k, _ in audit.events), str(audit.events))

    # --- B4: the happy path still advances the offset --------------------------------------
    with tempfile.TemporaryDirectory() as d6:
        cfg = _Cfg(d6)
        routed = []
        notify.get_updates = _one_update(uid=42, text="AUTO-1: yes")
        decisions.route_message = lambda cfg_, audit_, text: (routed.append(text) or True)
        handled = decisions.poll_once(cfg, _Audit())
        chk("B4 happy path routes the update and reports it handled",
            handled == 1 and routed == ["AUTO-1: yes"], f"handled={handled} routed={routed}")
        chk("B4 happy path advances the offset after routing",
            decisions._read_offset(cfg) == 42, str(decisions._read_offset(cfg)))

finally:
    decisions._run_bg = _orig_run_bg
    decisions.route_message = _orig_route_message
    notify.get_updates = _orig_get_updates
    notify.configured = _orig_configured

print("\n============ EU-372 DECISION CRASH-SAFETY QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
