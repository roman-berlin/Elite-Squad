"""Control panel — a local web app cockpit (`general serve`).

Serves the dashboard plus a control bar so you can launch work with a button
(task / ticket / drain), choose effort, toggle live, and watch results. Binds to
localhost only. Runs happen in a background thread so the page stays responsive.
"""
from __future__ import annotations

import asyncio
import html
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote

from . import backend_pref
from . import backends
from . import dashboard as D
from . import health
from . import intake
from . import memory
from . import models_views
from . import warroom
from .audit import AuditLog
from .config import Config, normalize_effort
from .loop import run as run_loop

# F16 (decompose server.py): the cockpit's shared run-state and the inline-HTML templates now
# live in dedicated modules. They are re-imported here so callers and tests that reach for
# ``server._state`` / ``server._control_bar`` / ``server._Tee`` etc. keep working unchanged, and
# so every module shares the SAME mutable ``_state``/``_LOG`` objects.
from . import cockpit_state
from .cockpit_state import _LOG, _Tee, _run_lock, _sse, _state, recent_log  # noqa: F401
# EU-64: per-project run state. The run/stop routes + the SSE stream key off the request's project
# via these helpers so distinct projects run (and stream) truly in parallel, while a second run on
# the SAME project is still refused (the per-app TOCTOU guard inside ``claim_run``).
from .cockpit_state import (  # noqa: F401
    active_run_count,
    active_runs,
    claim_run,
    get_autopilot_status,
    get_state,
    is_active,
    release_run,
    shared_log_seq,
)
from .cockpit_views import (  # noqa: F401
    _CHAT_STYLE,
    _actbar,
    _actbtn,
    _bug_desc,
    _bug_title,
    _charged,
    _chat_bubbles,
    _chat_inner,
    _chat_tabs,
    _control_bar,
    _dual_provider_gauge,
    _group_inner,
    _result_banner,
    _wrap,
    _working,
)


def _first_shippable(cfg) -> str:
    """The first app that is an actual PRODUCT — i.e. NOT the unit's own repo (that one promotes via
    'Update unit', not ship-review). Used when ship-review is invoked with no single project selected
    ('All projects'), so we never call cfg.app('*'). Falls back to the first app."""
    try:
        from . import sync as _sync
        root = _sync._repo_root(cfg)
        for a in (getattr(cfg, "apps", None) or []):
            try:
                if Path(a.repo_path).resolve() != root:
                    return a.name
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return cfg.apps[0].name if getattr(cfg, "apps", None) else ""


MERGE_STATS_TIME_RANGES = ("today", "week", "month", "all")


def compute_merge_stats(audit_path: str, time_range: str, now: float | None = None) -> dict:
    """EU-158: aggregate `merged`/`pr_opened` land-outcome audit events into merge statistics for one
    time window. Pure function (no Flask) so it's directly testable — the route below is a thin
    request-parsing wrapper around it.

    ``time_range`` must be one of ``MERGE_STATS_TIME_RANGES``; the caller (the route) is responsible
    for rejecting anything else with a 400 before calling this. ``now`` defaults to ``time.time()``;
    tests pass a fixed value so windows are deterministic.

    Window start (local time, matching AuditLog.record's ``time.strftime`` local timestamps):
      today  -> local midnight of `now`'s date
      week   -> now - 7 days
      month  -> now - 30 days
      all    -> no lower bound (epoch)
    ``success_rate`` = merged / (merged + pr_opened) within the same window; ``None`` when there were
    no land attempts (avoids a divide-by-zero and avoids implying a false 100%)."""
    import json as _json

    if now is None:
        now = time.time()
    if time_range == "today":
        midnight = time.localtime(now)
        window_start = time.mktime((midnight.tm_year, midnight.tm_mon, midnight.tm_mday,
                                     0, 0, 0, 0, 0, -1))
    elif time_range == "week":
        window_start = now - 7 * 86400
    elif time_range == "month":
        window_start = now - 30 * 86400
    else:  # "all"
        window_start = 0.0

    merged = 0
    pr_opened = 0
    for line in D.audit_lines(audit_path):
        try:
            ev = _json.loads(line)
        except ValueError:
            continue
        event = ev.get("event")
        if event not in ("merged", "pr_opened"):
            continue
        ts = D._parse_ts(ev.get("ts", ""))
        if ts is None or time.mktime(ts.timetuple()) < window_start:
            continue
        if event == "merged":
            merged += 1
        else:
            pr_opened += 1

    total_attempts = merged + pr_opened
    success_rate = (merged / total_attempts) if total_attempts else None
    return {
        "time_range": time_range,
        "total_merges": merged,
        "pr_opened": pr_opened,
        "success_rate": success_rate,
        "window_start": window_start,
    }


# EU-361: guards the compare-and-set on the one-shot ceremony flags below. Separate from
# cockpit_state's run locks on purpose — a ceremony is not a run and must not contend with one.
_flag_lock = threading.Lock()

def _claim_flag(name: str, value=True) -> bool:
    """Compare-and-set a one-shot ceremony flag. True = the caller now OWNS the ceremony and must
    clear the flag when done; False = someone else already owns it, do nothing.

    EU-361 (2026-07-16 audit): all seven ceremony routes used to read the flag in the request
    thread but SET it inside ``_bg()`` —

        if not _state.get("standuping"):
            def _bg():
                _state["standuping"] = True     # ...several thread-scheduler ticks later

    Under ``app.run(..., threaded=True)`` two clicks milliseconds apart both passed the ``if`` and
    both spawned a worker: two concurrent standups / councils / scribes, or two ``group_chat``
    rounds answering the same message. ``ship_review`` already set its flag before the thread (the
    in-repo precedent); this closes the window for the other seven with a real lock, so the check
    and the set can't be split at all.
    """
    with _flag_lock:
        if _state.get(name):
            return False
        _state[name] = value
        return True


def _resolve_run_backend(rcfg, app_name: str | None = None) -> str | None:
    """EU-190/EU-223: set this run's backend from the persisted sticky preference (cockpit
    /api/model), resolving an optional PER-APP override first, then the global sticky pref, then
    the config.yaml default carried on ``rcfg``.

    ``app_name`` should be the concrete project THIS run targets (pass the run's own app, e.g. the
    per-app autopilot's ``key`` — None for a legacy/unit-wide call so two parallel drains, EU-103,
    each resolve their own backend). When omitted, falls back to the run's own single app
    (``rcfg.apps[0].name``) so a caller that only has ``rcfg`` (tests, the CLI) still resolves
    per-app correctly.

    Returns an error string to BLOCK the run when the chosen backend isn't runnable (GLM selected
    but ``GLM_AUTH_TOKEN`` missing) — *no silent fallback*, per EU-190 — else ``None``."""
    if app_name is None:
        _apps = getattr(rcfg, "apps", None)
        app_name = _apps[0].name if _apps else None
    # 2026-07-19: MAIN/SECONDARY resolution — when the main model can't run and a usable
    # secondary is configured, the run proceeds on the secondary (loudly); with no secondary the
    # old hard block fires unchanged (EU-190's no-SILENT-fallback rule — this fallback is loud).
    bk, why = backends.resolve_for_run(rcfg, app_name)
    rcfg.model_backend = bk
    if why:
        _note_model_fallback(why)
    if bk == backends.GLM and not backends.available("glm"):
        return ("GLM is selected but GLM_AUTH_TOKEN is not configured — set it and restart, "
                "or switch the Main model back to Opus (or configure a Secondary).")
    return None


_MODEL_FALLBACK_LOG_INTERVAL_S = 600.0
_last_model_fallback_log = 0.0


def _note_model_fallback(why: str) -> None:
    """One throttled console + Telegram line per window when the secondary engages — loud, not spam."""
    global _last_model_fallback_log
    import time as _time
    now = _time.time()
    if now - _last_model_fallback_log >= _MODEL_FALLBACK_LOG_INTERVAL_S:
        _last_model_fallback_log = now
        print(f"  ⇄ model fallback: {why}", flush=True)
        try:
            from . import notify as _notify
            _notify.send(f"⇄ Model fallback engaged — {why}. Runs continue on the secondary; "
                         "switch back or fix the main model when ready.")
        except Exception:  # noqa: BLE001
            pass


def create_app(cfg: Config, port: int = 8787):
    """Build the cockpit Flask app. ``port`` is the port ``serve()`` will actually bind.

    EU-361: the port is a parameter (not a constant) because the EU-254 Host guard below has to
    know the real bind address. ``general serve --port N`` (main.py) is a supported flag, and with
    the port hardcoded to 8787 every legitimate POST on any other port was rejected as a
    DNS-rebinding attempt. Defaults to 8787 so the single-operator flow — and every existing
    ``create_app(cfg)`` caller — is unchanged.
    """
    from flask import Flask, Response, redirect, request
    app = Flask(__name__)
    audit = AuditLog(cfg.audit_path)
    from . import usage
    usage.configure(cfg.audit_path)   # the cockpit process meters token burn too
    # QW4: every agent call also lands an `agent_call` audit event (model, tokens, duration).
    from . import agent as _agent
    _agent.configure_audit(audit)
    _agent.configure_timeouts(cfg)   # EU-221: per-tag wall-clock budgets (officer/builder)

    # ----------------------------------------------------------------------------------------------
    # EU-63 — tabbed one-project-per-tab workspace. The cockpit no longer has an "All projects"/`*`
    # context: EVERY request is scoped to exactly ONE concrete project — the active tab of this
    # browser's server-side workspace (``cockpit_state``). ``_session_id`` mints a per-browser cookie
    # so the open tabs survive reloads; ``_scope`` resolves a request's ``app`` param to a concrete
    # project, opening/focusing its tab, and falls back to the active tab (else the first app) when the
    # param is missing or the retired ``*`` sentinel. This replaces the old global switcher + the
    # `"*" -> all projects` special-casing that used to live in run/ship/patrol/tickets.
    # ----------------------------------------------------------------------------------------------
    _SID_COOKIE = "eu_cockpit_sid"
    _app_names = {a.name for a in (getattr(cfg, "apps", None) or [])}

    def _session_id() -> str:
        """The opaque per-browser session id (cookie). Minted on first sight; the new value is stashed
        on the request env so ``_ensure_session_cookie`` can set it on the response."""
        sid = request.cookies.get(_SID_COOKIE)
        if not sid:
            sid = request.environ.get("eu_new_sid") or secrets.token_hex(16)
            request.environ["eu_new_sid"] = sid
        return sid

    @app.after_request
    def _ensure_session_cookie(resp):
        sid = request.environ.get("eu_new_sid")
        if sid:
            resp.set_cookie(_SID_COOKIE, sid, max_age=60 * 60 * 24 * 365,
                            httponly=True, samesite="Lax")
        return resp

    # ----------------------------------------------------------------------------------------------
    # EU-254 — CSRF/Origin/Host guard on every state-changing route. form-urlencoded/multipart POSTs
    # are CORS "simple requests" (no preflight), so WITHOUT this a page open in Roman's browser — or
    # an attacker domain DNS-rebound to 127.0.0.1:8787 — could drive the unit (run, model switch,
    # stop, approve) cross-origin. Generalizes the EU-187-era /api/terminal-only origin allowlist
    # (that endpoint was removed 2026-07-19 with the cockpit terminal panel) to all ~30
    # state-changing POST routes (this file has no PUT/PATCH/DELETE today; guarded anyway so a future
    # one is covered for free). Same-origin check: if an Origin header is present it must name an
    # allowed cockpit host; else if a Referer is present its host must match; and in ALL cases the
    # Host header itself must be one of the allowed hosts (closes DNS-rebinding — Host is otherwise
    # unvalidated and an attacker page cannot forge the browser's real Origin/Referer, but DNS
    # rebinding lets them control what the server sees as Host while the socket still lands on
    # 127.0.0.1:8787). "localhost" (no port) is allowed alongside the real bind so the Flask test
    # client's default synthetic Host keeps working — the dev server never actually listens on the
    # default HTTP port, so that value can't arise from a real request.
    #
    # EU-361: the allowed set is built from the REAL bind port (``create_app``'s ``port`` arg), not
    # a hardcoded 8787. `general serve --port 9000` used to 403 every legitimate POST, because the
    # browser's honest `Host: 127.0.0.1:9000` matched nothing in this set.
    # ----------------------------------------------------------------------------------------------
    _ALLOWED_HOSTS = {f"127.0.0.1:{port}", f"localhost:{port}", "localhost"}
    _STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
    # EU-361: routes that are state-changing despite being GETs, so the guard below can't be
    # side-stepped by the verb alone. /api/open-logs spawns `open` via subprocess.Popen — a
    # side effect on the Commander's desktop — so an attacker page's <img src=…>/link/fetch to it
    # must be rejected exactly like a POST. Kept as an explicit allowlist rather than "guard every
    # GET": the dashboard's read-only polls (/api/board every 5s, the SSE streams) must stay
    # unguarded, which is the EU-254 contract these routes were carved out of.
    _STATE_CHANGING_GETS = {"/api/open-logs"}

    def _origin_host(value: str) -> str:
        from urllib.parse import urlsplit
        try:
            return (urlsplit(value).netloc or "").lower()
        except Exception:  # noqa: BLE001 — a malformed header is just treated as "no match"
            return ""

    @app.before_request
    def _csrf_origin_guard():
        if (request.method not in _STATE_CHANGING_METHODS
                and request.path not in _STATE_CHANGING_GETS):
            return None
        if (request.host or "").lower() not in _ALLOWED_HOSTS:
            return Response("Forbidden: mismatched Host header.", status=403, mimetype="text/plain")
        origin = request.headers.get("Origin")
        if origin:
            if _origin_host(origin) not in _ALLOWED_HOSTS:
                return Response("Forbidden: cross-origin request rejected.", status=403,
                                mimetype="text/plain")
            return None
        referer = request.headers.get("Referer")
        if referer and _origin_host(referer) not in _ALLOWED_HOSTS:
            return Response("Forbidden: cross-origin request rejected.", status=403,
                            mimetype="text/plain")
        return None

    def _ensure_tabs(ws):
        """Ensure every configured app has a tab, and restore last-active on first load."""
        if getattr(cfg, "apps", None):
            was_empty = not ws.tabs
            for a in cfg.apps:
                if not ws.get_tab(a.name):
                    ws.add_tab(a.name, activate=False)
            if was_empty:
                try:
                    from . import projects
                    recents = projects.recents(cfg)
                    if recents and recents[0] in _app_names:
                        ws.set_active(recents[0])
                except Exception:
                    pass

    def _scope(raw) -> str:
        """Resolve a request's project param to ONE concrete project for this session's active tab.

        A concrete, configured project opens/focuses its tab and becomes active. The retired
        ``*``/empty selector is NOT honoured as "all projects" any more — it falls back to whatever
        tab is already active, else the first configured app. Returns "" only when no apps exist.
        """
        ws = cockpit_state.workspace_for(_session_id())
        _ensure_tabs(ws)
        name = (raw or "").strip()
        if name and name != cockpit_state.ALL_PROJECTS_SENTINEL and name in _app_names:
            ws.add_tab(name)        # open or focus that project's tab, and make it active
        if ws.active is None and _app_names:
            ws.add_tab(cfg.apps[0].name)   # seed a first tab so the cockpit is never project-less
        return ws.active or (cfg.apps[0].name if cfg.apps else "")

    def _board_project(raw) -> str:
        """Read-only project resolver for the per-tab board/SSE stream: render the concrete project
        the requesting tab asked for (so each tab streams ONLY its own board) WITHOUT mutating which
        tab is active — otherwise every tab's background poll would fight over the active tab. Falls
        back to the active tab, then the first app, and never honours the retired ``*`` sentinel."""
        ws = cockpit_state.workspace_for(_session_id())
        _ensure_tabs(ws)
        name = (raw or "").strip()
        if name and name != cockpit_state.ALL_PROJECTS_SENTINEL and name in _app_names:
            return name
        return ws.active or (cfg.apps[0].name if cfg.apps else "")

    def _view_state(app: str | None) -> dict:
        """The run-state to RENDER for one tab's board/header (EU-64: per-project).

        Returns ``app``'s own per-app run-state (so each tab's board/live-feed reflect ONLY that
        project's run), with the still-unit-wide ``autopilot`` field overlaid so the header autopilot
        switch + the autopilot 'live' chip keep working until autopilot itself goes per-project. The
        per-app state is shallow-copied before the overlay, so that dict is never mutated; this path is
        NOT fully read-only, though — it refreshes the unit-wide ``_state['autopilot']`` sub-dict in
        place (the live ``daemon_running`` probe below) and, for a daemon this cockpit process never
        started, CREATES that entry so the badge has something to read (EU-73).

        EU-73: injects a live ``daemon_running`` field (PID-file check via ``autopilot.daemon_running()``)
        into the autopilot sub-dict so ``autopilot_switch()`` in warroom.py has a single source of
        truth regardless of how autopilot was launched (cockpit Start button vs. detached terminal
        daemon whose War Room terminal has since closed).
        """
        from . import autopilot as _ap
        st = get_state(app or None)
        ap = _state.get("autopilot")
        # EU-73: live PID-file probe — replaces the stale in-memory boolean.
        daemon_alive = _ap.daemon_running()
        if ap is not None:
            # Refresh in-place so subsequent renders (SSE ticks) always see the current value.
            ap["daemon_running"] = daemon_alive
        elif daemon_alive:
            # Daemon is alive but was not started through this cockpit process (e.g. terminal
            # launched, terminal closed, launchd keepalive).  Create a minimal autopilot entry so
            # the badge shows ON — there is no stop_event so the cockpit can't stop it directly.
            ap = {"on": False, "daemon_running": True, "stopping": False, "app": "(external daemon)"}
            _state["autopilot"] = ap
        if st is _state or not ap:
            return st
        view = dict(st)
        view["autopilot"] = ap
        return view

    def _claim_cockpit_run(app_name: str, *, stop_event=None):
        """Claim ``app_name``'s run slot for a manual cockpit run (EU-64: per-project).

        Honours BOTH guards: the per-project one (a second run on the SAME project is refused, other
        projects unaffected) AND the still-unit-wide one that autopilot / Telegram-resume hold on the
        legacy ``_state`` (until those migrate, a manual project run must not overlap them). Returns
        the per-app run-state dict on success — the caller owns the run and MUST ``release_run`` it —
        or None when refused, with ``last_msg`` already set on the right state for the banner.

        EU-361: ``stop_event`` is published with the claim, in the REQUEST thread. It used to be
        minted inside the worker's ``_bg()``, so between "POST /api/run returns the redirect" and
        "the OS schedules the worker" the run was already ``active`` (the board shows Stop) but
        ``st['stop_event']`` was still None — a Stop click in that window found no event and
        silently did nothing. ``claim_run`` has always accepted the event (cockpit_state.claim_run);
        the autopilot Start path already passed it this way — this brings the three manual run
        routes onto the same pattern.
        """
        key = app_name or None
        st = get_state(key)
        busy = "a run is already in progress for this project — wait for it to finish, then start the new one"
        if is_active(key):
            st["last_msg"] = busy
            return None
        if key is not None and is_active(None):
            # A unit-wide run (autopilot / Telegram resume) holds the legacy global guard.
            _state["last_msg"] = ("a run is already in progress — stop it and wait for it to finish, "
                                  "then start the new one")
            return None
        if not claim_run(key, stop_event=stop_event):
            # Lost the start race (TOCTOU) or hit the max-parallel-runs cap.
            st["last_msg"] = busy if is_active(key) else (
                "too many projects are running at once — wait for one to finish, then start this one")
            return None
        return st

    @app.get("/")
    def index():
        appq = _scope(request.args.get("app"))   # one concrete project — the session's active tab
        try:
            from . import projects
            projects.record_recent(cfg, appq)   # VS-Code-style "recent projects"
        except Exception:  # noqa: BLE001
            pass
        h = health.summary(cfg)
        # One-shot result banner for ship/promote/patrol — shown once, then cleared (read-and-clear),
        # so a side-effectful action's outcome doesn't linger like the sticky last_msg note.
        # EU-106: pass is_mac so _control_bar can gate the '📂 Open logs' button (macOS only).
        import platform as _platform
        # EU-235: splice the '/models' nav link into the rendered bar. The link is owned by
        # models_views (see add_models_nav_link's docstring for why it's injected here rather
        # than edited into _control_bar) — the EU-293 nav-link finding, landed with the page.
        bar = (_result_banner(_state)
               + models_views.add_models_nav_link(
                   _control_bar(cfg, appq, h["healthy"],
                                is_mac=_platform.system() == "Darwin")))
        # EU-64: render THIS tab's project state so each project's board/live-feed is independent.
        # (The one-shot result banner stays on the unit-wide ``_state`` — ship/promote/patrol are
        # unit-level actions, not per-project runs.)
        return warroom.render_page(cfg, appq, _view_state(appq), bar, h)

    @app.get("/api/health")
    def health_api():
        import platform
        from flask import jsonify
        # EU-106: include is_mac so the UI layer can decide whether to show "Open logs" buttons.
        # EU-405: include active_run_count so `./general deploy` — a SEPARATE process that can't see
        # the serve process's in-memory cockpit_state — can probe the authoritative "is a build
        # running?" signal before it restarts. This is the live counterpart to
        # autopilot.in_flight_builds' audit-tail signal.
        return jsonify({**health.summary(cfg), "is_mac": platform.system() == "Darwin",
                        "active_run_count": active_run_count()})

    @app.get("/api/open-logs")
    def open_logs_api():
        """Open a local log file or folder in macOS Finder (macOS only).

        Query param: ``path=<log path>`` — absolute or relative path to the log file or folder.

        Security:
        * Path traversal guard: the resolved path must be under ``cfg.log_folder``;
          anything outside returns 403.
        * macOS only: non-Mac returns 403 (the ``open`` command is Darwin-specific).

        Returns JSON ``{"ok": true, "path": "<resolved>"}`` on success.
        """
        import platform
        import subprocess
        from flask import jsonify
        # Only macOS has the `open` shell command that opens Finder/default app.
        if platform.system() != "Darwin":
            return Response("This endpoint is only available on macOS.", status=403,
                            mimetype="text/plain")
        # 2026-07-19: ``path`` is OPTIONAL — no param opens the log root itself. The button used
        # to pass the raw cfg.log_folder string ("logs/"), which resolves against the CWD while
        # the guard's root is audit-path-anchored (state/logs) — so the button 403'd its own
        # endpoint forever. One derivation now: the endpoint anchors, callers don't.
        from . import run_logger as _rl
        root = _rl.log_root(cfg)
        path_param = (request.args.get("path") or "").strip()
        if not path_param:
            requested = root
        else:
            try:
                # a RELATIVE path is resolved under the log root (never the CWD); absolute paths
                # keep the strict under-root check below.
                requested = (Path(path_param) if Path(path_param).is_absolute()
                             else root / path_param).resolve()
            except Exception:
                return Response("Invalid path.", status=400, mimetype="text/plain")
        # Path traversal guard: the resolved path must sit under the log root.
        try:
            requested.relative_to(root)
        except ValueError:
            return Response("Path is outside the configured log folder.", status=403,
                            mimetype="text/plain")
        # Shell out to `open` — non-blocking; Finder/default app opens in the background.
        subprocess.Popen(["open", str(requested)], close_fds=True)   # noqa: S603,S607
        return jsonify({"ok": True, "path": str(requested)})

    @app.get("/api/autopilot")
    def autopilot_status_api():
        """Live autopilot state: PID-based daemon check + in-memory cockpit flags.

        Returns a JSON object with:
          - ``daemon_running``: True if the autopilot PID file exists and the process is alive
            (the single source of truth — EU-73)
          - ``on``: the unit-wide in-memory cockpit flag (True only when started via Start button)
          - ``stopping``: True while a graceful drain is in progress (unit-wide)
          - ``app``: the project the autopilot is working (or None) (unit-wide)
          - ``apps``: per-app map keyed by app slug; each entry is
            ``{on, stopping, ticket, mode}`` read from the per-app run-state for every tab
            open in the current workspace session (EU-103).
        """
        from flask import jsonify
        from . import autopilot as _ap
        alive = _ap.daemon_running()
        cur = _state.get("autopilot") or {}
        # Refresh daemon_running in the live state dict so subsequent page renders are consistent.
        if cur:
            cur["daemon_running"] = alive

        # EU-103: build per-app map for every tab open in this session's workspace.
        ws = cockpit_state.workspace_for(_session_id())
        apps_map: dict = {}
        for tab in ws.tabs:
            status = get_autopilot_status(tab.project)
            status["ticket"] = tab.ticket   # overlay the tab's selected ticket (workspace layer)
            apps_map[tab.project] = status

        return jsonify({
            "on": cur.get("on", False),
            "daemon_running": alive,
            "stopping": cur.get("stopping", False),
            "app": cur.get("app"),
            "apps": apps_map,
        })

    @app.post("/api/autopilot")
    def autopilot_api():
        import copy
        from . import autopilot as ap
        action = request.form.get("action", "toggle")

        # EU-103: resolve the TARGET project PER-APP. The per-tab controls always post their project's
        # `app`; a legacy form with no `app` targets the unit-wide (None-key) autopilot. ``_scope("")``
        # would seed the first app, so only run it when an app was actually posted — otherwise this is
        # the all-backlog-apps autopilot, keyed under None (== the default ``_state``).
        _form_app = (request.form.get("app") or "").strip()
        app_name = (_scope(_form_app) or None) if _form_app else None
        key = app_name
        st = get_state(key)

        # EU-74: preserve ?app= on every redirect so the project selector stays in sync with the
        # per-tab autopilot control. Prefer the posted app; else fall back to the unit-wide autopilot's
        # recorded scope (a legacy Stop/drain that carries no `app`); never leak a placeholder.
        _state_app = ((_state.get("autopilot") or {}).get("app") or "").strip()
        if _state_app in {"(no project)", "(external daemon)"}:
            _state_app = ""
        _redir_app = _form_app or _state_app
        _redir = f"/?app={_redir_app}" if _redir_app else "/"

        # EU-103: persist the per-app autopilot mode ('choose' | 'drain') when supplied. Stored in the
        # SELECTED app's run-state so each tab carries its own mode independently of whether autopilot
        # is running. Validation rejects unknown values so the state never holds garbage.
        _mode_param = (request.form.get("mode") or "").strip() or None
        if _mode_param is not None:
            if _mode_param not in ("choose", "drain"):
                st["last_msg"] = f"invalid autopilot mode {_mode_param!r} — expected 'choose' or 'drain'"
                return redirect(_redir)
            st["autopilot_mode"] = _mode_param

        ap_on = bool(get_autopilot_status(key)["on"])   # the per-app autopilot signal (not a manual run)

        if action == "start":
            # EU-103: actually HONOUR the mode — the two start buttons must do different things.
            #  · 'choose' hands off to the per-ticket picker (pick specific tickets, then run them) —
            #    this is the real per-ticket flow, not a blind drain dressed up with a lying confirm.
            #  · 'drain' (default) starts the continuous backlog autopilot for this project.
            mode = _mode_param or st.get("autopilot_mode")
            if mode == "choose":
                return redirect(f"/tickets?app={app_name}" if app_name else "/tickets")

            # ── Auto-drain: start the continuous per-app autopilot ────────────────────────────────
            if ap_on:
                st["last_msg"] = "autopilot is already running for this project"
                return redirect(_redir)
            if not health.summary(cfg)["healthy"]:
                st["last_msg"] = "autopilot blocked — fix the health problems first"
                return redirect(_redir)
            # Detached-daemon guard (EU-73), now per-app aware (EU-103): refuse only for a FOREIGN
            # daemon process (a launchd keepalive / a terminal that owns the whole queue), NOT this
            # cockpit's own in-process autopilot threads — those are tracked per-app and run in
            # parallel, so starting Elite-Unit while automatixy already runs must NOT be refused here.
            if ap.daemon_is_external():
                st["last_msg"] = ("autopilot is already running as a detached daemon — stop it first "
                                  "(unload the launchd keepalive agent, or close the terminal it runs "
                                  "in) before starting another")
                return redirect(_redir)
            # Cross-guard: a unit-wide run (an all-apps autopilot / Telegram resume on the None key)
            # blocks a per-app autopilot start too — it may touch this project. A DIFFERENT project's
            # run does NOT block (that is the per-app parallelism this ticket is about).
            if key is not None and is_active(None):
                st["last_msg"] = ("a run is already in progress — stop it and wait for it to finish, "
                                  "then start autopilot")
                return redirect(_redir)

            ev = threading.Event()
            ap_cfg = copy.copy(cfg)
            ap_cfg.dry_run = False     # continuous autopilot must be live (else it re-picks forever)
            _berr = _resolve_run_backend(ap_cfg, key)  # EU-190/EU-223: THIS app's (or global, key=None)
            if _berr:                              # sticky Model pick (blocks an unconfigured GLM), so
                st["last_msg"] = _berr             # two per-app drains (EU-103) each get their own.
                return redirect(_redir)
            # Claim THIS project's run slot atomically (per-app TOCTOU guard + the cross-project
            # parallel cap). Flask is threaded=True, so two near-simultaneous Starts for the same
            # project both pass the checks above; claim_run lets exactly one win. A manual run holding
            # this app's slot also makes the claim fail (the two can't run_loop the same app at once).
            if not claim_run(key, dry_run=False, stop_event=ev):
                st["last_msg"] = ("a run is already in progress for this project — wait for it to "
                                  "finish, then start autopilot") if is_active(key) else (
                    "too many projects are running at once — wait for one to finish, then start this one")
                return redirect(_redir)
            # The dedicated autopilot signal (distinct from a manual run's bare ``active``). Set
            # synchronously so the first render after Start already shows Autopilot ON; the loop sets it
            # again and the _bg finally clears it.
            st["autopilot_on"] = True

            def _bg():
                try:
                    asyncio.run(ap.autopilot(ap_cfg, key, once=False, stop_event=ev))
                except Exception as exc:  # noqa: BLE001
                    st["last_msg"] = f"autopilot error: {exc}"
                finally:
                    st["autopilot_on"] = False   # autopilot off for this app
                    release_run(key)             # clears active / run_started / stop_event for THIS app
            threading.Thread(target=_bg, daemon=True).start()
            return redirect(_redir)

        if action == "drain" and ap_on:
            # Graceful stop for THIS project: signal only this app's stop_event, then stay on +
            # "stopping" until the worker's finally clears autopilot_on. Acts on the SELECTED app's
            # state — never a single global autopilot — so draining one project leaves others running.
            ev = st.get("stop_event")
            # EU-356: record the REQUEST itself. ev.set() leaves no trace, so the 2026-07-15 forensics
            # could not place the operator's drain click closer than "somewhere in a 3-hour window" —
            # every stop order now lands in the audit trail whether or not a live event was reachable.
            audit.record("autopilot_stop_requested", action="drain", app=app_name or "",
                         event_reachable=ev is not None)
            if ev is not None:
                ev.set()
            # EU-120: for external daemons (launchd keepalive or detached terminal), durably stop
            # the launchd service after signaling the stop_event so the current ticket finishes.
            # This must happen AFTER ev.set() so graceful shutdown happens first.
            if ap.daemon_is_external():
                stopped = ap._stop_launchd_daemon()
                # EU-232: _stop_launchd_daemon() now polls daemon_running() post-bootout, so this is a
                # verified outcome (not launchctl's optimistic exit code) — surface it to the operator.
                if stopped:
                    st["last_msg"] = "✓ external launchd daemon confirmed stopped — it will not respawn."
                else:
                    st["last_msg"] = ("⚠ couldn't confirm the external launchd daemon stopped — "
                                      "KeepAlive may respawn it; check `launchctl list` manually.")
            return redirect(_redir)

        if action == "stop" and ap_on:
            # Immediate stop for THIS project: flip its autopilot OFF now (the in-flight build still
            # finishes in the background) and signal only this app's stop_event.
            ev = st.get("stop_event")
            # EU-356: same request-trail as the drain branch — the click itself must be auditable.
            audit.record("autopilot_stop_requested", action="stop", app=app_name or "",
                         event_reachable=ev is not None)
            if ev is not None:
                ev.set()
            st["autopilot_on"] = False
            # EU-120: for external daemons (launchd keepalive or detached terminal), durably stop
            # the launchd service. Without launchctl bootout, clicking 'Stop' on an external daemon
            # doesn't actually stop it—the KeepAlive respawn makes the button a no-op for external runs.
            if ap.daemon_is_external():
                stopped = ap._stop_launchd_daemon()
                # EU-232: verified outcome (see the drain branch above) — surface it to the operator.
                if stopped:
                    st["last_msg"] = "✓ external launchd daemon confirmed stopped — it will not respawn."
                else:
                    st["last_msg"] = ("⚠ couldn't confirm the external launchd daemon stopped — "
                                      "KeepAlive may respawn it; check `launchctl list` manually.")
            return redirect(_redir)

        # toggle / unknown action → no-op (the mode persist above already took effect).
        return redirect(_redir)

    @app.get("/api/board")
    def board_api():
        from flask import Response
        appq = _board_project(request.args.get("app"))   # board is per-tab — one concrete project
        # EU-106: log_lines param removed from render_board (Live Feed panel retired).
        return Response(warroom.render_board(cfg, appq, _view_state(appq)),
                        mimetype="text/html")

    @app.get("/api/stream")
    def stream_api():
        """Server-Sent Events: push a freshly-rendered board the moment the unit prints a step
        (sub-second), and at least every 2s (keeps the elapsed timer + heartbeat alive). The
        browser swaps #board on each frame; it falls back to the 5s poll if the stream drops."""
        from flask import Response
        # Resolve the tab's concrete project ONCE, up front: the generator outlives the request, so we
        # can't touch ``request`` inside it. Each tab opens its own EventSource(?app=<its project>), so
        # this makes every tab stream ONLY its own project's board.
        appq = _board_project(request.args.get("app"))

        def gen():
            last_seq = None
            last_emit = 0.0
            while True:
                # EU-64: wake on EITHER the shared stdout sequence (the unit's live feed isn't
                # per-app attributable — the stdout Tee bumps the shared counter) OR this project's
                # own ``log_seq`` (its run heartbeat). The RENDERED board is per-project, so each
                # tab's feed/board update independently even though the wake signal is shared.
                seq = (shared_log_seq(), get_state(appq or None).get("log_seq", 0))
                now = time.time()
                if seq != last_seq or now - last_emit >= 2.0:
                    last_seq, last_emit = seq, now
                    try:
                        # EU-106: log_lines param removed from render_board (Live Feed panel retired).
                        yield _sse("board",
                                   warroom.render_board(cfg, appq, _view_state(appq)))
                    except Exception:  # noqa: BLE001 - never let a render error kill the stream
                        yield ": render-error\n\n"
                time.sleep(0.5)

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/run-log-stream")
    def run_log_stream_api():
        """Stream the current run's log file in real-time using Server-Sent Events.

        EU-200: This provides a live view of the run log in the web dashboard,
        distinct from the interactive terminal panel. The log file is tailed
        and new lines are sent as SSE events.

        Query params:
            app: The project name (defaults to active tab)
        """
        from flask import Response
        appq = _board_project(request.args.get("app"))
        st = get_state(appq or None)

        def gen():
            appkey = appq or None
            # 2026-07-19: WAIT for the log instead of closing — the panel connects at page load,
            # often seconds BEFORE the run's open_run_log sets log_path; the old instant
            # "No active run log" + close left the panel on "Waiting for run output…" through
            # EventSource reconnect flicker. Poll the state until a path appears (or ~60s idle).
            log_path = st.get("log_path")
            waited = 0.0
            while not log_path and waited < 60.0:
                yield ": waiting-for-log\n\n"
                time.sleep(1.0)
                waited += 1.0
                log_path = get_state(appkey).get("log_path")
            if not log_path:
                yield _sse("log", "No active run log to stream.")
                return

            log_file = Path(log_path)
            if not log_file.exists():
                yield _sse("log", f"Log file not found: {log_path}")
                return

            def _drain(f: Path, start: int):
                """SSE frames for everything appended to ``f`` past byte ``start``."""
                with f.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(start)
                    for line in fh.readlines():
                        yield _sse("log", line.rstrip("\n\r"))

            # Stream the log file, sending new lines as they're added
            last_size = 0
            last_check = 0.0

            while True:
                try:
                    current_st = get_state(appkey)   # fresh state EVERY tick — see the roll below
                    current_size = log_file.stat().st_size
                    if current_size > last_size:
                        yield from _drain(log_file, last_size)
                        last_size = current_size
                        last_check = time.time()
                    else:
                        # EU-361 (2026-07-16 audit): re-read the run's log path on every tick. It
                        # used to be captured ONCE, above, before the loop — but a drain opens a NEW
                        # log file per ticket, so from ticket #2 on this stream sat tailing the
                        # FIRST ticket's finished, never-growing log. The unit was working; the
                        # cockpit looked frozen. Rolling only from the else-branch is deliberate:
                        # the finished file is fully drained first, so the tail of ticket N is never
                        # traded for the head of ticket N+1.
                        nxt = (current_st.get("log_path") or "").strip()
                        if nxt and nxt != str(log_file) and Path(nxt).exists():
                            log_file, last_size = Path(nxt), 0
                            last_check = time.time()
                            time.sleep(0.5)
                            continue
                        if not current_st.get("active") and not current_st.get("autopilot_on"):
                            # Run ended — drain whatever landed since the stat above, then close.
                            # (The pre-EU-361 code re-tested ``current_size > last_size`` here, which
                            # is by construction false inside this else-branch: the final lines of
                            # every run were silently dropped. Re-stat instead.)
                            final_size = log_file.stat().st_size
                            if final_size > last_size:
                                yield from _drain(log_file, last_size)
                            yield _sse("done", "Run ended.")
                            break
                        # No new lines - send keepalive every 2s
                        now = time.time()
                        if now - last_check >= 2.0:
                            yield ": keepalive\n\n"
                            last_check = now

                    time.sleep(0.5)
                except Exception:
                    yield _sse("error", "Error reading log file.")
                    break

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/tasks")
    def tasks_page():
        # EU-129: Resolve the active project first so needs.count() can scope to it.
        _appq = _board_project(request.args.get("app"))
        # 2026-07-19 (Commander order): the task log's job is "the tickets DONE in the project I
        # choose" — so with no explicit ?filter= it opens on Merged → dev. ?filter=all shows every
        # run; the other deep-link filters (needs/parked) keep working.
        _raw_flt = request.args.get("filter")
        flt = "merged" if _raw_flt is None else _raw_flt.strip()
        # 'parked' scopes to the auto-skipped blocked set, which lives outside the task log.
        blocked = warroom._load_blocked(cfg) if flt.lower() == "parked" else None
        try:
            from . import needs as _needs_mod
            _needs_cnt = _needs_mod.count(cfg, _appq)
        except Exception:  # noqa: BLE001
            _needs_cnt = None
        # 2026-07-19 (Commander order): Today / This week / This month / Total scoping — the
        # period filters the run set BEFORE rendering, so the KPI cards and the rows agree.
        _since = (request.args.get("since") or "all").strip().lower()
        _all_tasks = D.load_tasks(cfg.audit_path)
        page = D.render_html(D.filter_tasks_since(_all_tasks, _since), show_cost=_charged(),
                             dismissed=D.load_dismissed(cfg.audit_path),
                             active_filter=flt, blocked=blocked, needs_count=_needs_cnt,
                             cfg=cfg, app_name=_appq, period=_since)
        # Header injected after the template's </header>: a back button + a per-project chip row.
        # 2026-07-19: this used to append the FULL cockpit control bar (model selector, autopilot,
        # resume buttons) — none of which belongs on a log page — and its back-button CSS was
        # written as non-f-string pieces with doubled {{ }} braces, i.e. INVALID CSS, which left
        # the back-arrow SVG unsized (the giant-arrow bug). Plain single-brace CSS now, and the
        # only control is choosing WHICH project's log to read.
        _home = f"/?app={html.escape(_appq)}" if _appq else "/"
        chips = "".join(
            f"<a class='pchip{' on' if a.name == _appq else ''}' "
            f"href='/tasks?app={html.escape(a.name)}'>{html.escape(a.name)}</a>"
            for a in cfg.apps)
        back = (
            "<style>"
            ".backbtn{display:inline-flex;align-items:center;gap:10px;padding:10px 16px;"
            "background:var(--panel);border:1px solid var(--line2);border-radius:9px;"
            "color:var(--ink);font-size:14px;font-weight:600;text-decoration:none;margin:0}"
            ".backbtn svg{width:18px;height:18px;flex:none}"
            ".backbtn:hover{border-color:var(--accent);color:var(--accent)}"
            ".tasknav{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:16px 30px 0}"
            ".pchips{display:flex;gap:8px;flex-wrap:wrap;align-items:center}"
            ".pchips .plabel{color:var(--dim);font-size:12px}"
            ".pchip{padding:8px 14px;border:1px solid var(--line2);border-radius:99px;background:var(--panel);"
            "color:var(--dim);font-size:13px;font-weight:600;text-decoration:none}"
            ".pchip:hover{border-color:var(--accent);color:var(--ink)}"
            ".pchip.on{background:var(--accentbg);border-color:var(--accent);color:var(--ink)}"
            "</style>"
            "<div class=tasknav>"
            f"<a class='backbtn' href='{_home}' aria-label='Back to cockpit'>"
            "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.5' "
            "stroke-linecap='round' stroke-linejoin='round'>"
            "<path d='M19 12H5M12 19l-7-7 7-7'/></svg>cockpit</a>"
            f"<div class=pchips><span class=plabel>Project:</span>{chips}</div>"
            "</div>")
        return page.replace("</header>", "</header>" + back, 1)

    @app.post("/api/dismiss")
    def dismiss_api():
        tid = (request.form.get("ticket") or "").strip()
        back = (request.form.get("back") or "/tasks").strip()
        if tid:
            D.dismiss(cfg.audit_path, tid)
        return redirect(back if back in ("/tasks", "/needs") else "/tasks")

    @app.post("/api/unblock")
    def unblock_api():
        """Remove a ticket from blocked_tickets.json (the parked set).

        EU-102: redirects to /needs (unified inbox) instead of /tasks?filter=parked.
        Removes the ticket from blocked_tickets.json (the authoritative parked store),
        not just from dismissed.json. After this the autopilot will retry it next cycle
        and the row disappears from the parked section of the Needs-you inbox (EU-78)."""
        tid = (request.form.get("ticket") or "").strip()
        if tid:
            from . import autopilot as _ap
            _ap.unblock(cfg, tid)
        return redirect("/needs")

    @app.post("/api/security-reply")
    def security_reply_api():
        """Record a response to a security block finding (EU-145).

        Records the response to the audit log and optionally creates a Jira ticket
        for critical/high severity issues. Redirects back to the cockpit."""
        ticket_id = (request.form.get("ticket_id") or "").strip()
        iteration = request.form.get("iteration", "1")
        response = (request.form.get("response") or "").strip()[:2000]  # Truncate to 2000 chars

        if not ticket_id or not response:
            return redirect("/?app=*")  # Redirect to cockpit on invalid input

        # Record the response to the audit log
        try:
            audit.record("security_reply", ticket_id=ticket_id, iteration=iteration,
                        response=response, ts=time.strftime("%Y-%m-%dT%H:%M:%S"))
        except Exception as e:
            # Audit logging should never break the request
            print(f"Failed to record security reply: {e}")

        # Check if this is a critical/high severity issue (heuristic: look for keywords)
        is_critical = any(kw in response.upper() or kw in ticket_id.upper()
                          for kw in ("CRITICAL", "HIGH", "VULN", "EXPLOIT", "RCE"))

        if is_critical:
            # TODO: Create Jira ticket for critical issues.
            # EU-361 (2026-07-16 audit): this branch used to record a `security_reply_ticket_created`
            # audit event right under this TODO, with note="not yet implemented" — a forensics trail
            # asserting a ticket exists on the one path where none is created. Nothing consumed the
            # event, so it was pure false signal; the `security_reply` event recorded above already
            # captures the reply itself. Dropped rather than renamed: when the Jira call lands here,
            # THAT is what should record a `_created` event.
            pass

        # Redirect back to the cockpit (maintains the app selection)
        app = request.form.get("app") or request.args.get("app") or "*"
        return redirect(f"/?app={quote(app)}")

    @app.get("/tickets")
    def tickets_page():
        # EU-63: one concrete project per tab — the retired "All projects"/`*` grouped view is gone, so
        # this page always lists exactly one app's backlog. ``_scope`` resolves (and focuses) that tab.
        appq = _scope(request.args.get("app"))
        name = appq or None
        # 2026-07-19 (Commander order): the run bar is STICKY — pick tickets anywhere in a 40-row
        # list and "Develop selected" stays in view, with a live (N) count and disabled-at-zero.
        style = ("<style>.tlist{margin:10px 0;border:1px solid var(--line2);border-radius:10px;overflow:hidden}"
                 ".trow{display:flex;gap:12px;align-items:flex-start;padding:11px 14px;border-top:1px solid var(--line);cursor:pointer}"
                 ".trow:first-child{border-top:0}.trow:hover{background:var(--panel2)}"
                 ".trow input{margin-top:3px}.tkey{font-family:var(--mono);font-size:12px;color:var(--info);white-space:nowrap}"
                 ".tsum{color:var(--ink)}"
                 ".trun{position:sticky;bottom:0;z-index:20;display:flex;gap:14px;align-items:center;flex-wrap:wrap;"
                 "background:var(--panel);border:1px solid var(--line);border-radius:12px;"
                 "padding:12px 16px;margin:14px 0 4px;box-shadow:var(--shadow-2)}"
                 ".trun button{background:var(--accent);border:0;color:#fff;border-radius:8px;padding:10px 18px;font-weight:650;cursor:pointer}"
                 ".trun button:disabled{background:var(--line);color:var(--faint);cursor:not-allowed}"
                 ".hint{color:var(--dim);font-size:13px}"
                 ".tapp{margin-left:auto;font-size:11px;color:var(--dim);background:var(--panel2);border:1px solid var(--line2);border-radius:999px;padding:1px 9px;white-space:nowrap}"
                 ".tgrp{font-size:12px;color:var(--ink);font-weight:700;margin:16px 0 6px}"
                 ".tall{background:var(--well);font-weight:650}</style>")
        try:
            items = intake.from_drain(cfg, name, 40)
        except Exception as exc:  # noqa: BLE001
            who = f"<b>{html.escape(appq)}</b>" if appq else "this project"
            return _wrap("Choose tickets", style + f"<p class=hint>Couldn't load tickets for "
                         f"{who}: {html.escape(str(exc))}</p>")
        if not items:
            who = f"<b>{html.escape(appq)}</b>" if appq else "this project"
            return _wrap("Choose tickets", style + "<p class=hint>Nothing assigned to you in "
                         f"{who} (In Progress / To Do). Clear queue.</p>")

        effort = "".join(f"<option value='{e}'>{e}</option>"
                         for e in ("low", "medium", "high", "xhigh", "max"))

        def _checkbox_rows(its) -> str:
            return "".join(
                f'<label class=trow><input type=checkbox name=ticket value="{html.escape(t.id)}">'
                f'<span class=tkey>{html.escape(t.id)}</span>'
                f'<span class=tsum>{html.escape(t.summary or "(no summary)")}</span></label>'
                for t in its)

        # "Select all" header: a nameless checkbox that toggles every ticket box in ITS OWN form. No
        # name=ticket -> it is never submitted; it only flips the real boxes for this project's run.
        _toggle = "this.closest('form').querySelectorAll('input[name=ticket]').forEach(c=>c.checked=this.checked)"
        _select_all = ('<label class="trow tall"><input type=checkbox onclick="' + _toggle + '">'
                       '<span class=tsum>Select all</span></label>')

        def _run_form(target_app: str, rows_html: str, btn_label: str) -> str:
            # One run form = one app/Jira. Multiple ticket checkboxes are fine — they're all this app.
            return ('<form method=post action=/api/run-selected>'
                    f'<input type=hidden name=app value="{html.escape(target_app)}">'
                    f'<div class=tlist>{_select_all}{rows_html}</div>'
                    '<div class=trun>'
                    '<label><input type=checkbox name=dryrun> dry run (build only — no merge)</label>'
                    f'<select name=effort><option value="">effort: auto-size</option>{effort}</select>'
                    f'<button id=devbtn disabled>&#9654; {html.escape(btn_label)} <span id=devcount></span></button>'
                    '<span class=hint>default builds + merges to DEV — tick "dry run" to build only</span>'
                    '</div></form>'
                    # live count + disabled-at-zero: updates on every checkbox flip (incl. Select all)
                    '<script>(function(){var f=document.querySelector("form[action=\'/api/run-selected\']")'
                    '||document.forms[0];if(!f)return;var b=f.querySelector("#devbtn"),'
                    'c=f.querySelector("#devcount");function upd(){var n=f.querySelectorAll('
                    '"input[name=ticket]:checked").length;if(b)b.disabled=n===0;'
                    'if(c)c.textContent=n?"("+n+")":"";}f.addEventListener("change",upd);upd();})();</script>')

        body = (style
                + f'<p class=hint>{len(items)} ticket(s) assigned to you, in board-priority order. '
                  "Tick the ones to develop, then Run.</p>"
                + _run_form(appq, _checkbox_rows([t for _, t in items]), "Develop selected"))
        return _wrap(f"Choose tickets — {html.escape(appq)}", body)

    @app.post("/api/squad")
    def squad_api():
        # 2026-07-19 (Commander order — squad modes): persist WHICH formation builds tickets.
        # full = the standard pipeline; elite = the small careful trio (step-by-step Builder,
        # 2.4x turn budget, unchanged gate+review); auto = the sizer routes L/XL → elite.
        from . import squad_pref
        sq = (request.form.get("squad") or "").strip().lower()
        if sq in ("full", "elite", "auto"):
            squad_pref.set_mode(sq, cfg)
            get_state(None)["last_msg"] = {
                "full": "Full squad — the standard pipeline builds every ticket.",
                "elite": "Elite squad — the careful trio builds every ticket: ordered step plan, "
                         "one iterative Builder with per-step checks, independent review.",
                "auto": "Auto — big tickets (L/XL) go to the Elite squad, the rest to the Full squad.",
            }[sq]
        return redirect("/")

    @app.post("/api/model")
    def model_api():
        # EU-190/EU-223: persist the active model backend — GLOBALLY, or (an optional `app` field)
        # for ONE project only, so e.g. the Elite-Unit drain can stay on Opus while automatixy runs
        # GLM. When switching to GLM, run a LIVE connection test first so a bad setup
        # (missing/incorrect token, wrong URL) surfaces a clear, actionable alert ("what to fix, or
        # re-onboard") in the cockpit instead of failing mid-run. No silent fallback; never stores
        # or echoes the token — only the backend id.
        # 2026-07-19: the Mode selector posts mode=hybrid|backup (only meaningful with a Secondary).
        mode_raw = (request.form.get("mode") or "").strip().lower()
        if mode_raw in ("hybrid", "backup"):
            if not backend_pref.get_secondary(cfg):
                get_state(None)["last_msg"] = "Set a Secondary model first, then pick Hybrid or Backup."
            else:
                backend_pref.set_mode(mode_raw, cfg)
                get_state(None)["last_msg"] = (
                    "Hybrid — both models work per task: plan/review on the Main, building on the "
                    "Secondary." if mode_raw == "hybrid" else
                    "Backup — everything runs on the Main; the Secondary only takes over if the "
                    "Main hits its limit.")
            return redirect("/")
        # 2026-07-19: the Secondary selector posts secondary=<id|none> instead of backend=.
        sec_raw = (request.form.get("secondary") or "").strip()
        if sec_raw:
            if sec_raw.lower() == "none":
                backend_pref.set_secondary(None, cfg)
                get_state(None)["last_msg"] = "Secondary model cleared — single-model mode."
            else:
                from .model_registry import ModelRegistry as _MR
                _sbk = backends.resolve_selection(sec_raw, _MR(cfg))
                backend_pref.set_secondary(_sbk, cfg)
                get_state(None)["last_msg"] = (
                    f"Secondary model set to {_sbk}. Pick how they work: Hybrid (both, per task) "
                    "or Backup (Secondary only if the Main hits its limit).")
            return redirect("/")
        raw = (request.form.get("backend") or "").strip()
        app_param = (request.form.get("app") or "").strip() or None
        if app_param and raw.lower() in ("inherit", ""):
            # "Inherit global" — clear this app's override; it now resolves the global pick.
            backend_pref.set_active(None, cfg, app_name=app_param)
            _state.pop("model_alert", None)
            get_state(None)["last_msg"] = f"{app_param}: Model now inherits the global pick."
            return redirect("/")
        # EU-236: resolve against the registry so a KNOWN custom-backend id is persisted VERBATIM,
        # not flattened to opus by normalize(); opus/glm aliases still canonicalize, unknown -> opus.
        from .model_registry import ModelRegistry
        bk = backends.resolve_selection(raw, ModelRegistry(cfg))
        if bk == backends.GLM:
            ok, detail = backends.glm_test_connection()
            if not ok:
                _state["model_alert"] = (
                    f"GLM not enabled — {detail}.  Fix it in .env (GLM_AUTH_TOKEN / GLM_BASE_URL) "
                    "and restart, or re-onboard, then pick GLM again.")
                return redirect("/")
        if app_param:
            backend_pref.set_active(bk, cfg, app_name=app_param)
        else:
            backend_pref.set_active(bk, cfg)
        _state.pop("model_alert", None)
        if bk == backends.GLM:
            _label = "GLM (Z.ai) — connection OK."
        elif bk == backends.NATIVE:
            _label = "Opus (Claude)."
        else:
            _rec = ModelRegistry(cfg).get(bk)
            _label = f"{(_rec or {}).get('display_name') or bk} (custom backend)."
        get_state(None)["last_msg"] = (
            f"{app_param}: Model set to {_label} (this project only)." if app_param
            else "Model backend set to " + _label)
        return redirect("/")

    @app.post("/api/continue-on-alternate")
    def continue_on_alternate_api():
        # EU-191/EU-223: the plan-limit banner's "Continue on <backend>" button. Switch the sticky
        # model backend to a runnable alternate (e.g. GLM) — for the AFFECTED app only when the
        # banner named one (`app`), else globally as before — clear the plan-limit flags, and
        # AUTO-RESUME the last cockpit run on that backend, so an Opus/Claude plan-limit doesn't
        # stall the drain until it resets. Fail-closed: an unconfigured/unreachable backend surfaces
        # an alert and does not switch.
        bk = backends.normalize(request.form.get("backend"))
        app_param = (request.form.get("app") or "").strip() or None
        if not backends.available(bk):
            _state["model_alert"] = (f"Cannot continue on {bk} — it is not configured. Set its "
                                     "credentials in .env and restart.")
            return redirect("/")
        if bk == backends.GLM:
            ok, detail = backends.glm_test_connection()
            if not ok:
                _state["model_alert"] = f"GLM not usable — {detail}. Fix .env and restart."
                return redirect("/")
        if app_param:
            backend_pref.set_active(bk, cfg, app_name=app_param)
        else:
            backend_pref.set_active(bk, cfg)
        _state.pop("model_alert", None)
        # Drop the plan-limit banner (the operator chose to switch rather than wait). MUST include the
        # None key: the dashboard banner is rendered from the unit-wide None-keyed _state, so clearing
        # only the per-app keys would leave the banner stuck on-screen forever after the switch.
        for _a in [None, *_app_names]:
            try:
                cockpit_state.set_plan_limit_hit(_a, hit=False, reset_at=None)
            except Exception:  # noqa: BLE001 — clearing the flag must never 500 the switch
                pass
        # Auto-resume the last cockpit run on the new backend (EU-191 design: switch + resume).
        last = _state.get("last_run") or {}
        app_name = _scope(last.get("app"))
        keys = [k for k in (last.get("tickets") or []) if k]
        msg = f"Model backend switched to {bk.upper()}."
        if app_name and keys:
            import copy
            rcfg = copy.copy(cfg)
            rcfg.dry_run = False
            _resolve_run_backend(rcfg, app_name)   # picks up the just-set (per-app or global) backend
            try:
                worklist = intake.from_tickets(rcfg, app_name, keys)
                from . import decisions as _dec
                if _dec._run_bg(rcfg, audit, worklist, refuse_if_busy=True):
                    msg = f"Switched to {bk.upper()} — resuming {', '.join(keys)}."
                else:
                    msg = (f"Switched to {bk.upper()}; a run is already active for {app_name}, so "
                           f"{', '.join(keys)} was not re-started.")
            except Exception as exc:  # noqa: BLE001
                msg = f"Switched to {bk.upper()}, but auto-resume failed: {exc}"
        get_state(None)["last_msg"] = msg
        return redirect("/")

    # ── EU-235: /models — cockpit CRUD for user-defined model backends ──────────────────────────
    # The management UI over the EU-233 registry + EU-234 secrets store. EU-236 already consumes
    # registry entries in the Model selector (backend_control), so until this page the operator
    # could SELECT a custom backend but only create one by hand-editing state/model_registry.json.
    # Views live in models_views.py (string builders — no templates/ dir in this repo). Every POST
    # below is automatically covered by the EU-254 _csrf_origin_guard before_request hook (it
    # guards ALL state-changing methods), which is where the abandoned WIP branch's EU-292 CSRF
    # finding lands; the EU-293 nav-link finding lands via add_models_nav_link in index(). That
    # WIP branch (7d448e5) predates the landed secrets layer and is deliberately not merged.
    from .model_registry import ModelRegistry
    from .model_registry import _validate as _mr_validate
    from .secrets import Secrets as _Secrets

    # Placeholder credential_ref used only to satisfy the registry's required-field schema during
    # the dry-run validation + the initial add(); set_credential immediately rewrites it to the
    # real derived ``secret://<id>`` (model_registry.py:204-212).
    _PENDING_REF = "secret://pending"

    def _model_form_fields() -> tuple[dict, str]:
        """The form's registry fields (whitespace-stripped) + the api_key, SEPARATED: the raw key
        is never part of the record dict — it goes to the Secrets store via set_credential, which
        persists only the derived credential_ref (the registry's _ALLOWED_FIELDS whitelist would
        strip a smuggled key anyway; keeping it out entirely means it can't even transit)."""
        fields = {k: (request.form.get(k) or "").strip()
                  for k in ("display_name", "provider", "base_url", "model_id",
                            "small_fast_model_id", "tier")}
        return fields, (request.form.get("api_key") or "").strip()

    @app.get("/models")
    def models_page():
        return models_views.render_models_list(cfg)

    @app.get("/models/add")
    def models_add_form():
        return models_views.render_model_form(cfg)

    @app.post("/models/add")
    def models_add_api():
        fields, api_key = _model_form_fields()
        errors = []
        try:
            # Dry-run the registry's OWN schema (single source of truth — no duplicated rules
            # here) BEFORE writing anything: a bad form never leaves a half-added record behind,
            # and "missing display_name + missing api_key" reports both at once.
            _mr_validate({**fields, "credential_ref": _PENDING_REF}, partial=False)
        except ValueError as exc:
            errors.append(str(exc))
        if not api_key:
            errors.append("missing required field(s): api_key")
        if errors:
            return models_views.render_model_form(cfg, values=fields, errors=errors)
        # 2026-07-19: tier auto-detect — when the Commander left the tier on Auto, one cheap
        # LLM call classifies the new model (top/mid/light) so the unit knows what it just got.
        # Best-effort with a hard fallback to "mid"; never blocks the add.
        if not str(fields.get("tier") or "").strip():
            fields["tier"] = backends.classify_model_tier(
                fields.get("display_name", ""), fields.get("model_id", ""), cfg)
            get_state(None)["last_msg"] = (
                f"Model backend saved — auto-classified as {fields['tier']}-tier "
                "(editable on the backend's Edit form).")
        registry = ModelRegistry(cfg)
        record = registry.add({**fields, "credential_ref": _PENDING_REF})
        # Stores the raw key in the Secrets store and rewrites credential_ref to the real
        # secret://<id> — the key never touches the registry file (the EU-234 boundary).
        registry.set_credential(record["id"], api_key)
        return redirect("/models")

    @app.get("/models/edit/<model_id>")
    def models_edit_form(model_id):
        record = ModelRegistry(cfg).get(model_id)
        if record is None:
            return Response("Unknown model backend.", status=404, mimetype="text/plain")
        return models_views.render_model_form(cfg, record=record)

    @app.post("/models/edit/<model_id>")
    def models_edit_api(model_id):
        registry = ModelRegistry(cfg)
        record = registry.get(model_id)
        if record is None:
            return Response("Unknown model backend.", status=404, mimetype="text/plain")
        fields, api_key = _model_form_fields()
        try:
            # Full-form validation (the form always submits every field); the record's existing
            # credential_ref stands in because the form never carries one. A blank api_key is
            # VALID on edit — it means "keep the stored key" (the form renders only the mask).
            _mr_validate({**fields, "credential_ref": record.get("credential_ref") or _PENDING_REF},
                         partial=False)
        except ValueError as exc:
            return models_views.render_model_form(cfg, record=record, values=fields,
                                                  errors=[str(exc)])
        registry.update(model_id, fields)
        if api_key:
            registry.set_credential(model_id, api_key)
        return redirect("/models")

    @app.post("/models/delete/<model_id>")
    def models_delete_api(model_id):
        registry = ModelRegistry(cfg)
        record = registry.get(model_id)
        if record is not None:
            ref = record.get("credential_ref")
            if ref:
                # Secrets(cfg) anchors to the same cfg state dir set_credential wrote
                # (secrets.py:59-64), so this removes exactly the record's own stored key —
                # record AND secret go together, nothing orphans in secrets.json.
                _Secrets(cfg).delete(ref)
            registry.delete(model_id)
        # Unknown/already-deleted id: nothing to remove — land back on the list either way.
        return redirect("/models")

    @app.post("/models/test")
    def models_test_api():
        """EU-237: probe the form's CURRENT field values (not the saved record) so the operator
        can validate a backend before saving — NO state change, ever. A blank api_key with a
        ``record_id`` (the edit form's hidden field) means "retest with the stored key": the
        credential is resolved server-side via the record's credential_ref and used only inside
        the probe — the JSON reply carries {success, message} and never the key (the message is
        built by backends.test_backend_connection, which never embeds it). Covered by the EU-254
        origin guard like every other POST here."""
        fields, api_key = _model_form_fields()
        if not api_key:
            rid = (request.form.get("record_id") or "").strip()
            record = ModelRegistry(cfg).get(rid) if rid else None
            ref = (record or {}).get("credential_ref") or ""
            if ref:
                api_key = _Secrets(cfg).get(ref) or ""
        # Flask serializes the returned dict as the JSON body (200 either way — "the test ran and
        # says no" is a successful REQUEST; only the EU-254 guard produces a non-200 here).
        return backends.test_backend_connection(
            fields["provider"], fields["base_url"], fields["model_id"], api_key)

    @app.post("/api/run-selected")
    def run_selected_api():
        app_name = _scope(request.form.get("app"))   # the run targets exactly one concrete project
        keys = request.form.getlist("ticket")
        if not keys:
            return redirect(f"/tickets?app={app_name}")
        # EU-64: claim THIS project's run slot (per-project TOCTOU guard + the cross-project parallel
        # cap). A second run on the SAME project is refused; other projects are unaffected.
        # EU-361: the stop_event is minted HERE, not in _bg — see _claim_cockpit_run's docstring.
        ev = threading.Event()
        st = _claim_cockpit_run(app_name, stop_event=ev)
        if st is None:
            return redirect("/")
        if not health.summary(cfg)["healthy"]:
            release_run(app_name or None)
            st["last_msg"] = "blocked — fix the health problems first (see the banner)"
            return redirect("/")
        import copy
        rcfg = copy.copy(cfg)        # per-run config — never mutate the shared cfg
        rcfg.dry_run = request.form.get("dryrun") == "on"   # default: live (build + merge to DEV)
        st["dry_run"] = rcfg.dry_run
        effort = request.form.get("effort") or None
        if effort:
            rcfg.builder_effort = normalize_effort(effort)
            rcfg.adaptive_effort = False
        _berr = _resolve_run_backend(rcfg, app_name)   # EU-190/EU-223: this app's (or global) backend
        if _berr:
            release_run(app_name or None)
            st["last_msg"] = _berr
            return redirect("/")
        try:
            worklist = intake.from_tickets(rcfg, app_name, keys)
        except Exception as exc:  # noqa: BLE001
            release_run(app_name or None)
            st["dry_run"] = None
            st["last_msg"] = f"could not start: {exc}"
            return redirect("/")

        # EU-191: remember the last cockpit run so the plan-limit prompt can auto-resume it on an
        # alternate backend (GLM) if Opus hits its limit.
        _state["last_run"] = {"app": app_name, "tickets": list(keys)}

        def _bg():
            # EU-361: ``ev`` is the event _claim_cockpit_run already published on ``st`` in the
            # request thread — the worker closes over it instead of minting its own, so Stop can
            # never race the thread scheduler.
            st["last_msg"] = ""
            errored = False
            reports = []
            # EU-175: bracket this cockpit-initiated run_loop with run_start/run_end (mirroring main.py's
            # CLI path) so a hard-killed or exception-exiting worker still closes its run boundary —
            # run_end fires from the finally, same as run_start fires before the run begins. Without it a
            # ghost session leaves an unpaired boundary and a phantom "Working" card on the cockpit.
            if audit is not None:
                audit.record("run_start", mode=("DRY-RUN" if rcfg.dry_run else "LIVE"), tickets=len(worklist or []))
            try:
                reports = asyncio.run(run_loop(rcfg, worklist, audit, stop_event=ev))
            except Exception as exc:  # noqa: BLE001
                errored = True
                st["last_msg"] = str(exc)
            finally:
                if audit is not None:
                    audit.record("run_end", tickets=len(reports or []))
                release_run(app_name or None)   # clears active / run_started / stop_event for this app
                st["dry_run"] = None            # clear the dry/live flag so the cockpit shows no stale tag
                # EU-104: on a CLEAN terminal outcome, clear the transient 'Working / stopping…'
                # control-bar note so a finished run never lingers as 'Working'. Guarded by
                # ``errored`` so a real run error (set just above) stays visible — release_run no
                # longer clears last_msg, so the operator still sees why a failed run failed.
                if not errored:
                    st["last_msg"] = ""
                # EU-191: AFTER the fast cleanup (so a slow usage probe never delays clearing the
                # control-bar note — the race that broke eu104), raise the unit-wide (None-keyed)
                # plan-limit banner + its Continue-on-GLM offer if this run tripped the Opus/Claude cap.
                # The autopilot governor only sets this for its own loop, so without it the banner never
                # appears for a manual cockpit run. _state['last_run'] holds the ticket for the Continue button.
                try:
                    from . import usage as _u191
                    _pl = _u191.plan_limit_hit(cfg, force=True)
                    if _pl.get("hit"):
                        _reset = next((l.get("resets_at") for l in _pl.get("over_limits", [])
                                       if l.get("resets_at")), None)
                        cockpit_state.set_plan_limit_hit(None, hit=True, reset_at=_reset)
                except Exception:  # noqa: BLE001 — detection must never break run cleanup
                    pass
        threading.Thread(target=_bg, daemon=True).start()
        return redirect("/")

    @app.post("/api/run")
    def run_api():
        # EU-63: context is always ONE concrete project (the active tab) — no "All projects"/`*`. The
        # drain therefore drains just this project's backlog, not every app.
        app_name = _scope(request.form.get("app"))
        drain_app = app_name
        # EU-64: claim THIS project's run slot (per-project TOCTOU guard + the cross-project parallel
        # cap). A second run on the SAME project is refused; other projects are unaffected.
        # EU-361: the stop_event is minted HERE, not in _bg — see _claim_cockpit_run's docstring.
        ev = threading.Event()
        st = _claim_cockpit_run(app_name, stop_event=ev)
        if st is None:
            return redirect("/")
        if not health.summary(cfg)["healthy"]:
            release_run(app_name or None)
            st["last_msg"] = "blocked — fix the health problems first (see the banner)"
            return redirect("/")
        kind = request.form.get("kind", "task")
        text = (request.form.get("text") or "").strip()
        ttype = (request.form.get("type") or "feature").strip()
        effort = request.form.get("effort") or None
        import copy
        rcfg = copy.copy(cfg)        # per-run config — never mutate the shared cfg
        rcfg.dry_run = request.form.get("dryrun") == "on"   # default: live (build + merge to DEV)
        st["dry_run"] = rcfg.dry_run
        if effort:
            rcfg.builder_effort = normalize_effort(effort)
            rcfg.adaptive_effort = False     # an explicit pick bypasses auto-sizing for this run
        _berr = _resolve_run_backend(rcfg, app_name)   # EU-190/EU-223: this app's (or global) backend
        if _berr:
            release_run(app_name or None)
            st["last_msg"] = _berr
            return redirect("/")
        try:
            if kind == "task" and ttype == "bug":
                worklist = intake.from_text(rcfg, app_name, _bug_title(text), [],
                                            description=_bug_desc(cfg, text, request.files.get("screenshot")))
            elif kind == "task":
                worklist = intake.from_text(rcfg, app_name, text or "(no description)", [])
            elif kind == "ticket":
                worklist = intake.from_tickets(rcfg, app_name, text.split())
            else:
                worklist = intake.from_drain(rcfg, drain_app, rcfg.max_tickets_per_run)
        except Exception as exc:  # noqa: BLE001
            release_run(app_name or None)
            st["dry_run"] = None
            st["last_msg"] = f"could not start: {exc}"
            return redirect("/")

        def _bg():
            # EU-361: ``ev`` is the event _claim_cockpit_run already published on ``st`` in the
            # request thread — the worker closes over it instead of minting its own, so Stop can
            # never race the thread scheduler.
            st["last_msg"] = ""
            errored = False
            reports = []
            # EU-175: bracket this cockpit-initiated run_loop with run_start/run_end (mirroring main.py's
            # CLI path) so a hard-killed or exception-exiting worker still closes its run boundary —
            # run_end fires from the finally, same as run_start fires before the run begins. Without it a
            # ghost session leaves an unpaired boundary and a phantom "Working" card on the cockpit.
            if audit is not None:
                audit.record("run_start", mode=("DRY-RUN" if rcfg.dry_run else "LIVE"), tickets=len(worklist or []))
            try:
                reports = asyncio.run(run_loop(rcfg, worklist, audit, stop_event=ev))
            except Exception as exc:  # noqa: BLE001
                errored = True
                st["last_msg"] = str(exc)
            finally:
                if audit is not None:
                    audit.record("run_end", tickets=len(reports or []))
                release_run(app_name or None)   # clears active / run_started / stop_event for this app
                st["dry_run"] = None            # clear the dry/live flag so the cockpit shows no stale tag
                # EU-104: on a CLEAN terminal outcome, clear the transient 'Working / stopping…'
                # control-bar note so a finished run never lingers as 'Working'. Guarded by
                # ``errored`` so a real run error (set just above) stays visible — release_run no
                # longer clears last_msg, so the operator still sees why a failed run failed.
                if not errored:
                    st["last_msg"] = ""
                # EU-191: AFTER the fast cleanup (so a slow usage probe never delays clearing the
                # control-bar note — the race that broke eu104), raise the unit-wide (None-keyed)
                # plan-limit banner + its Continue-on-GLM offer if this run tripped the Opus/Claude cap.
                # The autopilot governor only sets this for its own loop, so without it the banner never
                # appears for a manual cockpit run. _state['last_run'] holds the ticket for the Continue button.
                try:
                    from . import usage as _u191
                    _pl = _u191.plan_limit_hit(cfg, force=True)
                    if _pl.get("hit"):
                        _reset = next((l.get("resets_at") for l in _pl.get("over_limits", [])
                                       if l.get("resets_at")), None)
                        cockpit_state.set_plan_limit_hit(None, hit=True, reset_at=_reset)
                except Exception:  # noqa: BLE001 — detection must never break run cleanup
                    pass
        threading.Thread(target=_bg, daemon=True).start()
        return redirect("/")

    @app.post("/api/stop-run")
    def stop_run_api():
        # EU-64: stop THIS project's run (the Stop button lives on a per-tab board). Resolve the tab's
        # project read-only (don't steal the active tab), then signal that app's stop Event.
        appq = _board_project(request.form.get("app"))
        st = get_state(appq or None)
        ev = st.get("stop_event")
        if ev is None and not (request.form.get("app") or "").strip():
            # The Stop form may not carry an ?app yet — fall back to the sole stoppable run, if there
            # is exactly one (keeps Stop working in the single-run case during the per-project rollout).
            stoppable = [k for k in active_runs() if get_state(k).get("stop_event") is not None]
            if len(stoppable) == 1:
                st = get_state(stoppable[0])
                ev = st.get("stop_event")
        if ev is not None:
            ev.set()
            st["last_msg"] = "stopping after the current step — DEV untouched, no merge"
        return redirect("/")

    @app.get("/standup")
    def standup_page():
        from . import council
        snap = "<pre class=rep>" + html.escape(D.standup(cfg)) + "</pre>"
        intro = ("<p style='color:#8a909c;margin:-4px 0 14px'>The stand-up now runs inside the daily "
                 "muster — see <a href='/council'>Daily muster &amp; meetings</a>. You don't initiate "
                 "it; officers post anything actionable to <a href='/needs'>Needs you</a> and ping you "
                 "on Telegram if blocked. The button below is only for an on-demand extra.</p>")
        btn = ('<form method=post action=/api/standup style="margin:14px 0">'
               '<button>&#129303; Run an extra stand-up now</button></form>')
        if _state.get("standuping"):
            rep = _working("The officers are reporting — Yesterday / Today / Blockers…")
        else:
            last = council.last_standup(cfg)
            rep = ("<pre class=rep>" + html.escape(last) + "</pre>" if last
                   else "<p style='color:#8a909c'>No officer stand-up recorded yet — the next one lands "
                        "automatically at the 10:00 muster. Each officer reports Yesterday / Today / "
                        "Blockers and flags who they need.</p>")
        return _wrap("Daily standup",
                     intro + "<h3>Snapshot</h3>" + snap + "<h3>Officer stand-up</h3>" + rep + btn)

    @app.post("/api/standup")
    def standup_api():
        if _claim_flag("standuping"):   # EU-361: claimed here, not inside _bg
            def _bg():
                try:
                    from . import council
                    asyncio.run(council.hold_standup(cfg, audit=audit))
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"standup failed: {exc}"
                finally:
                    _state["standuping"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/standup")

    @app.post("/api/council")
    def council_api():
        if _claim_flag("councilling"):   # EU-361: claimed here, not inside _bg
            def _bg():
                try:
                    from . import council
                    asyncio.run(council.hold_council(cfg, audit=audit))
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"council failed: {exc}"
                finally:
                    _state["councilling"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/council")

    @app.get("/council")
    def council_page():
        from . import council
        hist = council.history(cfg, limit=25)
        if _state.get("shipreview"):
            top = _working("&#128640; Ship-review in session — the Release Manager + officers are checking if "
                           "DEV is ready for MAIN. The verdict will appear below and on Telegram.")
        elif _state.get("councilling"):
            top = _working("The officers are in session — reading the record and debating…")
        else:
            top = ""
        acts = "" if (_state.get("councilling") or _state.get("shipreview")) else _actbar(
            _actbtn("/api/council", "&#128172; Hold a council now"))
        intro = ("<p style='color:#8a909c;margin:-6px 0 16px'>The officers hold a council "
                 "automatically each day — you don't need to call it. To brainstorm with them yourself, "
                 "use the <a href='/group'>Group room</a>.</p>")
        if not hist:
            return _wrap("Daily Council", acts + intro + top
                         + "<p style='color:#8a909c'>No councils yet.</p>")
        want = request.args.get("f") or hist[0]["file"]
        transcript = council.transcript_text(cfg, want) or "(transcript missing)"
        items = "".join(
            f"<li><a href='/council?f={html.escape(h['file'])}'>{html.escape(h['ts'][:16])} — "
            f"{html.escape(h['summary'])}</a></li>" for h in hist)
        body = (acts + intro + top + "<div style='display:flex;gap:24px;align-items:flex-start'>"
                "<div style='flex:1;min-width:0'><h3>Transcript</h3><pre class=rep>"
                + html.escape(transcript) + "</pre></div>"
                "<div style='width:300px'><h3>Recent councils</h3><ul>" + items + "</ul></div></div>")
        return _wrap("Daily Council", body)

    @app.post("/api/scribe")
    def scribe_api():
        if _claim_flag("scribing"):   # EU-361: claimed here, not inside _bg
            def _bg():
                try:
                    msg = asyncio.run(memory.scribe(cfg))
                    _state["last_msg"] = "✓ " + (str(msg).strip() or "Squad memory updated by the Technical Writer.")
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"scribe failed: {exc}"
                finally:
                    _state["scribing"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/memory")

    @app.get("/memory")
    def memory_page():
        memory.ensure()
        top = (_working("The Technical Writer is folding recent lessons into Squad memory…")
               if _state.get("scribing") else "")
        # 2026-07-19 (Commander order): ONE manual action — "Update memory". The old Consolidate
        # button and the "Reviewer keeps rejecting these" panel were removed: consolidation (log
        # dedup/prune + folding recurring Reviewer-rejection lessons) runs AUTOMATICALLY after
        # every productive autopilot cycle and after every scribe run, and the panel was an
        # all-time aggregate that no click could ever clear (plus "drill candidate" framing for
        # the drill feature deleted in EU-327). The lessons the engine learns land in the living
        # log below — which the officers actually read.
        act = ("" if _state.get("scribing")
               else _actbar(_actbtn("/api/scribe", "&#128221; Update memory")))
        # One-shot confirmation banner ("✓ Technical Writer folded … into Unit Memory") — shown once the Technical Writer
        # finishes (not mid-fold), so the action visibly "took" instead of silently returning here.
        _m = "" if _state.get("scribing") else (_state.pop("last_msg", "") or "")
        banner = (f"<div style='background:#10371f;border:1px solid #1c5238;color:#7fe3a6;border-radius:9px;"
                  f"padding:11px 14px;margin:0 0 14px;font-size:13.5px;font-weight:600'>"
                  f"{html.escape(str(_m))}</div>" if _m else "")
        # Doctrine (Commander-owned) + the FULL living lessons log. Officers only see the newest
        # PREAMBLE_LESSONS of the log in their prompt; the whole tail lives here for the Commander.
        live_full = memory._live_log()
        live_html = (
            "<h3 style='margin:18px 0 8px;font-size:14px;color:#c4c9d2'>Living lessons log "
            f"<span style='color:#8a929f;font-weight:400;font-size:12px'>· officers see the newest "
            f"{memory.PREAMBLE_LESSONS} in every prompt; the full log lives here</span></h3>"
            "<pre class=rep>" + html.escape(live_full or "(no lessons logged yet)") + "</pre>")
        body = (banner + act + top
                + "<h3 style='margin:14px 0 8px;font-size:14px;color:#c4c9d2'>Doctrine</h3>"
                + "<pre class=rep>" + html.escape(memory.load() or "(no Squad memory yet)") + "</pre>"
                + live_html)
        return _wrap("Unit Memory", body)

    @app.get("/meeting")
    def meeting_form():
        from . import council
        names = [o[0] for o in council.COUNCIL]
        checks = "".join(
            f'<label class=mrow><input type=checkbox name=officer value="{html.escape(n)}"> {html.escape(n)}</label>'
            for n in names)
        body = (
            "<style>.mrow{display:flex;gap:9px;align-items:center;padding:6px 0;font-size:14px}"
            "input[type=text]{width:480px;max-width:90%}.hint{color:#8a909c;font-size:13px}</style>"
            "<form method=post action=/api/meeting>"
            "<p>What's the meeting about?<br>"
            "<input type=text name=topic placeholder='e.g. how to close the superadmin authz gap'></p>"
            "<p class=hint>Attending — leave all unchecked for the whole council:</p>"
            f"<div>{checks}</div>"
            "<p><button>Convene meeting</button></p></form>"
            "<p class=hint>The officers debate, the CTO decides, and the outcome is written to "
            "Unit Memory. Watch it appear under <a href='/council'>councils</a>.</p>")
        return _wrap("Call a meeting", body)

    @app.post("/api/meeting")
    def meeting_api():
        topic = (request.form.get("topic") or "").strip()
        if not topic:
            return redirect("/meeting")
        if _claim_flag("meeting"):   # EU-361: claimed here, not inside _bg
            officers = request.form.getlist("officer") or None

            def _bg():
                try:
                    from . import council
                    asyncio.run(council.hold_meeting(cfg, topic, officers=officers, audit=audit))
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"meeting failed: {exc}"
                finally:
                    _state["meeting"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/council")

    @app.post("/api/qa")
    def qa_api():
        """2026-07-19 (Commander order): ONE QA action — Patrol and Ship-review merged. The two
        buttons ran near-identical officer inspections of DEV (patrol: QA/Security/Release inspect
        + FILE findings as Jira tickets; ship-review: the same lenses debating a DEV→MAIN GO/NO-GO),
        so a single "Run QA" now does both as phases: patrol first (findings land on the board),
        then the readiness verdict. The per-phase indicator flags (patrolling / shipreview) are
        kept live during their phase so the cockpit star and the /council in-session banner keep
        working unchanged."""
        appq = _scope(request.form.get("app"))
        app_name = appq or _first_shippable(cfg)
        if not app_name:
            _state["last_msg"] = ("No project to QA — configure a product repo first.")
            return redirect("/")
        if _claim_flag("qa"):   # EU-361 pattern: claimed here, not inside _bg
            _state["last_msg"] = (f"🔍 QA running for {app_name} — officers inspect DEV and file "
                                  "findings, then deliver the DEV→MAIN readiness verdict (posts "
                                  "here and to Telegram).")

            def _bg():
                notes = []
                try:
                    _state["patrolling"] = True
                    try:
                        from . import patrol as patrol_mod
                        asyncio.run(patrol_mod.patrol(cfg, app_name, do_file=True, audit=audit))
                        notes.append("patrol done — findings filed as Jira tickets")
                    finally:
                        _state["patrolling"] = False
                    _state["shipreview"] = True
                    try:
                        from . import council
                        asyncio.run(council.ship_review(cfg, app_name, audit=audit))
                        notes.append("ship verdict posted (see /council + Telegram)")
                    finally:
                        _state["shipreview"] = False
                    _state["last_result"] = f"✓ QA finished for {app_name} — " + "; ".join(notes)
                except Exception as exc:  # noqa: BLE001
                    _state["last_result"] = f"QA failed: {exc}" + (
                        f" (completed: {'; '.join(notes)})" if notes else "")
                finally:
                    _state["qa"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/")

    @app.get("/api/deploy-status")
    def deploy_status_api():
        """Live state for the deploy progress bar. The cockpit polls this so the button shows progress instead of looking dead."""
        from flask import jsonify
        # EU-204: promote and ship-main endpoints removed; this now always returns inactive
        return jsonify({"active": False, "kind": "", "msg": _state.get("last_result", "")})

    @app.get("/merge-stats")
    def merge_stats_page():
        """EU-159/EU-160/EU-161: the frontend merge-statistics page with a four-way time-range
        selector (today / this week / this month / all time). Reads ``?time_range=`` and validates
        it against ``MERGE_STATS_TIME_RANGES``, falling back to 'today' when absent or invalid, then
        server-renders the initial cards for that range so a bookmarked ``?time_range=week`` URL
        loads correctly. An inline script lets the user switch ranges client-side via the existing
        JSON API (GET /api/merge-stats?time_range=) without a full reload — it rewrites the card
        numbers (with a var(--t-fast) fade), moves the 'active'/aria-current marker to the clicked
        control, shows a loading placeholder while the request is in flight, shows a role=alert
        error state with a retry affordance if it fails, and updates the URL via history.pushState
        so the range stays shareable/bookmarkable.

        EU-161: markup wrapped in semantic <main>/<section> landmarks with a labelled stats section,
        each stat value associated with its label via aria-describedby, a responsive grid that
        collapses to one column below a small breakpoint, and colours/radius/motion pulled from the
        EU-39 design tokens (var(--panel)/var(--ink)/var(--t-fast)/var(--ring)/…) — no raw hex.
        Renders via ``_wrap`` (page title + '← cockpit' breadcrumb come from there for free)."""
        time_range = request.args.get("time_range")
        if time_range not in MERGE_STATS_TIME_RANGES:
            time_range = "today"
        stats = compute_merge_stats(cfg.audit_path, time_range)
        sr = stats["success_rate"]
        sr_str = "—" if sr is None else f"{round(sr * 100)}%"
        range_labels = {"today": "Today", "week": "This week", "month": "This month", "all": "All time"}
        style = (
            "<style>"
            ".msmain{max-width:920px}"
            ".mslede{color:var(--dim);margin:-4px 0 4px}"
            ".mssectitle{font-size:12px;text-transform:uppercase;letter-spacing:.06em;"
            "color:var(--dim);font-weight:700;margin:20px 0 8px}"
            ".msgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));"
            "gap:14px;margin:14px 0}"
            "@media (max-width:560px){.msgrid{grid-template-columns:1fr}}"
            ".mscard{background:var(--panel);border:1px solid var(--line);border-radius:var(--r-lg);"
            "padding:16px 18px;box-shadow:var(--shadow-1)}"
            ".msbig{color:var(--ink);font-size:32px;font-weight:750;margin:4px 0 6px;"
            "transition:opacity var(--t-fast)}"
            ".msbig.msfade{opacity:.25}"
            ".mslabel{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.06em;"
            "font-weight:700}"
            ".msranges{display:flex;gap:8px;margin:10px 0;flex-wrap:wrap}"
            ".msrange{background:var(--panel);border:1px solid var(--line);border-radius:var(--r-md);"
            "color:var(--ink);padding:7px 14px;font-weight:600;cursor:pointer;font:inherit;"
            "transition:background var(--t-fast),border-color var(--t-fast)}"
            ".msrange.active{background:var(--accent);border-color:var(--accent);color:#fff}"
            ".msloading{display:flex;align-items:center;gap:10px;color:var(--dim);font-size:13px;"
            "margin:10px 0}"
            ".msloading[hidden]{display:none}"
            ".msspin{width:16px;height:16px;border:2px solid var(--line2);border-top-color:var(--accent);"
            "border-radius:var(--r-pill);animation:msspin .8s linear infinite;flex:none}"
            "@keyframes msspin{to{transform:rotate(360deg)}}"
            ".mserror{display:flex;align-items:center;gap:12px;flex-wrap:wrap;background:var(--badbg);"
            "border:1px solid var(--badline);color:var(--bad);border-radius:var(--r-md);"
            "padding:12px 16px;margin:10px 0}"
            ".mserror[hidden]{display:none}"
            ".msretry{background:var(--panel);border:1px solid var(--badline);color:var(--ink);"
            "border-radius:var(--r-sm);padding:6px 14px;font-weight:650;cursor:pointer;font:inherit;"
            "transition:background var(--t-fast)}"
            "</style>")
        range_buttons = "".join(
            f"<button type=button class='msrange{' active' if r == time_range else ''}' "
            f"data-range='{r}'{' aria-current=page' if r == time_range else ''}>{html.escape(label)}</button>"
            for r, label in range_labels.items()
        )
        script = (
            "<script>"
            "(function(){"
            "var btns=document.querySelectorAll('.msrange');"
            "var loading=document.getElementById('ms-loading');"
            "var errorBox=document.getElementById('ms-error');"
            "var retryBtn=document.getElementById('ms-retry');"
            "var grid=document.getElementById('ms-stats-grid');"
            f"var lastRange={time_range!r};"
            "function setBusy(b){"
            "if(loading){loading.hidden=!b;loading.setAttribute('aria-busy',b?'true':'false');}"
            "if(grid){grid.setAttribute('aria-busy',b?'true':'false');}"
            "}"
            "function hideError(){if(errorBox){errorBox.hidden=true;}}"
            "function showError(){if(errorBox){errorBox.hidden=false;}}"
            "function applyStats(s){"
            "var total=document.getElementById('ms-total');"
            "var pr=document.getElementById('ms-pr');"
            "var sr=document.getElementById('ms-sr');"
            "[total,pr,sr].forEach(function(el){if(el){el.classList.add('msfade');}});"
            "total.textContent=s.total_merges;"
            "pr.textContent=s.pr_opened;"
            "var rate=s.success_rate;"
            "sr.textContent=(rate===null||rate===undefined)?'—':Math.round(rate*100)+'%';"
            "setTimeout(function(){"
            "[total,pr,sr].forEach(function(el){if(el){el.classList.remove('msfade');}});"
            "},16);"
            "}"
            "function loadRange(r){"
            "hideError();"
            "setBusy(true);"
            "fetch('/api/merge-stats?time_range='+r).then(function(resp){"
            "if(!resp.ok){throw new Error('merge-stats fetch failed');}"
            "return resp.json();"
            "}).then(function(s){"
            "setBusy(false);"
            "applyStats(s);"
            "lastRange=r;"
            "btns.forEach(function(x){"
            "var active=x.getAttribute('data-range')===r;"
            "x.classList.toggle('active',active);"
            "if(active){x.setAttribute('aria-current','page');}else{x.removeAttribute('aria-current');}"
            "});"
            "var url=new URL(window.location);"
            "url.searchParams.set('time_range',r);"
            "history.pushState({},'',url);"
            "}).catch(function(){"
            "setBusy(false);"
            "showError();"
            "});"
            "}"
            "btns.forEach(function(b){b.addEventListener('click',function(){"
            "loadRange(b.getAttribute('data-range'));"
            "});});"
            "if(retryBtn){retryBtn.addEventListener('click',function(){loadRange(lastRange);});}"
            "})();"
            "</script>")
        inner = (
            style
            + "<main class=msmain>"
            + "<p class=mslede>Land outcomes, aggregated from the audit log.</p>"
            + "<section aria-label='Time range'>"
            + f"<div class=msranges>{range_buttons}</div>"
            + "</section>"
            + "<section aria-labelledby=ms-stats-heading>"
            + "<h2 id=ms-stats-heading class=mssectitle>Overview</h2>"
            + "<div id=ms-loading class=msloading aria-busy=true hidden>"
            + "<span class=msspin></span>Loading merge statistics…</div>"
            + "<div id=ms-error class=mserror role=alert hidden>"
            + "<span class=msetext>Couldn't load merge statistics.</span>"
            + "<button type=button id=ms-retry class=msretry>Retry</button></div>"
            + "<div class=msgrid id=ms-stats-grid>"
            + (f"<div class=mscard><div class=msbig id=ms-total aria-describedby=ms-total-label>"
               f"{stats['total_merges']}</div><div class=mslabel id=ms-total-label>Total merges</div></div>")
            + (f"<div class=mscard><div class=msbig id=ms-pr aria-describedby=ms-pr-label>"
               f"{stats['pr_opened']}</div><div class=mslabel id=ms-pr-label>PRs opened</div></div>")
            + (f"<div class=mscard><div class=msbig id=ms-sr aria-describedby=ms-sr-label>"
               f"{html.escape(sr_str)}</div><div class=mslabel id=ms-sr-label>Success rate</div></div>")
            + "</div>"
            + "</section>"
            + "</main>"
            + script
        )
        return _wrap("Merge statistics", inner)

    @app.get("/api/merge-stats")
    def merge_stats_api():
        """EU-158: merge statistics for a time window, aggregated from the audit log's land-outcome
        events (`merged` / `pr_opened`). See ``compute_merge_stats`` for the aggregation itself."""
        from flask import jsonify
        time_range = request.args.get("time_range")
        if time_range not in MERGE_STATS_TIME_RANGES:
            return jsonify({
                "error": (f"invalid time_range {time_range!r} — must be one of: "
                          f"{'|'.join(MERGE_STATS_TIME_RANGES)}"),
            }), 400
        return jsonify(compute_merge_stats(cfg.audit_path, time_range))

    @app.post("/api/approve-proposals")
    def approve_proposals_api():
        """Approve a queued batch of unit-proposed tickets — file the checked subset to the board
        (de-duped). EU-61."""
        batch = (request.form.get("batch") or "").strip()
        titles = [t for t in request.form.getlist("titles") if t.strip()]
        if batch:
            from . import approvals
            try:
                res = approvals.approve_proposals(cfg, batch, titles)
                if res is None:
                    _state["last_msg"] = "That proposal batch was already actioned."
                else:
                    parts = [f"Filed {res.filed_n} ticket(s)"]
                    if res.deduped_n:
                        parts.append(f"{res.deduped_n} already open")
                    if res.failed_n:
                        parts.append(f"{res.failed_n} failed")
                    _state["last_msg"] = ", ".join(parts) + "."
            except Exception as exc:  # noqa: BLE001
                _state["last_msg"] = f"approve failed: {exc}"
        return redirect("/needs")

    @app.post("/api/deny-proposals")
    def deny_proposals_api():
        """Deny a queued batch of unit-proposed tickets — discard, nothing filed. EU-61."""
        batch = (request.form.get("batch") or "").strip()
        reason = (request.form.get("reason") or "").strip()
        if batch:
            from . import approvals
            if approvals.deny_proposals(cfg, batch, reason):
                _state["last_msg"] = "Proposal batch denied — nothing filed."
        return redirect("/needs")

    @app.post("/api/needs-sync")
    def needs_sync_api():
        """2026-07-19 (Commander order): reconcile Needs-you against live Jira NOW — clears
        parked/decision/errored entries whose tickets the Commander already moved (Done/QA) or
        re-queued (To Do) in Jira. Synchronous: the click waits for the truth."""
        from . import needs_sync
        r = needs_sync.reconcile(cfg, audit, force=True)
        n = len(r.get("cleared", []))
        get_state(None)["last_msg"] = (
            f"✓ Synced with Jira — cleared {n} item(s): "
            + ", ".join(t for t, _ in r.get("cleared", [])[:8])
            + ("…" if n > 8 else "") if n else
            f"✓ Synced with Jira — everything in Needs-you is still genuinely waiting "
            f"({r.get('checked', 0)} checked).")
        # EU-406 (AC2): one-line note when statuses were unrecognized — a renamed/custom Jira
        # column parked behind these is KEPT, not guessed-and-cleared; surface it so it's visible.
        unk = r.get("unknown", [])
        if unk:
            get_state(None)["last_msg"] += (
                f" ⚠ {len(unk)} in an unrecognized Jira status (kept, not guessed): "
                + ", ".join(t for t, _ in unk[:6]) + ("…" if len(unk) > 6 else ""))
        return redirect("/needs")

    @app.get("/needs")
    def needs_page():
        """Unified Commander inbox — decisions, errored runs, parked tickets, open PRs.

        EU-102: renders s['rows'] (already typed with category+why) grouped into four
        labelled sections.  Officer recommendations and ticket proposals (not yet in rows)
        are appended below as before.

        EU-129: scopes to the active project (via ?app=) so each tab shows only that project's
        items. The app parameter is resolved by _board_project() (read-only, doesn't change the
        active tab) and passed to summary() as app_name.
        """
        from . import needs as _needs
        appq = _board_project(request.args.get("app"))   # EU-129: scope to active project
        # 2026-07-19: throttled background Jira reconcile on every load — answers/status changes
        # made IN JIRA clear their Needs-you entries without waiting for a drain (5-min TTL; the
        # ↻ button below forces it synchronously).
        try:
            from . import needs_sync as _nsync
            threading.Thread(target=_nsync.reconcile, args=(cfg, audit),
                             kwargs={"ttl_s": 300.0}, daemon=True).start()
        except Exception:  # noqa: BLE001
            pass
        s = _needs.summary(cfg, appq)
        style = (
            "<style>"
            ".nsec{margin:4px 0 24px}.nsec h3{font-size:12px;text-transform:uppercase;letter-spacing:.08em;"
            "color:var(--dim);margin:0 0 10px;font-weight:700}"
            ".ncard{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:13px 15px;margin:9px 0}"
            ".ncard .q{color:var(--ink);margin-bottom:6px}.ncard .meta{color:var(--faint);font-size:12px;"
            "font-family:ui-monospace,Menlo,monospace}"
            ".nrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px}"
            ".nrow input[type=text]{flex:1;min-width:200px;background:var(--bg);border:1px solid var(--line2);"
            "color:var(--ink);border-radius:8px;padding:8px 10px;font:inherit}"
            ".nbtn{border:0;border-radius:8px;padding:8px 13px;font-weight:650;cursor:pointer;font:inherit;"
            "text-decoration:none;display:inline-block}"
            ".nbtn.ok{background:var(--okbg);border:1px solid var(--okline);color:var(--ok)}.nbtn.send{background:var(--accent);color:#fff}"
            ".nbtn.no{background:var(--badbg);border:1px solid var(--badline);color:var(--bad)}"
            ".nbtn.x{background:var(--panel2);border:1px solid var(--line2);color:var(--dim)}"
            ".ncard details>summary{cursor:pointer;color:var(--dim);font-size:12.5px;list-style:none;display:flex;"
            "align-items:center;gap:8px;outline:none}"
            ".ncard details>summary::-webkit-details-marker{display:none}"
            ".ncard details>summary::before{content:'\\25B8';color:var(--faint);font-size:11px;transition:transform .15s}"
            ".ncard details[open]>summary::before{transform:rotate(90deg)}"
            ".ncard .ndetail{margin:11px 0 2px;padding:11px 13px;background:var(--well);border:1px solid var(--line);"
            "border-radius:8px}"
            ".ncard .ndt{color:var(--ink);font-size:13px;line-height:1.5;margin:5px 0}"
            ".ncard .ndt.sub{color:var(--dim);padding-left:8px}.ncard .ndt.muted{color:var(--faint)}"
            ".ncard .ndt b{color:var(--ink);font-weight:650}"
            ".nbanner{background:var(--accentbg);border:1px solid var(--accentline);color:var(--info);border-radius:9px;"
            "padding:11px 14px;margin:0 0 16px;font-size:13.5px;font-weight:600}"
            ".ncard label.pcheck{display:flex;gap:8px;align-items:flex-start;margin:7px 0;"
            "color:var(--ink);font-size:13.5px;cursor:pointer}"
            ".ncard label.pcheck input{margin-top:3px}"
            ".ncard .psev{color:var(--warn);font-weight:650}.ncard .ptype{color:var(--faint);font-size:12px}"
            ".nempty{color:var(--ok);padding:30px;text-align:center;font-size:15px}"
            # EU-102 — colour-coded category badges for the unified inbox
            ".nbadge{display:inline-block;border-radius:5px;padding:2px 7px;font-size:11px;"
            "font-weight:700;letter-spacing:.04em;text-transform:uppercase;margin-right:6px}"
            ".nbadge.dec{background:var(--accentbg);color:var(--accent)}"    # decision   — purple
            ".nbadge.err{background:var(--badbg);color:var(--bad)}"    # errored    — red
            ".nbadge.prk{background:var(--warnbg);color:var(--warn)}"    # parked     — amber
            ".nbadge.opr{background:var(--okbg);color:var(--ok)}"    # open PR    — teal
            "</style>")
        # One-shot confirmation banner (e.g. "Answer sent to AUTO-23…") — read + clear so it shows once.
        _m = _state.pop("last_msg", "") or ""
        banner = f"<div class=nbanner>{html.escape(str(_m))}</div>" if _m else ""
        sync_btn = ("<form method=post action=/api/needs-sync style='margin:0 0 14px'>"
                    "<button class='nbtn x' title='Check every item against live Jira NOW — items "
                    "whose ticket you already moved (Done/QA) or re-queued (To Do) in Jira are "
                    "cleared'>&#8635; Sync with Jira</button></form>")
        if not s.get("total"):
            return _wrap("Needs you", style + banner + sync_btn
                         + "<div class=nempty>&#10003; All clear — nothing needs you right now.</div>")
        out = [style, banner, sync_btn]

        # ── Unified inbox rows — grouped by category (EU-102) ────────────────
        from urllib.parse import quote
        from . import dashboard as _dash
        from collections import defaultdict

        # Real needs.summary() always provides typed ``rows``; tests (and any legacy caller) may
        # hand us only the per-stream keys, so rebuild rows from them when absent. Either way the
        # page renders one section per category — count == the rows it shows (EU-102).
        rows = list(s.get("rows") or [])
        if not rows:
            for _d in s.get("decisions", []):
                rows.append({**_d, "category": "decision",
                             "why": _d.get("question") or _d.get("summary") or "pending decision"})
            for _t in s.get("tasks", []):
                _oc = _t.get("outcome") or ""
                rows.append({**_t, "category": "pr" if _oc == "PR / needs you" else "errored",
                             "why": _t.get("note") or _oc})
        by_cat: dict = defaultdict(list)
        for _row in rows:
            by_cat[_row.get("category")].append(_row)

        # ── 1. Decisions — the CTO's open questions ───────────────────────────
        if by_cat["decision"]:
            _items = by_cat["decision"]
            out.append(f"<div class=nsec><h3>&#128172; Decisions &middot; {len(_items)}</h3>")
            for d in _items:
                tid = html.escape(str(d.get("id") or ""))
                dapp = html.escape(str(d.get("app") or ""))
                _qfull = str(d.get("question") or d.get("why") or "")
                # 2026-07-19 (Commander order / EU-337 format): a structured question renders as a
                # BRIEF problem line + one-click option buttons (recommended highlighted); clicking
                # an option ships that option text as the answer — same /api/answer path, so it
                # lands as a Jira comment and the ticket re-runs (To Do). Unstructured questions
                # keep the free-text box as before; it also stays as the "Other" fallback.
                _po = None
                try:
                    from . import decisions as _dec
                    _po = _dec.parse_options(_qfull) or _dec.synthesize_options(_qfull)
                except Exception:  # noqa: BLE001
                    _po = None
                if _po:
                    head = html.escape(_po["summary"]
                                       or _dec.summarize_question(_qfull))
                    # a synthesized card keeps the original text reachable — briefly headlined,
                    # fully inspectable (2026-07-19: "unclear walls" order)
                    _ctx = ("<details><summary>Full context</summary>"
                            f"<div class=ndetail><div class=ndt>{html.escape(_qfull[:4000])}"
                            "</div></div></details>"
                            if _po.get("synthesized") else "")
                    btns = ""
                    for o in _po["options"]:
                        _cls = "nbtn ok" if o["recommended"] else "nbtn x"
                        _star = "&#9733; " if o["recommended"] else ""
                        _lbl = html.escape(o["text"][:110])
                        _val = html.escape(f"Option {o['n']}: {o['text']}")
                        btns += (
                            "<form method=post action=/api/answer style='margin:0'>"
                            f"<input type=hidden name=ticket value='{tid}'>"
                            f"<input type=hidden name=app value='{dapp}'>"
                            f"<input type=hidden name=text value=\"{_val}\">"
                            f"<button class='{_cls}' title='Ship this option — it lands as a Jira "
                            f"comment and the ticket re-runs with it'>{_star}{o['n']}. {_lbl}</button></form>")
                    out.append(
                        "<div class=ncard>"
                        f"<div class=q><span class='nbadge dec'>Decision</span>{head}</div>"
                        f"<div class=meta>{tid}{(' &middot; ' + dapp) if dapp else ''}</div>"
                        f"{_ctx}"
                        f"<div class=nrow>{btns}</div>"
                        "<form method=post action=/api/answer class=nrow>"
                        f"<input type=hidden name=ticket value='{tid}'>"
                        f"<input type=hidden name=app value='{dapp}'>"
                        "<input type=text name=text placeholder='Other — type your own decision'>"
                        "<button class='nbtn send'>Ship answer</button></form>"
                        "<div class=nrow>"
                        "<form method=post action=/needs/resolve style='margin:0'>"
                        f"<input type=hidden name=ticket value='{tid}'>"
                        "<button class='nbtn x'>Dismiss</button></form></div>"
                        "</div>")
                    continue
                _brief = html.escape(_dec.summarize_question(_qfull)
                                     or str(d.get("why") or "(no question on file)"))
                _full = ("<details><summary>Full context</summary>"
                         f"<div class=ndetail><div class=ndt>{html.escape(_qfull[:4000])}"
                         "</div></div></details>"
                         if len(_qfull) > 160 else "")
                out.append(
                    "<div class=ncard>"
                    f"<div class=q><span class='nbadge dec'>Decision</span>{_brief}</div>"
                    f"<div class=meta>{tid}{(' &middot; ' + dapp) if dapp else ''}</div>"
                    f"{_full}"
                    "<form method=post action=/api/answer class=nrow>"
                    f"<input type=hidden name=ticket value='{tid}'>"
                    f"<input type=hidden name=app value='{dapp}'>"
                    "<input type=text name=text placeholder='Answer the unit — your decision re-runs the ticket with it baked in'>"
                    "<button class='nbtn send'>Ship answer</button></form>"
                    # Dismiss without re-running — marks question handled, removes from inbox.
                    "<div class=nrow>"
                    "<form method=post action=/needs/resolve style='margin:0'>"
                    f"<input type=hidden name=ticket value='{tid}'>"
                    "<button class='nbtn x'>Dismiss</button></form></div>"
                    "</div>")
            out.append("</div>")

        # ── 2. Errored runs — tickets that ended errored / escalated ──────────
        if by_cat["errored"]:
            _items = by_cat["errored"]
            out.append(f"<div class=nsec><h3>&#9888;&#65039; Errored runs &middot; {len(_items)}</h3>")
            for t in _items:
                tid = html.escape(str(t.get("ticket_id") or ""))
                tapp = html.escape(str(t.get("app") or ""))
                why = html.escape(str(t.get("why") or "run ended with an error"))
                note = html.escape(_dash._short(t.get("note") or "", 120))
                detail = _dash.needs_detail_html(t)          # full 'what went wrong' block
                prefill = quote(_dash.needs_chat_summary(t)) # pre-loaded into the CTO chat
                out.append(
                    "<div class=ncard><details><summary>"
                    f"<span class='nbadge err'>Errored run</span>"
                    f"<span class=meta>{tid}</span>"
                    + (f" &nbsp;<span style='color:#6b7480'>&mdash; {note}</span>" if note else "")
                    + f"</summary>"
                    f"<div class=ndetail><div class=ndt>{why}</div>{detail}</div></details>"
                    # Primary: provide a directive — re-runs the ticket with the answer baked in.
                    "<form method=post action=/api/answer class=nrow>"
                    f"<input type=hidden name=ticket value='{tid}'>"
                    f"<input type=hidden name=app value='{tapp}'>"
                    "<input type=text name=text placeholder='Answer the unit — your directive re-runs the ticket'>"
                    "<button class='nbtn send'>Ship answer</button></form>"
                    # Secondary: talk it through with the CTO, or clear the row.
                    f"<div class=nrow><a class='nbtn x' href='/chat?prefill={prefill}'>Discuss with the CTO</a>"
                    "<form method=post action=/api/dismiss style='margin:0'>"
                    f"<input type=hidden name=ticket value='{tid}'><input type=hidden name=back value='/needs'>"
                    "<button class='nbtn x'>Dismiss</button></form></div></div>")
            out.append("</div>")

        # ── 3. Parked tickets — autopilot is skipping these ───────────────────
        if by_cat["parked"]:
            _items = by_cat["parked"]
            out.append(f"<div class=nsec><h3>&#128274; Parked tickets &middot; {len(_items)}</h3>")
            for t in _items:
                tid = html.escape(str(t.get("ticket_id") or ""))
                tapp = html.escape(str(t.get("app") or ""))
                why = html.escape(str(t.get("why") or "blocked — autopilot skipping"))
                out.append(
                    "<div class=ncard>"
                    f"<div class=q><span class='nbadge prk'>Parked</span>{why}</div>"
                    f"<div class=meta>{tid}{(' &middot; ' + tapp) if tapp else ''}</div>"
                    # Unblock removes the ticket from blocked_tickets.json so autopilot retries it.
                    "<div class=nrow>"
                    "<form method=post action=/api/unblock style='margin:0'>"
                    f"<input type=hidden name=ticket value='{tid}'>"
                    "<button class='nbtn ok'>Unblock</button></form>"
                    "</div>"
                    "</div>")
            out.append("</div>")

        # ── 4. Open PRs — runs that ended with a PR opened ────────────────────
        if by_cat["pr"]:
            _items = by_cat["pr"]
            out.append(f"<div class=nsec><h3>&#128257; Open PRs &middot; {len(_items)}</h3>")
            for t in _items:
                tid = html.escape(str(t.get("ticket_id") or ""))
                tapp = html.escape(str(t.get("app") or ""))
                why = html.escape(str(t.get("why") or "PR opened — review needed"))
                pr_url = str(t.get("pr_url") or "")
                pr_link = (f" &middot; <a href='{html.escape(pr_url)}' target=_blank "
                           f"style='color:var(--ok)'>{html.escape(pr_url)}</a>") if pr_url else ""
                review_btn = (f"<a class='nbtn ok' href='{html.escape(pr_url)}' target=_blank>"
                              "Review PR</a>") if pr_url else ""
                out.append(
                    "<div class=ncard>"
                    f"<div class=q><span class='nbadge opr'>Open PR</span>{why}</div>"
                    f"<div class=meta>{tid}{(' &middot; ' + tapp) if tapp else ''}{pr_link}</div>"
                    f"<div class=nrow>{review_btn}"
                    "<form method=post action=/api/dismiss style='margin:0'>"
                    f"<input type=hidden name=ticket value='{tid}'><input type=hidden name=back value='/needs'>"
                    "<button class='nbtn x'>Dismiss</button></form>"
                    "</div>"
                    "</div>")
            out.append("</div>")

        if s.get("proposals"):
            out.append(f"<div class=nsec><h3>&#128203; Tickets to file &middot; {len(s['proposals'])}</h3>")
            for b in s["proposals"]:
                bid = html.escape(str(b.get("id") or ""))
                src = html.escape(str(b.get("source") or "proposed"))
                bapp = html.escape(str(b.get("app") or ""))
                props = b.get("proposals") or []
                prows = ""
                for p in props:
                    pt = html.escape(str(p.get("title") or ""))
                    sev = html.escape(str(p.get("severity") or "?"))
                    typ = html.escape(str(p.get("type") or "Task"))
                    prows += (f"<label class=pcheck><input type=checkbox name=titles value=\"{pt}\" checked> "
                              f"<span><span class=psev>[{sev}]</span> {pt} <span class=ptype>{typ}</span></span></label>")
                out.append(
                    "<div class=ncard>"
                    f"<div class=q>Proposed tickets &mdash; {src}</div>"
                    f"<div class=meta>{bapp} &middot; {len(props)} ticket(s) &mdash; pick which to file</div>"
                    "<form method=post action=/api/approve-proposals>"
                    f"<input type=hidden name=batch value=\"{bid}\">"
                    + prows +
                    "<div class=nrow><button class='nbtn ok'>&#9989; Approve &amp; file selected</button></div></form>"
                    "<form method=post action=/api/deny-proposals class=nrow style='margin:6px 0 0'>"
                    f"<input type=hidden name=batch value=\"{bid}\">"
                    "<button class='nbtn no'>Deny &mdash; discard</button></form></div>")
            out.append("</div>")

        return _wrap("Needs you", "".join(out))

    @app.get("/usage")
    def usage_page():
        from . import usage as _usage
        w = _usage.windows(cfg)
        bs = _usage.budget_status(cfg)
        style = (
            "<style>"
            ".ugrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin:6px 0 20px}"
            ".ucard{background:#12161f;border:1px solid #232936;border-radius:12px;padding:15px 17px}"
            ".ut{color:#8a929f;font-size:12px;text-transform:uppercase;letter-spacing:.07em;font-weight:700}"
            ".ubig{color:#e9ecf1;font-size:30px;font-weight:750;margin:7px 0 2px}.ubig .us{font-size:13px;color:#6b7480;font-weight:500}"
            ".umeta{color:#6b7480;font-size:12px;font-family:ui-monospace,Menlo,monospace}"
            ".umodels{width:100%;border-collapse:collapse;margin-top:11px;font-size:12px}"
            ".umodels th{color:#6b7480;text-align:left;font-weight:600;padding:3px 6px;border-bottom:1px solid #232936}"
            ".umodels td{color:#c3cad6;padding:3px 6px;border-bottom:1px solid #1a1f2a}"
            ".umodels .r{text-align:right;font-family:ui-monospace,Menlo,monospace}"
            ".budget{background:#12161f;border:1px solid #232936;border-radius:12px;padding:14px 17px;margin:6px 0 18px}"
            ".budget .bl{color:#e9ecf1;font-size:13px;margin-bottom:9px}"
            ".bar{height:9px;background:#0d1119;border-radius:6px;overflow:hidden;border:1px solid #222a38}"
            ".bar .fill{display:block;height:100%}.bar .fill.ok{background:#3b6cff}.bar .fill.warn{background:#d99a2b}.bar .fill.over{background:#f0676b}"
            ".bnote{color:#8a929f;font-size:12px;margin-top:8px}.mono{font-family:ui-monospace,Menlo,monospace;color:#8a929f}"
            # EU-77 — live Claude Max subscription-limits panel (the real ceiling), green→amber→red.
            ".plan{background:#12161f;border:1px solid #232936;border-radius:12px;padding:14px 17px;margin:6px 0 18px}"
            ".plan .ph{color:#e9ecf1;font-size:13px;font-weight:650;display:flex;justify-content:space-between;align-items:baseline;gap:10px}"
            ".plan .ps{color:#6b7480;font-size:11px;font-weight:500}"
            ".plan .pl{margin-top:13px}"
            ".plan .plh{display:flex;justify-content:space-between;align-items:baseline;color:#c3cad6;font-size:12px;margin-bottom:5px}"
            ".plan .plh .pp{font-family:ui-monospace,Menlo,monospace;color:#e9ecf1;font-weight:650}"
            ".plan .pm{color:#6b7480;font-size:11px;margin-top:5px;font-family:ui-monospace,Menlo,monospace}"
            ".pbar{height:9px;background:#0d1119;border-radius:6px;overflow:hidden;border:1px solid #222a38}"
            ".pbar .pf{display:block;height:100%}"
            ".pbar .pf.g{background:#3fb950}.pbar .pf.a{background:#d99a2b}.pbar .pf.r{background:#f0676b}"
            ".plan .pnote{color:#8a929f;font-size:12px;margin-top:8px}"
            "</style>")

        # EU-77 — the REAL Claude Max ceiling (session / weekly · all models / per-model), read live.
        # plan_usage() probes Claude Code ONLY on this render (cached 5 min, best-effort); on any
        # failure it reports unavailable and we fall back to the EU-75 own-ledger gauge (the daily
        # budget bar below) with a "not machine-readable" note.
        _PLAN_TONE_CLS = {"ok": "g", "warn": "a", "bad": "r"}

        def plan_panel(pu: dict) -> str:
            head = ("<div class=ph><span>Claude Max — subscription limits</span>"
                    "<span class=ps>live · the real ceiling</span></div>")
            if not pu.get("available"):
                return ("<div class=plan>" + head +
                        "<div class=pnote>Live subscription limits aren’t machine-readable right now "
                        "(CLI probe unavailable) — falling back to the unit’s own token ledger below "
                        "(the daily-budget gauge). They’ll appear here when the probe can reach them."
                        "</div></div>")
            rows = []
            for lim in pu.get("limits", []):
                cls = _PLAN_TONE_CLS.get(lim.get("tone"), "g")
                pct = int(lim.get("pct", 0))
                wpct = min(100, max(0, pct))
                label = html.escape(str(lim.get("label", "")))
                reset = html.escape(str(lim.get("resets_in", "")))
                over = lim.get("overage")
                if reset and reset not in ("now", ""):
                    meta = f"resets in {reset}" + (" · using overage" if over else "")
                elif reset == "now":
                    meta = "resetting now" + (" · using overage" if over else "")
                else:
                    meta = "using overage" if over else ""
                aria = f"{label} {pct}% used" + (f", resets in {reset}" if reset and reset not in ("now", "") else "")
                rows.append(
                    "<div class=pl>"
                    f"<div class=plh><span>{label}</span><span class=pp>{pct}%</span></div>"
                    f"<div class=pbar role=progressbar aria-valuemin=0 aria-valuemax=100 "
                    f'aria-valuenow={wpct} aria-label="{aria}">'
                    f"<span class='pf {cls}' style='width:{wpct}%'></span></div>"
                    + (f"<div class=pm>{meta}</div>" if meta else "")
                    + "</div>")
            return "<div class=plan>" + head + "".join(rows) + "</div>"

        def card(title: str, d: dict) -> str:
            rows = "".join(
                f"<tr><td>{html.escape(m)}</td><td class=r>{v['calls']:,}</td>"
                f"<td class=r>{v['in']:,}</td><td class=r>{v['out']:,}</td></tr>"
                for m, v in sorted(d["by_model"].items(), key=lambda kv: -(kv[1]['in'] + kv[1]['out'])))
            tbl = (f"<table class=umodels><tr><th>model</th><th class=r>calls</th><th class=r>in</th>"
                   f"<th class=r>out</th></tr>{rows}</table>") if rows else ""
            return (f"<div class=ucard><div class=ut>{title}</div>"
                    f"<div class=ubig>{d['total']:,}<span class=us> tokens</span></div>"
                    f"<div class=umeta>{d['calls']:,} calls · {d['input']:,} in · {d['output']:,} out</div>{tbl}</div>")

        if bs["on"]:
            pct = min(100, int(bs["pct"] * 100))
            barcls = "over" if bs["over"] else ("warn" if bs["alert"] else "ok")
            note = ("⛔ Autopilot pauses new tickets until midnight." if bs["over"]
                    else (f"⚠️ Past the {int(float(getattr(cfg, 'budget_alert_pct', 0.8)) * 100)}% alert line." if bs["alert"]
                          else "On track."))
            budget = (f"<div class=budget><div class=bl>Daily budget — <b>{bs['used']:,}</b> / "
                      f"{bs['cap']:,} tokens ({pct}%)</div>"
                      f"<div class=bar><span class='fill {barcls}' style='width:{pct}%'></span></div>"
                      f"<div class=bnote>{note}</div></div>")
        else:
            budget = ("<div class=budget><div class=bl>No daily budget set — "
                      "<span class=mono>daily_token_budget: 0</span>. Set it in config.yaml so Autopilot "
                      "auto-pauses runaway spend.</div></div>")

        mix = _usage.code_mix(cfg, _usage._day_start())
        if mix["total"]:
            bt = mix["by_tier"]
            mixbanner = (
                f"<div class=budget><div class=bl>Model ladder today — <b>{mix['total']}</b> code calls: "
                f"<b>{int(mix['cheap_pct'] * 100)}%</b> Sonnet/Haiku · {int(mix['opus_pct'] * 100)}% Opus</div>"
                f"<div class=bnote>builder / reviewer / engineers — opus {bt['opus']} · sonnet {bt['sonnet']} · "
                f"haiku {bt['haiku']}. A higher Sonnet/Haiku share = the economical ladder working "
                f"(complex work still escalates to Opus).</div></div>")
        else:
            mixbanner = ""

        # The live subscription ceiling (EU-77) sits beside the own-ledger daily-budget gauge (EU-75):
        # one shows the real plan cap, the other the unit's self-imposed budget. Best-effort probe.
        plan = plan_panel(_usage.plan_usage(cfg))

        # EU-122: Dual-provider budget gauge — shows Claude + GLM side-by-side with low-watermark indicators
        # GLM usage data is None for now (placeholder) until backend integration is added
        dual_gauge = _dual_provider_gauge(cfg, _usage.plan_usage(cfg), glm_usage=None)

        body = (style + dual_gauge + plan + budget + mixbanner + "<div class=ugrid>"
                + card("Today", w["today"]) + card("Last 7 days", w["week"])
                + card("Last 30 days", w["month"]) + "</div>")
        return _wrap("Token usage", body)

    @app.get("/budget")
    def budget_page():
        """EU-122: Dedicated budget page — dual-provider budget monitor with Claude + GLM side-by-side.

        Shows a focused view of both providers' budget status with:
        - Claude Max plan limits (live subscription data)
        - GLM quota (placeholder until backend integration)
        - Low-watermark indicators (green → amber → red)
        - Reset times and remaining percentages
        """
        from . import usage as _usage

        style = (
            "<style>"
            ".budgetpage{max-width:900px;margin:0 auto;padding:20px 0}"
            ".bhead{color:#e9ecf1;font-size:22px;font-weight:700;margin-bottom:18px}"
            ".bsubhead{color:#8a929f;font-size:14px;margin-bottom:24px}"
            "</style>")

        # Get current usage data for both providers
        claude_usage = _usage.plan_usage(cfg)
        glm_usage = None  # Placeholder until GLM backend integration is added

        # Render the dual-provider gauge
        dual_gauge = _dual_provider_gauge(cfg, claude_usage, glm_usage)

        # Additional explanation text
        expl = (
            '<div class=bsubhead>'
            'Track remaining budget across all configured providers. '
            'Low-watermark indicators show when a provider is running low (amber) or critical (red).'
            '</div>'
        )

        return _wrap("Budget monitor", style + "<div class=budgetpage>"
                     '<div class=bhead>Dual-provider budget monitor</div>' + expl + dual_gauge + "</div>")

    @app.get("/jira")
    def jira_page():
        """Pick a Jira per project + quick-connect a new one. Roman runs several products against
        DIFFERENT Jira accounts; this lets him switch the active Jira for the current project (or add one)
        without hand-editing .env / config. Tokens are shown masked; the raw token never leaves the box."""
        from . import connections as _conn
        appq = (request.args.get("app") or (cfg.apps[0].name if cfg.apps else "")).strip()
        conns = _conn.list_connections(cfg)
        active_id = _conn.assigned_id(cfg, appq)
        active = next((c for c in conns if c["id"] == active_id), None)
        esc = html.escape

        proj_opts = "".join(
            f"<option value='{esc(a.name)}' {'selected' if a.name == appq else ''}>{esc(a.name)}</option>"
            for a in cfg.apps)
        switcher = (f"<label class=jlbl>Project</label><select class=jsel "
                    f"onchange=\"location.href='/jira?app='+encodeURIComponent(this.value)\">{proj_opts}</select>")
        if active:
            active_html = (f"<div class=jactive><span class=jdot></span>"
                           f"<div><div class=jbig>&#9989; Connected to Jira &middot; <b>{esc(active['name'])}</b> "
                           f"&middot; <span class=jmono>{esc(active['base_url'])}</span></div>"
                           f"<div class=jsub>{esc(active['email'])} &middot; token {esc(active['token_hint'])}"
                           + (f" &middot; project {esc(active['project_key'])}" if active['project_key'] else "")
                           + " &middot; use Edit below to change</div></div></div>")
        else:
            # No cockpit quick-connect assigned — but the app may ALREADY use Jira via its config.yaml
            # `backlog:` + env-var creds (this is how automatixy pulls AUTO-* today). Show THAT as the
            # live connection with its real details, instead of wrongly claiming "no Jira".
            try:
                _appcfg = cfg.app(appq)
            except Exception:  # noqa: BLE001
                _appcfg = None
            _b = (getattr(_appcfg, "backlog", {}) or {}) if _appcfg else {}
            if _appcfg and getattr(_appcfg, "backlog_backend", "") == "jira" and _b.get("base_url"):
                _ee = _b.get("email_env", "JIRA_EMAIL")
                _te = _b.get("token_env", "JIRA_API_TOKEN")
                _email = os.environ.get(_ee, "")
                _who = (esc(_email) if _email
                        else f"<span class=jmono>{esc(_ee)}</span> <span class=jsub>(not set in this process)</span>")
                _tok = (f"token <span class=jmono>{esc(_te)}</span> &#10003; set" if os.environ.get(_te)
                        else f"token <span class=jmono>{esc(_te)}</span> &mdash; not set")
                _proj = _b.get("project_key", "")
                active_html = (
                    "<div class=jactive><span class=jdot></span><div>"
                    f"<div class=jbig>&#9989; Connected to Jira &middot; project <b>{esc(_proj) or '&mdash;'}</b> "
                    f"&middot; <span class=jmono>{esc(_b['base_url'])}</span></div>"
                    f"<div class=jsub>user {_who} &middot; {_tok} &middot; from config.yaml + .env "
                    "&middot; use Edit below to override</div>"
                    "</div></div>")
            else:
                active_html = ("<div class=jactive off><span class=jdot off></span><div>No Jira for "
                               f"<b>{esc(appq)}</b> &mdash; free-text tasks only. Connect one below to pull "
                               "tickets.</div></div>")

        if conns:
            cards = []
            for c in conns:
                is_active = c["id"] == active_id
                if is_active:
                    use = "<span class='jbtn on' title='Active for this project'>&#10003; Active</span>"
                else:
                    use = (f"<form method=post action=/api/jira-assign class=jf>"
                           f"<input type=hidden name=app value='{esc(appq)}'>"
                           f"<input type=hidden name=id value='{esc(c['id'])}'>"
                           f"<button class=jbtn>Use for {esc(appq)}</button></form>")
                forget = (f"<form method=post action=/api/jira-forget class=jf "
                          f"onsubmit=\"return confirm('Forget the Jira connection &quot;{esc(c['name'])}&quot;?')\">"
                          f"<input type=hidden name=app value='{esc(appq)}'>"
                          f"<input type=hidden name=id value='{esc(c['id'])}'>"
                          f"<button class='jbtn ghost'>Forget</button></form>")
                cards.append(
                    f"<div class='jcard{' act' if is_active else ''}'>"
                    f"<div class=jname>{esc(c['name'])}{' <span class=jtag>active</span>' if is_active else ''}</div>"
                    f"<div class=jmeta><span class=jmono>{esc(c['base_url'])}</span></div>"
                    f"<div class=jmeta>{esc(c['email'])} &middot; token <span class=jmono>{esc(c['token_hint'])}</span>"
                    + (f" &middot; project <span class=jmono>{esc(c['project_key'])}</span>" if c['project_key'] else "")
                    + f"</div><div class=jrow>{use}{forget}</div></div>")
            conns_html = "<div class=jcards>" + "".join(cards) + "</div>"
        else:
            conns_html = ("<div class=jempty>No saved Jira connections yet. Add your first one below — for "
                          "example one account for Automatixy and another for your algo-trading robot.</div>")

        form = (
            f"<form method=post action=/api/jira-connect class=jconnect>"
            f"<input type=hidden name=app value='{esc(appq)}'>"
            "<div class=jgrid>"
            "<label>Name<input name=name placeholder='e.g. Automatixy Jira' required></label>"
            "<label>Site URL<input name=base_url type=url placeholder='https://your-site.atlassian.net' required></label>"
            "<label>Email<input name=email type=email placeholder='you@example.com' required></label>"
            "<label>API token<input name=token type=password placeholder='paste Atlassian API token' required></label>"
            "<label>Project key <span class=jopt>(optional)</span><input name=project_key placeholder='e.g. AUTO'></label>"
            "</div>"
            f"<label class=jassign><input type=checkbox name=assign value=1 checked> Use this Jira for "
            f"<b>{esc(appq)}</b> right away</label>"
            "<div class=jrow><button class='jbtn primary'>&#128268; Connect Jira</button>"
            "<span class=jhint>Create a token at id.atlassian.com &rarr; Security &rarr; API tokens. "
            "Stored locally, masked here, never committed to git.</span></div>"
            "</form>")

        style = (
            "<style>"
            ".jbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:4px 0 16px}"
            ".jlbl{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.06em;font-weight:700}"
            ".jsel{min-width:190px}"
            ".jactive{display:flex;gap:11px;align-items:center;background:var(--okbg);border:1px solid var(--okline);"
            "border-radius:12px;padding:14px 16px;margin:0 0 22px}"
            ".jactive.off{background:var(--warnbg);border-color:var(--warnline)}"
            ".jactive .jbig{font-size:15px;font-weight:700;color:var(--ink)}"
            ".jdot{width:9px;height:9px;border-radius:50%;background:var(--ok);flex:none}"
            ".jdot.off{background:var(--warn)}"
            ".jsub{color:var(--dim);font-size:12.5px;margin-top:3px}"
            ".jmono{font-family:var(--mono);color:var(--dim)}"
            ".jcards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:13px;margin:6px 0 26px}"
            ".jcard{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}"
            ".jcard.act{border-color:var(--okline)}"
            ".jname{font-size:15px;font-weight:700;color:var(--ink);margin-bottom:7px}"
            ".jtag{font-size:10px;font-weight:800;color:var(--ok);border:1px solid var(--okline);border-radius:99px;"
            "padding:1px 7px;margin-left:6px;vertical-align:middle;text-transform:uppercase}"
            ".jmeta{color:var(--dim);font-size:12.5px;margin-top:3px}"
            ".jrow{display:flex;gap:8px;align-items:center;margin-top:12px;flex-wrap:wrap}"
            ".jf{margin:0}"
            ".jbtn{background:var(--panel2);border:1px solid var(--line2);color:var(--ink);border-radius:8px;padding:8px 13px;"
            "font:inherit;font-size:13px;font-weight:600;cursor:pointer}.jbtn:hover{border-color:var(--accent)}"
            ".jbtn.primary{background:var(--accent);border-color:var(--accent);color:#fff}.jbtn.primary:hover{background:var(--accent-hover)}"
            ".jbtn.ghost{background:none;color:var(--dim)}.jbtn.ghost:hover{color:var(--bad);border-color:var(--badline)}"
            ".jbtn.on{background:none;border:1px solid var(--okline);color:var(--ok);padding:8px 13px;border-radius:8px;"
            "font-size:13px;font-weight:700}"
            ".jconnect{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px}"
            ".jgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}"
            ".jconnect label{display:block;color:var(--ink);font-size:12.5px;font-weight:600}"
            ".jconnect .jgrid input{width:100%;margin-top:5px;box-sizing:border-box}"
            ".jopt{color:var(--faint);font-weight:400}"
            ".jassign{display:flex;align-items:center;gap:8px;margin:14px 0 4px;color:var(--ink);font-weight:500!important}"
            ".jhint{color:var(--faint);font-size:12px}"
            "details.jeditbox>summary{list-style:none;display:inline-block;cursor:pointer}"
            "details.jeditbox>summary::-webkit-details-marker{display:none}"
            "details.jeditbox[open]>summary .jbtn{border-color:var(--accent);color:var(--accent)}"
            "details.jeditbox>.jconnect{margin-top:12px}"
            "h3{margin:24px 0 8px;font-size:14px;color:var(--dim)}"
            "</style>")

        # 2026-07-19 (Commander order): a healthy connection reads as ONE line — "Connected to
        # Jira ✅" with the essentials — and the connect form hides behind an Edit button instead
        # of a permanent wall of boxes. Saved connections render only when there ARE any; a
        # not-connected project keeps the form open (there is nothing to hide behind).
        _connected = "jactive off" not in active_html
        # 2026-07-19 (Commander order): MULTI-JIRA is first-class — one project on Jira X,
        # another on Jira Y. The same form both edits and ADDS (a new name + site = a new saved
        # connection; the checkbox binds it to the project selected above), and every saved
        # connection card carries a "Use for <project>" button.
        edit_box = ("<details class=jeditbox" + ("" if _connected else " open") + ">"
                    "<summary><span class=jbtn>&#9998; "
                    + ("Edit &middot; &#65291; Add another Jira" if _connected else "Connect a Jira")
                    + "</span></summary>"
                    "<p class=jhint style='margin:10px 0 8px'>Each project can use its OWN Jira — "
                    "pick the project above, then connect (or assign a saved connection). A new "
                    "name + site here saves as an additional connection.</p>"
                    + form + "</details>")
        body = (style + "<div class=jbar>" + switcher + "</div>" + active_html
                + (("<h3>Saved Jira connections</h3>" + conns_html) if conns else "")
                + edit_box)
        return _wrap("Jira connections", body)

    @app.post("/api/jira-connect")
    def jira_connect_api():
        from . import connections as _conn
        f = request.form
        app_name = (f.get("app") or "").strip()
        base = (f.get("base_url") or "").strip()
        email = (f.get("email") or "").strip()
        token = (f.get("token") or "").strip()
        if base and email and token:
            cid = _conn.add(cfg, name=(f.get("name") or "").strip(), base_url=base, email=email,
                            token=token, project_key=(f.get("project_key") or "").strip())
            if f.get("assign") and app_name:
                _conn.assign(cfg, app_name, cid)
        return redirect(f"/jira?app={quote(app_name)}")

    @app.post("/api/jira-assign")
    def jira_assign_api():
        from . import connections as _conn
        app_name = (request.form.get("app") or "").strip()
        _conn.assign(cfg, app_name, (request.form.get("id") or "").strip())
        return redirect(f"/jira?app={quote(app_name)}")

    @app.post("/api/jira-forget")
    def jira_forget_api():
        from . import connections as _conn
        _conn.remove(cfg, (request.form.get("id") or "").strip())
        return redirect(f"/jira?app={quote((request.form.get('app') or '').strip())}")

    @app.get("/onboard")
    def onboard_page():
        """Scaffold a new product into config.yaml — so the unit serves SignalDesk / the EAs, not just
        Automatixy. Detects branches, backs up the file, optionally wires a saved Jira connection.
        Repos found next to your configured ones are listed as click-to-onboard (they pre-fill the form)."""
        from . import connections as _conn
        from . import onboarding as _ob
        from . import projects as _proj
        esc = html.escape
        # Pre-fill from a "Found nearby" click (?name=&repo=) — values flow straight into the form.
        pre_name = (request.args.get("name") or "").strip()
        pre_repo = (request.args.get("repo") or "").strip()
        pre_base = pre_protected = ""
        if pre_repo:
            try:
                rp = Path(pre_repo).expanduser()
                if _ob.is_git_repo(rp):
                    pre_base, pre_protected = _ob.suggest_branches(rp)
            except Exception:  # noqa: BLE001
                pass
        conns = _conn.list_connections(cfg)
        copts = ("<option value=''>— none (free-text tasks, or JIRA_EMAIL/JIRA_API_TOKEN) —</option>"
                 + "".join(f"<option value='{esc(c['id'])}'>{esc(c['name'])} &middot; {esc(c['base_url'])}"
                           f"</option>" for c in conns))
        current = ", ".join(esc(a.name) for a in cfg.apps) or "—"
        style = (
            "<style>"
            ".cur{color:#8a929f;margin:2px 0 18px}.cur b{color:#e9ecf1}"
            ".ob{background:#12161f;border:1px solid #232936;border-radius:12px;padding:17px 19px}"
            ".obgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:13px}"
            ".ob label{display:block;color:#c4c9d2;font-size:12.5px;font-weight:600}"
            ".ob input,.ob select{width:100%;margin-top:5px;box-sizing:border-box}"
            ".ob .opt{color:#5c6573;font-weight:400}"
            ".obrow{display:flex;gap:12px;align-items:center;margin-top:15px;flex-wrap:wrap}"
            ".jbtn{background:#1b2230;border:1px solid #2a3343;color:#e9ecf1;border-radius:8px;padding:9px 15px;"
            "font:inherit;font-size:13px;font-weight:650;cursor:pointer}"
            ".jbtn.primary{background:#2b5cff;border-color:#2b5cff;color:#fff}.jbtn.primary:hover{background:#2350e6}"
            ".hint{color:#6b7480;font-size:12px}.hint2{color:#8a929f;font-size:12.5px;margin-top:16px}"
            ".nearby{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 8px}"
            ".chip{display:inline-flex;flex-direction:column;gap:1px;background:#12161f;border:1px solid #2a3343;"
            "border-radius:10px;padding:8px 12px;text-decoration:none;color:#e9ecf1;font-size:13px}"
            ".chip:hover{border-color:#3b6cff;background:#161b25}.chip.on{border-color:#3b6cff;background:#16203a}"
            ".chip small{color:#6b7480;font-size:11px;font-family:ui-monospace,Menlo,monospace}"
            "h3{margin:18px 0 9px;font-size:14px;color:#c4c9d2}"
            "</style>")
        # Found-nearby repos (git repos beside your configured ones, not yet in config) → click to pre-fill.
        try:
            disc = _proj.discover_repos(cfg)
        except Exception:  # noqa: BLE001
            disc = []
        nearby = ""
        if disc:
            chips = "".join(
                f"<a class='chip{' on' if r['path'] == pre_repo else ''}' "
                f"href='/onboard?name={quote(_ob.slug(r['name']))}&repo={quote(r['path'])}'>"
                f"{esc(r['name'])}<small>{esc(r['path'])}</small></a>" for r in disc)
            nearby = ("<h3>Found nearby <span class=opt style='color:#5c6573;font-weight:400'>"
                      "(click to fill the form)</span></h3><div class=nearby>" + chips + "</div>")

        def val(v):
            return f" value='{esc(v)}'" if v else ""
        form = (
            "<form method=post action=/api/onboard class=ob>"
            "<div class=obgrid>"
            f"<label>Product name<input name=name placeholder='e.g. signaldesk'{val(pre_name)} required></label>"
            f"<label>Repo path<input name=repo_path placeholder='/Users/&hellip;/SignalDesk'{val(pre_repo)} required></label>"
            "<label>Base branch <span class=opt>(features merge here)</span>"
            f"<input name=base placeholder='auto-detect'{val(pre_base)}></label>"
            "<label>Protected branch <span class=opt>(production)</span>"
            f"<input name=protected placeholder='auto-detect'{val(pre_protected)}></label>"
            f"<label>Jira <span class=opt>(pick a saved connection)</span><select name=jira>{copts}</select></label>"
            "</div>"
            "<div class=obrow><button class='jbtn primary'>&#10133; Add product</button>"
            "<span class=hint>Detects branches from the repo, backs up config.yaml, inserts the entry. "
            "Restart the cockpit to load it.</span></div></form>")
        body = (style + f"<p class=cur>Current products: <b>{current}</b></p>"
                + nearby + "<h3>Onboard a new product</h3>" + form
                + "<p class=hint2>No Jira yet? <a href='/jira'>Connect one first</a>, then pick it here.</p>")
        return _wrap("Onboard a product", body)

    @app.post("/api/onboard")
    def onboard_api():
        from . import onboarding
        esc = html.escape
        f = request.form
        path = getattr(cfg, "_source_path", "config.yaml")
        jira = (f.get("jira") or "").strip()
        r = onboarding.scaffold(path, name=(f.get("name") or ""), repo_path=(f.get("repo_path") or ""),
                                base=(f.get("base") or None), protected=(f.get("protected") or None),
                                backlog=("jira" if jira else "none"), connection_id=jira, write=True)
        if not r["ok"]:
            return _wrap("Onboard a product",
                         f"<p style='color:#f0676b'>&#10007; {esc(r['error'])}</p>"
                         "<style>.backlnk{{display:inline-flex;align-items:center;gap:8px;color:var(--ink);"
                         "font-size:13px;font-weight:600;text-decoration:none;padding:8px 14px;"
                         "background:var(--panel2);border:1px solid var(--line);border-radius:var(--r-md);"
                         "transition:all var(--t-fast);margin:8px 0}}"
                         ".backlnk:hover{{background:var(--line);border-color:var(--accent);color:var(--accent);"
                         "transform:translateX(-2px)}}"
                         ".backlnk svg{{width:16px;height:16px;flex:none;transition:transform var(--t-fast)}}"
                         ".backlnk:hover svg{{transform:translateX(-2px)}}"
                         ".backlnk:focus-visible{{outline:none;box-shadow:var(--ring)}}</style>"
                         "<a class='backlnk' href='/onboard' aria-label='Go back to onboarding'>"
                         "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'>"
                         "<path d='M19 12H5M12 19l-7-7 7-7'/></svg>back</a>")
        warn = "".join(f"<li>&#9888; {esc(w)}</li>" for w in r["warnings"])
        warnhtml = f"<ul style='color:#d99a2b'>{warn}</ul>" if warn else ""
        return _wrap("Onboard a product",
                     f"<p style='color:#3fb961'>&#10003; Added <b>{esc(r['name'])}</b> — base "
                     f"{esc(r['base'])} &rarr; protected {esc(r['protected'])}. Backed up config.yaml; "
                     f"<b>restart the cockpit / autopilot</b> to load it.</p>{warnhtml}"
                     f"<pre class=rep>{esc(r['block'])}</pre>"
                     "<style>.navbtns{{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:8px 0}}"
                     ".navbtn{{display:inline-flex;align-items:center;gap:8px;color:var(--ink);"
                     "font-size:13px;font-weight:600;text-decoration:none;padding:8px 14px;"
                     "background:var(--panel2);border:1px solid var(--line);border-radius:var(--r-md);"
                     "transition:all var(--t-fast)}}"
                     ".navbtn:hover{{background:var(--line);border-color:var(--accent);color:var(--accent);"
                     "transform:translateX(-2px)}}"
                     ".navbtn svg{{width:16px;height:16px;flex:none;transition:transform var(--t-fast)}}"
                     ".navbtn:hover svg{{transform:translateX(-2px)}}"
                     ".navbtn:focus-visible{{outline:none;box-shadow:var(--ring)}}</style>"
                     "<div class=navbtns>"
                     "<a class='navbtn' href='/onboard' aria-label='Onboard another product'>"
                     "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'>"
                     "<path d='M19 12H5M12 19l-7-7 7-7'/></svg>onboard another</a> "
                     "<a class='navbtn' href='/'>cockpit</a></div>")

    @app.get("/forensics")
    def forensics_page():
        """Why tickets fail — a taxonomy of causes, repeat offenders, and the auto-written post-mortems."""
        from . import forensics as _fx
        esc = html.escape
        cat = (request.args.get("cat") or "").strip()
        if cat:  # a KPI/taxonomy deep-link: show only the failed runs in this cause category
            label = _fx._LABELS.get(cat, cat)
            runs = [r for r in _fx.scan(cfg) if r.get("category") == cat]
            action = _fx._ACTIONS.get(cat, "")
            head = ("<style>.backlnk{{display:inline-flex;align-items:center;gap:8px;color:var(--ink);"
                    "font-size:13px;font-weight:600;text-decoration:none;padding:8px 14px;"
                    "background:var(--panel2);border:1px solid var(--line);border-radius:var(--r-md);"
                    "transition:all var(--t-fast);margin:8px 0}}"
                    ".backlnk:hover{{background:var(--line);border-color:var(--accent);color:var(--accent);"
                    "transform:translateX(-2px)}}"
                    ".backlnk svg{{width:16px;height:16px;flex:none;transition:transform var(--t-fast)}}"
                    ".backlnk:hover svg{{transform:translateX(-2px)}}"
                    ".backlnk:focus-visible{{outline:none;box-shadow:var(--ring)}}</style>"
                    f"<p><a class='backlnk' href='/forensics' aria-label='Back to forensics'>"
                    f"<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'>"
                    f"<path d='M19 12H5M12 19l-7-7 7-7'/></svg>forensics</a></p>"
                    f"<h2 style='font-size:16px;margin:4px 0 2px'>{esc(label)}</h2>"
                    f"<p style='color:#8a929f'>{len(runs)} matching run(s)."
                    + (f" &#8594; {esc(action)}" if action else "") + "</p>")
            if not runs:
                inner = head + ("<p style='color:#8a929f'>None on record — nothing flagged in this "
                                "category.</p>")
                return _wrap(f"Forensics — {label}", inner)
            trs = []
            for r in runs:
                when = r["started"].strftime("%b %d %H:%M") if r.get("started") else "—"
                trs.append(f"<tr><td><b>{esc(str(r.get('ticket_id') or '?'))}</b></td>"
                           f"<td>{esc(str(r.get('app') or '—'))}</td><td>{esc(when)}</td>"
                           f"<td>{esc(str(r.get('outcome') or ''))}</td>"
                           f"<td>{esc((r.get('note') or r.get('verdict') or '').strip()[:160])}</td></tr>")
            table = ("<table class=fxtbl><tr><th>ticket</th><th>app</th><th>when</th>"
                     "<th>outcome</th><th>note</th></tr>" + "".join(trs) + "</table>")
            style = ("<style>.fxtbl{width:100%;border-collapse:collapse;margin:8px 0;font-size:13px}"
                     ".fxtbl th{color:#6b7480;text-align:left;font-weight:600;padding:6px 8px;border-bottom:1px solid #232936}"
                     ".fxtbl td{color:#c3cad6;padding:6px 8px;border-bottom:1px solid #1a1f2a}</style>")
            return _wrap(f"Forensics — {label}", style + head + table)
        pm = (request.args.get("pm") or "").strip()
        if pm:  # view one post-mortem
            try:
                md = _fx.postmortem_path(cfg, pm).read_text(encoding="utf-8")
            except OSError:
                md = ""
            inner = ("<style>.backlnk{{display:inline-flex;align-items:center;gap:8px;color:var(--ink);"
                     "font-size:13px;font-weight:600;text-decoration:none;padding:8px 14px;"
                     "background:var(--panel2);border:1px solid var(--line);border-radius:var(--r-md);"
                     "transition:all var(--t-fast);margin:8px 0}}"
                     ".backlnk:hover{{background:var(--line);border-color:var(--accent);color:var(--accent);"
                     "transform:translateX(-2px)}}"
                     ".backlnk svg{{width:16px;height:16px;flex:none;transition:transform var(--t-fast)}}"
                     ".backlnk:hover svg{{transform:translateX(-2px)}}"
                     ".backlnk:focus-visible{{outline:none;box-shadow:var(--ring)}}</style>"
                     f"<p><a class='backlnk' href='/forensics' aria-label='Back to forensics'>"
                     f"<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'>"
                     f"<path d='M19 12H5M12 19l-7-7 7-7'/></svg>forensics</a></p>"
                     + (f"<pre class=rep>{esc(md)}</pre>" if md
                        else f"<p>No post-mortem on file for {esc(pm)}.</p>"))
            return _wrap(f"Post-mortem — {pm}", inner)

        tax = _fx.taxonomy(cfg)
        offenders = _fx.repeat_offenders(cfg, threshold=2)
        total = sum(t["count"] for t in tax)
        style = (
            "<style>"
            ".fxtax{display:flex;flex-direction:column;gap:9px;margin:6px 0 22px}"
            ".fxrow{background:#12161f;border:1px solid #232936;border-radius:10px;padding:11px 14px}"
            ".fxhead{display:flex;justify-content:space-between;align-items:baseline;gap:10px}"
            ".fxlabel{color:#e9ecf1;font-weight:650;font-size:13.5px}.fxn{color:#8a929f;font-size:12px;font-family:ui-monospace,Menlo,monospace}"
            ".fxbar{height:7px;background:#0d1119;border-radius:5px;overflow:hidden;border:1px solid #222a38;margin:8px 0 7px}"
            ".fxbar .fill{display:block;height:100%;background:#3b6cff}"
            ".fxact{color:#8a929f;font-size:12.5px}"
            ".fxtbl{width:100%;border-collapse:collapse;margin:4px 0 20px;font-size:13px}"
            ".fxtbl th{color:#6b7480;text-align:left;font-weight:600;padding:6px 8px;border-bottom:1px solid #232936}"
            ".fxtbl td{color:#c3cad6;padding:6px 8px;border-bottom:1px solid #1a1f2a}"
            ".fxtbl .c{color:#f0a93f;font-weight:700;font-family:ui-monospace,Menlo,monospace}"
            ".fxempty{background:#101620;border:1px solid #1f6f43;border-radius:12px;padding:16px 18px;color:#aab2c0}"
            "h3{margin:20px 0 8px;font-size:14px;color:#c4c9d2}"
            "</style>")
        if not tax:
            return _wrap("Failure forensics", style + "<div class=fxempty>&#10003; No failed runs on "
                         "record — clean sheet.</div>")
        mx = max(t["count"] for t in tax) or 1
        rows = "".join(
            f"<div class=fxrow><div class=fxhead><span class=fxlabel>{esc(t['label'])}</span>"
            f"<span class=fxn>{t['count']} / {total}</span></div>"
            f"<div class=fxbar><span class=fill style='width:{int(t['count']/mx*100)}%'></span></div>"
            f"<div class=fxact>&#8594; {esc(t['action'])}</div></div>" for t in tax)
        off_html = ""
        if offenders:
            trs = []
            for o in offenders:
                exists = _fx.postmortem_path(cfg, o["ticket_id"]).exists()
                link = (f"<a href='/forensics?pm={quote(o['ticket_id'])}'>post-mortem &rarr;</a>"
                        if exists else "<span style='color:#5c6573'>—</span>")
                trs.append(f"<tr><td><b>{esc(o['ticket_id'])}</b></td><td>{esc(o['app'] or '—')}</td>"
                           f"<td class=c>×{o['count']}</td><td>{esc(o['label'])}</td><td>{link}</td></tr>")
            off_html = ("<h3>Repeat offenders</h3><table class=fxtbl><tr><th>ticket</th><th>app</th>"
                        "<th>fails</th><th>dominant cause</th><th>post-mortem</th></tr>"
                        + "".join(trs) + "</table>")
        body = (style + f"<p style='color:#8a929f'>{total} failed run(s), grouped by cause.</p>"
                + "<div class=fxtax>" + rows + "</div>" + off_html)
        return _wrap("Failure forensics", body)

    @app.get("/roster-doc")
    def roster_doc_page():
        from . import roster as _roster
        return _wrap("Unit roster", _roster.html_view(cfg, _roster.latest_status(cfg)))

    @app.get("/ship-preview")
    def ship_preview_page():
        from . import sync as _sync
        appq = (request.args.get("app") or "").strip() or (cfg.apps[0].name if cfg.apps else "")
        # Skinned with the EU-39 design tokens injected by _wrap; the purple "ship" accent
        # stays a distinct brand colour (matches the control-bar Ship button) on purpose.
        style = (
            "<style>"
            ".shp{max-width:940px}.shhead{background:#171226;border:1px solid #2c2148;border-radius:var(--r-lg);"
            "padding:16px 18px;margin:4px 0 18px}.shhead h2{margin:0 0 6px;color:var(--ink);font-size:20px}"
            ".shhead .meta{color:#b9a6e6;font-size:13px;font-family:var(--mono)}"
            ".shtix{margin:14px 0 6px;color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.07em;font-weight:700}"
            ".shcard{background:var(--panel);border:1px solid var(--line);border-radius:var(--r-md);padding:12px 15px;margin:9px 0}"
            ".shcard .tk{color:var(--ink);font-weight:700;font-size:14px}.shcard .tk a{color:#7aa2ff;text-decoration:none}"
            ".shcard .n{color:var(--dim);font-size:12px;margin-left:6px}"
            ".shcard ul{margin:8px 0 0;padding-left:0;list-style:none}"
            ".shcard li{color:#c3cad6;font-size:13px;padding:3px 0;display:flex;gap:9px}"
            ".shcard li .sha{color:#7aa2ff;font-family:var(--mono);white-space:nowrap}"
            ".shbar{display:flex;gap:10px;align-items:center;margin:20px 0 8px}"
            ".shgo{background:#7c3aed;border:0;color:#fff;border-radius:var(--r-md);padding:11px 18px;font-weight:700;cursor:pointer;font:inherit}"
            ".shgo:hover{background:#6d28d9}.shgo:focus-visible{outline:none;box-shadow:var(--ring)}}"
            ".shcancel{{display:inline-flex;align-items:center;gap:8px;color:var(--ink);"
            "font-size:13px;font-weight:600;text-decoration:none;padding:10px 16px;"
            "background:var(--panel2);border:1px solid var(--line);border-radius:var(--r-md);"
            "transition:all var(--t-fast);box-shadow:var(--shadow-1)}}"
            ".shcancel:hover{{background:var(--line);border-color:var(--accent);color:var(--accent);"
            "transform:translateX(-2px);box-shadow:var(--shadow-2)}}"
            ".shcancel svg{{width:16px;height:16px;flex:none;transition:transform var(--t-fast)}}"
            ".shcancel:hover svg{{transform:translateX(-2px)}}"
            ".shcancel:focus-visible{{outline:none;box-shadow:var(--ring)}}"
            ".shempty{color:var(--ok);padding:30px;text-align:center;font-size:15px}</style>")
        if not appq:
            return _wrap("Ship to production", style + "<div class=shempty>No app selected.</div>")
        try:
            app_cfg = cfg.app(appq)
            st = _sync.app_promote_status(app_cfg)
            commits = _sync.app_promote_commits(app_cfg)
        except Exception as e:  # noqa: BLE001
            return _wrap("Ship to production",
                         f"<p>Couldn't read {html.escape(appq)}: {html.escape(str(e)[:200])}</p>")
        base, prot, ahead = st.get("base", "DEV"), st.get("prot", "MAIN"), st.get("ahead", 0)
        back = f'<a class=shcancel href="/?app={html.escape(appq)}" aria-label="Back to cockpit"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>back to cockpit</a>'
        if not ahead:
            return _wrap("Ship to production", style + f'<div class=shp><div class=shempty>&#10003; '
                         f'{html.escape(appq)} — {html.escape(base)} and {html.escape(prot)} are in sync. '
                         f'Nothing to ship.</div>{back}</div>')
        # group commits by ticket, preserving first-seen order
        groups: "dict[str, list]" = {}
        for c in commits:
            groups.setdefault(c.get("ticket") or "—", []).append(c)
        tickets = [t for t in groups if t != "—"]
        jira_base = ""
        try:
            jira_base = str((getattr(app_cfg, "backlog", {}) or {}).get("base_url", "")).rstrip("/")
        except Exception:  # noqa: BLE001
            jira_base = ""
        # EU-55/F12(b): the commit SHAs rendered as dead text — make each a real deep-link to the
        # commit on the git host so "what's about to ship" is actually inspectable. Derive the web
        # base from the app repo's origin remote (handles both scp-style ssh and https URLs); when
        # no usable http(s) origin resolves, degrade to plain text (no broken link).
        def _web_base() -> str:
            try:
                raw = (_sync._origin_url(Path(app_cfg.repo_path).expanduser()) or "").strip()
            except Exception:  # noqa: BLE001
                raw = ""
            if not raw:
                return ""
            if raw.endswith(".git"):
                raw = raw[:-4]
            if raw.startswith("git@") and ":" in raw:        # git@github.com:owner/repo
                host, _, path = raw[4:].partition(":")
                return f"https://{host}/{path.lstrip('/')}"
            if raw.startswith("ssh://"):                     # ssh://git@host/owner/repo
                rest = raw[len("ssh://"):]
                return "https://" + (rest.split("@", 1)[1] if "@" in rest else rest)
            return raw if raw.startswith(("http://", "https://")) else ""
        commit_base = _web_base().rstrip("/")
        cards = []
        for tk in tickets + (["—"] if "—" in groups else []):
            cs = groups[tk]
            # EU-55/F12(a): emit a real Jira deep-link only when the app's backlog base_url is known,
            # and url-quote the key so the target can't break; otherwise degrade to escaped plain text.
            label = (f'<a href="{html.escape(jira_base)}/browse/{quote(tk)}" target=_blank '
                     f'rel=noopener>{html.escape(tk)}</a>' if (tk != "—" and jira_base)
                     else (html.escape(tk) if tk != "—" else "No ticket"))
            lis = "".join(
                '<li>'
                + (f'<a class=sha href="{html.escape(commit_base)}/commit/{quote(c["sha"])}" '
                   f'target=_blank rel=noopener>{html.escape(c["sha"])}</a>' if commit_base
                   else f'<span class=sha>{html.escape(c["sha"])}</span>')
                + f'<span>{html.escape(c["subject"])}</span></li>' for c in cs)
            cards.append(f'<div class=shcard><div class=tk>{label}<span class=n>· {len(cs)} commit'
                         f'{"s" if len(cs) != 1 else ""}</span></div><ul>{lis}</ul></div>')
        tix_summary = ", ".join(tickets) if tickets else "none tagged"
        body = (style + '<div class=shp>'
                f'<div class=shhead><h2>&#128640; Ship {html.escape(appq)} &rarr; production</h2>'
                f'<div class=meta>{ahead} commit{"s" if ahead != 1 else ""} · {len(tickets)} ticket'
                f'{"s" if len(tickets) != 1 else ""} · {html.escape(base)} &rarr; {html.escape(prot)}</div></div>'
                f'<div class=shtix>Tickets going live: {html.escape(tix_summary)}</div>'
                + "".join(cards)
                + '<div class=shbar>' + back + '</div></div>')
        return _wrap("Ship to production", body)

    @app.get("/chat")
    def chat_page():
        try:
            from . import decisions
            npend = len(decisions.load(cfg))
        except Exception:  # noqa: BLE001
            npend = 0
        # 'Discuss with the CTO' on /needs hands us a ready-made brief of the problem to send.
        prefill = html.escape((request.args.get("prefill") or "")[:800], quote=True)
        body = (_CHAT_STYLE + _chat_tabs("general", npend)
                + '<div class=chat><div id=cinner>' + _chat_inner(cfg) + '</div></div>'
                '<div class=composer><div id=chaterr class=chaterr role=alert aria-live=assertive></div>'
                '<form id=chatform method=post action=/api/chat>'
                f'<input type=text id=chatinput name=text autocomplete=off autofocus value="{prefill}" '
                'placeholder="Message the CTO…  (or reply  AUTO-1: your decision)"><button>Send</button></form></div>'
                '<script>window.scrollTo(0,document.body.scrollHeight);'
                # EU-305 — append/patch only what's new instead of wholesale-replacing #cinner
                # innerHTML every 5s (which jank-reset scroll position on every tick). The
                # .pending 'needs-your-call' cards are cheap and sit above the thread, so they're
                # just swapped in whole; the .thread is diffed on each bubble's data-seq (its
                # stable absolute index into the transcript, EU-305/cockpit_views._chat_inner) and
                # only bubbles newer than what's already on screen are appended — existing nodes
                # are never rewritten, so scroll position is never disturbed by the poll itself.
                # EU-307 — pulled the poll body into refreshChat(force) so the Enter-to-send handler
                # below can await the same patch-in-place refresh (force=true skips the "near
                # bottom" check, since the Commander's own just-sent message should always pull the
                # view down) instead of duplicating the #cinner reconciliation logic.
                'async function refreshChat(force){try{var r=await fetch("/api/chat-thread",{cache:"no-store"});'
                'if(!r.ok)return;'
                'var near=force||((window.innerHeight+window.scrollY)>=document.body.scrollHeight-140);'
                'var frag=document.createElement("div");frag.innerHTML=await r.text();'
                'var cinner=document.getElementById("cinner");'
                'var newPending=frag.querySelector(".pending"),oldPending=cinner.querySelector(".pending");'
                # EU-305 iter2 — only swap the .pending block when it actually changed, so text the
                # Commander is mid-typing into a pending card's reply input isn't wiped every tick.
                'if(newPending){if(oldPending){if(oldPending.outerHTML!==newPending.outerHTML)'
                'oldPending.outerHTML=newPending.outerHTML;}'
                'else cinner.insertBefore(newPending,cinner.firstChild);}'
                'else if(oldPending)oldPending.remove();'
                'var newThread=frag.querySelector(".thread"),oldThread=cinner.querySelector(".thread");'
                # EU-318 — capture the fetched window's max seq BEFORE the append loop below moves
                # those nodes OUT of frag. The old code scanned frag for `total` AFTER the move, so on
                # any burst tick total undercounted by the burst size (thread 10-29, 6 new → fetch
                # 16-35 → 30-35 moved out → frag max 29 → total 30, not 36), making the load-earlier
                # offset too small and a click within ~5s prepend duplicate bubbles.
                'var fetchedMax=-1;'
                'frag.querySelectorAll(".msg[data-seq]").forEach(function(m){'
                'var s=parseInt(m.dataset.seq,10);if(s>fetchedMax)fetchedMax=s;});'
                'if(newThread&&oldThread){'
                'var maxSeq=-1;'
                'oldThread.querySelectorAll(".msg[data-seq]").forEach(function(m){'
                'var s=parseInt(m.dataset.seq,10);if(s>maxSeq)maxSeq=s;});'
                'if(maxSeq===-1)oldThread.innerHTML="";'
                'newThread.querySelectorAll(".msg[data-seq]").forEach(function(m){'
                'if(parseInt(m.dataset.seq,10)>maxSeq)oldThread.appendChild(m);});}'
                # EU-305 iter2 — reconcile the EU-304 'load earlier' control against the LIVE window.
                # The poller appends new bubbles, so the on-screen window grows and the button's
                # original offset goes stale; a click at a stale offset would prepend duplicates.
                # Anchor it to the true boundary: offset = (server total) - (oldest on-screen seq),
                # where total = max fetched data-seq + 1. Insert the control when it newly appears,
                # remove it when the fetched fragment no longer has one, update its offset otherwise.
                'var newLE=frag.querySelector(".load-earlier"),oldLE=cinner.querySelector(".load-earlier");'
                'if(newLE){var minSeq=Infinity,total=fetchedMax+1;'
                'if(oldThread)oldThread.querySelectorAll(".msg[data-seq]").forEach(function(m){'
                'var s=parseInt(m.dataset.seq,10);if(s<minSeq)minSeq=s;});'
                'var off=(minSeq===Infinity)?parseInt(newLE.dataset.offset||"0",10):(total-minSeq);'
                'if(oldLE){oldLE.dataset.offset=off;oldLE.dataset.limit=newLE.dataset.limit;}'
                'else{newLE.dataset.offset=off;cinner.insertBefore(newLE,oldThread||null);}}'
                'else if(oldLE)oldLE.remove();'
                'if(near)window.scrollTo(0,document.body.scrollHeight);'
                '}catch(e){}}'
                'setInterval(function(){refreshChat(false);},5000);'
                # EU-307 — Telegram-style Enter-to-send: intercept the composer's submit so Enter
                # (or the Send button) posts via fetch instead of a native form submit (which would
                # full-page-reload /chat and drop scroll position / any in-flight poll). After the
                # POST resolves, clear the input, patch-in-place refresh #cinner via the same
                # refreshChat() the poller uses (so it degrades gracefully to the full-log render
                # if EU-286a windowing isn't present), then re-focus the input and stick the view to
                # the newest message (own message included).
                'var chatform=document.getElementById("chatform"),chatinput=document.getElementById("chatinput"),'
                'chaterr=document.getElementById("chaterr");'
                'if(chatform)chatform.addEventListener("submit",async function(ev){'
                'ev.preventDefault();'
                'var text=chatinput.value;if(!text.trim())return;'
                # POST the send; treat a network throw OR a non-ok HTTP status as failure. On
                # failure DO NOT clear the input — keep the Commander's typed text so it can be
                # retried — surface a visible inline error, and leave focus in the box so a
                # re-press of Enter re-sends. Only on success do we clear + refresh + stick down.
                'var ok=false;'
                'try{var r=await fetch("/api/chat",{method:"POST",body:new FormData(chatform)});ok=!!(r&&r.ok);}'
                'catch(e){ok=false;}'
                'if(!ok){chatinput.classList.add("cerr");'
                'if(chaterr){chaterr.textContent="Message not sent — check your connection and press Enter to retry.";'
                'chaterr.classList.add("on");}chatinput.focus();return;}'
                'chatinput.classList.remove("cerr");if(chaterr)chaterr.classList.remove("on");'
                'chatinput.value="";'
                'await refreshChat(true);'
                'chatinput.focus();'
                'window.scrollTo(0,document.body.scrollHeight);'
                '});'
                # EU-304 — 'load earlier': fetch the next-older batch (offset grows by its own
                # data-limit each click) and prepend it into the live .thread, no reload. An empty
                # response means there's nothing older left, so the button removes itself.
                'async function loadEarlierChat(btn){'
                'var off=parseInt(btn.dataset.offset||"0",10),lim=parseInt(btn.dataset.limit||"20",10);'
                'btn.disabled=true;var prev=btn.textContent;btn.textContent="Loading…";'
                'try{var r=await fetch("/api/chat-thread?offset="+off,{cache:"no-store"});'
                'var t=r.ok?await r.text():"";'
                'if(t.trim()){var th=document.querySelector("#cinner .thread"),f=document.createElement("div");'
                'f.innerHTML=t;'
                # EU-318 — dedup by data-seq before prepending. Belt-and-braces to the offset fix in
                # refreshChat: the 5s poll can still land between this click's fetch and its insert,
                # so drop any fetched bubble whose seq is already on screen rather than double-render.
                'var have={};th.querySelectorAll(".msg[data-seq]").forEach(function(m){'
                'have[m.dataset.seq]=1;});'
                'f.querySelectorAll(".msg[data-seq]").forEach(function(m){'
                'if(have[m.dataset.seq])m.remove();});'
                'while(f.lastChild){th.insertBefore(f.lastChild,th.firstChild);}'
                'btn.dataset.offset=off+lim;btn.disabled=false;btn.textContent=prev;}'
                'else{btn.remove();}}catch(e){btn.disabled=false;btn.textContent=prev;}}'
                '</script>')
        return _wrap("Chat with the CTO", body)

    @app.get("/group")
    def group_page():
        officer = (request.args.get("officer") or "").strip()
        # EU-287: a lightweight typing indicator (not a banner) — the officer(s) triage picked are
        # composing. `_state['grouping']` already tracks the in-flight window (set in group_api below).
        busy = (f'<div class=typing>&middot; {html.escape(officer) if officer else "an officer"} '
                'is weighing in&hellip;</div>' if _state.get("grouping") else "")
        aim = (f'<div class=aim>Consulting <b>{html.escape(officer)}</b> directly — only they answer. '
               '<a href="/group">ask the whole unit instead</a></div>') if officer else ""
        oin = f'<input type=hidden name=officer value="{html.escape(officer)}">' if officer else ""
        ph = (f"Ask {html.escape(officer)} something…" if officer
              else "Ask the unit / brainstorm with the officers…")
        body = (_CHAT_STYLE + _chat_tabs("group")
                + '<div class=chat>' + aim + busy + '<div id=ginner>' + _group_inner(cfg) + '</div></div>'
                '<div class=composer><div id=grouperr class=chaterr role=alert aria-live=assertive></div>'
                '<form id=groupform method=post action=/api/group>' + oin
                + f'<input type=text id=groupinput name=text autocomplete=off autofocus '
                f'placeholder="{ph}"><button>Send</button></form></div>'
                '<script>window.scrollTo(0,document.body.scrollHeight);'
                # EU-307 — same patch-in-place-friendly pattern as /chat's refreshChat(): pull the
                # poll body into a function so Enter-to-send can await the identical refresh,
                # instead of duplicating the #ginner reconciliation logic. Group has no windowing
                # (EU-304/305 only landed for /chat), so this stays a plain innerHTML swap — that's
                # the graceful-degradation path the ticket asks for regardless of EU-286a.
                'async function refreshGroup(force){try{var r=await fetch("/api/group-thread",{cache:"no-store"});'
                'if(!r.ok)return;'
                'var near=force||((window.innerHeight+window.scrollY)>=document.body.scrollHeight-140);'
                'document.getElementById("ginner").innerHTML=await r.text();'
                'if(near)window.scrollTo(0,document.body.scrollHeight);'
                '}catch(e){}}'
                'setInterval(function(){refreshGroup(false);},4000);'
                # EU-307 — Telegram-style Enter-to-send for the group composer, mirroring /chat's
                # chatform handler: intercept submit, POST via fetch, and only on success clear the
                # input, patch-refresh #ginner, refocus, and stick to the newest message. On failure
                # keep the typed text and surface an inline error so Enter retries the same send.
                'var groupform=document.getElementById("groupform"),groupinput=document.getElementById("groupinput"),'
                'grouperr=document.getElementById("grouperr");'
                'if(groupform)groupform.addEventListener("submit",async function(ev){'
                'ev.preventDefault();'
                'var text=groupinput.value;if(!text.trim())return;'
                'var ok=false,busy=false;'
                'try{var r=await fetch("/api/group",{method:"POST",body:new FormData(groupform)});'
                'ok=!!(r&&r.ok);busy=!!(r&&r.status===409);}'
                'catch(e){ok=false;}'
                # EU-319: a 409 means the officers are mid-reply and the send was REFUSED, not that
                # it failed — the text stays in the composer either way (that is the data-loss fix),
                # but backpressure gets its own copy and no red `cerr` ring, because retrying in a
                # few seconds is the correct move and nothing is broken.
                'if(!ok){'
                'if(busy){if(grouperr){grouperr.textContent="The unit is still replying — your message was NOT sent. Press Enter to try again in a moment.";'
                'grouperr.classList.add("on");}}'
                'else{groupinput.classList.add("cerr");'
                'if(grouperr){grouperr.textContent="Message not sent — check your connection and press Enter to retry.";'
                'grouperr.classList.add("on");}}'
                'groupinput.focus();return;}'
                'groupinput.classList.remove("cerr");if(grouperr)grouperr.classList.remove("on");'
                'groupinput.value="";'
                'await refreshGroup(true);'
                'groupinput.focus();'
                'window.scrollTo(0,document.body.scrollHeight);'
                '});'
                '</script>')
        return _wrap("Group room — the unit", body)

    @app.get("/api/group-thread")
    def group_thread_api():
        from flask import Response
        return Response(_group_inner(cfg), mimetype="text/html")

    @app.post("/api/group")
    def group_api():
        from urllib.parse import quote

        from flask import jsonify
        text = (request.form.get("text") or "").strip()
        officer = (request.form.get("officer") or "").strip() or None
        _dest = "/group?officer=" + quote(officer) if officer else "/group"
        if not text:
            return redirect(_dest)
        # EU-319 + EU-361: claim the room ATOMICALLY, in the request thread. Two problems used to
        # live in one line (``if text and not _state.get("grouping")``):
        #   * the flag was set inside _bg(), so two quick sends both passed the check (EU-361);
        #   * a send that lost the check fell straight through to the 302 below, so the EU-307
        #     Enter-to-send fetch followed the redirect, saw r.ok, cleared the composer — and the
        #     Commander's typed message was GONE with the UI reporting success (EU-319: real data
        #     loss on his own channel, found 2026-07-14 reviewing EU-307's landed code).
        # A 409 is the honest answer: nothing was queued. The composer keeps the text and says so.
        if not _claim_flag("grouping"):
            return jsonify({"queued": False, "busy": True,
                            "error": "the unit is still replying — nothing was sent"}), 409
        from . import council
        council._append_group(cfg, "you", text)   # echo instantly; the bg adds officer replies

        def _bg():
            try:
                asyncio.run(council.group_chat(cfg, text, officers=[officer] if officer else None,
                                               audit=audit, echo=False))
            except Exception as exc:  # noqa: BLE001
                _state["last_msg"] = f"group chat failed: {exc}"
            finally:
                _state["grouping"] = False
        threading.Thread(target=_bg, daemon=True).start()
        return redirect(_dest)

    @app.get("/api/chat-thread")
    def chat_thread_api():
        from flask import Response
        try:
            offset = max(int(request.args.get("offset") or 0), 0)
        except ValueError:
            offset = 0
        return Response(_chat_inner(cfg, offset=offset), mimetype="text/html")

    @app.post("/api/chat")
    def chat_api():
        text = (request.form.get("text") or "").strip()
        tid = (request.form.get("ticket") or "").strip()
        if text:
            msg = f"{tid}: {text}" if tid else text

            # EU-307 — echo the Commander's freeform message into the chat transcript
            # SYNCHRONOUSLY, before the (double-threaded) CTO reply path runs, so the
            # client's post-send refreshChat() immediately sees the own message and can
            # stick the view to it — closing the 'own message included' race deterministically
            # rather than hoping the bg reply lands first. respond_to_commander() dedups its
            # own leading append against this echo (council._last_chat_line). Only freeform
            # composer messages are chat bubbles: a pending-decision reply (tid set) resolves
            # via handle_reply and a '/command' is dispatched — neither renders as a Q bubble,
            # so don't echo those.
            if not tid and not text.startswith("/"):
                from . import council
                council.append_chat(cfg, "Q", msg)

            def _bg():
                try:
                    from . import decisions
                    decisions.route_message(cfg, audit, msg)
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"chat failed: {exc}"
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/chat")

    @app.post("/api/answer")
    def answer_api():
        """Ship the Commander's answer to a parked ticket straight from the Needs-you page.

        The reply is classified (orchestrator/intent.py) and routed to the matching action, so a
        directive is *executed* instead of being baked into the parked ticket's re-run (EU-82):

          'file_ticket'   — File a NEW backlog ticket from the directive, linked to the parked
                            one; do NOT re-run the original ticket.
          'close'         — Transition the parked ticket to Done via the backlog adapter; do NOT
                            re-run.
          'defer'         — Dismiss the row only (done below); do NOT re-run.
          'clarification' — Existing behaviour: resolve the pending decision and re-run the ticket
                            with the answer baked into its spec (decisions.handle_reply). If there's
                            no pending decision on file, record the answer as a ticket comment (the
                            next build reads ALL comments) and unblock so autopilot retries it.

        A Commander directive is authoritative — it is NEVER handed to out-of-scope triage
        (_route_out_of_scope).

        The Needs-you row is cleared only when the chosen action actually succeeds: 'file_ticket'
        clears it once create_task returns a key (else the row stays and the real outcome is
        reported, not a fake "Logged"), and 'close' clears it once the transition succeeds.
        'file_ticket'/'close' make a single backlog call synchronously so they can report that real
        outcome; the 'clarification'/fallback branch (handle_reply + comment/unblock) self-backgrounds
        in a thread so the redirect stays snappy.
        """
        tid = (request.form.get("ticket") or "").strip()
        app_name = (request.form.get("app") or "").strip()
        ans = (request.form.get("text") or "").strip()
        if not (tid and ans):
            return redirect("/needs")

        from .intent import classify_intent
        intent = classify_intent(ans)
        appcfg = cfg.app(app_name) if app_name else None
        supports_backlog = bool(appcfg) and getattr(appcfg, "backlog_backend", "none") != "none"

        def _dismiss_row() -> None:
            # Fast local write so the row visibly disappears on redirect; if the ticket
            # re-escalates later a fresh row reappears. Called ONLY once the action succeeded.
            try:
                D.dismiss(cfg.audit_path, tid)
            except Exception:  # noqa: BLE001
                pass

        if intent == "file_ticket":
            # Commander wants a NEW backlog ticket — file it, linked to the parked one, and stop.
            # Never re-run the parked ticket and never call _route_out_of_scope. Clear the row ONLY
            # once the ticket is actually filed; on a none/failed backend keep the row and report
            # the real outcome (never a fake "Logged"). (EU-82 iter-3.)
            key = None
            err = ""
            if supports_backlog:
                try:
                    from .backlog.base import make_backlog
                    summary = f"[Commander directive from {tid}] {ans[:200]}"
                    description = (
                        f"Filed from a Commander directive on the Needs-you reply to {tid}.\n\n"
                        f"Directive: {ans}\n\nRelates to {tid}."
                    )
                    key = make_backlog(appcfg).create_task(
                        summary, description, labels=["commander-directive"])
                except Exception as exc:  # noqa: BLE001 - filing must never crash the cockpit
                    err = str(exc)
            if key:
                _dismiss_row()
                try:
                    from . import notify as _notify
                    _notify.send(f"✓ Commander directive filed as {key} (from {tid})")
                except Exception:  # noqa: BLE001
                    pass
                _state["last_msg"] = (
                    f"✓ Filed {key} from your directive (linked to {tid}) — cleared from Needs-you.")
            elif not supports_backlog:
                _state["last_msg"] = (
                    f"⚠ Couldn't file a ticket for {tid}: no backlog is configured for "
                    f"{app_name or 'this app'}. Left it in Needs-you.")
            else:
                _state["last_msg"] = (
                    f"⚠ Couldn't file a ticket from your directive — {tid} kept in Needs-you."
                    + (f" ({err})" if err else " (the backlog rejected the request)."))
            return redirect("/needs")

        if intent == "close":
            # Commander wants the parked ticket closed — transition it to Done; do NOT re-run.
            # Clear the row only once the transition actually succeeds (EU-82 iter-3).
            ok = False
            if supports_backlog:
                try:
                    from .backlog.base import make_backlog
                    from .contracts import Ticket
                    bl = make_backlog(appcfg)
                    t_obj = Ticket(id=tid, key=tid, summary=tid, description="", app=app_name)
                    bl.set_status(t_obj, "Done")
                    bl.add_comment(t_obj, f"Closed by the Commander: {ans}")
                    ok = True
                except Exception:  # noqa: BLE001 - close must never crash the cockpit
                    ok = False
            if ok:
                _dismiss_row()
                _state["last_msg"] = f"✓ Closed {tid} per your reply — cleared from Needs-you."
            elif not supports_backlog:
                _state["last_msg"] = (
                    f"⚠ Couldn't close {tid}: no backlog is configured for "
                    f"{app_name or 'this app'}. Left it in Needs-you.")
            else:
                _state["last_msg"] = f"⚠ Couldn't close {tid} (backlog error) — left it in Needs-you."
            return redirect("/needs")

        if intent == "defer":
            # Commander defers — leave the ticket parked, don't re-run, never call
            # _route_out_of_scope. Dismissing the row just stops it nagging; it re-appears if the
            # ticket re-escalates later.
            _dismiss_row()
            _state["last_msg"] = f"✓ Deferred {tid} — cleared from Needs-you (left parked)."
            return redirect("/needs")

        # intent == 'clarification': resolve the pending decision and re-run the original ticket with
        # the answer baked in; if there's no pending decision on file, post the answer as a comment
        # and unblock for an autopilot retry. handle_reply (a Jira comment) and the fallback
        # comment/unblock are synchronous network calls, so run them in a background thread — the row
        # is dismissed and the redirect returns immediately.
        _dismiss_row()

        def _bg_clarify():
            try:
                from . import decisions
                if not decisions.handle_reply(cfg, audit, f"{tid}: {ans}"):
                    # No pending decision on file: persist the answer ON the ticket + unblock.
                    try:
                        if appcfg and getattr(appcfg, "backlog_backend", "") == "jira":
                            from .backlog.base import make_backlog
                            from .contracts import Ticket
                            bl = make_backlog(appcfg)
                            t_obj = Ticket(id=tid, key=tid, summary=tid, description="", app=app_name)
                            bl.add_comment(t_obj, ans)
                            bl.set_status(t_obj, "To Do")
                    except Exception:  # noqa: BLE001 - a comment failure must not block the unblock
                        pass
                    try:
                        from . import autopilot as _ap
                        _ap.unblock(cfg, tid)
                    except Exception as exc:  # noqa: BLE001
                        # 2026-07-19 stabilization: this swallow let the banner claim "the unit
                        # is re-running it" while the ticket stayed parked — the answer was
                        # silently lost. Surface it so the Commander can act.
                        _state["last_msg"] = (f"⚠ answer to {tid} was saved as a comment but the "
                                              f"unblock FAILED ({exc}) — the ticket is still "
                                              "parked; /unblock it by hand.")
                        try:
                            audit.record("clarify_unblock_failed", ticket_id=tid,
                                         error=str(exc)[:200])
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    # handle_reply succeeded - ticket is being re-run, transition to To Do
                    try:
                        if appcfg and getattr(appcfg, "backlog_backend", "") == "jira":
                            from .backlog.base import make_backlog
                            from .contracts import Ticket
                            bl = make_backlog(appcfg)
                            t_obj = Ticket(id=tid, key=tid, summary=tid, description="", app=app_name)
                            bl.set_status(t_obj, "To Do")
                    except Exception:  # noqa: BLE001 - status transition must not block the re-run
                        pass
            except Exception as exc:  # noqa: BLE001 - the re-run must never break the cockpit
                _state["last_msg"] = f"answer to {tid} failed: {exc}"

        threading.Thread(target=_bg_clarify, daemon=True).start()
        _state["last_msg"] = (f"✓ Answer sent to {tid} — cleared from Needs-you; ticket moved to To Do "
                              "and the unit is re-running it with your decision.")
        return redirect("/needs")

    @app.post("/needs/resolve")
    def needs_resolve():
        """Dismiss a pending decision without re-running the ticket.

        Pops the entry from pending_decisions.json (same as resolving a Telegram reply, but
        without kicking off a build). Use when the question is moot or the Commander already
        handled it another way. The item disappears from Needs-you immediately; if the ticket
        re-escalates later it will re-appear as a fresh row."""
        from . import decisions
        tid = (request.form.get("ticket") or "").strip()
        if tid:
            try:
                # comment=False: the Commander chose to dismiss, not answer — nothing to echo back.
                decisions.resolve(cfg, "dismissed by Commander", ticket_id=tid, comment=False)
            except Exception:  # noqa: BLE001 - never block the redirect
                pass
            _state["last_msg"] = f"✓ Decision for {tid} dismissed — removed from Needs you."
        return redirect("/needs")

    @app.get("/report")
    def report_form():
        apps = "".join(f"<option>{html.escape(a.name)}</option>" for a in cfg.apps)
        form = (
            "<form method=post action=/api/report enctype=multipart/form-data>"
            f"<p>App: <select name=app>{apps}</select></p>"
            "<p>What's wrong on DEV — where, what you saw, expected vs actual:<br>"
            "<textarea name=text rows=6 cols=72 placeholder='On /leads the long column gets "
            "cut off; expected it to scroll to the last card…'></textarea></p>"
            "<p>Screenshot (optional): <input type=file name=screenshot accept='image/*'></p>"
            "<p><label><input type=checkbox name=dryrun> dry run (build only — no merge)</label></p>"
            "<p><button>Send to the CTO</button></p></form>")
        return _wrap("Report a problem", form)

    @app.post("/api/report")
    def report_api():
        app_name = request.form.get("app") or (cfg.apps[0].name if cfg.apps else "")
        # EU-64: claim THIS project's run slot (per-project TOCTOU guard + the cross-project parallel
        # cap). A second run on the SAME project is refused; other projects are unaffected.
        # EU-361: the stop_event is minted HERE, not in _bg — see _claim_cockpit_run's docstring.
        ev = threading.Event()
        st = _claim_cockpit_run(app_name, stop_event=ev)
        if st is None:
            return redirect("/")
        if not health.summary(cfg)["healthy"]:
            release_run(app_name or None)
            st["last_msg"] = "blocked — fix the health problems first (see the banner)"
            return redirect("/")
        text = (request.form.get("text") or "").strip()
        import copy
        rcfg = copy.copy(cfg)        # per-run config — never mutate the shared cfg
        rcfg.dry_run = request.form.get("dryrun") == "on"   # default: live (build + merge to DEV)
        st["dry_run"] = rcfg.dry_run
        _berr = _resolve_run_backend(rcfg, app_name)   # EU-190/EU-223: this app's (or global) backend
        if _berr:
            release_run(app_name or None)
            st["last_msg"] = _berr
            return redirect("/")
        desc = _bug_desc(cfg, text, request.files.get("screenshot"))
        title = _bug_title(text)
        try:
            worklist = intake.from_text(rcfg, app_name, title, [], description=desc)
        except Exception as exc:  # noqa: BLE001
            release_run(app_name or None)
            st["dry_run"] = None
            st["last_msg"] = f"could not start: {exc}"
            return redirect("/")

        def _bg():
            # EU-361: ``ev`` comes from _claim_cockpit_run (request thread) — see the run_api twin.
            st["last_msg"] = ""
            errored = False
            reports = []
            # EU-361: report_api's run was the one cockpit-initiated run_loop with NO EU-175
            # boundary — run_api / run_selected_api both bracket theirs. Without the pair, a bug
            # reported through '/report' opened a session that forensics could never close, leaving
            # an unpaired boundary and a phantom "Working" card (the same ghost-session class EU-175
            # closed everywhere else).
            if audit is not None:
                audit.record("run_start", mode=("DRY-RUN" if rcfg.dry_run else "LIVE"),
                             tickets=len(worklist or []))
            try:
                reports = asyncio.run(run_loop(rcfg, worklist, audit, stop_event=ev))
            except Exception as exc:  # noqa: BLE001
                errored = True
                st["last_msg"] = str(exc)
            finally:
                # EU-361: run_end fires from the finally — same as its run_api twin — so a worker
                # that dies on an exception still closes its own boundary.
                if audit is not None:
                    audit.record("run_end", tickets=len(reports or []))
                release_run(app_name or None)   # clears active / run_started / stop_event for this app
                st["dry_run"] = None            # clear the dry/live flag so the cockpit shows no stale tag
                # EU-104: on a CLEAN terminal outcome, clear the transient 'Working / stopping…'
                # control-bar note so a finished run never lingers as 'Working'. Guarded by
                # ``errored`` so a real run error (set just above) stays visible — release_run no
                # longer clears last_msg, so the operator still sees why a failed run failed.
                if not errored:
                    st["last_msg"] = ""
                # EU-191: AFTER the fast cleanup (so a slow usage probe never delays clearing the
                # control-bar note — the race that broke eu104), raise the unit-wide (None-keyed)
                # plan-limit banner + its Continue-on-GLM offer if this run tripped the Opus/Claude cap.
                # The autopilot governor only sets this for its own loop, so without it the banner never
                # appears for a manual cockpit run. _state['last_run'] holds the ticket for the Continue button.
                try:
                    from . import usage as _u191
                    _pl = _u191.plan_limit_hit(cfg, force=True)
                    if _pl.get("hit"):
                        _reset = next((l.get("resets_at") for l in _pl.get("over_limits", [])
                                       if l.get("resets_at")), None)
                        cockpit_state.set_plan_limit_hit(None, hit=True, reset_at=_reset)
                except Exception:  # noqa: BLE001 — detection must never break run cleanup
                    pass
        threading.Thread(target=_bg, daemon=True).start()
        return redirect("/")

    return app


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8787) -> None:
    try:
        # EU-361: hand the app the port we are about to bind — the EU-254 Host guard validates
        # against it, so `general serve --port N` must not leave the guard pinned to 8787.
        app = create_app(cfg, port=port)
    except ImportError:
        raise SystemExit("Flask is required for the control panel. Run: pip install -r requirements.txt")
    # Stale-process forensics (2026-07-09): the resident serve process kept running PRE-EU-201 code
    # for 11 hours after the unit landed EU-201 on itself, silently stranding split fragments. One
    # audit line per process start (git SHA + pid) makes "which code was this run actually on?" a
    # single grep instead of log archaeology.
    try:
        import os as _os, subprocess as _sp
        _sha = _sp.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                       text=True, timeout=5).stdout.strip()
        AuditLog(cfg.audit_path).record("process_start", role="serve", sha=_sha, pid=_os.getpid())
    except Exception:  # noqa: BLE001 — forensics must never block the cockpit
        pass
    # Quiet the per-request access log (the dashboard polls GET /api/board every 5s). Without this
    # the terminal is flooded and the unit's real progress is lost in the noise.
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    # Mirror the unit's progress output into the dashboard's Live feed.
    import sys
    if not isinstance(sys.stdout, _Tee):
        sys.stdout = _Tee(sys.stdout)
    # Two-way decisions: watch Telegram for replies that resume paused tickets. EU-185 (Wave 0):
    # only the ELECTED poller host polls — a second poller on the same bot token splits/loses the
    # Commander's messages (getUpdates is single-consumer). A non-poller host runs cockpit-only.
    # EU-257: route through ensure_poll_loop, the process-level singleton — serve's startup is one
    # of TWO spawn sites (the other is autopilot()/each cockpit drain), and without the singleton
    # each per-app drain Start on this host would add its own immortal poller thread.
    from . import decisions, notify
    _poll, _why = decisions.should_poll_telegram(cfg)
    if _poll:
        decisions.ensure_poll_loop(cfg, AuditLog(cfg.audit_path))
        print("  decision listener: ON — reply to ❓ messages in Telegram to resume tickets")
    elif notify.configured():
        print(f"  decision listener: OFF — {_why} (cockpit-only on this host)")
    # EU-405 AC2: the serve-level self-restart watcher. Consumes the EU-387 flag at an idle boundary
    # EVEN WHEN NO DRAIN IS ARMED — closing the manual-cockpit-run gap where a self-repo land announced
    # "restarting automatically once idle" and then never did (the stale-process class that ran old
    # code 11h). Gated on active_run_count()==0, so it can never kill a live build; the drain-cycle
    # call remains the fast path. Singleton, so a re-entry never stacks a second thread.
    from . import autopilot as _ap
    _ap.ensure_self_restart_watcher(cfg, AuditLog(cfg.audit_path))
    print(f"\u2b22 Squad HQ: http://{host}:{port}   (Ctrl-C to stop)")
    print("  (the terminal shows the unit's progress only — dashboard polling is hidden)\n")
    # threaded: the SSE stream holds a long-lived request — without this it would block the cockpit.
    app.run(host=host, port=port, threaded=True)
