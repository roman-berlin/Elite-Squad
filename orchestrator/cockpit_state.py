"""Cockpit shared run-state + the stdout→Live-feed plumbing.

Split out of ``server.py`` (F16: decompose templates/routes/state) so the global
run-state and the log ring buffer live in one small module that both the routes
(``server.py``) and the views (``cockpit_views.py``) import. Behaviour is unchanged:
``server`` re-exports these names, so ``server._state`` / ``server._Tee`` / ``server.recent_log``
stay valid for callers and tests, and every importer shares the SAME mutable objects.
"""
from __future__ import annotations

import collections as _collections
import contextvars
import os
import threading
import time
from dataclasses import asdict, dataclass, field

# --------------------------------------------------------------------------------------------------
# EU-64 — PER-PROJECT run state (retire the single global run-lock).
#
# The cockpit used to carry ONE global ``_state`` dict and ONE global ``_run_lock``, which serialised
# every run across the whole unit: project B couldn't start while project A was running. That single
# global is replaced here by per-app keyed state + a per-app lock so distinct projects run truly in
# parallel, while two near-simultaneous POSTs for the SAME project still can't double-start (the
# per-app TOCTOU guard).
#
# Back-compat: the legacy single-context names stay valid. ``_state`` and ``_run_lock`` ARE the
# state/lock for the default key (``None``) — so existing callers that do ``_state["active"]`` or
# ``with _run_lock:`` keep operating on the default run, while new per-app callers go through
# ``get_state(app)`` / ``run_lock_for(app)`` / ``claim_run(app)``.
# --------------------------------------------------------------------------------------------------

_STATE_KEYS = ("active", "last_msg", "last_msg_record", "last_result", "last_result_record",
               "dry_run", "last_activity", "run_started", "stop_event", "stopping", "log_seq",
               "autopilot_mode", "autopilot_on", "log_path",
               "plan_limit_hit", "plan_limit_reset_at",
               # EU-579/582: QA run-state fields — set/reset/tracked by qa_api()._bg
               "qa_started", "qa_phase", "qa_error_phase",
               "qa_findings", "qa_verdict", "qa_dismissed",
               "qa_app")   # EU-582: remembers the app that ran QA (for retry after failure)


def _new_state() -> dict:
    """A fresh, fully-keyed run-state for one project (or the default ``None`` key)."""
    return {"active": False, "last_msg": "", "last_msg_record": None, "last_result": "",
            "last_result_record": None, "dry_run": None, "last_activity": None,
            "run_started": None, "stop_event": None,
            # EU-687: set True the moment a stop is CONFIRMED for this app's run (the cockpit's
            # stop paths set it alongside stop_event.set()); render_board reads it to flip the
            # run card 'Working' → amber 'Stopping — finishing the current step' at once, and
            # release_run clears it so the chip never survives into the idle 'last run' header.
            "stopping": False, "log_seq": 0,
            "autopilot_mode": None, "autopilot_on": False, "log_path": None,
            "plan_limit_hit": False, "plan_limit_reset_at": None,
            # EU-579/582: QA run-state defaults
            "qa_started": None, "qa_phase": None, "qa_error_phase": None,
            "qa_findings": [], "qa_verdict": "", "qa_dismissed": True,
            "qa_app": None}   # EU-582

# ``last_msg``  : sticky control-bar note (run/standup/drill state); cleared on /memory & /needs.
# ``last_result``: one-shot read-and-clear result banner for the side-effectful / actions
#                  (ship / promote / patrol) — set by their _bg, shown once on /, then cleared.

# The default (single-context) run-state, kept under the ``None`` key. ``_state`` is an ALIAS of
# ``_states[None]`` so legacy ``_state[...]`` access stays valid.
_state = _new_state()
_run_lock = threading.Lock()
# Per-app registries. The ``None`` key is the legacy/default run; concrete app names are added lazily
# by ``get_state`` / ``run_lock_for``. ``_registry_lock`` guards the lazy-create of both dicts.
_states: "dict[object, dict]" = {None: _state}
_run_locks: "dict[object, threading.Lock]" = {None: _run_lock}
_registry_lock = threading.Lock()
# Serialises the cross-app cap check + per-app claim so the max-parallel-runs cap is honoured even
# when two DIFFERENT projects race to start at the same instant.
_claim_lock = threading.Lock()

# Shared, monotonic log sequence — bumped on every captured stdout line so SSE streamers (per-tab or
# global) can long-poll for "is there anything new". Each app's own ``log_seq`` is bumped too.
_shared_log_seq = 0
_log_seq_lock = threading.Lock()

# Optional cap on how many projects may run CONCURRENTLY (0 / None = unlimited). Set from config via
# ``set_max_parallel_runs``; an ``EU_MAX_PARALLEL_RUNS`` env var overrides it.
_max_parallel_runs = 0


# --------------------------------------------------------------------------------------------------
# Per-app run-state accessors / helpers (the foundation the other EU-64 slices import).
# --------------------------------------------------------------------------------------------------

def get_state(app: str | None = None) -> dict:
    """The run-state dict for ``app`` (lazily created). ``app=None`` returns the default ``_state``.

    Same object on every call for a given app, so callers mutate the live state in place."""
    if app is None:
        return _state
    with _registry_lock:
        st = _states.get(app)
        if st is None:
            st = _new_state()
            _states[app] = st
        return st


def run_lock_for(app: str | None = None) -> threading.Lock:
    """The per-app run lock (lazily created). ``app=None`` returns the default ``_run_lock``.

    Acquire it around any check-then-set on ``get_state(app)["active"]`` to close the same-project
    TOCTOU race that the single global ``_run_lock`` used to close unit-wide."""
    if app is None:
        return _run_lock
    with _registry_lock:
        lk = _run_locks.get(app)
        if lk is None:
            lk = threading.Lock()
            _run_locks[app] = lk
        return lk


def is_active(app: str | None = None) -> bool:
    """Whether ``app`` currently has a run in flight."""
    return bool(get_state(app).get("active"))


def active_runs() -> list[object]:
    """The keys (app names; ``None`` for the default) of every currently-active run."""
    with _registry_lock:
        return [key for key, st in _states.items() if st.get("active")]


def active_run_count() -> int:
    """How many PROJECTS are running right now (distinct app keys with a claimed run).

    This is a per-app boolean tally: under ``max_concurrent_builders ≥ 2`` one app can
    build two tickets at once and still counts as 1 here.  For the true concurrent-BUILD
    total — the header badge's number (EU-479) — use ``warroom.total_live_run_count()``,
    which sums ``live_runs()`` per app on demand on top of ``active_runs()``."""
    return len(active_runs())


def max_parallel_runs() -> int:
    """The concurrent-run cap (0 = unlimited). ``EU_MAX_PARALLEL_RUNS`` env var overrides config."""
    env = os.environ.get("EU_MAX_PARALLEL_RUNS")
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            pass
    return max(0, int(_max_parallel_runs or 0))


def set_max_parallel_runs(n: int | None) -> None:
    """Set the concurrent-run cap from config (0 / None = unlimited)."""
    global _max_parallel_runs
    _max_parallel_runs = max(0, int(n or 0))


def claim_run(app: str | None = None, *, dry_run: bool | None = None,
              stop_event: object | None = None) -> bool:
    """Atomically claim a run slot for ``app``: the check-then-set that starts exactly one run.

    Returns True if the caller now owns the run (it must ``release_run`` when done), or False if a
    run for ``app`` is already in flight OR the max-parallel-runs cap is reached. Holds the global
    claim lock (so the cross-app cap is honoured under a race) and the per-app lock (so two POSTs for
    the SAME project can't both win)."""
    with _claim_lock:
        cap = max_parallel_runs()
        st = get_state(app)
        if cap and not st.get("active") and active_run_count() >= cap:
            return False
        with run_lock_for(app):
            if st.get("active"):
                return False
            st["active"] = True
            st["run_started"] = time.time()
            st["last_activity"] = time.time()
            # EU-687: a freshly-claimed run is working, never stopping — clear the flag here too
            # (not only in release_run) so a stale True left by a run that died WITHOUT releasing
            # can never make the NEXT run's card open on the amber 'Stopping' chip.
            st["stopping"] = False
            if dry_run is not None:
                st["dry_run"] = dry_run
            if stop_event is not None:
                st["stop_event"] = stop_event
            return True


def bind_stop_event(app: str | None, stop_event: object) -> None:
    """Make ``stop_event`` THE authoritative stop signal for ``app`` — the one the cockpit reaches.

    The invariant this exists to hold (EU-356): *while ``autopilot_on`` is true for an app, the event
    reachable at ``st["stop_event"]`` IS the event that app's live loop polls.*

    ``claim_run`` binds the event only on a SUCCESSFUL claim, which silently drops it on a failed one.
    That is not a theoretical gap: ``autopilot()`` treats a failed claim as "someone else owns the run
    slot" and runs the drain ANYWAY (``owns_run_state=False``), polling a local Event that the state
    never learned about. The slot's previous owner's Event stays bound — so the cockpit's Stop/drain
    sets a DEAD event, ``get_autopilot_status`` reads that same dead event back as ``stopping: true``,
    and the live loop — never signalled — keeps picking new tickets: unstoppable except by killing the
    process. (The live 2026-07-15 22:39→01:53 drain that motivated EU-356 turned out, on audit-log
    forensics, NOT to be this path — its binding was correct and its stop was late because run_loop's
    ticket-boundary stop check was never armed; see loop.run's ``stop_between_tickets``. This binding
    hole is the adjacent defect the same investigation demonstrated with a live repro harness.)

    So binding must NOT be conditional on owning the slot: the live loop is the authority on its own
    stop signal, and it rebinds on entry. Pair every bind with ``unbind_stop_event`` on the way out so
    the event can never outlive the loop that polls it and go stale.
    """
    with run_lock_for(app):
        get_state(app)["stop_event"] = stop_event


def unbind_stop_event(app: str | None, stop_event: object) -> bool:
    """Clear ``app``'s bound stop event — but ONLY when it is still ``stop_event`` (identity-checked).

    The mirror of ``bind_stop_event``, and the reason a stale event can't linger: an exiting loop
    retracts its own signal, so nothing can later ``.set()`` a dead Event and be told ``stopping: true``.

    Identity-checked so a loop that has already been SUPERSEDED (a newer drain for the same app rebound
    its own event while this one was winding down) can't clear the live loop's binding on its way out —
    the same last-writer-wins hazard, just in the other direction. Returns True when this call actually
    cleared the binding.
    """
    with run_lock_for(app):
        st = get_state(app)
        if st.get("stop_event") is stop_event:
            st["stop_event"] = None
            return True
        return False


def release_run(app: str | None = None) -> None:
    """Release ``app``'s run slot on any terminal outcome (merged / errored / no_changes /
    postmortem / stopped).

    Clears the run-slot + liveness fields — ``active``, ``autopilot_on``, ``run_started``,
    ``stop_event``, ``stopping`` and ``last_activity`` — so a finished run never lingers as
    'Working' (or 'Stopping') on the cockpit tab.  Mirror of ``claim_run`` (EU-104).

    EU-687: ``stopping`` is cleared HERE — the single place every terminal outcome funnels
    through — so the amber 'Stopping — finishing the current step' chip dies with the run and
    the card returns to its idle state. render_board ORs this flag with the stop_event's own
    is_set(), so zeroing ``stop_event`` alone (below) also hides the chip; clearing the flag is
    what keeps it from resurfacing on the next render that reads the persisted state.

    Deliberately does NOT touch ``last_msg``.  Every run's ``_bg`` writes the failure reason
    there (``st['last_msg'] = str(exc)``) and ``release_run`` runs in the SAME ``finally``
    immediately afterwards — so clearing it here would silently swallow every run error before the
    operator could read why the run failed (the EU-104 iteration-2 review rejection).  Zeroing the
    transient 'Working / stopping…' control-bar note on a CLEAN terminal outcome is the ``_bg``'s
    job instead: it alone knows whether the run raised, so it clears the note only when it didn't —
    see the run/report/autopilot ``_bg`` finally blocks in ``server.py`` (EU-104 iteration-3).

    EU-200: Bumps the log sequence so the SSE stream wakes immediately and the board reflects
    the run-end state change without waiting for the 2s heartbeat.
    """
    with run_lock_for(app):
        st = get_state(app)
        st["active"] = False
        st["autopilot_on"] = False     # zero the autopilot badge; set externally too, but defensive
        st["run_started"] = None
        st["stop_event"] = None
        st["stopping"] = False         # EU-687: the confirmed-stop chip must not outlive the run
        st["last_activity"] = None     # clear heartbeat so stale timestamps never show after release
        # EU-200: Wake the SSE stream immediately so the board updates without delay.
        # Only bump the sequence counters, NOT last_activity (EU-104 requires it stay None).
        _bump_log_seq_only(app)


def bump_log_seq(app: str | None = None) -> int:
    """Bump the shared log sequence AND ``app``'s own ``log_seq`` + heartbeat; return the shared seq.

    Called on every captured stdout line. The shared counter wakes global SSE streamers; the per-app
    ``log_seq`` wakes that project's tab streamer."""
    global _shared_log_seq
    with _log_seq_lock:
        _shared_log_seq += 1
        seq = _shared_log_seq
    st = get_state(app)
    st["last_activity"] = time.time()   # heartbeat — proves the run is alive
    st["log_seq"] = st.get("log_seq", 0) + 1
    return seq


def _bump_log_seq_only(app: str | None = None) -> int:
    """Bump only the sequence counters, NOT last_activity. Used by release_run to wake SSE.

    EU-200: Wakes the SSE stream immediately on run_end without setting last_activity
    (which EU-104 requires stays None after release_run)."""
    global _shared_log_seq
    with _log_seq_lock:
        _shared_log_seq += 1
        seq = _shared_log_seq
    st = get_state(app)
    st["log_seq"] = st.get("log_seq", 0) + 1
    return seq


def shared_log_seq() -> int:
    """The current shared log sequence (for global, not-per-app, SSE long-polling)."""
    return _shared_log_seq


def get_autopilot_status(app: str | None = None) -> dict:
    """Per-app autopilot status snapshot: ``{on, stopping, mode, external}``.

    Derives the snapshot from the per-app run-state rather than the unit-wide ``_state["autopilot"]``
    sub-dict, so each tab's badge is independent.

    ``on`` is read from the dedicated per-app ``autopilot_on`` flag — set ONLY when an autopilot loop
    claims this app's run (``server.py`` Start / ``autopilot.autopilot``), never by a plain manual
    run. This de-conflates the two (EU-103 iter-2): a manual cockpit/answer-box run sets ``active``
    but NOT ``autopilot_on``, so it is no longer rendered as "Autopilot ON" with dead Stop/Finish
    buttons. ``active`` alone means "a run is in flight"; ``autopilot_on`` means "that run is the
    autopilot".

    * ``on``       — True while an AUTOPILOT loop is running for this app (the dedicated flag) OR
                     an external daemon is running (detected via ``autopilot.daemon_running()`` and
                     ``autopilot.daemon_is_external()``).
    * ``stopping`` — True when autopilot is on AND a stop_event has been issued but the loop hasn't
                     exited yet (a graceful drain is in progress).
    * ``mode``     — the per-app autopilot mode (``'choose'`` | ``'drain'`` | ``None``).
    * ``external`` — True when a detached daemon (foreign process) is running the autopilot — detected
                     by checking if the PID file exists and belongs to a different process (EU-120).

    The ``ticket`` field (the workspace tab's selected ticket) is NOT included here because
    cockpit_state has no access to the workspace layer; the caller (``server.py``) overlays it.
    """
    from . import autopilot as _autopilot

    st = get_state(app)
    stop_ev = st.get("stop_event")
    internal_on = bool(st.get("autopilot_on"))

    # EU-120: detect external daemon (detached terminal run or launchd keepalive)
    external_daemon = _autopilot.daemon_is_external() if _autopilot.daemon_running() else False

    # When an external daemon is running, report autopilot as ON even if this cockpit's
    # per-app ``autopilot_on`` flag is False (the daemon lives in a different process).
    on = internal_on or external_daemon

    # EU-356: ``stopping`` is gated on ``internal_on``, NOT ``on``. A stop_event is an in-process
    # threading.Event — it can only ever signal a loop running in THIS process, so an EXTERNAL daemon
    # (a different process; ``on`` is true via the PID-file probe) can never be "stopping" because of
    # a local Event. Reading it through the wider ``on`` let a leftover Event from this cockpit's own
    # earlier drain render a foreign daemon as "stopping…" forever — a badge that promised a stand-down
    # nobody had ordered and no loop would honour. Only the live local loop's own event speaks here;
    # ``bind_stop_event`` guarantees that is the one bound while ``autopilot_on``.
    return {
        "on": on,
        "stopping": bool(internal_on and stop_ev is not None and stop_ev.is_set()),
        "mode": st.get("autopilot_mode"),
        "external": external_daemon,
    }


def reset_run_state() -> None:
    """Drop every per-app run-state + lock back to the default-only registry — test seam."""
    global _shared_log_seq
    defaults = _new_state()
    with _registry_lock:
        _states.clear()
        _states[None] = _state
        _run_locks.clear()
        _run_locks[None] = _run_lock
        # Fully replace with fresh defaults (handles both existing keys and new
        # ones like EU-579's qa_* fields, plus drops orphaned ad-hoc flags).
        _state.clear()
        _state.update(defaults)
    with _log_seq_lock:
        _shared_log_seq = 0

# Ring buffer of the unit's stdout — fed to the War Room's "Live feed" panel so you can watch
# the implementation steps in the dashboard, not just the terminal.
# EU-104: each entry is a (line, app_key) tuple so per-tab live-feed panels can filter to
# their own project's output.  ``app_key`` is the concrete app name (a string) when exactly
# one project was active at write time, or ``None`` for unattributed / ambiguous output.
_LOG: "_collections.deque[tuple[str, object]]" = _collections.deque(maxlen=600)


def recent_log(n: int = 60, app: object = None) -> list[str]:
    """Return the last ``n`` log lines, optionally scoped to a single project.

    When ``app`` is ``None`` (the default) all entries are returned regardless of their
    attributed project — preserving the existing "show everything" behaviour used by the
    global / unscoped view.  When ``app`` is provided only lines whose ``app_key`` matches
    are included, so each tab's live-feed panel shows only its own project's output (EU-104).
    """
    if app is not None:
        lines = [line for line, key in list(_LOG) if key == app]
    else:
        lines = [line for line, _key in list(_LOG)]
    return lines[-n:]


def _sse(event: str, data: str) -> str:
    """Format one Server-Sent Event. Multi-line `data` is split into the required `data:` lines."""
    body = "".join("data: " + ln + "\n" for ln in data.replace("\r", "").split("\n"))
    return f"event: {event}\n{body}\n"


# EU-272: run-scoped stdout attribution. The global len(active_runs)==1 heuristic below collapses
# to None the moment two runs are in flight — under a concurrent drain (EU-380) that is 100% of
# lines. A ContextVar set by the run wrapper (same pattern as backends._BACKEND, EU-189) flows
# through asyncio tasks, so each slot's prints attribute to ITS app with no global state.
_RUN_APP: "contextvars.ContextVar[str | None]" = contextvars.ContextVar("run_app", default=None)


def set_run_app(app_key: str | None) -> contextvars.Token[str | None]:
    """Bind this (async) context's stdout attribution to ``app_key``; returns the reset token."""
    return _RUN_APP.set(app_key)


def reset_run_app(token) -> None:
    try:
        _RUN_APP.reset(token)
    except Exception:  # noqa: BLE001 — a cross-context reset must never crash a run teardown
        pass


# EU-635: run-log FILE routing — deliberately a SEPARATE ContextVar from _RUN_APP. The concurrent
# drain registers each ticket's log handle under the (app.name, slot) TUPLE (two same-app slots
# sharing one key would clobber each other's handle — the EU-272 hazard). But _RUN_APP's value
# also feeds the _LOG ring-buffer append, bump_log_seq and per-tab UI filtering, which all want
# the bare app.name — so the file-routing key gets its own var instead of widening _RUN_APP into
# a tuple. _Tee.write uses this var for run_logger.write_line ONLY; unset (the serial path and
# every non-concurrent caller) it falls back to app_key, byte-identical to pre-EU-635 behaviour.
_RUN_LOG_KEY: "contextvars.ContextVar[object]" = contextvars.ContextVar("run_log_key", default=None)


def set_run_log_key(log_key: object) -> contextvars.Token[object]:
    """Bind this (async) context's run-log file routing to ``log_key``; returns the reset token.

    ``log_key`` is the handle-registry key the concurrent _worker opened its per-ticket log under
    (the ``(app.name, slot)`` tuple). Captured lines then route to THAT file while UI attribution
    (_RUN_APP) stays the bare app name. Mirror of ``set_run_app`` (EU-272)."""
    return _RUN_LOG_KEY.set(log_key)


def reset_run_log_key(token) -> None:
    try:
        _RUN_LOG_KEY.reset(token)
    except Exception:  # noqa: BLE001 — a cross-context reset must never crash a run teardown
        pass


class _Tee:
    """Mirror stdout to the real terminal AND the ring buffer (skips the noisy poll line)."""
    def __init__(self, real):
        self._real = real

    def write(self, s: str) -> None:
        self._real.write(s)
        # EU-104: tag each captured line with the currently-active project so per-tab live-feed
        # panels can filter to their own project's output. EU-272: the run-scoped ContextVar wins
        # when set (correct under concurrency); otherwise the old single-active-run heuristic —
        # exactly today's behaviour for threads and paths that never bound a context.
        app_key = _RUN_APP.get()
        if app_key is None:
            runs = active_runs()
            app_key = runs[0] if len(runs) == 1 else None
        # EU-635: the per-ticket log FILE routes through the separate _RUN_LOG_KEY ContextVar so a
        # concurrent drain's (app.name, slot)-keyed handle actually receives this slot's lines.
        # app_key (UI attribution for the _LOG append + bump_log_seq below) stays the bare app name.
        # Unset → fall back to app_key: the exact pre-EU-635 lookup the serial path relies on.
        log_key = _RUN_LOG_KEY.get()
        if log_key is None:
            log_key = app_key
        for line in s.splitlines():
            t = line.rstrip()
            if t and "/api/board" not in t and "GET /api/" not in t:
                _LOG.append((t, app_key))
                # Heartbeat + wake SSE streamers: bump the shared seq (wakes global streamers)
                # AND the per-app seq (wakes that project's tab streamer).  Falls back to the
                # default (None-key) state when no concrete app is active.
                bump_log_seq(app_key)
                # EU-106: stream the line to the per-run log file (if one is open for this app).
                # Lazy import avoids a circular dependency at module load time.
                try:
                    from . import run_logger as _rl
                    _rl.write_line(log_key, t)
                except Exception:  # noqa: BLE001 — log writes must never abort a run
                    pass

    def flush(self) -> None:
        self._real.flush()

    def isatty(self) -> bool:
        return getattr(self._real, "isatty", lambda: False)()


# --------------------------------------------------------------------------------------------------
# EU-63 — per-tab cockpit workspace (one-project-per-tab; "All projects"/* is gone).
#
# The cockpit used to carry a SINGLE selected project (``?app=…``) with a literal ``"*"`` sentinel
# meaning "All projects". That single-context assumption is replaced here by an explicit *workspace*:
# an ordered set of TABS, each pinned to exactly ONE concrete project, each holding its own view
# state (board / ticket-picker selection / runs / needs-you). The routes (server.py) and the views
# (cockpit_views.py) build on this model. The "*"/all-projects view no longer exists, so ``"*"`` is
# rejected as a project name here rather than special-cased downstream.
#
# Persistence: workspaces live in a server-side, per-session store (``_workspaces``, guarded by
# ``_workspace_lock``) so the open tabs survive a page reload within a session. ``to_dict`` /
# ``from_dict`` give the routes a JSON-able shape they can ALSO hand to the browser for a
# localStorage round-trip (rehydrate-on-load) — see ``rehydrate_workspace``.
# --------------------------------------------------------------------------------------------------

ALL_PROJECTS_SENTINEL = "*"   # the retired "All projects" selector — never a valid tab project.


@dataclass
class Tab:
    """One cockpit tab, pinned to a single concrete project, carrying that tab's view state.

    A tab is the unit of the tabbed workspace: it never represents "all projects". ``project`` is a
    real app name (``config.yaml``); the remaining fields are the per-tab view state the cockpit
    panels render — kept here (not in module globals) so each open project keeps its own selection,
    board layout, run list and needs-you queue independently of the others.
    """
    project: str
    ticket: str | None = None          # ticket-picker selection (the chosen ticket key, or None)
    board: dict = field(default_factory=dict)       # per-tab board view state (columns/filters)
    runs: list = field(default_factory=list)        # run ids/state surfaced in this tab
    needs_you: list = field(default_factory=list)   # pending "needs-you" decision ids for this tab

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Tab":
        """Build a Tab from an untrusted dict (localStorage / session store), dropping unknown keys
        and coercing the containers so a malformed payload can't crash the cockpit."""
        project = str(data.get("project", "")).strip()
        if not project or project == ALL_PROJECTS_SENTINEL:
            raise ValueError(f"a tab must be pinned to a concrete project, not {project!r}")
        ticket = data.get("ticket")
        return cls(
            project=project,
            ticket=str(ticket) if ticket else None,
            board=dict(data.get("board") or {}),
            runs=list(data.get("runs") or []),
            needs_you=list(data.get("needs_you") or []),
        )


@dataclass
class Workspace:
    """An ordered set of tabs plus which one is active — one cockpit session's open projects.

    Mutual exclusion is the core invariant: a project may be open in AT MOST one tab. ``add_tab``
    focuses the existing tab instead of opening a duplicate, so the tab list is always a set of
    distinct projects in open order.

    Thread-safety: ``_lock`` (one per ``Workspace`` instance) guards concurrent tab mutations
    (``add_tab``, ``remove_tab``, ``set_active``). The module-level ``_workspace_lock`` guards only
    the ``_workspaces`` dict (the lazy-create step in ``workspace_for``); these two locks are
    independent and never held at the same time.
    """
    tabs: list[Tab] = field(default_factory=list)
    active: str | None = None          # the active tab's project (None when no tabs are open)
    _lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    # -- queries -----------------------------------------------------------------------------------
    def projects(self) -> list[str]:
        """The open projects, in tab order."""
        return [t.project for t in self.tabs]

    def get_tab(self, project: str) -> Tab | None:
        """The tab pinned to ``project``, or None if that project isn't open."""
        for t in self.tabs:
            if t.project == project:
                return t
        return None

    def active_tab(self) -> Tab | None:
        """The currently active tab, or None when the workspace is empty."""
        return self.get_tab(self.active) if self.active else None

    # -- mutations ---------------------------------------------------------------------------------
    def add_tab(self, project: str, *, activate: bool = True) -> Tab:
        """Open ``project`` in a tab (or focus its existing tab — mutual exclusion).

        Rejects the retired "All projects" sentinel and empty names. Returns the tab either way, so
        callers get the same object whether the project was already open or freshly added.
        """
        with self._lock:
            project = (project or "").strip()
            if not project or project == ALL_PROJECTS_SENTINEL:
                raise ValueError(f"cannot open a tab for {project!r}: tabs pin to one concrete project")
            existing = self.get_tab(project)
            if existing is not None:
                if activate:
                    self.active = project
                return existing
            tab = Tab(project=project)
            self.tabs.append(tab)
            if activate or self.active is None:
                self.active = project
            return tab

    def remove_tab(self, project: str) -> bool:
        """Close ``project``'s tab. Returns True if a tab was removed.

        If the closed tab was active, the active tab falls back to the neighbour to its left (or the
        new first tab), matching the usual editor "close tab" behaviour; the workspace goes empty
        (``active is None``) only when the last tab is closed.
        """
        with self._lock:
            idx = next((i for i, t in enumerate(self.tabs) if t.project == project), None)
            if idx is None:
                return False
            was_active = self.tabs[idx].project == self.active
            self.tabs.pop(idx)
            if was_active:
                if not self.tabs:
                    self.active = None
                else:
                    self.active = self.tabs[max(0, idx - 1)].project
            return True

    def set_active(self, project: str) -> bool:
        """Focus an already-open tab. Returns False (no change) if the project isn't open."""
        with self._lock:
            if self.get_tab(project) is None:
                return False
            self.active = project
            return True

    # -- persistence -------------------------------------------------------------------------------
    def to_dict(self) -> dict:
        """JSON-able snapshot for the session store AND the browser localStorage round-trip."""
        return {"tabs": [t.to_dict() for t in self.tabs], "active": self.active}

    @classmethod
    def from_dict(cls, data: dict | None) -> "Workspace":
        """Rebuild a workspace from an untrusted snapshot, enforcing mutual exclusion.

        Malformed tabs are skipped rather than fatal; duplicate projects collapse to the first
        occurrence; ``active`` is clamped to a project that actually survived (else the first tab).
        """
        ws = cls()
        seen: set[str] = set()
        for raw in (data or {}).get("tabs") or []:
            try:
                tab = Tab.from_dict(raw)
            except (ValueError, TypeError, AttributeError):
                continue
            if tab.project in seen:
                continue
            seen.add(tab.project)
            ws.tabs.append(tab)
        active = (data or {}).get("active")
        ws.active = active if active in seen else (ws.tabs[0].project if ws.tabs else None)
        return ws


# Server-side per-session workspace store. Keyed by an opaque session id supplied by the routes
# (cookie / header). Guarded so concurrent requests for the same session can't corrupt the tab list.
#
# EU-362 — the store is BOUNDED. ``server._session_id()`` mints a fresh ``token_hex`` for any
# request that arrives without the session cookie, so every curl / health probe / uptime monitor
# hit of ``/`` used to add a Workspace here permanently — a slow-motion leak in a cockpit that
# runs for days (2026-07-16 total audit, item 1). Two bounds, both enforced on every store access:
#   TTL  — a session untouched for ``_WORKSPACE_TTL_S`` is dead (a real browser re-sends its
#          cookie on every poll, so live sessions are re-stamped constantly);
#   LRU  — ``_WORKSPACE_MAX`` hard-caps the dict however fast one-shot probes mint fresh ids,
#          evicting the least-recently-touched sessions first. 256 concurrent cockpit sessions is
#          far beyond a single-operator unit; an evicted-but-alive browser degrades gracefully —
#          its next request rebuilds an empty workspace (or rehydrates from localStorage).
_WORKSPACE_TTL_S = 24 * 3600
_WORKSPACE_MAX = 256
_workspaces: "dict[str, Workspace]" = {}
_workspace_touch: "dict[str, float]" = {}   # session id -> last-access ts (the LRU/TTL ledger)
_workspace_lock = threading.Lock()


def _evict_workspaces(now: float, keep: str) -> None:
    """Enforce the TTL + LRU bounds. Caller holds ``_workspace_lock``; ``keep`` (the session being
    served right now) is never evicted."""
    for sid, ts in list(_workspace_touch.items()):
        if sid != keep and now - ts > _WORKSPACE_TTL_S:
            _workspaces.pop(sid, None)
            _workspace_touch.pop(sid, None)
    if len(_workspaces) > _WORKSPACE_MAX:
        for sid, _ts in sorted(_workspace_touch.items(), key=lambda kv: kv[1]):
            if sid == keep:
                continue
            _workspaces.pop(sid, None)
            _workspace_touch.pop(sid, None)
            if len(_workspaces) <= _WORKSPACE_MAX:
                break


def workspace_for(session_id: str) -> Workspace:
    """The (lazily created) server-side workspace for ``session_id``. Same object on every call, so
    routes mutate the live tab set in place; persists while the session stays live (EU-362: idle
    sessions expire after ``_WORKSPACE_TTL_S`` and the store is LRU-capped at ``_WORKSPACE_MAX``).

    Thread-safety: ``_workspace_lock`` guards only this dict (the lazy-create/evict steps below);
    each ``Workspace`` carries its own ``_lock`` for concurrent tab mutations (``add_tab``,
    ``remove_tab``, ``set_active``). The two locks are independent and never nested."""
    with _workspace_lock:
        now = time.time()
        _workspace_touch[session_id] = now
        ws = _workspaces.get(session_id)
        if ws is None:
            ws = Workspace()
            _workspaces[session_id] = ws
        # Sweep AFTER the insert so the cap counts the incoming session too (evicting before it
        # lands would let the store settle at cap+1); ``keep`` shields the session being served.
        _evict_workspaces(now, keep=session_id)
        return ws


def rehydrate_workspace(session_id: str, data: dict | None) -> Workspace:
    """Replace ``session_id``'s server-side workspace from a client snapshot (localStorage rehydrate
    on load) and return it. Lets the browser restore the open tabs after a reload that started a
    fresh server session; the rebuilt workspace re-enforces mutual exclusion via ``from_dict``."""
    ws = Workspace.from_dict(data)
    with _workspace_lock:
        now = time.time()
        _workspace_touch[session_id] = now       # EU-362: the other store writer stamps too
        _workspaces[session_id] = ws
        _evict_workspaces(now, keep=session_id)  # sweep after the insert — see workspace_for
    return ws


def reset_workspaces() -> None:
    """Drop every stored workspace — test seam / session-clear hook."""
    with _workspace_lock:
        _workspaces.clear()
        _workspace_touch.clear()


# --------------------------------------------------------------------------------------------------
# EU-118 — Plan-limit state tracking
# --------------------------------------------------------------------------------------------------

def set_plan_limit_hit(app: str | None = None, *, hit: bool = True,
                       reset_at: float | None = None) -> None:
    """Set the plan-limit state for ``app`` and optionally when it resets.

    Called by the autopilot when plan limits are hit/cleared. The reset timestamp
    is when the Claude plan limit renews (typically weekly for Max plans).
    """
    st = get_state(app)
    st["plan_limit_hit"] = hit
    st["plan_limit_reset_at"] = reset_at


def is_plan_limit_hit(app: str | None = None) -> bool:
    """Whether a plan limit is currently hit for ``app``."""
    return bool(get_state(app).get("plan_limit_hit"))


def plan_limit_reset_at(app: str | None = None) -> float | None:
    """When the plan limit for ``app`` resets (epoch seconds), or None if unknown."""
    return get_state(app).get("plan_limit_reset_at")
