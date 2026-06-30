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

from . import dashboard as D
from . import health
from . import intake
from . import memory
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


def _audit_ship(audit, app_name: str, r: dict) -> None:
    """Record an app DEV→MAIN ship in the audit so it shows up in the logs/history — until now a ship's
    only trace was the in-memory cockpit banner, so 'check the logs' came up empty. Best-effort."""
    try:
        audit.record("ship", app=app_name, base=r.get("base"), prot=r.get("prot"),
                     ahead=int(r.get("ahead_before", 0) or 0), ok=bool(r.get("ok")),
                     error=(r.get("error") or "")[:300])
    except Exception:  # noqa: BLE001 - logging a ship must never break the ship
        pass


def _audit_promote(audit, r: dict) -> None:
    """Record a unit dev→main promote ('Update unit') in the audit, same rationale as _audit_ship."""
    try:
        audit.record("promote", target="unit", ahead=int(r.get("ahead_before", 0) or 0),
                     ok=bool(r.get("ok")), error=(r.get("error") or "")[:300])
    except Exception:  # noqa: BLE001
        pass


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


def create_app(cfg: Config):
    from flask import Flask, Response, redirect, request
    app = Flask(__name__)
    audit = AuditLog(cfg.audit_path)
    from . import usage
    usage.configure(cfg.audit_path)   # the cockpit process meters token burn too

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

    def _scope(raw) -> str:
        """Resolve a request's project param to ONE concrete project for this session's active tab.

        A concrete, configured project opens/focuses its tab and becomes active. The retired
        ``*``/empty selector is NOT honoured as "all projects" any more — it falls back to whatever
        tab is already active, else the first configured app. Returns "" only when no apps exist.
        """
        ws = cockpit_state.workspace_for(_session_id())
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

    def _claim_cockpit_run(app_name: str):
        """Claim ``app_name``'s run slot for a manual cockpit run (EU-64: per-project).

        Honours BOTH guards: the per-project one (a second run on the SAME project is refused, other
        projects unaffected) AND the still-unit-wide one that autopilot / Telegram-resume hold on the
        legacy ``_state`` (until those migrate, a manual project run must not overlap them). Returns
        the per-app run-state dict on success — the caller owns the run and MUST ``release_run`` it —
        or None when refused, with ``last_msg`` already set on the right state for the banner."""
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
        if not claim_run(key):
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
        bar = (_result_banner(_state)
               + _control_bar(cfg, appq, h["healthy"],
                              is_mac=_platform.system() == "Darwin"))
        # EU-64: render THIS tab's project state so each project's board/live-feed is independent.
        # (The one-shot result banner stays on the unit-wide ``_state`` — ship/promote/patrol are
        # unit-level actions, not per-project runs.)
        return warroom.render_page(cfg, appq, _view_state(appq), bar, h)

    @app.get("/api/health")
    def health_api():
        import platform
        from flask import jsonify
        # EU-106: include is_mac so the UI layer can decide whether to show "Open logs" buttons.
        return jsonify({**health.summary(cfg), "is_mac": platform.system() == "Darwin"})

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
        path_param = (request.args.get("path") or "").strip()
        if not path_param:
            return Response("'path' query parameter is required.", status=400,
                            mimetype="text/plain")
        from . import run_logger as _rl
        root = _rl.log_root(cfg)
        try:
            requested = Path(path_param).resolve()
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
            if ev is not None:
                ev.set()
            # EU-120: for external daemons (launchd keepalive or detached terminal), durably stop
            # the launchd service after signaling the stop_event so the current ticket finishes.
            # This must happen AFTER ev.set() so graceful shutdown happens first.
            if ap.daemon_is_external():
                stopped = ap._stop_launchd_daemon()
                if stopped:
                    # Success — the daemon will finish its in-flight work and exit
                    pass
                else:
                    # launchctl failed — the stop_event is still set, so graceful shutdown proceeds,
                    # but KeepAlive may respawn it. Log this but don't block the redirect.
                    pass
            return redirect(_redir)

        if action == "stop" and ap_on:
            # Immediate stop for THIS project: flip its autopilot OFF now (the in-flight build still
            # finishes in the background) and signal only this app's stop_event.
            ev = st.get("stop_event")
            if ev is not None:
                ev.set()
            st["autopilot_on"] = False
            # EU-120: for external daemons (launchd keepalive or detached terminal), durably stop
            # the launchd service. Without launchctl bootout, clicking 'Stop' on an external daemon
            # doesn't actually stop it—the KeepAlive respawn makes the button a no-op for external runs.
            if ap.daemon_is_external():
                stopped = ap._stop_launchd_daemon()
                if stopped:
                    # Success — the daemon will finish its in-flight work and exit
                    pass
                else:
                    # launchctl failed — the stop_event is still set, so graceful shutdown proceeds,
                    # but KeepAlive may respawn it. Log this but don't block the redirect.
                    pass
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

    @app.get("/tasks")
    def tasks_page():
        flt = (request.args.get("filter") or "").strip()
        # 'parked' scopes to the auto-skipped blocked set, which lives outside the task log.
        blocked = warroom._load_blocked(cfg) if flt.lower() == "parked" else None
        try:
            from . import needs as _needs_mod
            _needs_cnt = _needs_mod.count(cfg)
        except Exception:  # noqa: BLE001
            _needs_cnt = None
        page = D.render_html(D.load_tasks(cfg.audit_path), show_cost=_charged(),
                             dismissed=D.load_dismissed(cfg.audit_path),
                             active_filter=flt, blocked=blocked, needs_count=_needs_cnt)
        # This board view is reached from the cockpit's Reports menu, so it needs a way back like
        # every other sub-page (it renders via D.render_html, which bypasses _wrap's "← cockpit").
        # Carry the active tab's concrete project so 'back' returns to it (EU-63: no 'All projects').
        _appq = _board_project(request.args.get("app"))
        _home = f"/?app={html.escape(_appq)}" if _appq else "/"
        back = (f"<p style='margin:14px 30px 4px'><a href='{_home}' style='color:#6aa9ff'>"
                "&larr; cockpit</a></p>")
        return page.replace("</header>", "</header>" + back + _control_bar(cfg, _board_project(request.args.get("app"))), 1)

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
            # TODO: Create Jira ticket for critical issues
            # For now, just note it in the audit log
            try:
                audit.record("security_reply_ticket_created", ticket_id=ticket_id,
                            iteration=iteration, response=response,
                            note="Jira ticket creation not yet implemented")
            except Exception as e:
                print(f"Failed to record ticket creation: {e}")

        # Redirect back to the cockpit (maintains the app selection)
        app = request.form.get("app") or request.args.get("app") or "*"
        return redirect(f"/?app={quote(app)}")

    @app.get("/tickets")
    def tickets_page():
        # EU-63: one concrete project per tab — the retired "All projects"/`*` grouped view is gone, so
        # this page always lists exactly one app's backlog. ``_scope`` resolves (and focuses) that tab.
        appq = _scope(request.args.get("app"))
        name = appq or None
        style = ("<style>.tlist{margin:10px 0;border:1px solid #232936;border-radius:10px;overflow:hidden}"
                 ".trow{display:flex;gap:12px;align-items:flex-start;padding:11px 14px;border-top:1px solid #1a1f29;cursor:pointer}"
                 ".trow:first-child{border-top:0}.trow:hover{background:#151a23}"
                 ".trow input{margin-top:3px}.tkey{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#6aa9ff;white-space:nowrap}"
                 ".tsum{color:#e8eaed}.trun{display:flex;gap:14px;align-items:center;margin-top:14px;flex-wrap:wrap}"
                 ".trun button{background:#2b5cff;border:0;color:#fff;border-radius:8px;padding:9px 18px;font-weight:650;cursor:pointer}"
                 ".hint{color:#8a909c;font-size:13px}"
                 ".tapp{margin-left:auto;font-size:11px;color:#8a909c;background:#161b24;border:1px solid #232936;border-radius:999px;padding:1px 9px;white-space:nowrap}"
                 ".tgrp{font-size:12px;color:#c4c9d2;font-weight:700;margin:16px 0 6px}"
                 ".tall{background:#10141b;font-weight:650}</style>")
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
            return ('<form method=post action=/api/run-selected '
                    'onsubmit="return this.dryrun.checked||confirm(\'Build and merge to DEV. Continue?\')">'
                    f'<input type=hidden name=app value="{html.escape(target_app)}">'
                    f'<div class=tlist>{_select_all}{rows_html}</div>'
                    '<div class=trun>'
                    '<label><input type=checkbox name=dryrun> dry run (build only — no merge)</label>'
                    f'<select name=effort><option value="">effort: auto-size</option>{effort}</select>'
                    f'<button>&#9654; {html.escape(btn_label)}</button>'
                    '<span class=hint>default builds + merges to DEV — tick "dry run" to build only</span>'
                    '</div></form>')

        body = (style
                + f'<p class=hint>{len(items)} ticket(s) assigned to you, in board-priority order. '
                  "Tick the ones to develop, then Run.</p>"
                + _run_form(appq, _checkbox_rows([t for _, t in items]), "Develop selected"))
        return _wrap(f"Choose tickets — {html.escape(appq)}", body)

    @app.post("/api/run-selected")
    def run_selected_api():
        app_name = _scope(request.form.get("app"))   # the run targets exactly one concrete project
        keys = request.form.getlist("ticket")
        if not keys:
            return redirect(f"/tickets?app={app_name}")
        # EU-64: claim THIS project's run slot (per-project TOCTOU guard + the cross-project parallel
        # cap). A second run on the SAME project is refused; other projects are unaffected.
        st = _claim_cockpit_run(app_name)
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
        try:
            worklist = intake.from_tickets(rcfg, app_name, keys)
        except Exception as exc:  # noqa: BLE001
            release_run(app_name or None)
            st["dry_run"] = None
            st["last_msg"] = f"could not start: {exc}"
            return redirect("/")

        # EU-106: open a per-run log file so every Tee-captured stdout line lands on disk.
        # Derive the label from the first ticket's id; fall back gracefully so test stubs never crash.
        try:
            _ticket_label = worklist[0][1].id if worklist else (keys[0] if keys else "run")
        except (IndexError, AttributeError, TypeError):
            _ticket_label = keys[0] if keys else "run"
        try:
            from . import run_logger as _rl
            _rl.open_run_log(rcfg, app_name, _ticket_label)
        except Exception:  # noqa: BLE001 — log setup must never block a run
            pass

        def _bg():
            st["last_msg"] = ""
            ev = threading.Event()
            st["stop_event"] = ev
            errored = False
            try:
                asyncio.run(run_loop(rcfg, worklist, audit, stop_event=ev))
            except Exception as exc:  # noqa: BLE001
                errored = True
                st["last_msg"] = str(exc)
            finally:
                # EU-106: close the run log before releasing the run slot.
                try:
                    from . import run_logger as _rl
                    _rl.close_run_log(app_name or None)
                except Exception:  # noqa: BLE001
                    pass
                release_run(app_name or None)   # clears active / run_started / stop_event for this app
                st["dry_run"] = None            # clear the dry/live flag so the cockpit shows no stale tag
                # EU-104: on a CLEAN terminal outcome, clear the transient 'Working / stopping…'
                # control-bar note so a finished run never lingers as 'Working'. Guarded by
                # ``errored`` so a real run error (set just above) stays visible — release_run no
                # longer clears last_msg, so the operator still sees why a failed run failed.
                if not errored:
                    st["last_msg"] = ""
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
        st = _claim_cockpit_run(app_name)
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

        # EU-106: open a per-run log file so every Tee-captured stdout line lands on disk.
        # Derive the label from the first ticket's id; fall back gracefully to the run kind so test
        # stubs (which may return plain strings as worklist items) never crash the request.
        try:
            _ticket_label = worklist[0][1].id if worklist else kind
        except (IndexError, AttributeError, TypeError):
            _ticket_label = kind
        try:
            from . import run_logger as _rl
            _rl.open_run_log(rcfg, app_name, _ticket_label)
        except Exception:  # noqa: BLE001 — log setup must never block a run
            pass

        def _bg():
            st["last_msg"] = ""
            ev = threading.Event()
            st["stop_event"] = ev
            errored = False
            try:
                asyncio.run(run_loop(rcfg, worklist, audit, stop_event=ev))
            except Exception as exc:  # noqa: BLE001
                errored = True
                st["last_msg"] = str(exc)
            finally:
                # EU-106: close the run log before releasing the run slot.
                try:
                    from . import run_logger as _rl
                    _rl.close_run_log(app_name or None)
                except Exception:  # noqa: BLE001
                    pass
                release_run(app_name or None)   # clears active / run_started / stop_event for this app
                st["dry_run"] = None            # clear the dry/live flag so the cockpit shows no stale tag
                # EU-104: on a CLEAN terminal outcome, clear the transient 'Working / stopping…'
                # control-bar note so a finished run never lingers as 'Working'. Guarded by
                # ``errored`` so a real run error (set just above) stays visible — release_run no
                # longer clears last_msg, so the operator still sees why a failed run failed.
                if not errored:
                    st["last_msg"] = ""
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
        if not _state.get("standuping"):
            def _bg():
                _state["standuping"] = True
                try:
                    from . import council
                    asyncio.run(council.hold_standup(cfg, audit=audit))
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"standup failed: {exc}"
                finally:
                    _state["standuping"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/standup")

    @app.post("/api/drill")
    def drill_api():
        if not _state.get("drilling"):
            def _bg():
                _state["drilling"] = True
                try:
                    from . import drillmaster
                    rep = asyncio.run(drillmaster.drill(cfg))
                    Path(cfg.audit_path).with_name("drill-report.md").write_text(rep, encoding="utf-8")
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"drill failed: {exc}"
                finally:
                    _state["drilling"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/drill")

    @app.get("/drill")
    def drill_page():
        rep = Path(cfg.audit_path).with_name("drill-report.md")
        if _state.get("drilling"):
            body = _working("Engineering Coach is reviewing the unit's record and proposing officer upgrades…")
        else:
            act = _actbar(_actbtn("/api/drill", "&#127894; Run drill"))
            if rep.exists():
                body = act + "<pre class=rep>" + html.escape(rep.read_text(encoding="utf-8")) + "</pre>"
            else:
                body = act + ("<p style='color:#8a909c'>No drill yet. Run one — the Engineering Coach reviews "
                              "the unit's record and proposes officer upgrades, which you Approve in the "
                              "<a href='/approvals'>Approvals</a> inbox.</p>")
        return _wrap("Engineering Coach report", body)

    @app.post("/api/council")
    def council_api():
        if not _state.get("councilling"):
            def _bg():
                _state["councilling"] = True
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
        if not _state.get("scribing"):
            def _bg():
                _state["scribing"] = True
                try:
                    msg = asyncio.run(memory.scribe(cfg))
                    _state["last_msg"] = "✓ " + (str(msg).strip() or "Unit Memory updated by the Technical Writer.")
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"scribe failed: {exc}"
                finally:
                    _state["scribing"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/memory")

    @app.post("/api/consolidate")
    def consolidate_api():
        from . import consolidate
        try:
            r = consolidate.run(cfg)
            _state["last_msg"] = "✓ " + (str(r).strip() if r else "Consolidated Unit Memory — deduped/pruned the log and folded in recurring lessons.")
        except Exception as exc:  # noqa: BLE001
            _state["last_msg"] = f"consolidate failed: {exc}"
        return redirect("/memory")

    @app.get("/memory")
    def memory_page():
        memory.ensure()
        from . import consolidate
        top = (_working("The Technical Writer is folding recent lessons into Unit Memory…")
               if _state.get("scribing") else "")
        act = ("" if _state.get("scribing")
               else _actbar(_actbtn("/api/scribe", "&#128221; Update memory"),
                            _actbtn("/api/consolidate", "&#129529; Consolidate",
                                    confirm="Dedup/prune the Lessons log and fold in any recurring "
                                            "Reviewer-rejection lessons?")))
        # Surface what the Reviewer keeps rejecting — the unit's own recurring mistakes.
        pat_html = ""
        try:
            pats = consolidate.rejection_patterns(cfg, min_count=2)
        except Exception:  # noqa: BLE001
            pats = []
        if pats:
            rows = "".join(
                f"<div class=lrow><div class=lhead><b>{html.escape(p['label'])}</b>"
                f"<span class=ln>×{p['count']} · {html.escape(', '.join(p['tickets'][:5]))}</span></div>"
                f"<div class=lact>&#8594; {html.escape(p['action'])}</div></div>" for p in pats)
            pat_html = (
                "<style>.lrej{margin:4px 0 18px}.lrow{background:#161122;border:1px solid #3a2b4a;"
                "border-radius:10px;padding:11px 14px;margin-bottom:9px}.lhead{display:flex;"
                "justify-content:space-between;gap:10px;align-items:baseline}.lhead b{color:#e9ecf1;font-size:13.5px}"
                ".ln{color:#b59ad6;font-size:12px;font-family:ui-monospace,Menlo,monospace}"
                ".lact{color:#9aa3b2;font-size:12.5px;margin-top:5px}</style>"
                "<h3 style='margin:14px 0 8px;font-size:14px;color:#c4c9d2'>&#9888; Reviewer keeps "
                "rejecting these</h3><p style='color:#8a929f;font-size:12.5px;margin:0 0 10px'>Folded "
                "into the log on Consolidate. Each is a drill candidate.</p>"
                f"<div class=lrej>{rows}</div>")
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
        body = (banner + act + top + pat_html
                + "<h3 style='margin:14px 0 8px;font-size:14px;color:#c4c9d2'>Doctrine</h3>"
                + "<pre class=rep>" + html.escape(memory.load() or "(no Unit Memory yet)") + "</pre>"
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
        if not _state.get("meeting"):
            officers = request.form.getlist("officer") or None

            def _bg():
                _state["meeting"] = True
                try:
                    from . import council
                    asyncio.run(council.hold_meeting(cfg, topic, officers=officers, audit=audit))
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"meeting failed: {exc}"
                finally:
                    _state["meeting"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/council")

    @app.post("/api/ship-review")
    def ship_review_api():
        # Ship-review is per-PRODUCT and per-tab now (EU-63): the active tab's project, falling back to
        # the first shippable product when no tab resolves (never the retired "*"/all-projects).
        appq = _scope(request.form.get("app"))
        app_name = appq or _first_shippable(cfg)
        if not app_name:
            # No product repo distinct from the unit's own — don't launch ship-review or flash an
            # empty "running for  …" banner; explain why and bail out.
            _state["last_msg"] = ("No shippable product configured — ship-review needs a product repo "
                                  "distinct from the unit.")
            return redirect("/council")
        if not _state.get("shipreview"):
            _state["shipreview"] = True   # set BEFORE redirect so /council shows the in-session indicator
            _state["last_msg"] = (f"🚀 Ship-review running for {app_name} — the Release Manager + officers are "
                                  "checking if DEV is ready for MAIN. The verdict posts here and to Telegram.")

            def _bg():
                try:
                    from . import council
                    asyncio.run(council.ship_review(cfg, app_name, audit=audit))
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"ship-review failed: {exc}"
                finally:
                    _state["shipreview"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/council")

    @app.post("/api/promote")
    def promote_api():
        """Promote DEV -> main from the cockpit (the server auto-deploys main). Mac-only + ff-only."""
        from . import sync
        if not sync.can_promote():
            return Response("Deploy is disabled on this cockpit (read-only box).", status=403)
        if _state.get("active"):
            _state["last_result"] = "finish the active run before deploying DEV → main"
            return redirect("/")
        if not _state.get("promoting"):
            _state["promoting"] = True   # set BEFORE redirect so the reloaded page shows the progress bar (no race)

            def _bg():
                try:
                    r = sync.promote(cfg)
                    _audit_promote(audit, r)
                    if r.get("ok"):
                        n = r.get("ahead_before", 0)
                        _state["last_result"] = (f"Deployed {n} commit(s) DEV → main — the server self-updates within ~15 min."
                                                 if n else "Unit already current — nothing to deploy.")
                    else:
                        _state["last_result"] = "Deploy failed: " + (r.get("error") or "unknown")
                except Exception as exc:  # noqa: BLE001
                    _state["last_result"] = f"Deploy error: {exc}"
                finally:
                    _state["promoting"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/")

    @app.get("/api/deploy-status")
    def deploy_status_api():
        """Live state for the deploy progress bar: is a unit-promote or app-ship still running, and the
        latest result line. The cockpit polls this so the button shows progress instead of looking dead."""
        from flask import jsonify
        active = bool(_state.get("promoting") or _state.get("shipping"))
        kind = "ship" if _state.get("shipping") else ("promote" if _state.get("promoting") else "")
        return jsonify({"active": active, "kind": kind, "msg": _state.get("last_result", "")})

    @app.post("/api/ship-main")
    def ship_main_api():
        """Ship the CURRENT app's DEV -> MAIN (production) from the cockpit. Mac-only + ff-only."""
        from . import sync
        if not sync.can_promote():
            return Response("Shipping is disabled on this cockpit (read-only box).", status=403)
        app_name = _scope(request.form.get("app"))   # ship the active tab's one concrete project
        if _state.get("active"):
            _state["last_result"] = "finish the active run before shipping to production"
            return redirect("/")
        if not _state.get("shipping"):
            _state["shipping"] = True   # set BEFORE redirect so the reloaded page shows the progress bar (no race)

            def _bg():
                try:
                    r = sync.promote_app(cfg.app(app_name))
                    _audit_ship(audit, app_name, r)
                    if r.get("ok"):
                        n = r.get("ahead_before", 0)
                        _state["last_result"] = (f"Shipped {app_name} {r['base']}→{r['prot']} "
                                                 f"({n} commit(s)) to PRODUCTION." if n else
                                                 f"{app_name} already shipped — nothing ahead.")
                    else:
                        _state["last_result"] = "Ship failed: " + (r.get("error") or "unknown")
                except Exception as exc:  # noqa: BLE001
                    _state["last_result"] = f"Ship error: {exc}"
                finally:
                    _state["shipping"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/")

    @app.post("/api/patrol")
    def patrol_api():
        # EU-63: patrol the ONE concrete project of the active tab — the "All projects"/`*` sweep is gone.
        app_name = _scope(request.form.get("app"))
        targets = [app_name] if app_name else []
        if not _state.get("patrolling"):
            def _bg():
                _state["patrolling"] = True
                try:
                    from . import patrol as patrol_mod
                    for name in targets:
                        asyncio.run(patrol_mod.patrol(cfg, name, do_file=True, audit=audit))
                    scope = targets[0] if targets else "(no project)"
                    _state["last_result"] = (f"✓ Patrol finished for {scope} — any findings were filed as "
                                             "Jira tickets assigned to you (see Needs you / your backlog).")
                except Exception as exc:  # noqa: BLE001
                    _state["last_result"] = f"patrol failed: {exc}"
                finally:
                    _state["patrolling"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/")

    @app.get("/approvals")
    def approvals_page():
        from . import approvals
        pend = approvals.pending(cfg)
        if _state.get("approving"):
            inner = _working(f"Applying the {_state.get('approving')} recommendation — editing officer "
                             "doctrine, then committing + pushing…")
        elif not pend:
            inner = ("<p style='color:#8a909c'>No pending recommendations. When the Engineering Coach or "
                     "Engineering Manager proposes something (after a drill or council), it lands here for your "
                     "Approve / Disapprove — Approve applies it and pushes the doctrine.</p>")
        else:
            style = ("<style>.apcard{background:#12161f;border:1px solid #232936;border-radius:12px;padding:14px 16px;margin:12px 0}"
                     ".aphead{font-weight:650;color:#fbbf24;margin-bottom:8px}"
                     ".aprow{display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap}"
                     ".apok{background:#10371f;border:1px solid #1c5238;color:#56d98a;border-radius:8px;padding:9px 14px;font-weight:650;cursor:pointer}"
                     ".apno{background:#23191a;border:1px solid #3a2f12;color:#f0676b;border-radius:8px;padding:9px 14px;cursor:pointer}</style>")
            cards = ""
            for it in pend:
                cards += (
                    "<div class=apcard>"
                    f"<div class=aphead>{html.escape(it['label'])}</div>"
                    f"<pre class=rep>{html.escape(it['body'])}</pre><div class=aprow>"
                    f"<form method=post action=/api/approve style='margin:0' "
                    "onsubmit=\"return confirm('Approve — the unit will apply this and push the doctrine. Continue?')\">"
                    f"<input type=hidden name=kind value='{html.escape(it['kind'])}'>"
                    "<button class=apok>&#9989; Approve — apply &amp; push</button></form>"
                    "<form method=post action=/api/disapprove style='display:flex;gap:6px;margin:0;flex:1'>"
                    f"<input type=hidden name=kind value='{html.escape(it['kind'])}'>"
                    "<input type=text name=reason placeholder='why not? (logged so it won&#39;t re-propose)' style='flex:1'>"
                    "<button class=apno>Disapprove</button></form></div></div>")
            inner = style + cards
        return _wrap("Approvals — officer recommendations", inner)

    @app.post("/api/approve")
    def approve_api():
        kind = (request.form.get("kind") or "").strip()
        if kind and not _state.get("approving"):
            def _bg():
                _state["approving"] = kind
                try:
                    from . import approvals
                    asyncio.run(approvals.approve(cfg, kind))
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"approve failed: {exc}"
                finally:
                    _state["approving"] = None
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/approvals")

    @app.post("/api/disapprove")
    def disapprove_api():
        kind = (request.form.get("kind") or "").strip()
        reason = (request.form.get("reason") or "").strip()
        if kind:
            from . import approvals
            approvals.disapprove(cfg, kind, reason)
        return redirect("/approvals")

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

    @app.get("/needs")
    def needs_page():
        """Unified Commander inbox — decisions, errored runs, parked tickets, open PRs.

        EU-102: renders s['rows'] (already typed with category+why) grouped into four
        labelled sections.  Officer recommendations and ticket proposals (not yet in rows)
        are appended below as before.
        """
        from . import needs as _needs
        s = _needs.summary(cfg)
        style = (
            "<style>"
            ".nsec{margin:4px 0 24px}.nsec h3{font-size:12px;text-transform:uppercase;letter-spacing:.08em;"
            "color:#8a929f;margin:0 0 10px;font-weight:700}"
            ".ncard{background:#12161f;border:1px solid #232936;border-radius:11px;padding:13px 15px;margin:9px 0}"
            ".ncard .q{color:#e9ecf1;margin-bottom:6px}.ncard .meta{color:#6b7480;font-size:12px;"
            "font-family:ui-monospace,Menlo,monospace}"
            ".nrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px}"
            ".nrow input[type=text]{flex:1;min-width:200px;background:#0d1119;border:1px solid #2a3343;"
            "color:#e9ecf1;border-radius:8px;padding:8px 10px;font:inherit}"
            ".nbtn{border:0;border-radius:8px;padding:8px 13px;font-weight:650;cursor:pointer;font:inherit;"
            "text-decoration:none;display:inline-block}"
            ".nbtn.ok{background:#10371f;border:1px solid #1c5238;color:#56d98a}.nbtn.send{background:#3b6cff;color:#fff}"
            ".nbtn.no{background:#23191a;border:1px solid #3a2f12;color:#f0676b}"
            ".nbtn.x{background:#1a1f2a;border:1px solid #2a3343;color:#8a929f}"
            ".ncard details>summary{cursor:pointer;color:#e9ecf1;list-style:none;display:flex;"
            "align-items:center;gap:8px;outline:none}"
            ".ncard details>summary::-webkit-details-marker{display:none}"
            ".ncard details>summary::before{content:'\\25B8';color:#6b7480;font-size:11px;transition:transform .15s}"
            ".ncard details[open]>summary::before{transform:rotate(90deg)}"
            ".ncard .ndetail{margin:11px 0 2px;padding:11px 13px;background:#0d1119;border:1px solid #222a38;"
            "border-radius:8px}"
            ".ncard .ndt{color:#c3cad6;font-size:13px;line-height:1.5;margin:5px 0}"
            ".ncard .ndt.sub{color:#8a929f;padding-left:8px}.ncard .ndt.muted{color:#6b7480}"
            ".ncard .ndt b{color:#e9ecf1;font-weight:650}"
            ".nbanner{background:#0f2740;border:1px solid #1c4a78;color:#9cc9ff;border-radius:9px;"
            "padding:11px 14px;margin:0 0 16px;font-size:13.5px;font-weight:600}"
            ".ncard label.pcheck{display:flex;gap:8px;align-items:flex-start;margin:7px 0;"
            "color:#c3cad6;font-size:13.5px;cursor:pointer}"
            ".ncard label.pcheck input{margin-top:3px}"
            ".ncard .psev{color:#fbbf24;font-weight:650}.ncard .ptype{color:#6b7480;font-size:12px}"
            ".nempty{color:#56d98a;padding:30px;text-align:center;font-size:15px}"
            # EU-102 — colour-coded category badges for the unified inbox
            ".nbadge{display:inline-block;border-radius:5px;padding:2px 7px;font-size:11px;"
            "font-weight:700;letter-spacing:.04em;text-transform:uppercase;margin-right:6px}"
            ".nbadge.dec{background:#1e1450;color:#a78bfa}"    # decision   — purple
            ".nbadge.err{background:#2a1010;color:#f87171}"    # errored    — red
            ".nbadge.prk{background:#1f1600;color:#fbbf24}"    # parked     — amber
            ".nbadge.opr{background:#0c1f20;color:#34d399}"    # open PR    — teal
            ".nbadge.spc{background:#0c1a2a;color:#60a5fa}"    # specialist — blue
            "</style>")
        # One-shot confirmation banner (e.g. "Answer sent to AUTO-23…") — read + clear so it shows once.
        _m = _state.pop("last_msg", "") or ""
        banner = f"<div class=nbanner>{html.escape(str(_m))}</div>" if _m else ""
        if not s.get("total"):
            return _wrap("Needs you", style + banner
                         + "<div class=nempty>&#10003; All clear — nothing needs you right now.</div>")
        out = [style, banner]

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
                why = html.escape(str(d.get("why") or "(no question on file)"))
                out.append(
                    "<div class=ncard>"
                    f"<div class=q><span class='nbadge dec'>Decision</span>{why}</div>"
                    f"<div class=meta>{tid}{(' &middot; ' + dapp) if dapp else ''}</div>"
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
                           f"style='color:#34d399'>{html.escape(pr_url)}</a>") if pr_url else ""
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

        # ── Officer recommendations — own action form (also counted in rows) ──
        if s.get("approvals"):
            out.append(f"<div class=nsec><h3>&#9989; Officer recommendations · {len(s['approvals'])}</h3>")
            for it in s["approvals"]:
                out.append(
                    f"<div class=ncard><div class=q>{html.escape(it['label'])}</div>"
                    f"<pre class=rep style='max-height:220px;overflow:auto;margin:6px 0 0'>{html.escape(it['body'])}</pre>"
                    "<div class=nrow>"
                    "<form method=post action=/api/approve style='margin:0' "
                    "onsubmit=\"return confirm('Approve — apply and push the doctrine. Continue?')\">"
                    f"<input type=hidden name=kind value='{html.escape(it['kind'])}'>"
                    "<button class='nbtn ok'>&#9989; Approve</button></form>"
                    "<form method=post action=/api/disapprove class=nrow style='flex:1;margin:0'>"
                    f"<input type=hidden name=kind value='{html.escape(it['kind'])}'>"
                    "<input type=text name=reason placeholder='why not? (logged so it won&#39;t re-propose)'>"
                    "<button class='nbtn no'>Disapprove</button></form></div></div>")
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

        # ── Specialist rosters — provisioning awaiting approve/decline (EU-102 iter-3) ──
        # Folded into rows/count, so they MUST render here too — otherwise a non-zero badge would
        # point at an empty inbox (the specialist-only dead-end this iteration fixes). Approve/decline
        # route through /api/answer → decisions.handle_reply → hr.resolve_specialist_approval_reply,
        # which reads 'approve' / 'decline' (no new endpoint needed).
        if s.get("specialist_approvals"):
            _specs = s["specialist_approvals"]
            out.append(f"<div class=nsec><h3>&#129513; Specialist rosters &middot; {len(_specs)}</h3>")
            for sp in _specs:
                tid = html.escape(str(sp.get("ticket_id") or ""))
                dom = html.escape(str(sp.get("domain") or ""))
                sapp = html.escape(str(sp.get("app_name") or sp.get("app") or ""))
                tsum = html.escape(str(sp.get("ticket_summary") or ""))
                roster = ""
                charters = sp.get("charters") or []
                if charters:
                    lis = "".join(
                        f"<li>{html.escape(str(c.get('name') or c.get('lane_key') or 'specialist'))}"
                        + (f" <span class=ptype>&middot; {html.escape(str(c.get('lane_key')))}</span>"
                           if c.get('lane_key') else "")
                        + "</li>"
                        for c in charters if isinstance(c, dict))
                    if lis:
                        roster = ("<ul style='margin:8px 0 2px 18px;padding:0;color:#c3cad6;"
                                  f"font-size:13px'>{lis}</ul>")
                out.append(
                    "<div class=ncard>"
                    f"<div class=q><span class='nbadge spc'>Specialist</span>"
                    f"Provision a {dom or 'specialist'} squad to build {tid}"
                    + (f" &mdash; {tsum}" if tsum else "")
                    + "</div>"
                    f"<div class=meta>{tid}{(' &middot; ' + sapp) if sapp else ''}</div>"
                    + roster +
                    "<div class=nrow>"
                    "<form method=post action=/api/answer style='margin:0'>"
                    f"<input type=hidden name=ticket value='{tid}'>"
                    f"<input type=hidden name=app value='{sapp}'>"
                    "<input type=hidden name=text value='approve'>"
                    "<button class='nbtn ok'>&#9989; Approve &amp; provision</button></form>"
                    "<form method=post action=/api/answer style='margin:0'>"
                    f"<input type=hidden name=ticket value='{tid}'>"
                    f"<input type=hidden name=app value='{sapp}'>"
                    "<input type=hidden name=text value='decline'>"
                    "<button class='nbtn no'>Decline &mdash; solo build</button></form>"
                    "</div></div>")
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
                           f"<div><b>{esc(active['name'])}</b> &middot; <span class=jmono>{esc(active['base_url'])}</span>"
                           f"<div class=jsub>{esc(active['email'])} &middot; token {esc(active['token_hint'])}"
                           + (f" &middot; project {esc(active['project_key'])}" if active['project_key'] else "")
                           + f"</div></div></div>")
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
                    f"<b>Connected via config + env</b> &middot; <span class=jmono>{esc(_b['base_url'])}</span>"
                    f"<div class=jsub>project <b>{esc(_proj) or '&mdash;'}</b> &middot; user {_who} &middot; {_tok}</div>"
                    f"<div class=jsub>From config.yaml under <span class=jmono>{esc(appq)}</span>, creds from "
                    "the cockpit&#39;s <span class=jmono>.env</span>. Quick-connect below only to override it.</div>"
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
            ".jlbl{color:#8a929f;font-size:12px;text-transform:uppercase;letter-spacing:.06em;font-weight:700}"
            ".jsel{min-width:190px}"
            ".jactive{display:flex;gap:11px;align-items:flex-start;background:#101620;border:1px solid #1f6f43;"
            "border-radius:12px;padding:13px 16px;margin:0 0 22px}"
            ".jactive.off{border-color:#3a2a18}"
            ".jdot{width:9px;height:9px;border-radius:50%;background:#3fb961;margin-top:6px;flex:none;"
            "box-shadow:0 0 0 4px rgba(63,185,97,.16)}.jdot.off{background:#d99a2b;box-shadow:0 0 0 4px rgba(217,154,43,.16)}"
            ".jsub{color:#8a929f;font-size:12px;margin-top:3px}"
            ".jmono{font-family:ui-monospace,Menlo,monospace;color:#9aa3b2}"
            ".jcards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:13px;margin:6px 0 26px}"
            ".jcard{background:#12161f;border:1px solid #232936;border-radius:12px;padding:14px 16px}"
            ".jcard.act{border-color:#1f6f43}"
            ".jname{font-size:15px;font-weight:700;color:#e9ecf1;margin-bottom:7px}"
            ".jtag{font-size:10px;font-weight:800;color:#3fb961;border:1px solid #1f6f43;border-radius:99px;"
            "padding:1px 7px;margin-left:6px;vertical-align:middle;text-transform:uppercase}"
            ".jmeta{color:#aab2c0;font-size:12.5px;margin-top:3px}"
            ".jrow{display:flex;gap:8px;align-items:center;margin-top:12px;flex-wrap:wrap}"
            ".jf{margin:0}"
            ".jbtn{background:#1b2230;border:1px solid #2a3343;color:#e9ecf1;border-radius:8px;padding:8px 13px;"
            "font:inherit;font-size:13px;font-weight:600;cursor:pointer}.jbtn:hover{background:#222b3b}"
            ".jbtn.primary{background:#2b5cff;border-color:#2b5cff;color:#fff}.jbtn.primary:hover{background:#2350e6}"
            ".jbtn.ghost{background:none;color:#9aa3b2}.jbtn.ghost:hover{color:#f0676b;border-color:#5a2a2e}"
            ".jbtn.on{background:none;border:1px solid #1f6f43;color:#3fb961;padding:8px 13px;border-radius:8px;"
            "font-size:13px;font-weight:700}"
            ".jempty,.jconnect{background:#12161f;border:1px solid #232936;border-radius:12px;padding:16px 18px}"
            ".jempty{color:#8a929f}"
            ".jgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}"
            ".jconnect label{display:block;color:#c4c9d2;font-size:12.5px;font-weight:600}"
            ".jconnect .jgrid input{width:100%;margin-top:5px;box-sizing:border-box}"
            ".jopt{color:#5c6573;font-weight:400}"
            ".jassign{display:flex;align-items:center;gap:8px;margin:14px 0 4px;color:#c4c9d2;font-weight:500!important}"
            ".jhint{color:#6b7480;font-size:12px}"
            "h3{margin:24px 0 8px;font-size:14px;color:#c4c9d2}"
            "</style>")

        body = (style + "<div class=jbar>" + switcher + "</div>" + active_html
                + "<h3>Saved Jira connections</h3>" + conns_html
                + "<h3>Quick connect a Jira</h3>" + form)
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
                         "<p><a href='/onboard'>&larr; back</a></p>")
        warn = "".join(f"<li>&#9888; {esc(w)}</li>" for w in r["warnings"])
        warnhtml = f"<ul style='color:#d99a2b'>{warn}</ul>" if warn else ""
        return _wrap("Onboard a product",
                     f"<p style='color:#3fb961'>&#10003; Added <b>{esc(r['name'])}</b> — base "
                     f"{esc(r['base'])} &rarr; protected {esc(r['protected'])}. Backed up config.yaml; "
                     f"<b>restart the cockpit / autopilot</b> to load it.</p>{warnhtml}"
                     f"<pre class=rep>{esc(r['block'])}</pre>"
                     "<p><a href='/onboard'>&larr; onboard another</a> &middot; <a href='/'>cockpit</a></p>")

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
            head = (f"<p><a href='/forensics'>&larr; forensics</a></p>"
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
            inner = (f"<p><a href='/forensics'>&larr; forensics</a></p>"
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
            ".shgo:hover{background:#6d28d9}.shgo:focus-visible,.shcancel:focus-visible{outline:none;box-shadow:var(--ring)}"
            ".shcancel{color:var(--dim);text-decoration:none;padding:11px 6px}"
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
        back = f'<a class=shcancel href="/?app={html.escape(appq)}">&larr; back to cockpit</a>'
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
                + '<form method=post action=/api/ship-main class=shbar '
                + 'onsubmit="return confirm(\'Ship ' + html.escape(appq)
                + ' to PRODUCTION now? This deploys your live product.\')">'
                + f'<input type=hidden name=app value="{html.escape(appq)}">'
                + f'<button class=shgo>&#128640; Ship {html.escape(appq)} to production</button>'
                + back + '</form></div>')
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
                '<div class=composer><form method=post action=/api/chat>'
                f'<input type=text name=text autocomplete=off autofocus value="{prefill}" '
                'placeholder="Message the CTO…  (or reply  AUTO-1: your decision)"><button>Send</button></form></div>'
                '<script>window.scrollTo(0,document.body.scrollHeight);'
                'setInterval(async function(){try{var r=await fetch("/api/chat-thread",{cache:"no-store"});'
                'if(r.ok){var near=(window.innerHeight+window.scrollY)>=document.body.scrollHeight-140;'
                'document.getElementById("cinner").innerHTML=await r.text();'
                'if(near)window.scrollTo(0,document.body.scrollHeight);}}catch(e){}},5000);'
                '</script>')
        return _wrap("Chat with the CTO", body)

    @app.get("/group")
    def group_page():
        officer = (request.args.get("officer") or "").strip()
        busy = ('<div class=cempty>&#128225; the unit is weighing in… replies appear below.</div>'
                if _state.get("grouping") else "")
        aim = (f'<div class=aim>Consulting <b>{html.escape(officer)}</b> directly — only they answer. '
               '<a href="/group">ask the whole unit instead</a></div>') if officer else ""
        oin = f'<input type=hidden name=officer value="{html.escape(officer)}">' if officer else ""
        ph = (f"Ask {html.escape(officer)} something…" if officer
              else "Ask the unit / brainstorm with the officers…")
        body = (_CHAT_STYLE + _chat_tabs("group")
                + '<div class=chat>' + aim + busy + '<div id=ginner>' + _group_inner(cfg) + '</div></div>'
                '<div class=composer><form method=post action=/api/group>' + oin
                + f'<input type=text name=text autocomplete=off autofocus '
                f'placeholder="{ph}"><button>Send</button></form></div>'
                '<script>window.scrollTo(0,document.body.scrollHeight);'
                'setInterval(async function(){try{var r=await fetch("/api/group-thread",{cache:"no-store"});'
                'if(r.ok){var near=(window.innerHeight+window.scrollY)>=document.body.scrollHeight-140;'
                'document.getElementById("ginner").innerHTML=await r.text();'
                'if(near)window.scrollTo(0,document.body.scrollHeight);}}catch(e){}},4000);'
                '</script>')
        return _wrap("Group room — the unit", body)

    @app.get("/api/group-thread")
    def group_thread_api():
        from flask import Response
        return Response(_group_inner(cfg), mimetype="text/html")

    @app.post("/api/group")
    def group_api():
        from urllib.parse import quote
        text = (request.form.get("text") or "").strip()
        officer = (request.form.get("officer") or "").strip() or None
        if text and not _state.get("grouping"):
            from . import council
            council._append_group(cfg, "you", text)   # echo instantly; the bg adds officer replies
            def _bg():
                _state["grouping"] = True
                try:
                    asyncio.run(council.group_chat(cfg, text, officers=[officer] if officer else None,
                                                   audit=audit, echo=False))
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"group chat failed: {exc}"
                finally:
                    _state["grouping"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/group?officer=" + quote(officer) if officer else "/group")

    @app.get("/api/chat-thread")
    def chat_thread_api():
        from flask import Response
        return Response(_chat_inner(cfg), mimetype="text/html")

    @app.post("/api/chat")
    def chat_api():
        text = (request.form.get("text") or "").strip()
        tid = (request.form.get("ticket") or "").strip()
        if text:
            msg = f"{tid}: {text}" if tid else text

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
        st = _claim_cockpit_run(app_name)
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
            st["last_msg"] = ""
            ev = threading.Event()
            st["stop_event"] = ev
            errored = False
            try:
                asyncio.run(run_loop(rcfg, worklist, audit, stop_event=ev))
            except Exception as exc:  # noqa: BLE001
                errored = True
                st["last_msg"] = str(exc)
            finally:
                release_run(app_name or None)   # clears active / run_started / stop_event for this app
                st["dry_run"] = None            # clear the dry/live flag so the cockpit shows no stale tag
                # EU-104: on a CLEAN terminal outcome, clear the transient 'Working / stopping…'
                # control-bar note so a finished run never lingers as 'Working'. Guarded by
                # ``errored`` so a real run error (set just above) stays visible — release_run no
                # longer clears last_msg, so the operator still sees why a failed run failed.
                if not errored:
                    st["last_msg"] = ""
        threading.Thread(target=_bg, daemon=True).start()
        return redirect("/")

    return app


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8787) -> None:
    try:
        app = create_app(cfg)
    except ImportError:
        raise SystemExit("Flask is required for the control panel. Run: pip install -r requirements.txt")
    # Quiet the per-request access log (the dashboard polls GET /api/board every 5s). Without this
    # the terminal is flooded and the unit's real progress is lost in the noise.
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    # Mirror the unit's progress output into the dashboard's Live feed.
    import sys
    if not isinstance(sys.stdout, _Tee):
        sys.stdout = _Tee(sys.stdout)
    # Two-way decisions: watch Telegram for replies that resume paused tickets.
    from . import decisions, notify
    if notify.configured():
        threading.Thread(target=decisions.poll_loop, args=(cfg, AuditLog(cfg.audit_path)),
                         daemon=True).start()
        print("  decision listener: ON — reply to ❓ messages in Telegram to resume tickets")
    print(f"★ War Room: http://{host}:{port}   (Ctrl-C to stop)")
    print("  (the terminal shows the unit's progress only — dashboard polling is hidden)\n")
    # threaded: the SSE stream holds a long-lived request — without this it would block the cockpit.
    app.run(host=host, port=port, threaded=True)
