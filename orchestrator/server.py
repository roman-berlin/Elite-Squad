"""Control panel — a local web app cockpit (`general serve`).

Serves the dashboard plus a control bar so you can launch work with a button
(task / ticket / drain), choose effort, toggle live, and watch results. Binds to
localhost only. Runs happen in a background thread so the page stays responsive.
"""
from __future__ import annotations

import asyncio
import html
import os
import threading
from pathlib import Path

from . import dashboard as D
from . import health
from . import intake
from . import memory
from . import warroom
from .audit import AuditLog
from .config import Config, normalize_effort
from .loop import run as run_loop

_state = {"active": False, "last_msg": "", "drilling": False}


def _wrap(title: str, inner: str) -> str:
    return ("<!doctype html><meta charset=utf-8><title>" + html.escape(title) + "</title>"
            "<style>body{background:#0d0f14;color:#e8eaed;font:14px/1.6 -apple-system,"
            "BlinkMacSystemFont,sans-serif;margin:0;padding:22px 30px}a{color:#6aa9ff}"
            ".rep{white-space:pre-wrap;background:#151a23;border:1px solid #232936;"
            "border-radius:10px;padding:16px}"
            "textarea,select,input{background:#151a23;border:1px solid #232936;color:#e8eaed;"
            "border-radius:8px;padding:8px;font:inherit}"
            "button{background:#2b5cff;border:0;color:#fff;border-radius:8px;padding:9px 16px;"
            "font-weight:650;cursor:pointer}</style>"
            f"<p><a href='/'>&larr; cockpit</a></p><h2>{html.escape(title)}</h2>{inner}")


def _charged() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _control_bar(cfg: Config, current_app: str | None = None, healthy: bool = True) -> str:
    app0 = current_app or (cfg.apps[0].name if cfg.apps else "")
    apps = "".join(
        f"<option value='{html.escape(a.name)}' {'selected' if a.name == current_app else ''}>"
        f"{html.escape(a.name)}</option>" for a in cfg.apps)
    effort = "".join(f"<option value='{e}'>{e}</option>"
                     for e in ("low", "medium", "high", "xhigh", "max"))
    run_dis = "disabled" if (_state["active"] or not healthy) else ""
    if _state["active"]:
        status = '<span class="tbnote run">&#9679; run in progress…</span>'
    elif _state.get("last_msg"):
        status = f'<span class="tbnote bad">{html.escape(_state["last_msg"])}</span>'
    else:
        status = ""

    def busy(k):
        return "disabled" if _state.get(k) else ""

    return f"""
<style>
.tbar{{display:flex;gap:9px;align-items:center;flex-wrap:wrap;padding:11px 26px;border-bottom:1px solid #1f2531;background:#0e1219}}
.tbar .btn{{display:inline-flex;align-items:center;gap:7px;background:#161b25;border:1px solid #2a3343;color:#e9ecf1;border-radius:9px;padding:9px 13px;font:inherit;font-size:13px;font-weight:600;cursor:pointer;text-decoration:none;white-space:nowrap}}
.tbar .btn:hover{{background:#1b2230}}
.tbar .btn.primary{{background:#3b6cff;border-color:#3b6cff;color:#fff}}
.tbar .btn.primary:hover{{background:#2f5ce0}}
.tbar details.menu{{position:relative}}
.tbar details.menu>summary{{list-style:none}}
.tbar details.menu>summary::-webkit-details-marker{{display:none}}
.tbar details.menu>summary::after{{content:" \\25BE";color:#8a929f;font-size:10px}}
.tbar details[open]>summary{{background:#1b2230;border-color:#3b6cff}}
.tbar .panel{{position:absolute;top:calc(100% + 7px);left:0;z-index:30;min-width:212px;background:#12161f;border:1px solid #2a3343;border-radius:12px;padding:6px;display:flex;flex-direction:column;gap:2px;box-shadow:0 16px 40px rgba(0,0,0,.5)}}
.tbar .panel.right{{left:auto;right:0}}
.tbar .panel a,.tbar .panel form>button{{display:flex;align-items:center;gap:9px;width:100%;text-align:left;background:none;border:0;color:#e9ecf1;border-radius:8px;padding:9px 11px;font:inherit;font-size:13px;font-weight:500;cursor:pointer;text-decoration:none;white-space:nowrap}}
.tbar .panel a:hover,.tbar .panel form>button:hover{{background:#1b2230}}
.tbar .panel form{{margin:0}}
.tbar .panel.form{{min-width:312px;gap:9px;padding:13px}}
.tbar .panel.form select,.tbar .panel.form input[type=text]{{background:#0d1119;border:1px solid #2a3343;color:#e9ecf1;border-radius:8px;padding:8px 10px;font:inherit;width:100%}}
.tbar .panel.form .row{{display:flex;gap:8px;align-items:center}}
.tbar .panel.form button{{display:block;width:100%;background:#3b6cff;color:#fff;border:0;border-radius:8px;padding:9px;font-weight:650;cursor:pointer}}
.tbar .panel.form button:disabled{{background:#222a37;color:#5c6573;cursor:not-allowed}}
.tbar .panel .sep{{height:1px;background:#1f2531;margin:5px 4px}}
.tbar .panel .ph{{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:#5c6573;padding:6px 11px 3px}}
.tbar .tbnote{{font-size:12px;margin-left:2px}}.tbar .tbnote.run{{color:#f7b955}}.tbar .tbnote.bad{{color:#f0676b}}
.tbar .grow{{flex:1}}
</style>
<div class=tbar>
  <a class="btn primary" href="/tickets?app={html.escape(app0)}">&#127915; Choose a ticket</a>

  <details class=menu>
    <summary class=btn>&#43; Free task</summary>
    <div class="panel form">
      <form method=post action=/api/run>
        <input type=hidden name=kind value=task>
        <div class=ph>Describe a bug or feature</div>
        <input type=text name=text placeholder="e.g. fix the cut-off column on /leads">
        <div class=row>
          <select name=app title=project style="flex:1">{apps}</select>
          <select name=effort title=effort style="flex:1"><option value=''>effort: auto</option>{effort}</select>
        </div>
        <label style="font-size:13px;color:#c4c9d2"><input type=checkbox name=live> live (build + merge to DEV)</label>
        <button {run_dis}>&#9654; Run</button>
      </form>
    </div>
  </details>

  <details class=menu>
    <summary class=btn>&#9881; Unit</summary>
    <div class=panel>
      <div class=ph>Convene the officers</div>
      <form method=post action=/api/council><button {busy('councilling')}>&#128172; Hold council</button></form>
      <a href="/meeting">&#127908; Convene a meeting</a>
      <form method=post action=/api/ship-review><input type=hidden name=app value="{html.escape(app0)}"><button {busy('shipreview')}>&#128640; Ship review</button></form>
      <div class=sep></div>
      <form method=post action=/api/drill><button {busy('drilling')}>&#127894; Run drill</button></form>
      <form method=post action=/api/scribe><button {busy('scribing')}>&#128221; Update memory</button></form>
    </div>
  </details>

  <details class=menu>
    <summary class=btn>&#9776; Views</summary>
    <div class="panel right">
      <a href="/tasks">&#128203; Task log</a>
      <a href="/council">&#128172; Councils &amp; meetings</a>
      <a href="/memory">&#128221; Unit memory</a>
      <a href="/standup">&#129303; Daily standup</a>
      <a href="/drill">&#127894; Last drill</a>
      <div class=sep></div>
      <a href="/report">&#128030; Report a problem</a>
    </div>
  </details>

  <span class=grow></span>
  {status}
</div>"""


def create_app(cfg: Config):
    from flask import Flask, redirect, request
    app = Flask(__name__)
    audit = AuditLog(cfg.audit_path)

    @app.get("/")
    def index():
        appq = request.args.get("app")
        h = health.summary(cfg)
        return warroom.render_page(cfg, appq, _state, _control_bar(cfg, appq, h["healthy"]), h)

    @app.get("/api/health")
    def health_api():
        from flask import jsonify
        return jsonify(health.summary(cfg))

    @app.post("/api/autopilot")
    def autopilot_api():
        import copy
        from . import autopilot as ap
        cur = _state.get("autopilot") or {}
        action = request.form.get("action", "toggle")
        want_on = action == "start" or (action == "toggle" and not cur.get("on"))
        if want_on and not cur.get("on"):
            if not health.summary(cfg)["healthy"]:
                _state["last_msg"] = "autopilot blocked — fix the health problems first"
                return redirect("/")
            app_name = (request.form.get("app") or "").strip() or None
            ev = threading.Event()
            ap_cfg = copy.copy(cfg)
            ap_cfg.dry_run = False     # continuous autopilot must be live (else it re-picks forever)

            def _bg():
                try:
                    asyncio.run(ap.autopilot(ap_cfg, app_name, once=False, stop_event=ev))
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"autopilot error: {exc}"
                finally:
                    st = _state.get("autopilot")
                    if st:
                        st["on"] = False
            _state["autopilot"] = {"on": True, "stop": ev, "app": app_name or "all projects"}
            threading.Thread(target=_bg, daemon=True).start()
        elif cur.get("on") and cur.get("stop"):
            cur["stop"].set()
            cur["on"] = False
        return redirect("/")

    @app.get("/api/board")
    def board_api():
        from flask import Response
        appq = request.args.get("app")
        return Response(warroom.render_board(cfg, appq, _state), mimetype="text/html")

    @app.get("/tasks")
    def tasks_page():
        page = D.render_html(D.load_tasks(cfg.audit_path), show_cost=_charged())
        return page.replace("</header>", "</header>" + _control_bar(cfg), 1)

    @app.get("/tickets")
    def tickets_page():
        app_name = request.args.get("app") or (cfg.apps[0].name if cfg.apps else "")
        style = ("<style>.tlist{margin:10px 0;border:1px solid #232936;border-radius:10px;overflow:hidden}"
                 ".trow{display:flex;gap:12px;align-items:flex-start;padding:11px 14px;border-top:1px solid #1a1f29;cursor:pointer}"
                 ".trow:first-child{border-top:0}.trow:hover{background:#151a23}"
                 ".trow input{margin-top:3px}.tkey{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#6aa9ff;white-space:nowrap}"
                 ".tsum{color:#e8eaed}.trun{display:flex;gap:14px;align-items:center;margin-top:14px;flex-wrap:wrap}"
                 ".trun button{background:#2b5cff;border:0;color:#fff;border-radius:8px;padding:9px 18px;font-weight:650;cursor:pointer}"
                 ".hint{color:#8a909c;font-size:13px}</style>")
        try:
            items = intake.from_drain(cfg, app_name, 30)
        except Exception as exc:  # noqa: BLE001
            return _wrap("Choose tickets", f"<p class=hint>Couldn't load tickets for "
                         f"<b>{html.escape(app_name)}</b>: {html.escape(str(exc))}</p>")
        if not items:
            return _wrap("Choose tickets", "<p class=hint>Nothing assigned to you in "
                         f"<b>{html.escape(app_name)}</b> (In Progress / To Do). Clear queue.</p>")
        rows = "".join(
            f'<label class=trow><input type=checkbox name=ticket value="{html.escape(t.id)}">'
            f'<span class=tkey>{html.escape(t.id)}</span>'
            f'<span class=tsum>{html.escape(t.summary or "(no summary)")}</span></label>'
            for _, t in items)
        effort = "".join(f"<option value='{e}'>{e}</option>"
                         for e in ("low", "medium", "high", "xhigh", "max"))
        body = (style
                + f'<p class=hint>{len(items)} ticket(s) assigned to you, in board-priority order. '
                  "Tick the ones to develop, then Run.</p>"
                  '<form method=post action=/api/run-selected>'
                  f'<input type=hidden name=app value="{html.escape(app_name)}">'
                  f'<div class=tlist>{rows}</div>'
                  '<div class=trun>'
                  '<label><input type=checkbox name=live> live (build + merge to DEV)</label>'
                  f'<select name=effort><option value="">effort: auto-size</option>{effort}</select>'
                  '<button>&#9654; Develop selected</button>'
                  '<span class=hint>leave “live” off for a safe dry-run first</span>'
                  '</div></form>')
        return _wrap(f"Choose tickets — {app_name}", body)

    @app.post("/api/run-selected")
    def run_selected_api():
        if _state["active"]:
            return redirect("/")
        app_name = request.form.get("app") or (cfg.apps[0].name if cfg.apps else "")
        keys = request.form.getlist("ticket")
        if not keys:
            return redirect(f"/tickets?app={app_name}")
        if not health.summary(cfg)["healthy"]:
            _state["last_msg"] = "blocked — fix the health problems first (see the banner)"
            return redirect("/")
        import copy
        rcfg = copy.copy(cfg)        # per-run config — never mutate the shared cfg
        rcfg.dry_run = request.form.get("live") != "on"
        effort = request.form.get("effort") or None
        if effort:
            rcfg.builder_effort = normalize_effort(effort)
            rcfg.adaptive_effort = False
        try:
            worklist = intake.from_tickets(rcfg, app_name, keys)
        except Exception as exc:  # noqa: BLE001
            _state["last_msg"] = f"could not start: {exc}"
            return redirect("/")

        def _bg():
            _state["active"], _state["last_msg"] = True, ""
            try:
                asyncio.run(run_loop(rcfg, worklist, audit))
            except Exception as exc:  # noqa: BLE001
                _state["last_msg"] = str(exc)
            finally:
                _state["active"] = False
        threading.Thread(target=_bg, daemon=True).start()
        return redirect("/")

    @app.post("/api/run")
    def run_api():
        if _state["active"]:
            return redirect("/")
        if not health.summary(cfg)["healthy"]:
            _state["last_msg"] = "blocked — fix the health problems first (see the banner)"
            return redirect("/")
        app_name = request.form.get("app") or (cfg.apps[0].name if cfg.apps else "")
        kind = request.form.get("kind", "task")
        text = (request.form.get("text") or "").strip()
        effort = request.form.get("effort") or None
        import copy
        rcfg = copy.copy(cfg)        # per-run config — never mutate the shared cfg
        rcfg.dry_run = request.form.get("live") != "on"
        if effort:
            rcfg.builder_effort = normalize_effort(effort)
            rcfg.adaptive_effort = False     # an explicit pick bypasses auto-sizing for this run
        try:
            if kind == "task":
                worklist = intake.from_text(rcfg, app_name, text or "(no description)", [])
            elif kind == "ticket":
                worklist = intake.from_tickets(rcfg, app_name, text.split())
            else:
                worklist = intake.from_drain(rcfg, app_name or None, rcfg.max_tickets_per_run)
        except Exception as exc:  # noqa: BLE001
            _state["last_msg"] = f"could not start: {exc}"
            return redirect("/")

        def _bg():
            _state["active"], _state["last_msg"] = True, ""
            try:
                asyncio.run(run_loop(rcfg, worklist, audit))
            except Exception as exc:  # noqa: BLE001
                _state["last_msg"] = str(exc)
            finally:
                _state["active"] = False
        threading.Thread(target=_bg, daemon=True).start()
        return redirect("/")

    @app.get("/standup")
    def standup_page():
        return _wrap("Daily standup", "<pre class=rep>" + html.escape(D.standup(cfg)) + "</pre>")

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
            body = "<p>🎖️ Drillmaster is reviewing the unit… reload in a minute.</p>"
        elif rep.exists():
            body = "<pre class=rep>" + html.escape(rep.read_text(encoding="utf-8")) + "</pre>"
        else:
            body = "<p>No drill report yet — click 🎖️ Drill on the cockpit.</p>"
        return _wrap("Drillmaster report", body)

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
        top = "<p>🎖️ The officers are in session… reload shortly.</p>" if _state.get("councilling") else ""
        if not hist:
            return _wrap("Daily Council", top + "<p>No councils yet — press &#128172; Council on "
                         "the cockpit, or run <code>general council</code>.</p>")
        want = request.args.get("f") or hist[0]["file"]
        transcript = council.transcript_text(cfg, want) or "(transcript missing)"
        items = "".join(
            f"<li><a href='/council?f={html.escape(h['file'])}'>{html.escape(h['ts'][:16])} — "
            f"{html.escape(h['summary'])}</a></li>" for h in hist)
        body = (top + "<div style='display:flex;gap:24px;align-items:flex-start'>"
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
                    asyncio.run(memory.scribe(cfg))
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"scribe failed: {exc}"
                finally:
                    _state["scribing"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/memory")

    @app.get("/memory")
    def memory_page():
        memory.ensure()
        top = ("<p>📝 The Scribe is updating Unit Memory… reload shortly.</p>"
               if _state.get("scribing") else "")
        body = top + "<pre class=rep>" + html.escape(memory.load() or "(no Unit Memory yet)") + "</pre>"
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
            "<p class=hint>The officers debate, the General decides, and the outcome is written to "
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
        app_name = request.form.get("app") or (cfg.apps[0].name if cfg.apps else "")
        if not _state.get("shipreview"):
            def _bg():
                _state["shipreview"] = True
                try:
                    from . import council
                    asyncio.run(council.ship_review(cfg, app_name, audit=audit))
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"ship-review failed: {exc}"
                finally:
                    _state["shipreview"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/council")

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
            "<p><label><input type=checkbox name=live> live (build + merge to DEV)</label></p>"
            "<p><button>Send to the General</button></p></form>")
        return _wrap("Report a problem", form)

    @app.post("/api/report")
    def report_api():
        if _state["active"]:
            return redirect("/")
        if not health.summary(cfg)["healthy"]:
            _state["last_msg"] = "blocked — fix the health problems first (see the banner)"
            return redirect("/")
        app_name = request.form.get("app") or (cfg.apps[0].name if cfg.apps else "")
        text = (request.form.get("text") or "").strip()
        import copy
        rcfg = copy.copy(cfg)        # per-run config — never mutate the shared cfg
        rcfg.dry_run = request.form.get("live") != "on"
        desc = f"Fix this problem found during QA on DEV:\n{text or '(no description)'}"
        f = request.files.get("screenshot")
        if f and f.filename:
            import re
            import time as _t
            qa = Path(cfg.audit_path).resolve().parent / "qa-reports"
            qa.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", f.filename)
            path = qa / f"{int(_t.time())}-{safe}"
            f.save(str(path))
            desc += f"\n\nScreenshot of the problem (open and view it to understand the bug): {path}"
        title = (text.splitlines()[0][:60] if text else "QA bug report")
        try:
            worklist = intake.from_text(rcfg, app_name, title, [], description=desc)
        except Exception as exc:  # noqa: BLE001
            _state["last_msg"] = f"could not start: {exc}"
            return redirect("/")

        def _bg():
            _state["active"], _state["last_msg"] = True, ""
            try:
                asyncio.run(run_loop(rcfg, worklist, audit))
            except Exception as exc:  # noqa: BLE001
                _state["last_msg"] = str(exc)
            finally:
                _state["active"] = False
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
    # Two-way decisions: watch Telegram for replies that resume paused tickets.
    from . import decisions, notify
    if notify.configured():
        threading.Thread(target=decisions.poll_loop, args=(cfg, AuditLog(cfg.audit_path)),
                         daemon=True).start()
        print("  decision listener: ON — reply to ❓ messages in Telegram to resume tickets")
    print(f"★ War Room: http://{host}:{port}   (Ctrl-C to stop)")
    print("  (the terminal shows the unit's progress only — dashboard polling is hidden)\n")
    app.run(host=host, port=port)
