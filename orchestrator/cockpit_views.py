"""Cockpit templates / view helpers — the inline-HTML builders.

Split out of ``server.py`` (F16: decompose templates/routes/state) so the presentation
layer (page chrome, the control bar, the chat/group renderers, action buttons) lives
apart from the Flask route handlers. These functions take plain data + ``Config`` and
return HTML strings — they never touch ``request``/``redirect``. ``server`` re-exports
them, so ``server._control_bar`` / ``server._wrap`` etc. stay valid for callers and tests.
"""
from __future__ import annotations

import html
import os
from pathlib import Path

from . import dashboard as D
from .cockpit_state import _state
from .config import Config


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

    # ── Two DIFFERENT repos, two DIFFERENT promotions — kept visually distinct so they can't be
    # confused. (A) "Update unit": THE GENERAL'S OWN code (this tool) dev->main -> the 24/7 VPS
    # self-updates. (B) "Ship <app>": your PRODUCT (e.g. Automatixy) DEV->MAIN -> live production.
    # Both only on a cockpit allowed to push (the Mac, via GENERAL_COCKPIT_PROMOTE).
    promote_html = ""
    try:
        from . import sync as _sync
        if _sync.can_promote():
            _ahead = _sync.promote_status(cfg).get("ahead", 0)
            if _ahead:
                _pc = (f"Update THE UNIT itself — promote the CTO (this tool\\u2019s own code, the "
                       f"~/Projects/General repo) dev \\u2192 main, {_ahead} commit(s). The 24/7 server "
                       f"self-updates within ~15 min. This is the unit\\u2019s brain, NOT your app.")
                promote_html = (
                    '<span class=tbdiv></span>'
                    '<form method=post action=/api/promote class=tbf '
                    f'''onsubmit="return confirm('{_pc}')">'''
                    f'<button class="btn deploy" title="Promote the CTO — this tool&#39;s OWN code — '
                    f'dev&#8594;main. The VPS self-updates. NOT your app." {busy("promoting")}>'
                    f'&#9881;&#65039; Update unit<span class=cbadge>{_ahead}</span></button></form>')
            else:
                promote_html = ('<span class=tbdiv></span><span class="tbnote ok" '
                                'title="The CTO (the unit\'s own code) is in sync with the server">'
                                '&#10003; unit current</span>')
    except Exception:  # noqa: BLE001
        promote_html = ""

    # (B) Ship the CURRENT app DEV -> MAIN (production). Names the app + says PRODUCTION so it's never
    # mistaken for the unit self-deploy above.
    ship_html = ""
    try:
        from . import sync as _sync
        # An app whose repo IS the General's OWN repo (e.g. the 'Elite-Unit' app, added so the unit can
        # work its own EU tickets) is promoted via "Update unit" — NOT shipped as a product. Suppress its
        # Ship button so there's no duplicate/ambiguous "ship the unit" path next to Update-unit.
        _is_unit_repo = False
        if app0:
            try:
                _is_unit_repo = Path(cfg.app(app0).repo_path).resolve() == _sync._repo_root(cfg)
            except Exception:  # noqa: BLE001
                _is_unit_repo = False
        if _sync.can_promote() and app0 and not _is_unit_repo:
            _sa = _sync.app_promote_status(cfg.app(app0))
            _sn = _sa.get("ahead", 0)
            if _sn:
                # The button now OPENS A REVIEW PAGE (commits + their tickets) instead of shipping on
                # the spot — you see exactly what's going to production, then confirm there.
                ship_html = (
                    f'<a class="btn ship" href="/ship-preview?app={html.escape(app0)}" '
                    f'title="Review the {html.escape(app0)} commits + tickets, then ship to production">'
                    f'&#128640; Ship {html.escape(app0)} &rarr; production<span class=cbadge>{_sn}</span></a>')
            else:
                # Nothing ahead — DEV is fully merged into production. Show it explicitly (don't just hide
                # the button) so "all shipped" is unmistakable after a merge.
                ship_html = ('<span class="tbnote ok" '
                             f'title="{html.escape(app0)} DEV is fully merged into production — nothing to ship">'
                             f'&#10003; {html.escape(app0)} shipped</span>')
    except Exception:  # noqa: BLE001
        ship_html = ""

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
    try:
        from . import usage as _usg
        _u = _usg.today_tokens(cfg)
        fr_usage = f" · {_u // 1000}k today" if _u >= 1000 else (f" · {_u} today" if _u else "")
    except Exception:  # noqa: BLE001
        fr_usage = ""
    try:
        from . import forensics as _fx
        _nf = sum(t["count"] for t in _fx.taxonomy(cfg))
        fr_fx = f" · {_nf}" if _nf else ""
    except Exception:  # noqa: BLE001
        fr_fx = ""

    # Deploy progress: while a unit-promote or app-ship runs in the background, show a live bar that
    # polls /api/deploy-status and reloads when it finishes — so the button never looks dead (the push
    # to GitHub can take 10-30s). One strip covers BOTH deploy buttons.
    deploy_strip = ""
    if _state.get("promoting") or _state.get("shipping"):
        dlabel = (f"Shipping {html.escape(app0)} &rarr; production…" if _state.get("shipping")
                  else "Deploying the unit (dev &rarr; main)…")
        deploy_strip = (
            "<div class=deploybar><div class=dspin></div>"
            f"<div class=dmsg>{dlabel} <span class=dsub>pushing to GitHub — the server self-updates "
            "after. Leave this open; it clears itself when done.</span></div>"
            "<div class=dprog><span class=dprogfill></span></div></div>"
            "<script>(function(){function p(){fetch('/api/deploy-status')"
            ".then(function(r){return r.json()}).then(function(d){"
            "if(!d.active){location.reload()}else{setTimeout(p,1500)}})"
            ".catch(function(){setTimeout(p,2500)})}setTimeout(p,1500)})();</script>")

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
.tbar .btn.ship{{background:#7c3aed;border-color:#8b5cf6;color:#fff}}.tbar .btn.ship:hover{{background:#6d28d9}}.tbar .btn.ship .cbadge{{background:#3b1d7a}}
.tbar .tbdiv{{width:1px;height:22px;background:#2a3343;margin:0 7px;align-self:center;display:inline-block}}
.tbar .grow{{flex:1}}
.tbar .chatbtn{{display:inline-flex;align-items:center;gap:6px}}
.tbar .cbadge{{background:#f0676b;color:#fff;font-size:10px;font-weight:800;border-radius:99px;padding:1px 6px}}
.tbar form.tbf{{margin:0;display:inline-flex}}
.tbar .btn:disabled{{opacity:.5;cursor:not-allowed}}
.tbar .panel a{{display:flex;align-items:center}}
.tbar .panel .mfresh{{margin-left:auto;padding-left:14px;color:#5c6573;font-size:11px;font-weight:400}}
.deploybar{{display:flex;align-items:center;gap:13px;padding:11px 26px;background:#0f1626;border-bottom:1px solid #20304d}}
.deploybar .dspin{{width:18px;height:18px;border:3px solid #21314f;border-top-color:#3b6cff;border-radius:50%;animation:dsp .9s linear infinite;flex:none}}
.deploybar .dmsg{{color:#cfe0ff;font-size:13px;font-weight:650}}
.deploybar .dsub{{color:#7f8ba3;font-weight:400;font-size:12px}}
.deploybar .dprog{{flex:1;max-width:300px;height:6px;background:#0c1119;border-radius:99px;overflow:hidden;border:1px solid #21314f}}
.deploybar .dprogfill{{display:block;width:38%;height:100%;background:linear-gradient(90deg,#2b5cff,#6aa9ff);border-radius:99px;animation:dsl 1.4s ease-in-out infinite}}
@keyframes dsp{{to{{transform:rotate(360deg)}}}}
@keyframes dsl{{0%{{margin-left:-38%}}100%{{margin-left:100%}}}}
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
      <form method=post action=/api/run enctype=multipart/form-data onsubmit="return this.dryrun.checked||confirm('Build and merge to DEV. Continue?')">
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
        <label style="font-size:13px;color:#c4c9d2"><input type=checkbox name=dryrun> dry run (build only — no merge)</label>
        <button {run_dis}>&#9654; Run</button>
      </form>
    </div>
  </details>

  <form method=post action=/api/patrol class=tbf onsubmit="return confirm('Run a patrol? QA Engineer + Security Engineer + Release Manager will inspect DEV and FILE findings as Jira tickets assigned to you.')"><input type=hidden name=app value="{html.escape(app0)}"><button class=btn {busy('patrolling')}>&#128225; Patrol</button></form>
  <form method=post action=/api/ship-review class=tbf><input type=hidden name=app value="{html.escape(app0)}"><button class=btn {busy('shipreview')}>&#128640; Ship review</button></form>
  <a class="btn" href="/jira?app={html.escape(app0)}" title="Pick or connect the Jira this project uses">&#128268; Jira</a>
  <a class="btn" href="/onboard" title="Scaffold a new product into the unit (config + Jira)">&#10133; Product</a>
  <a class="btn chatbtn" href="/needs">&#128276; Needs you{needs_badge}</a>
  {promote_html}
  {ship_html}

  <details class=menu>
    <summary class=btn>&#128202; Reports</summary>
    <div class="panel right">
      <a href="/tasks">&#128203; Task log{fr_tasks}</a>
      <a href="/council">&#128172; Daily muster &amp; meetings{fr_council}</a>
      <a href="/memory">&#128221; Unit memory{fr_mem}</a>
      <a href="/usage">&#128202; Token usage{fr_usage}</a>
      <a href="/forensics">&#129513; Failure forensics{fr_fx}</a>
      <a href="/roster-doc">&#128101; Unit roster</a>
      <a href="/drill">&#127894; Last drill{fr_drill}</a>
    </div>
  </details>

  <span class=grow></span>
  {status}
</div>
{deploy_strip}"""


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
        notes = council.chat_transcript(cfg, lines=400)
    except Exception:  # noqa: BLE001
        notes = ""
    bubbles = ""
    for who, text in _chat_bubbles(notes):
        label = "You" if who == "you" else "CTO"
        bubbles += (f'<div class="msg {who}"><div class=who>{label}</div>'
                    f'<div class=bub>{html.escape(text)}</div></div>')
    if not bubbles and not cards:
        bubbles = ('<div class=cempty>No messages yet. When an officer needs a decision it shows '
                   'up here — or send the CTO a message below.</div>')
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
    return (f'<div class=ctabs><a class="ctab {g}" href="/chat">&#128172; CTO{badge}</a>'
            f'<a class="ctab {gr}" href="/group">&#128101; Group room</a></div>')


def _group_inner(cfg: Config) -> str:
    from . import council
    msgs = council.group_messages(cfg, limit=200)
    if not msgs:
        return ('<div class=cempty>No messages yet. Ask the unit anything — the relevant officers '
                'weigh in, others can add a comment. (The CTO is your 1:1 chat.)</div>')
    out = ""
    for who, text in msgs:
        side = "you" if who == "you" else "unit"
        label = "You" if who == "you" else html.escape(who)
        out += (f'<div class="msg {side}"><div class=who>{label}</div>'
                f'<div class=bub>{html.escape(text)}</div></div>')
    return f'<div class=thread>{out}</div>'
