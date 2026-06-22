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
from .cockpit_state import _LOG, _Tee, _run_lock, _sse, _state, recent_log  # noqa: F401
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
    _group_inner,
    _wrap,
    _working,
)


def create_app(cfg: Config):
    from flask import Flask, redirect, request
    app = Flask(__name__)
    audit = AuditLog(cfg.audit_path)
    from . import usage
    usage.configure(cfg.audit_path)   # the cockpit process meters token burn too

    @app.get("/")
    def index():
        appq = request.args.get("app")
        try:
            from . import projects
            projects.record_recent(cfg, appq)   # VS-Code-style "recent projects"
        except Exception:  # noqa: BLE001
            pass
        h = health.summary(cfg)
        return warroom.render_page(cfg, appq, _state, _control_bar(cfg, appq, h["healthy"]), h,
                                   log_lines=recent_log())

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
        return Response(warroom.render_board(cfg, appq, _state, recent_log()), mimetype="text/html")

    @app.get("/api/stream")
    def stream_api():
        """Server-Sent Events: push a freshly-rendered board the moment the unit prints a step
        (sub-second), and at least every 2s (keeps the elapsed timer + heartbeat alive). The
        browser swaps #board on each frame; it falls back to the 5s poll if the stream drops."""
        from flask import Response
        appq = request.args.get("app")

        def gen():
            last_seq = None
            last_emit = 0.0
            while True:
                seq = _state.get("log_seq", 0)
                now = time.time()
                if seq != last_seq or now - last_emit >= 2.0:
                    last_seq, last_emit = seq, now
                    try:
                        yield _sse("board", warroom.render_board(cfg, appq, _state, recent_log()))
                    except Exception:  # noqa: BLE001 - never let a render error kill the stream
                        yield ": render-error\n\n"
                time.sleep(0.5)

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/tasks")
    def tasks_page():
        page = D.render_html(D.load_tasks(cfg.audit_path), show_cost=_charged(),
                             dismissed=D.load_dismissed(cfg.audit_path))
        # This board view is reached from the cockpit's Reports menu, so it needs a way back like
        # every other sub-page (it renders via D.render_html, which bypasses _wrap's "← cockpit").
        back = "<p style='margin:14px 30px 4px'><a href='/' style='color:#6aa9ff'>&larr; cockpit</a></p>"
        return page.replace("</header>", "</header>" + back + _control_bar(cfg), 1)

    @app.post("/api/dismiss")
    def dismiss_api():
        tid = (request.form.get("ticket") or "").strip()
        back = (request.form.get("back") or "/tasks").strip()
        if tid:
            D.dismiss(cfg.audit_path, tid)
        return redirect(back if back in ("/tasks", "/needs") else "/tasks")

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
                  '<form method=post action=/api/run-selected '
                  'onsubmit="return this.dryrun.checked||confirm(\'Build and merge to DEV. Continue?\')">'
                  f'<input type=hidden name=app value="{html.escape(app_name)}">'
                  f'<div class=tlist>{rows}</div>'
                  '<div class=trun>'
                  '<label><input type=checkbox name=dryrun> dry run (build only — no merge)</label>'
                  f'<select name=effort><option value="">effort: auto-size</option>{effort}</select>'
                  '<button>&#9654; Develop selected</button>'
                  '<span class=hint>default builds + merges to DEV — tick “dry run” to build only</span>'
                  '</div></form>')
        return _wrap(f"Choose tickets — {app_name}", body)

    @app.post("/api/run-selected")
    def run_selected_api():
        # Atomically claim the run: check-then-set under the lock so two near-simultaneous
        # POSTs can't both pass this guard and start two runs.
        with _run_lock:
            if _state["active"]:
                _state["last_msg"] = "a run is already in progress — stop it and wait for it to finish, then start the new one"
                return redirect("/")
            _state["active"] = True
        app_name = request.form.get("app") or (cfg.apps[0].name if cfg.apps else "")
        keys = request.form.getlist("ticket")
        if not keys:
            _state["active"] = False
            return redirect(f"/tickets?app={app_name}")
        if not health.summary(cfg)["healthy"]:
            _state["active"] = False
            _state["last_msg"] = "blocked — fix the health problems first (see the banner)"
            return redirect("/")
        import copy
        rcfg = copy.copy(cfg)        # per-run config — never mutate the shared cfg
        rcfg.dry_run = request.form.get("dryrun") == "on"   # default: live (build + merge to DEV)
        _state["dry_run"] = rcfg.dry_run
        effort = request.form.get("effort") or None
        if effort:
            rcfg.builder_effort = normalize_effort(effort)
            rcfg.adaptive_effort = False
        try:
            worklist = intake.from_tickets(rcfg, app_name, keys)
        except Exception as exc:  # noqa: BLE001
            _state["active"] = False
            _state["last_msg"] = f"could not start: {exc}"
            return redirect("/")

        def _bg():
            _state["active"], _state["last_msg"] = True, ""
            _state["run_started"] = _state["last_activity"] = time.time()
            ev = threading.Event()
            _state["stop_event"] = ev
            try:
                asyncio.run(run_loop(rcfg, worklist, audit, stop_event=ev))
            except Exception as exc:  # noqa: BLE001
                _state["last_msg"] = str(exc)
            finally:
                _state["active"] = False
                _state["stop_event"] = None
                _state["dry_run"] = None        # clear the dry/live flag so the cockpit shows no stale tag
                _state["run_started"] = None
        threading.Thread(target=_bg, daemon=True).start()
        return redirect("/")

    @app.post("/api/run")
    def run_api():
        # Atomically claim the run: check-then-set under the lock so two near-simultaneous
        # POSTs can't both pass this guard and start two runs.
        with _run_lock:
            if _state["active"]:
                _state["last_msg"] = "a run is already in progress — stop it and wait for it to finish, then start the new one"
                return redirect("/")
            _state["active"] = True
        if not health.summary(cfg)["healthy"]:
            _state["active"] = False
            _state["last_msg"] = "blocked — fix the health problems first (see the banner)"
            return redirect("/")
        app_name = request.form.get("app") or (cfg.apps[0].name if cfg.apps else "")
        kind = request.form.get("kind", "task")
        text = (request.form.get("text") or "").strip()
        ttype = (request.form.get("type") or "feature").strip()
        effort = request.form.get("effort") or None
        import copy
        rcfg = copy.copy(cfg)        # per-run config — never mutate the shared cfg
        rcfg.dry_run = request.form.get("dryrun") == "on"   # default: live (build + merge to DEV)
        _state["dry_run"] = rcfg.dry_run
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
                worklist = intake.from_drain(rcfg, app_name or None, rcfg.max_tickets_per_run)
        except Exception as exc:  # noqa: BLE001
            _state["active"] = False
            _state["last_msg"] = f"could not start: {exc}"
            return redirect("/")

        def _bg():
            _state["active"], _state["last_msg"] = True, ""
            _state["run_started"] = _state["last_activity"] = time.time()
            ev = threading.Event()
            _state["stop_event"] = ev
            try:
                asyncio.run(run_loop(rcfg, worklist, audit, stop_event=ev))
            except Exception as exc:  # noqa: BLE001
                _state["last_msg"] = str(exc)
            finally:
                _state["active"] = False
                _state["stop_event"] = None
                _state["dry_run"] = None        # clear the dry/live flag so the cockpit shows no stale tag
                _state["run_started"] = None
        threading.Thread(target=_bg, daemon=True).start()
        return redirect("/")

    @app.post("/api/stop-run")
    def stop_run_api():
        ev = _state.get("stop_event")
        if ev is not None:
            ev.set()
            _state["last_msg"] = "stopping after the current step — DEV untouched, no merge"
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
            body = _working("Drillmaster is reviewing the unit's record and proposing officer upgrades…")
        else:
            act = _actbar(_actbtn("/api/drill", "&#127894; Run drill"))
            if rep.exists():
                body = act + "<pre class=rep>" + html.escape(rep.read_text(encoding="utf-8")) + "</pre>"
            else:
                body = act + ("<p style='color:#8a909c'>No drill yet. Run one — the Drillmaster reviews "
                              "the unit's record and proposes officer upgrades, which you Approve in the "
                              "<a href='/approvals'>Approvals</a> inbox.</p>")
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
        top = _working("The officers are in session — reading the record and debating…") if _state.get("councilling") else ""
        acts = "" if _state.get("councilling") else _actbar(
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
                    _state["last_msg"] = "✓ " + (str(msg).strip() or "Unit Memory updated by the Scribe.")
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
        top = (_working("The Scribe is folding recent lessons into Unit Memory…")
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
        # One-shot confirmation banner ("✓ Scribe folded … into Unit Memory") — shown once the Scribe
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

    @app.post("/api/promote")
    def promote_api():
        """Promote DEV -> main from the cockpit (the server auto-deploys main). Mac-only + ff-only."""
        from . import sync
        if not sync.can_promote():
            return Response("Deploy is disabled on this cockpit (read-only box).", status=403)
        if _state.get("active"):
            _state["last_msg"] = "finish the active run before deploying DEV → main"
            return redirect("/")
        if not _state.get("promoting"):
            _state["promoting"] = True   # set BEFORE redirect so the reloaded page shows the progress bar (no race)

            def _bg():
                try:
                    r = sync.promote(cfg)
                    if r.get("ok"):
                        n = r.get("ahead_before", 0)
                        _state["last_msg"] = (f"Deployed {n} commit(s) DEV → main — the server self-updates within ~15 min."
                                              if n else "Unit already current — nothing to deploy.")
                    else:
                        _state["last_msg"] = "Deploy failed: " + (r.get("error") or "unknown")
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"Deploy error: {exc}"
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
        return jsonify({"active": active, "kind": kind, "msg": _state.get("last_msg", "")})

    @app.post("/api/ship-main")
    def ship_main_api():
        """Ship the CURRENT app's DEV -> MAIN (production) from the cockpit. Mac-only + ff-only."""
        from . import sync
        if not sync.can_promote():
            return Response("Shipping is disabled on this cockpit (read-only box).", status=403)
        app_name = request.form.get("app") or (cfg.apps[0].name if cfg.apps else "")
        if _state.get("active"):
            _state["last_msg"] = "finish the active run before shipping to production"
            return redirect("/")
        if not _state.get("shipping"):
            _state["shipping"] = True   # set BEFORE redirect so the reloaded page shows the progress bar (no race)

            def _bg():
                try:
                    r = sync.promote_app(cfg.app(app_name))
                    if r.get("ok"):
                        n = r.get("ahead_before", 0)
                        _state["last_msg"] = (f"Shipped {app_name} {r['base']}→{r['prot']} "
                                              f"({n} commit(s)) to PRODUCTION." if n else
                                              f"{app_name} already shipped — nothing ahead.")
                    else:
                        _state["last_msg"] = "Ship failed: " + (r.get("error") or "unknown")
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"Ship error: {exc}"
                finally:
                    _state["shipping"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return redirect("/")

    @app.post("/api/patrol")
    def patrol_api():
        app_name = request.form.get("app") or (cfg.apps[0].name if cfg.apps else "")
        if not _state.get("patrolling"):
            def _bg():
                _state["patrolling"] = True
                try:
                    from . import patrol as patrol_mod
                    asyncio.run(patrol_mod.patrol(cfg, app_name, do_file=True, audit=audit))
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"patrol failed: {exc}"
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
            inner = ("<p style='color:#8a909c'>No pending recommendations. When the Drillmaster or "
                     "Adjutant proposes something (after a drill or council), it lands here for your "
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

    @app.get("/needs")
    def needs_page():
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
            ".nempty{color:#56d98a;padding:30px;text-align:center;font-size:15px}</style>")
        # One-shot confirmation banner (e.g. "Answer sent to AUTO-23…") — read + clear so it shows once.
        _m = _state.pop("last_msg", "") or ""
        banner = f"<div class=nbanner>{html.escape(str(_m))}</div>" if _m else ""
        if not s["total"]:
            return _wrap("Needs you", style + banner
                         + "<div class=nempty>&#10003; All clear — nothing needs you right now.</div>")
        out = [style, banner]
        if s["decisions"]:
            out.append(f"<div class=nsec><h3>&#128172; Questions from the General · {len(s['decisions'])}</h3>")
            for d in s["decisions"]:
                tid = html.escape(str(d.get("id") or ""))
                dapp = html.escape(str(d.get("app") or ""))
                q = html.escape(str(d.get("question") or d.get("summary") or "(question)"))
                out.append(
                    f"<div class=ncard><div class=q>{q}</div><div class=meta>{tid} · {dapp}</div>"
                    "<form method=post action=/api/answer class=nrow>"
                    f"<input type=hidden name=ticket value='{tid}'><input type=hidden name=app value='{dapp}'>"
                    "<input type=text name=text placeholder='Your decision — it re-runs the ticket with this baked in'>"
                    "<button class='nbtn send'>Ship answer</button></form></div>")
            out.append("</div>")
        if s["approvals"]:
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
        if s["tasks"]:
            from urllib.parse import quote
            from . import dashboard as _dash
            out.append(f"<div class=nsec><h3>&#9888;&#65039; Runs that need you · {len(s['tasks'])}</h3>")
            for t in s["tasks"]:
                tid = html.escape(str(t.get("ticket_id") or ""))
                tapp = html.escape(str(t.get("app") or ""))
                oc = html.escape(str(t.get("outcome") or ""))
                note = html.escape(_dash._short(t.get("note") or "", 120))
                detail = _dash.needs_detail_html(t)            # the full 'what went wrong'
                prefill = quote(_dash.needs_chat_summary(t))   # pre-loaded into the General chat
                out.append(
                    "<div class=ncard><details><summary>"
                    f"<span class=meta>{tid}</span> &nbsp;{oc}"
                    + (f" <span class=muted>— {note}</span>" if note else "")
                    + f"</summary><div class=ndetail>{detail}</div></details>"
                    # Primary action: ship a real answer — resolves the decision + re-runs the ticket.
                    + "<form method=post action=/api/answer class=nrow>"
                    f"<input type=hidden name=ticket value='{tid}'><input type=hidden name=app value='{tapp}'>"
                    "<input type=text name=text placeholder='Answer the unit — your decision; it re-runs the ticket'>"
                    "<button class='nbtn send'>Ship answer</button></form>"
                    # Secondary: talk it through, or clear it.
                    + f"<div class=nrow><a class='nbtn x' href='/chat?prefill={prefill}'>Discuss with the General</a>"
                    "<form method=post action=/api/dismiss style='margin:0'>"
                    f"<input type=hidden name=ticket value='{tid}'><input type=hidden name=back value='/needs'>"
                    "<button class='nbtn x'>Dismiss</button></form></div></div>")
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
            "</style>")

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

        body = (style + budget + "<div class=ugrid>"
                + card("Today", w["today"]) + card("Last 7 days", w["week"])
                + card("Last 30 days", w["month"]) + "</div>")
        return _wrap("Token usage", body)

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
        appq = (request.args.get("app") or "").strip() or app0
        style = (
            "<style>"
            ".shp{max-width:940px}.shhead{background:#171226;border:1px solid #2c2148;border-radius:12px;"
            "padding:16px 18px;margin:4px 0 18px}.shhead h2{margin:0 0 6px;color:#e9ecf1;font-size:20px}"
            ".shhead .meta{color:#b9a6e6;font-size:13px;font-family:ui-monospace,Menlo,monospace}"
            ".shtix{margin:14px 0 6px;color:#8a929f;font-size:12px;text-transform:uppercase;letter-spacing:.07em;font-weight:700}"
            ".shcard{background:#12161f;border:1px solid #232936;border-radius:11px;padding:12px 15px;margin:9px 0}"
            ".shcard .tk{color:#e9ecf1;font-weight:700;font-size:14px}.shcard .tk a{color:#7aa2ff;text-decoration:none}"
            ".shcard .n{color:#6b7480;font-size:12px;margin-left:6px}"
            ".shcard ul{margin:8px 0 0;padding-left:0;list-style:none}"
            ".shcard li{color:#c3cad6;font-size:13px;padding:3px 0;display:flex;gap:9px}"
            ".shcard li .sha{color:#7aa2ff;font-family:ui-monospace,Menlo,monospace;white-space:nowrap}"
            ".shbar{display:flex;gap:10px;align-items:center;margin:20px 0 8px}"
            ".shgo{background:#7c3aed;border:0;color:#fff;border-radius:9px;padding:11px 18px;font-weight:700;cursor:pointer;font:inherit}"
            ".shgo:hover{background:#6d28d9}.shcancel{color:#8a929f;text-decoration:none;padding:11px 6px}"
            ".shempty{color:#56d98a;padding:30px;text-align:center;font-size:15px}</style>")
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
            jira_base = str((app_cfg.backlog or {}).get("site", "")).rstrip("/")
        except Exception:  # noqa: BLE001
            jira_base = ""
        cards = []
        for tk in tickets + (["—"] if "—" in groups else []):
            cs = groups[tk]
            label = (f'<a href="{jira_base}/browse/{tk}" target=_blank>{tk}</a>' if (tk != "—" and jira_base)
                     else (tk if tk != "—" else "No ticket"))
            lis = "".join(f'<li><span class=sha>{html.escape(c["sha"])}</span>'
                          f'<span>{html.escape(c["subject"])}</span></li>' for c in cs)
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
        # 'Discuss with the General' on /needs hands us a ready-made brief of the problem to send.
        prefill = html.escape((request.args.get("prefill") or "")[:800], quote=True)
        body = (_CHAT_STYLE + _chat_tabs("general", npend)
                + '<div class=chat><div id=cinner>' + _chat_inner(cfg) + '</div></div>'
                '<div class=composer><form method=post action=/api/chat>'
                f'<input type=text name=text autocomplete=off autofocus value="{prefill}" '
                'placeholder="Message the General…  (or reply  AUTO-1: your decision)"><button>Send</button></form></div>'
                '<script>window.scrollTo(0,document.body.scrollHeight);'
                'setInterval(async function(){try{var r=await fetch("/api/chat-thread",{cache:"no-store"});'
                'if(r.ok){var near=(window.innerHeight+window.scrollY)>=document.body.scrollHeight-140;'
                'document.getElementById("cinner").innerHTML=await r.text();'
                'if(near)window.scrollTo(0,document.body.scrollHeight);}}catch(e){}},5000);'
                '</script>')
        return _wrap("Chat with the General", body)

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
        """Ship the Commander's answer to a parked ticket straight from the Needs-you page. Resolves a
        pending decision and re-runs the ticket with the answer baked into its spec; if there's no pending
        decision on file, it records the answer as a ticket comment (the next build reads ALL comments)
        and unblocks the ticket so autopilot retries it."""
        tid = (request.form.get("ticket") or "").strip()
        app_name = (request.form.get("app") or "").strip()
        ans = (request.form.get("text") or "").strip()
        if not (tid and ans):
            return redirect("/needs")

        # Clear the Needs-you row NOW (synchronous, fast local write) so it visibly disappears on the
        # redirect, and give immediate confirmation — the actual re-run is slow, so it runs in the
        # background. If the ticket re-escalates later, a fresh row reappears.
        try:
            D.dismiss(cfg.audit_path, tid)
        except Exception:  # noqa: BLE001
            pass

        def _bg():
            try:
                from . import decisions
                if decisions.handle_reply(cfg, audit, f"{tid}: {ans}"):
                    return   # a real pending decision — resolved + re-running with the answer baked in
                # No pending decision on file: persist the answer ON the ticket + unblock for retry.
                try:
                    appcfg = cfg.app(app_name) if app_name else None
                    if appcfg and getattr(appcfg, "backlog_backend", "") == "jira":
                        from .backlog.base import make_backlog
                        from .contracts import Ticket
                        make_backlog(appcfg).add_comment(
                            Ticket(id=tid, key=tid, summary=tid, description="", app=app_name), ans)
                except Exception:  # noqa: BLE001 - a comment failure must not block the unblock
                    pass
                try:
                    from . import autopilot as _ap
                    _ap.unblock(cfg, tid)
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001 - the re-run must never break the cockpit
                pass
        threading.Thread(target=_bg, daemon=True).start()
        _state["last_msg"] = (f"✓ Answer sent to {tid} — cleared from Needs-you; the unit is "
                              "re-running it with your decision.")
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
            "<p><button>Send to the General</button></p></form>")
        return _wrap("Report a problem", form)

    @app.post("/api/report")
    def report_api():
        # Atomically claim the run: check-then-set under the lock so two near-simultaneous
        # POSTs can't both pass this guard and start two runs.
        with _run_lock:
            if _state["active"]:
                _state["last_msg"] = "a run is already in progress — stop it and wait for it to finish, then start the new one"
                return redirect("/")
            _state["active"] = True
        if not health.summary(cfg)["healthy"]:
            _state["active"] = False
            _state["last_msg"] = "blocked — fix the health problems first (see the banner)"
            return redirect("/")
        app_name = request.form.get("app") or (cfg.apps[0].name if cfg.apps else "")
        text = (request.form.get("text") or "").strip()
        import copy
        rcfg = copy.copy(cfg)        # per-run config — never mutate the shared cfg
        rcfg.dry_run = request.form.get("dryrun") == "on"   # default: live (build + merge to DEV)
        _state["dry_run"] = rcfg.dry_run
        desc = _bug_desc(cfg, text, request.files.get("screenshot"))
        title = _bug_title(text)
        try:
            worklist = intake.from_text(rcfg, app_name, title, [], description=desc)
        except Exception as exc:  # noqa: BLE001
            _state["active"] = False
            _state["last_msg"] = f"could not start: {exc}"
            return redirect("/")

        def _bg():
            _state["active"], _state["last_msg"] = True, ""
            _state["run_started"] = _state["last_activity"] = time.time()
            ev = threading.Event()
            _state["stop_event"] = ev
            try:
                asyncio.run(run_loop(rcfg, worklist, audit, stop_event=ev))
            except Exception as exc:  # noqa: BLE001
                _state["last_msg"] = str(exc)
            finally:
                _state["active"] = False
                _state["stop_event"] = None
                _state["dry_run"] = None        # clear the dry/live flag so the cockpit shows no stale tag
                _state["run_started"] = None
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
