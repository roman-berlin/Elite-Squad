"""EU-257 — Telegram poller: process-level singleton + stop lifecycle + locked offset.

EU-185 elects ONE HOST to poll Telegram; this closes the per-PROCESS gap it leaves. On the
elected host, TWO spawn sites (serve's startup, and every autopilot()/cockpit-drain invocation)
each unconditionally started their own `poll_loop` thread against one unlocked offset file — serve's
poller plus one more per drain Start, all racing on `getUpdates`+offset and each running
`route_message`'s side effects (ticket resume, Jira comment/transition, council reply) on the same
update.

Pinned:
  1. ensure_poll_loop() is a process-level singleton: repeated calls (simulating serve + two
     per-app drain Starts) leave exactly ONE live poll_loop thread.
  2. poll_loop honours a stop_event (a poller owned by a finite run stands down when that run
     stops) and clears the module registration on exit, so a later start re-establishes a poller;
     repeated start/stop cycles never grow the thread count.
  3. Two poll_once calls racing one offset file, with notify.get_updates stubbed to return exactly
     one queued update, route route_message exactly once (locking.locked_call serialises the
     fetch-then-ack window) — no double-route.
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, ".")

from orchestrator import decisions, notify, sync   # noqa: E402

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


class _Cfg:
    def __init__(self, tmp_dir, poller="server"):
        self.telegram_poller_host = poller
        self.audit_path = os.path.join(tmp_dir, "audit.jsonl")


def _live_poller_threads():
    return [t for t in threading.enumerate() if t.name == "telegram-poll-loop" and t.is_alive()]


_orig_configured = notify.configured
_orig_host_id = sync.host_id
_orig_env = os.environ.get("GENERAL_TELEGRAM_POLLER")
_orig_poll_once = decisions.poll_once
_orig_get_updates = notify.get_updates
_orig_route_message = decisions.route_message


def _elect_this_host():
    notify.configured = lambda: True
    sync.host_id = lambda cfg=None: "server"
    os.environ.pop("GENERAL_TELEGRAM_POLLER", None)   # no override -> election rule decides


try:
    _elect_this_host()

    # -------------------------------------------------------------------------------------
    # 1. Repeated ensure_poll_loop() calls (serve start + two per-app drain Starts) on a host
    #    where should_poll_telegram is True leave exactly ONE live poll_loop thread.
    # -------------------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as d1:
        cfg1 = _Cfg(d1)
        decisions.poll_once = lambda cfg, audit: 0   # no real network I/O in the background loop
        ev1 = threading.Event()   # attached to the FIRST (winning) start — see below
        try:
            t_serve = decisions.ensure_poll_loop(cfg1, audit=None, stop_event=ev1, interval=0.01)
            t_drain1 = decisions.ensure_poll_loop(cfg1, audit=None, stop_event=threading.Event(),
                                                   interval=0.01)
            t_drain2 = decisions.ensure_poll_loop(cfg1, audit=None, stop_event=threading.Event(),
                                                   interval=0.01)
            time.sleep(0.05)
            live = _live_poller_threads()
            chk("repeated starts -> exactly one live poll_loop thread", len(live) == 1,
                f"{len(live)} live threads")
            chk("ensure_poll_loop returns the SAME thread every call (no-op when already live)",
                t_serve is t_drain1 is t_drain2, (t_serve, t_drain1, t_drain2))
        finally:
            # Only the FIRST call's stop_event is wired to the actual (singleton) thread — the
            # no-op calls' events were never attached to anything live. Stop it so it can't leak
            # into the next section's thread count.
            ev1.set()
            t_serve.join(timeout=2)
            decisions.poll_once = _orig_poll_once

    # -------------------------------------------------------------------------------------
    # 2. poll_loop given a stop_event terminates once that event is set, and clears the module
    #    singleton registration so a SUBSEQUENT start re-establishes a poller. Repeated
    #    start/stop cycles keep the live thread count at 1 (never growing).
    # -------------------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as d2:
        cfg2 = _Cfg(d2)
        decisions.poll_once = lambda cfg, audit: 0
        try:
            max_seen = 0
            last_thread = None
            for cycle in range(3):
                ev = threading.Event()
                t = decisions.ensure_poll_loop(cfg2, audit=None, stop_event=ev, interval=0.01)
                time.sleep(0.03)
                max_seen = max(max_seen, len(_live_poller_threads()))
                ev.set()
                t.join(timeout=2)
                chk(f"cycle {cycle}: poller thread stopped after stop_event.set()", not t.is_alive())
                chk(f"cycle {cycle}: module registration cleared on exit",
                    decisions._poller_thread is None, decisions._poller_thread)
                last_thread = t
            chk("start/stop cycles never grow the live thread count (max stayed at 1)",
                max_seen == 1, f"max_seen={max_seen}")
            # A later start after the registration cleared re-establishes a fresh poller.
            ev2 = threading.Event()
            t_new = decisions.ensure_poll_loop(cfg2, audit=None, stop_event=ev2, interval=0.01)
            chk("subsequent start re-establishes a NEW poller thread", t_new is not last_thread and t_new.is_alive())
            ev2.set()
            t_new.join(timeout=2)
        finally:
            decisions._poller_thread = None
            decisions.poll_once = _orig_poll_once

    # -------------------------------------------------------------------------------------
    # 3. Two poll_once calls racing one offset file: with exactly one queued update, route_message
    #    runs exactly once and the offset advances once — no double-route.
    # -------------------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as d3:
        cfg3 = _Cfg(d3)
        call_count = {"n": 0}
        call_lock = threading.Lock()
        release = threading.Event()
        delivered = {"served": False}

        def _slow_get_updates(offset=None, timeout=0):
            # Simulate Telegram's single-consumer getUpdates: the one queued update is served at
            # most once — a LATER call (offset already advanced past it) sees nothing new. Both
            # threads wait on the same gate so they reach this call at the same real time, then a
            # sleep widens the check-then-set window: WITHOUT poll_once's locking, two threads can
            # both observe "not yet served" here and both walk away with the update; WITH it, the
            # whole fetch-then-ack sequence is serialised so the second call always runs strictly
            # after the first one already set the flag.
            release.wait(1)
            if delivered["served"]:
                return []
            time.sleep(0.05)
            delivered["served"] = True
            return [{"update_id": 42, "message": {"text": "AUTO-1: yes", "chat": {"id": ""}}}]

        def _route_message(cfg, audit, text):
            with call_lock:
                call_count["n"] += 1
            return True

        notify.get_updates = _slow_get_updates
        decisions.route_message = _route_message
        os.environ.pop("TELEGRAM_CHAT_ID", None)

        results_box = []
        def _run():
            results_box.append(decisions.poll_once(cfg3, audit=None))

        try:
            threads = [threading.Thread(target=_run) for _ in range(2)]
            for t in threads:
                t.start()
            time.sleep(0.1)   # let both threads reach get_updates before releasing either
            release.set()
            for t in threads:
                t.join(timeout=2)

            chk("route_message ran exactly once despite two racing poll_once calls",
                call_count["n"] == 1, f"call_count={call_count['n']}")
            offset = decisions._read_offset(cfg3)
            chk("offset advanced exactly once (to the single update's id)", offset == 42, offset)
            total_handled = sum(results_box)
            chk("exactly one poll_once call reports the update as handled",
                total_handled == 1, f"total_handled={total_handled}")
        finally:
            notify.get_updates = _orig_get_updates
            decisions.route_message = _orig_route_message

finally:
    notify.configured = _orig_configured
    sync.host_id = _orig_host_id
    decisions.poll_once = _orig_poll_once
    notify.get_updates = _orig_get_updates
    decisions.route_message = _orig_route_message
    decisions._poller_thread = None
    if _orig_env is None:
        os.environ.pop("GENERAL_TELEGRAM_POLLER", None)
    else:
        os.environ["GENERAL_TELEGRAM_POLLER"] = _orig_env

passed = sum(1 for _, ok, _ in results if ok)
print(f"\neu257_poller_singleton_test: {passed}/{len(results)} passed")
for n, ok, det in results:
    if not ok:
        print(f"  FAIL: {n}" + (f" — {det}" if det else ""))
sys.exit(0 if passed == len(results) else 1)
