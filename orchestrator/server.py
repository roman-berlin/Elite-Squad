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

from . import dashboard as D
from . import health
from . import intake
from . import memory
from . import warroom
from .audit import AuditLog
from .config import Config, normalize_effort
from .loop import run as run_loop

_state = {"active": False, "last_msg": "", "drilling": False, "dry_run": None,
          "last_activity": None, "run_started": None, "stop_event": None, "log_seq": 0,
          "approving": None}

# Ring buffer of the unit's stdout — fed to the War Room's "Live feed" panel so you can watch
# the implementation steps in the dashboard, not just the terminal.
import collections as _collections  # noqa: E402
_LOG: "_collections.deque[str]" = _collections.deque(maxlen=600)


def recent_log(n: int = 60) -> list[str]:
    return list(_LOG)[-n:]


def _sse(event: str, data: str) -> str:
    """Format one Server-Sent Event. Multi-line `data` is split into the required `data:` lines."""
    body = "".join("data: " + ln + "\n" for ln in data.replace("\r", "").split("\n"))
    return f"event: {event}\n{body}\n"


class _Tee:
    """Mirror stdout to the real terminal AND the ring buffer (skips the noisy poll line)."""
    def __init__(self, real):
        self._real = real

    def write(self, s: str):
        self._real.write(s)
        for line in s.splitlines():
            t = line.rstrip()
            if t and "/api/board" not in t and "GET /api/" not in t:
                _LOG.append(t)
                _state["last_activity"] = time.time()   # heartbeat — proves the unit is alive
                _state["log_seq"] = _state.get("log_seq", 0) + 1   # wake SSE streamers (real-time push)

    def flush(self):
        self._real.flush()

    def isatty(self):
        return getattr(self._real, "isatty", lambda: False)()


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


def _working(msg: str, secs: int = 5) -> str:
    """A live 'working…' panel: spinner + indeterminate progress bar + auto-refresh, so a long
    officer task (drill/council/scribe/standup) shows progress instead of a dead 'reload later'."""
    return (
        "<style>.wk{display:flex;flex-direction:column;gap:13px;align-items:flex-start;max-width:560px}"
        ".wkrow{display:flex;align-items:center;gap:12px}"
        ".spin{width:24px;height:24px;border:3px solid #232936;border-top-color:#3b6cff;border-radius:50%;"
        "animation:sp .9s linear infinite;flex:none}@keyframes sp{to{transform:rotate(360deg)}}"
        ".wkmsg{font-size:15px}.wkhint{color:#8a909c;font-size:12px}"
        ".wkbar{width:100%;height:6px;background:#1a1f29;border-radius:99px;overflow:hidden}"
        ".wkfill{width:36%;height:100%;background:linear-gradient(90deg,#2b5cff,#6aa9ff);border-radius:99px;"
        "animation:wksl 1.5s ease-in-out infinite}"
        "@keyframes wksl{0%{margin-left:-36%}100%{margin-left:100%}}</style>"
        f"<div class=wk><div class=wkrow><div class=spin></div><div class=wkmsg>{html.escape(msg)}</div></div>"
        "<div class=wkbar><div class=wkfill></div></div>"
        "<div class=wkhint>Working… this page refreshes itself — no need to reload.</div></div>"
        f"<script>setTimeout(function(){{location.reload()}},{secs * 1000})</script>")


def _charged() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _actbtn(action: str, label: str, app: str = "", confirm: str = "") -> str:
    """A single on-page officer-action button (POST form). These actions used to live in the
    'Unit' toolbar menu; they now sit on the page that shows their result, so each activity
    appears exactly once."""
    hidden = f'<input type=hidden name=app value="{html.escape(app)}">' if app else ""
    onsub = f" onsubmit=\"return confirm('{confirm}')\"" if confirm else ""
    return (f"<form method=post action={action} style='margin:0'{onsub}>{hidden}"
            f"<button class=actbtn>{label}</button></form>")


def _actbar(*items: str) -> str:
    """A row of on-page officer-action controls."""
    return ("<style>.actbar{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 18px}"
            ".actbtn{background:#161b25;border:1px solid #2a3343;color:#e9ecf1;border-radius:9px;"
            "padding:9px 14px;font:inherit;font-size:14px;font-weight:600;cursor:pointer;"
            "text-decoration:none;display:inline-block}.actbtn:hover{background:#1b2230}</style>"
            "<div class=actbar>" + "".join(items) + "</div>")


def _bug_title(text: str) -> str:
    return (text.splitlines()[0][:60] if text.strip() else "QA bug report")


def _bug_desc(cfg: Config, text: str, screenshot=None) -> str:
    """Frame a bug report for the unit (and save an optional screenshot beside the audit log).
    Single source of truth for both the '+ New task → Bug' panel and the legacy /report form."""
    desc = f"Fix this problem found during QA on DEV:\n{text or '(no description)'}"
    if screenshot is not None and getattr(screenshot, "filename", ""):
        import re
        import time as _t
        qa = Path(cfg.audit_path).resolve().parent / "qa-reports"
        qa.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", screenshot.filename)
        path = qa / f"{int(_t.time())}-{safe}"
        screenshot.save(str(path))
        desc += f"\n\nScreenshot of the problem (open and view it to understand the bug): {path}"
    return desc


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

    try:
        from . import needs
        _nneeds = needs.count(cfg)
    except Exception:  # noqa: BLE001
        _nneeds = 0
    needs_badge = f'<span class=cbadge>{_nneeds}</span>' if _nneeds else ""

    # Deploy DEV -> main — only on a cockpit allowed to push (the Mac, via GENERAL_COCKPIT_PROMOTE).
    # Shows how far DEV is ahead of main = approved changes not yet on the 24/7 server.
    promote_html = ""
    try:
        from . import sync as _sync
        if _sync.can_promote():
            _ahead = _sync.promote_status(cfg).get("ahead", 0)
            if _ahead:
                promote_html = (
                    '<form method=post action=/api/promote class=tbf '
                    f'''onsubmit="return confirm('Deploy {_ahead} commit(s) DEV \\u2192 main? The 24/7 server self-updates within ~15 min.')">'''
                    f'<button class="btn deploy" {busy("promoting")}>&#9650; Deploy'
                    f'<span class=cbadge>{_ahead}</span> &rarr; main</button></form>')
            else:
                promote_html = '<span class="tbnote ok" title="DEV and main are in sync — nothing to deploy">&#10003; deployed</span>'
    except Exception:  # noqa: BLE001
        promote_html = ""

    # Freshness — show "· 28m ago" next to each Reports item so staleness is visible at a glance.
    from . import warroom as _wr
    _base = Path(cfg.audit_path)

    def _fresh(dt) -> str:
        return f' <span class=mfresh>· {html.escape(_wr._rel(dt))}</span>' if dt else ""

    try:
        _tk = D.load_tasks(cfg.audit_path)
        _tkdt = (_tk[0].get("ended") or _tk[0].get("started")) if _tk else None
    except Exception:  # noqa: BLE001
        _tkdt = None
    fr_tasks = _fresh(_tkdt)
    fr_council = _fresh(_wr._last_council(cfg))
    fr_standup = _fresh(_wr._mtime(_base.with_name("last-standup.md")))
    fr_drill = _fresh(_wr._mtime(_base.with_name("drill-report.md")))
    try:
        from . import memory as _mem
        fr_mem = _fresh(_wr._mtime(_mem.UNIT_PATH))
    except Exception:  # noqa: BLE001
        fr_mem = ""

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
.tbar .tbnote{{font-size:12px;margin-left:2px}}.tbar .tbnote.run{{color:#f7b955}}.tbar .tbnote.bad{{color:#f0676b}}.tbar .tbnote.ok{{color:#52b788;font-weight:600}}
.tbar .btn.deploy{{background:#1f7a45;border-color:#2c9a5f;color:#fff}}.tbar .btn.deploy:hover{{background:#1a6b3c}}.tbar .btn.deploy .cbadge{{background:#0c3a22}}
.tbar .grow{{flex:1}}
.tbar .chatbtn{{display:inline-flex;align-items:center;gap:6px}}
.tbar .cbadge{{background:#f0676b;color:#fff;font-size:10px;font-weight:800;border-radius:99px;padding:1px 6px}}
.tbar form.tbf{{margin:0;display:inline-flex}}
.tbar .btn:disabled{{opacity:.5;cursor:not-allowed}}
.tbar .panel a{{display:flex;align-items:center}}
.tbar .panel .mfresh{{margin-left:auto;padding-left:14px;color:#5c6573;font-size:11px;font-weight:400}}
@media(max-width:820px){{
.tbar{{gap:7px;padding:10px 14px}}
.tbar .btn{{padding:8px 10px;font-size:12px}}
.tbar .tbnote{{order:99;flex-basis:100%;margin:4px 0 0}}
.tbar .panel.form{{min-width:0;width:min(320px,92vw)}}
}}
</style>
<div class=tbar>
  <a class="btn primary" href="/tickets?app={html.escape(app0)}">&#127915; Choose a ticket</a>

  <details class=menu>
    <summary class=btn>&#43; New task</summary>
    <div class="panel form">
      <form method=post action=/api/run enctype=multipart/form-data onsubmit="return !this.live.checked||confirm('Run LIVE — build and MERGE to DEV. Continue?')">
        <input type=hidden name=kind value=task>
        <div class=row style="gap:16px;margin-bottom:3px">
          <label style="display:flex;gap:6px;align-items:center;font-size:13px;color:#c4c9d2;cursor:pointer"><input type=radio name=type value=feature checked> &#10024; Feature</label>
          <label style="display:flex;gap:6px;align-items:center;font-size:13px;color:#c4c9d2;cursor:pointer"><input type=radio name=type value=bug> &#128030; Bug</label>
        </div>
        <input type=text name=text placeholder="Describe the feature — or the bug: where, what you saw, expected">
        <label style="font-size:12px;color:#8a909c;display:block;margin:3px 0 0">Screenshot <span style="color:#5c6573">(optional, for bugs)</span><input type=file name=screenshot accept="image/*" style="display:block;margin-top:3px;font-size:12px"></label>
        <div class=row>
          <select name=app title=project style="flex:1">{apps}</select>
          <select name=effort title=effort style="flex:1"><option value=''>effort: auto</option>{effort}</select>
        </div>
        <label style="font-size:13px;color:#c4c9d2"><input type=checkbox name=live> live (build + merge to DEV)</label>
        <button {run_dis}>&#9654; Run</button>
      </form>
    </div>
  </details>

  <form method=post action=/api/patrol class=tbf onsubmit="return confirm('Run a patrol? Scout + Provost + Quartermaster will inspect DEV and FILE findings as Jira tickets assigned to you.')"><input type=hidden name=app value="{html.escape(app0)}"><button class=btn {busy('patrolling')}>&#128225; Patrol</button></form>
  <form method=post action=/api/ship-review class=tbf><input type=hidden name=app value="{html.escape(app0)}"><button class=btn {busy('shipreview')}>&#128640; Ship review</button></form>
  <a class="btn chatbtn" href="/needs">&#128276; Needs you{needs_badge}</a>
  {promote_html}

  <details class=menu>
    <summary class=btn>&#128202; Reports</summary>
    <div class="panel right">
      <a href="/tasks">&#128203; Task log{fr_tasks}</a>
      <a href="/council">&#128172; Daily muster &amp; meetings{fr_council}</a>
      <a href="/memory">&#128221; Unit memory{fr_mem}</a>
      <a href="/drill">&#127894; Last drill{fr_drill}</a>
    </div>
  </details>

  <span class=grow></span>
  {status}
</div>"""


def _chat_bubbles(notes: str) -> list[tuple[str, str]]:
    """Parse the commander-notes log ('Q: …' / 'A (General): …') into chat bubbles."""
    out: list[tuple[str, str]] = []
    for raw in (notes or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("Q:"):
            out.append(("you", s[2:].strip()))
        elif "A (General):" in s:
            out.append(("unit", s.split("A (General):", 1)[1].strip()))
        elif out:
            out[-1] = (out[-1][0], (out[-1][1] + " " + s).strip())
        else:
            out.append(("unit", s))
    return out


def _chat_inner(cfg: Config) -> str:
    from . import council, decisions
    try:
        pend = decisions.load(cfg)
    except Exception:  # noqa: BLE001
        pend = []
    cards = ""
    for d in pend:
        tid = html.escape(str(d.get("id", "")))
        q = html.escape((d.get("question") or d.get("summary") or "").strip())[:1600]
        cards += (f'<div class=pcard><div class=ph2>&#128681; {tid} · the unit needs your call</div>'
                  f'<div class=pq>{q}</div>'
                  '<form class=preply method=post action=/api/chat>'
                  f'<input type=hidden name=ticket value="{tid}">'
                  f'<input type=text name=text placeholder="your decision for {tid}…" autocomplete=off>'
                  '<button>Send</button></form></div>')
    pending_html = f'<div class=pending>{cards}</div>' if cards else ""
    try:
        notes = council.recent_commander_notes(cfg, lines=240)
    except Exception:  # noqa: BLE001
        notes = ""
    bubbles = ""
    for who, text in _chat_bubbles(notes):
        label = "You" if who == "you" else "The General"
        bubbles += (f'<div class="msg {who}"><div class=who>{label}</div>'
                    f'<div class=bub>{html.escape(text)}</div></div>')
    if not bubbles and not cards:
        bubbles = ('<div class=cempty>No messages yet. When an officer needs a decision it shows '
                   'up here — or send the General a message below.</div>')
    return pending_html + f'<div class=thread>{bubbles}</div>'


_CHAT_STYLE = ("<style>"
               ".chat{max-width:780px;margin:0 auto}"
               ".ctabs{max-width:780px;margin:0 auto 14px;display:flex;gap:6px;border-bottom:1px solid #1e222b}"
               ".ctab{padding:9px 14px;color:#8a929f;font-size:13px;font-weight:600;border-bottom:2px solid transparent;text-decoration:none}"
               ".ctab.on{color:#e9ecf1;border-bottom-color:#3b6cff}.ctab:hover{color:#e9ecf1}"
               ".cbadge{background:#f0676b;color:#fff;font-size:10px;font-weight:800;border-radius:99px;padding:1px 6px;margin-left:5px}"
               ".aim{max-width:780px;margin:0 auto 10px;color:#9be7bd;font-size:13px}.aim a{color:#6aa9ff}"
               ".pcard{background:#1a160f;border:1px solid #3a2f12;border-radius:14px;padding:14px 16px;margin-bottom:12px}"
               ".pcard .ph2{color:#f7b955;font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:7px}"
               ".pcard .pq{color:#e9ecf1;font-size:13px;white-space:pre-wrap;max-height:260px;overflow:auto;font-family:ui-monospace,Menlo,monospace;line-height:1.5}"
               ".preply{display:flex;gap:8px;margin-top:11px}.preply input{flex:1}"
               ".thread{display:flex;flex-direction:column;gap:9px;margin:16px 0 96px}"
               ".msg{display:flex;flex-direction:column;max-width:80%}"
               ".msg.you{align-self:flex-end;align-items:flex-end}.msg.unit{align-self:flex-start}"
               ".who{font-size:10px;color:#5c6573;margin:0 6px 2px}"
               ".bub{padding:9px 13px;border-radius:14px;font-size:13px;line-height:1.5;white-space:pre-wrap}"
               ".msg.unit .bub{background:#161b25;border:1px solid #232b38;border-bottom-left-radius:4px}"
               ".msg.you .bub{background:#1e3a5f;border-bottom-right-radius:4px;color:#eaf1fb}"
               ".cempty{color:#8a929f;padding:30px 8px;text-align:center;font-size:13px}"
               ".composer{position:fixed;bottom:0;left:0;right:0;background:#0d0f14;border-top:1px solid #1e222b;padding:12px 30px}"
               ".composer form{max-width:780px;margin:0 auto;display:flex;gap:8px}.composer input{flex:1}"
               "</style>")


def _chat_tabs(active: str, npend: int = 0) -> str:
    badge = f'<span class=cbadge>{npend}</span>' if npend else ""
    g = "on" if active == "general" else ""
    gr = "on" if active == "group" else ""
    return (f'<div class=ctabs><a class="ctab {g}" href="/chat">&#128172; The General{badge}</a>'
            f'<a class="ctab {gr}" href="/group">&#128101; Group room</a></div>')


def _group_inner(cfg: Config) -> str:
    from . import council
    msgs = council.group_messages(cfg, limit=200)
    if not msgs:
        return ('<div class=cempty>No messages yet. Ask the unit anything — the relevant officers '
                'weigh in, others can add a comment. (The General is your 1:1 chat.)</div>')
    out = ""
    for who, text in msgs:
        side = "you" if who == "you" else "unit"
        label = "You" if who == "you" else html.escape(who)
        out += (f'<div class="msg {side}"><div class=who>{label}</div>'
                f'<div class=bub>{html.escape(text)}</div></div>')
    return f'<div class=thread>{out}</div>'


def create_app(cfg: Config):
    from flask import Flask, redirect, request
    app = Flask(__name__)
    audit = AuditLog(cfg.audit_path)

    @app.get("/")
    def index():
        appq = request.args.get("app")
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
        return page.replace("</header>", "</header>" + _control_bar(cfg), 1)

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
                  'onsubmit="return !this.live.checked||confirm(\'Run LIVE — build and MERGE to DEV. Continue?\')">'
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
        _state["dry_run"] = rcfg.dry_run
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
        ttype = (request.form.get("type") or "feature").strip()
        effort = request.form.get("effort") or None
        import copy
        rcfg = copy.copy(cfg)        # per-run config — never mutate the shared cfg
        rcfg.dry_run = request.form.get("live") != "on"
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
        top = (_working("The Scribe is folding recent lessons into Unit Memory…")
               if _state.get("scribing") else "")
        act = "" if _state.get("scribing") else _actbar(_actbtn("/api/scribe", "&#128221; Update memory"))
        body = act + top + "<pre class=rep>" + html.escape(memory.load() or "(no Unit Memory yet)") + "</pre>"
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
            def _bg():
                _state["promoting"] = True
                try:
                    r = sync.promote(cfg)
                    if r.get("ok"):
                        n = r.get("ahead_before", 0)
                        _state["last_msg"] = f"Deployed {n} commit(s) DEV → main — the server self-updates within ~15 min."
                    else:
                        _state["last_msg"] = "Deploy failed: " + (r.get("error") or "unknown")
                except Exception as exc:  # noqa: BLE001
                    _state["last_msg"] = f"Deploy error: {exc}"
                finally:
                    _state["promoting"] = False
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
            ".nempty{color:#56d98a;padding:30px;text-align:center;font-size:15px}</style>")
        if not s["total"]:
            return _wrap("Needs you", style
                         + "<div class=nempty>&#10003; All clear — nothing needs you right now.</div>")
        out = [style]
        if s["decisions"]:
            out.append(f"<div class=nsec><h3>&#128172; Questions from the General · {len(s['decisions'])}</h3>")
            for d in s["decisions"]:
                tid = html.escape(str(d.get("id") or ""))
                q = html.escape(str(d.get("question") or d.get("summary") or "(question)"))
                out.append(
                    f"<div class=ncard><div class=q>{q}</div><div class=meta>{tid} · {html.escape(str(d.get('app') or ''))}</div>"
                    "<form method=post action=/api/chat class=nrow>"
                    f"<input type=hidden name=ticket value='{tid}'>"
                    "<input type=text name=text placeholder='Your decision — e.g. use DD/MM'>"
                    "<button class='nbtn send'>Reply</button></form></div>")
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
            out.append(f"<div class=nsec><h3>&#9888;&#65039; Runs that need you · {len(s['tasks'])}</h3>")
            for t in s["tasks"]:
                tid = html.escape(str(t.get("ticket_id") or ""))
                oc = html.escape(str(t.get("outcome") or ""))
                note = html.escape(str(t.get("note") or "")[:140])
                out.append(
                    f"<div class=ncard><div class=q><span class=meta>{tid}</span> &nbsp;{oc}</div>"
                    + (f"<div class=meta>{note}</div>" if note else "")
                    + "<div class=nrow><a class='nbtn send' href='/chat'>Discuss with the General</a>"
                    "<form method=post action=/api/dismiss style='margin:0'>"
                    f"<input type=hidden name=ticket value='{tid}'><input type=hidden name=back value='/needs'>"
                    "<button class='nbtn x'>Dismiss</button></form></div></div>")
            out.append("</div>")
        return _wrap("Needs you", "".join(out))

    @app.get("/chat")
    def chat_page():
        try:
            from . import decisions
            npend = len(decisions.load(cfg))
        except Exception:  # noqa: BLE001
            npend = 0
        body = (_CHAT_STYLE + _chat_tabs("general", npend)
                + '<div class=chat><div id=cinner>' + _chat_inner(cfg) + '</div></div>'
                '<div class=composer><form method=post action=/api/chat>'
                '<input type=text name=text autocomplete=off autofocus '
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
        _state["dry_run"] = rcfg.dry_run
        desc = _bug_desc(cfg, text, request.files.get("screenshot"))
        title = _bug_title(text)
        try:
            worklist = intake.from_text(rcfg, app_name, title, [], description=desc)
        except Exception as exc:  # noqa: BLE001
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
