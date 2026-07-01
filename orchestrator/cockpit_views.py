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
from urllib.parse import quote

from . import dashboard as D
from .cockpit_state import _state, get_autopilot_status
from .config import Config

# ── DESIGN TOKENS (EU-39) ─────────────────────────────────────────────────────
# Slice 1 made the War Room's ``:root{…}`` block the single source of truth for the
# cockpit's palette / radius / elevation / focus-ring. The standalone pages built
# here (``_wrap`` chrome → forensics, chat, ship-preview, …) live in their OWN HTML
# documents and never see that block, so they must inject it too. ``_token_css``
# pulls it LIVE out of ``warroom._PAGE`` — re-skin there and every page follows — and
# falls back to a bundled copy when that block can't be read (tests / offline preview).
_TOKENS_FALLBACK = (
    ":root{color-scheme:dark;"
    "--bg:#080a0f;--panel:#0f141d;--panel2:#141a25;--line:#1b2230;--line2:#283342;"
    "--ink:#e7ebf2;--dim:#7e8795;--faint:#515a67;"
    "--ok:#34d399;--okbg:#0e2a1e;--okline:#1c5238;"
    "--warn:#f5b34a;--warnbg:#2c2410;--warnline:#5a4a1c;"
    "--bad:#f0676b;--badbg:#2a1417;--badline:#5a1f22;"
    "--info:#6aa9ff;--infobg:#0a1f2e;--infoline:#1a3a5c;"
    "--accent:#4d7cff;--accentbg:#0f1c30;--accentline:#1e3457;"
    "--mono:ui-monospace,\"SF Mono\",Menlo,Consolas,monospace;"
    "--r-sm:6px;--r-md:9px;--r-lg:13px;--r-xl:14px;--r-pill:999px;"
    "--shadow-1:0 1px 2px rgba(0,0,0,.35);--shadow-2:0 8px 24px rgba(0,0,0,.45);"
    "--shadow-3:0 16px 40px rgba(0,0,0,.55);"
    "--ring:0 0 0 2px var(--bg),0 0 0 4px rgba(77,124,255,.6);--t-fast:.15s ease}")


def _token_css() -> str:
    """The slice-1 design tokens as a ``<style>:root{…}</style>`` block, so every standalone
    cockpit page shares ONE palette source with the War Room (EU-39). Read live from
    ``warroom._PAGE``; falls back to ``_TOKENS_FALLBACK`` when unavailable."""
    try:
        import re

        from . import warroom
        m = re.search(r":root\{[^}]*\}", warroom._PAGE)
        if m:
            return "<style>" + m.group(0) + "</style>"
    except Exception:  # noqa: BLE001 - tests / preview render without the War Room module loaded
        pass
    return "<style>" + _TOKENS_FALLBACK + "</style>"


def _back_home() -> str:
    """The cockpit URL to return to — carries the active ?app= so 'back to cockpit' lands on the project
    you were in, not 'All projects'. Reads it from the request when there is one; falls back to '/'."""
    try:
        from flask import request
        appq = (request.args.get("app") or "").strip()
    except Exception:  # noqa: BLE001 - rendered outside a request context (tests / previews)
        appq = ""
    return f"/?app={html.escape(appq)}" if appq else "/"


def _wrap(title: str, inner: str) -> str:
    return ("<!doctype html><meta charset=utf-8><title>" + html.escape(title) + "</title>"
            + _token_css() +
            "<style>*{box-sizing:border-box}"
            "body{background:radial-gradient(1100px 440px at 80% -10%,rgba(77,124,255,.08),transparent 60%),"
            "var(--bg);color:var(--ink);font:14px/1.6 -apple-system,BlinkMacSystemFont,"
            "\"Segoe UI\",Inter,sans-serif;margin:0;padding:22px 30px}a{color:var(--info)}"
            ".rep{white-space:pre-wrap;background:var(--panel);border:1px solid var(--line);"
            "border-radius:var(--r-lg);padding:16px}"
            "textarea,select,input{background:var(--panel);border:1px solid var(--line2);color:var(--ink);"
            "border-radius:var(--r-md);padding:8px;font:inherit}"
            "button{background:var(--accent);border:0;color:#fff;border-radius:var(--r-md);padding:9px 16px;"
            "font-weight:650;cursor:pointer}"
            "a:focus-visible,button:focus-visible,select:focus-visible,textarea:focus-visible,"
            "input:focus-visible{outline:none;box-shadow:var(--ring)}"
            ".backbtn{display:inline-flex;align-items:center;gap:10px;padding:12px 18px;"
            "background:var(--panel2);border:1px solid var(--line);border-radius:var(--r-md);"
            "color:var(--ink);font-size:14px;font-weight:600;text-decoration:none;"
            "transition:all var(--t-fast);margin-bottom:16px;box-shadow:var(--shadow-1)}"
            ".backbtn svg{width:18px;height:18px;transition:transform var(--t-fast);flex:none}"
            ".backbtn:hover{background:var(--line);border-color:var(--accent);color:var(--accent);"
            "transform:translateX(-3px);box-shadow:var(--shadow-2)}"
            ".backbtn:hover svg{transform:translateX(-2px)}"
            ".backbtn:active{transform:translateX(-1px)}"
            ".backbtn:focus-visible{outline:none;box-shadow:var(--ring)}</style>"
            f"<a class='backbtn' href='{_back_home()}' aria-label='Back to cockpit'>"
            f"<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'>"
            f"<path d='M19 12H5M12 19l-7-7 7-7'/></svg>cockpit</a> "
            f"<h2>{html.escape(title)}</h2>{inner}")


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
            ".actbtn{background:var(--panel2);border:1px solid var(--line2);color:var(--ink);"
            "border-radius:var(--r-md);padding:9px 14px;font:inherit;font-size:14px;font-weight:600;"
            "cursor:pointer;text-decoration:none;display:inline-block;transition:background var(--t-fast)}"
            ".actbtn:hover{background:var(--line)}"
            ".actbtn:focus-visible{outline:none;box-shadow:var(--ring)}</style>"
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


def _result_banner(state: dict) -> str:
    """One-shot read-and-clear result banner for the side-effectful / actions (ship/promote/patrol).

    Mirrors the /memory banner: the outcome of a ship/promote/patrol is shown ONCE on the next load
    of /, then cleared — unlike the sticky shared ``last_msg`` rendered as a control-bar note, which
    would otherwise persist across unrelated later actions. Pops ``last_result`` so a subsequent
    reload (with no new action) no longer shows it."""
    msg = (state.pop("last_result", "") or "").strip()
    if not msg:
        return ""
    bad = any(w in msg.lower() for w in ("fail", "error"))
    fg, border, bg = (("#f0676b", "#5a1f22", "#2a1417") if bad
                      else ("#7fe3a6", "#1c5238", "#10371f"))
    return (f"<div style='background:{bg};border-bottom:1px solid {border};color:{fg};"
            f"padding:11px 26px;font-size:13.5px;font-weight:600'>{html.escape(msg)}</div>")


def _plan_limit_banner(state: dict) -> str:
    """Plan-limit warning banner: shown when a Claude plan limit is hit.

    Prominent red banner that persists until the limit resets (not a one-shot like
    ``_result_banner``). Displays which limit was hit and when it resets.
    """
    from . import cockpit_state as _cs
    from . import usage as _usg

    # Check if plan limit is hit in the state
    if not state.get("plan_limit_hit"):
        return ""

    # Get reset timestamp - if not set, check usage data
    reset_at = state.get("plan_limit_reset_at")
    if not reset_at:
        try:
            usage_data = _usg.plan_usage(force=True)
            if usage_data and usage_data.get("limits"):
                for limit in usage_data.get("limits", []):
                    if float(limit.get("utilization", 0.0)) >= 1.0:
                        reset_at = limit.get("resets_at")
                        if reset_at:
                            # Store in state for next time
                            state["plan_limit_reset_at"] = reset_at
                        break
        except Exception:  # noqa: BLE001 - banner must never break the cockpit
            pass

    # Format reset time
    reset_text = "unknown time"
    if reset_at:
        try:
            import datetime
            reset_dt = datetime.datetime.fromtimestamp(reset_at, tz=datetime.timezone.utc)
            # Format like "Mon Jun 30 14:30 UTC"
            reset_text = reset_dt.strftime("%a %b %d %H:%M %Z")
        except Exception:  # noqa: BLE001
            reset_text = "unknown time"

    return (
        "<div style='background:#2a1417;border-bottom:2px solid #5a1f22;color:#f0676b;"
        "padding:16px 26px;font-size:14px;font-weight:650;display:flex;align-items:center;gap:11px'>"
        "<span style='font-size:20px'>&#9888;</span>"
        "<div>"
        "<div style='font-size:15px;margin-bottom:4px'>&#9888; Claude plan limit reached &#8212; implementation paused</div>"
        f"<div style='font-size:13px;color:#e7ebf2;font-weight:400'>Resets at {html.escape(reset_text)}. "
        "New builds will wait until the limit renews.</div>"
        "</div></div>"
    )


def _workspace_tabs(cfg: Config, current_app: str | None) -> tuple[list[str], str | None]:
    """The open projects (tab order) + the active project for THIS browser's tabbed workspace.

    EU-63: the cockpit is one-project-per-tab. The tab bar mirrors the SAME server-side workspace the
    routes mutate, so it resolves the session id exactly as ``server._session_id`` does — the cookie
    first, then the id freshly minted onto the request env on a first-contact request (set on the
    response by ``server._ensure_session_cookie``). Outside a request (tests / static previews) — or
    before any tab exists — it degrades to a single synthetic tab for ``current_app`` so the bar still
    renders one project rather than crashing. Never honours the retired ``*`` sentinel as a tab."""
    try:
        from flask import request

        from . import cockpit_state
        sid = request.cookies.get("eu_cockpit_sid") or request.environ.get("eu_new_sid")
        if sid:
            ws = cockpit_state.workspace_for(sid)
            if ws.tabs:
                return ws.projects(), ws.active
    except Exception:  # noqa: BLE001 - rendered outside a request context (tests / previews)
        pass
    app0 = current_app if (current_app and current_app != "*") else (cfg.apps[0].name if cfg.apps else "")
    return ([app0] if app0 else []), app0


def _tab_bar(cfg: Config, current_app: str | None) -> str:
    """The one-project-per-tab strip: a tab per open project (active highlighted) + a '+' add-tab
    picker offering ONLY projects not already open in another tab — the mutual-exclusion invariant.

    Tab clicks and picker entries are plain ``/?app=<project>`` links: the per-tab route (``_scope``
    in server.py) opens-or-focuses that project's tab and makes it active. There is no '*'/"All
    projects" entry any more — every tab is pinned to one concrete project."""
    open_projects, active = _workspace_tabs(cfg, current_app)
    open_set = set(open_projects)
    # EU-103: which open tabs have autopilot (any active run) live right now?
    try:
        _ap_live = {p for p in open_projects if get_autopilot_status(p).get("on")}
    except Exception:  # noqa: BLE001
        _ap_live = set()
    tabs = "".join(
        f"<a class='ptab{' on' if p == active else ''}' href='/?app={html.escape(p)}' "
        f"title='Switch to {html.escape(p)}'>"
        + (f'<span class=tabdot title="autopilot running"></span>' if p in _ap_live else "")
        + f"{html.escape(p)}</a>"
        for p in open_projects)
    # The add-tab picker offers only NOT-already-open projects; the open ones are shown greyed +
    # non-clickable so the "a project lives in at most one tab" rule is visible, not just enforced.
    openable = [a.name for a in cfg.apps if a.name not in open_set]
    rows = "".join(
        f"<a href='/?app={html.escape(n)}'>{html.escape(n)}</a>" for n in openable)
    taken = "".join(
        f"<span class=taken title='already open in a tab'>{html.escape(n)} &middot; open</span>"
        for n in (a.name for a in cfg.apps) if n in open_set)
    if not openable:
        rows = "<span class=allopen>Every project is already open in a tab.</span>"
    # EU-143: add "New product" entry to the tab picker instead of a separate button
    new_product = "<a href='/onboard' class=newprod>&#10133; New product</a>"
    picker = (f"<div class=tabpick><div class=ph>Open a project in a new tab</div>{new_product}{rows}"
              + (f"<div class=sep></div>{taken}" if taken else "") + "</div>")
    return f"""
<style>
.tabstrip{{display:flex;gap:4px;align-items:flex-end;flex-wrap:wrap;padding:8px 26px 0;border-bottom:1px solid var(--line);background:var(--panel)}}
.tabstrip .ptab{{display:inline-flex;align-items:center;gap:7px;background:var(--panel2);border:1px solid var(--line2);border-bottom:none;color:var(--dim);border-radius:var(--r-md) var(--r-md) 0 0;padding:8px 14px;font:inherit;font-size:13px;font-weight:600;cursor:pointer;text-decoration:none;white-space:nowrap;position:relative;top:1px;transition:background var(--t-fast),color var(--t-fast)}}
.tabstrip .tabdot{{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--ok);flex:none;animation:pulse2 1.3s infinite}}
.tabstrip .ptab:hover{{background:var(--line);color:var(--ink)}}
.tabstrip .ptab.on{{background:var(--bg);color:var(--ink);border-color:var(--line2);border-bottom:1px solid var(--bg)}}
.tabstrip details.addtab{{position:relative}}
.tabstrip details.addtab>summary{{list-style:none;display:inline-flex;align-items:center;justify-content:center;width:30px;height:32px;background:var(--panel2);border:1px solid var(--line2);border-radius:var(--r-md) var(--r-md) 0 0;color:var(--dim);font-size:17px;font-weight:700;cursor:pointer;position:relative;top:1px}}
.tabstrip details.addtab>summary::-webkit-details-marker{{display:none}}
.tabstrip details.addtab>summary:hover{{background:var(--line);color:var(--ink)}}
.tabstrip details[open]>summary{{background:var(--bg);color:var(--ink);border-color:var(--accent)}}
.tabstrip summary:focus-visible,.tabstrip .ptab:focus-visible,.tabstrip .tabpick a:focus-visible{{outline:none;box-shadow:var(--ring)}}
.tabstrip .tabpick{{position:absolute;top:calc(100% + 6px);left:0;z-index:30;min-width:212px;background:var(--panel);border:1px solid var(--line2);border-radius:var(--r-lg);padding:6px;display:flex;flex-direction:column;gap:2px;box-shadow:var(--shadow-3)}}
.tabstrip .tabpick a{{display:flex;align-items:center;color:var(--ink);border-radius:var(--r-md);padding:9px 11px;font-size:13px;font-weight:500;text-decoration:none;white-space:nowrap}}
.tabstrip .tabpick a:hover{{background:var(--line)}}
.tabstrip .tabpick a.newprod{{color:var(--accent);font-weight:700}}
.tabstrip .tabpick a.newprod:hover{{background:var(--accentbg);border:1px solid var(--accentline)}}
.tabstrip .tabpick .ph{{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--faint);padding:6px 11px 3px}}
.tabstrip .tabpick .sep{{height:1px;background:var(--line);margin:5px 4px}}
.tabstrip .tabpick .taken,.tabstrip .tabpick .allopen{{display:flex;align-items:center;color:var(--faint);padding:9px 11px;font-size:13px;font-weight:500;cursor:not-allowed;white-space:nowrap}}
@media(max-width:820px){{.tabstrip{{padding:7px 14px 0}}.tabstrip .ptab{{padding:7px 11px;font-size:12px}}}}
</style>
<div class=tabstrip>
  {tabs}
  <details class=addtab>
    <summary title="Open another project in a tab">&#43;</summary>
    {picker}
  </details>
</div>"""


def _control_bar(cfg: Config, current_app: str | None = None, healthy: bool = True,
                 is_mac: bool = False) -> str:
    """Render the cockpit's top control bar.

    EU-106: ``is_mac`` — when True, a global '📂 Open logs' button is appended that calls
    ``/api/open-logs?path=<cfg.log_folder>``.  The button is gated on macOS because the
    ``open`` shell command is Darwin-specific; on non-Mac machines the button would call
    an endpoint that returns 403.
    """
    # "*" is the retired "All projects" selector — it is truthy but NOT a real app, so it must never
    # become app0 (every button below bakes app0 into an ?app= / hidden field; a literal "*" reaches
    # cfg.app("*") -> KeyError). Normalize to a concrete app for single-app ACTION buttons. EU-63:
    # the NAV link "Choose a ticket" keeps ?app=* when there is no concrete project (legacy / test
    # path) so the tickets route (which handles * safely) still lists all projects — this is harmless
    # because the tickets route is the one caller that handles the * sentinel without a cfg.app("*").
    app0 = current_app if (current_app and current_app != "*") else (cfg.apps[0].name if cfg.apps else "")
    # EU-63: the retired "*" all-projects sentinel must never appear in any URL we emit.  Use the
    # same concrete project as ``app0`` for the "Choose a ticket" nav link too.
    nav_app = app0  # concrete project (or first configured app if none active); never "*"
    tab_bar = _tab_bar(cfg, current_app)
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
    # confused. (A) "Update unit": THE CTO'S OWN code (this tool) dev->main -> the 24/7 VPS
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
        # An app whose repo IS the CTO's OWN repo (e.g. the 'Elite-Unit' app, added so the unit can
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

    # EU-103: per-project autopilot controls — read from the per-app run-state.
    # State is resolved here (not in the template) so the HTML is a pure string.
    ap_status: dict = {"on": False, "stopping": False, "mode": None, "external": False}
    if app0:
        try:
            ap_status = get_autopilot_status(app0)
        except Exception:  # noqa: BLE001
            pass
    ap_on = ap_status.get("on", False)
    ap_stopping = ap_status.get("stopping", False)
    ap_external = ap_status.get("external", False)
    ap_appq = html.escape(app0)
    # Disable start buttons when the system is unhealthy OR no project is selected.
    ap_dis = "" if (healthy and app0) else "disabled"
    if ap_stopping:
        # Drain in progress: show a neutral "finishing…" label, no buttons.
        ap_html = (
            '<span class=tbdiv></span>'
            '<div class="tbap stopping" title="Finishing current ticket, then standing down">'
            '<span class="apdot-sm stop"></span>'
            '<span class=tbaplabel>&#9203;&nbsp;Stopping&hellip;</span>'
            '</div>')
    elif ap_on:
        # Autopilot running: offer graceful drain or hard stop.
        # EU-120: when external daemon is running, mark it as external in the label.
        ap_label = f'Autopilot&nbsp;<b>ON</b>&nbsp;<span class=ext>(external)</span>&nbsp;&middot;&nbsp;{ap_appq}' if ap_external else f'Autopilot&nbsp;<b>ON</b>&nbsp;&middot;&nbsp;{ap_appq}'
        ap_class = "tbap on ext" if ap_external else "tbap on"
        ap_html = (
            '<span class=tbdiv></span>'
            f'<div class="{ap_class}">'
            '<span class="apdot-sm on"></span>'
            f'<span class=tbaplabel>{ap_label}</span>'
            f'<form method=post action=/api/autopilot class=tbf>'
            f'<input type=hidden name=action value=drain>'
            f'<input type=hidden name=app value="{ap_appq}">'
            '<button class="aptbtn drain" '
            'title="Let the current ticket finish landing on DEV, then stand down">'
            'Finish&nbsp;&amp;&nbsp;stop</button></form>'
            f'<form method=post action=/api/autopilot class=tbf>'
            f'<input type=hidden name=action value=stop>'
            f'<input type=hidden name=app value="{ap_appq}">'
            '<button class="aptbtn stop" '
            'title="Mark autopilot off now — the in-flight build still finishes in the background">'
            'Stop</button></form>'
            '</div>')
    else:
        # Autopilot off: offer two start modes that genuinely differ (EU-103 iter-2).
        #  · 'Choose tickets' opens the per-ticket picker (pick specific tickets, then run them).
        #  · 'Auto-drain' starts the continuous backlog autopilot for this project.
        _conf_choose = (f"return confirm('Open the ticket picker for "
                        f"{html.escape(app0 or '')} to choose specific tickets to develop?')")
        _conf_drain = (f"return confirm('Start Auto-drain for "
                       f"{html.escape(app0 or '')}? "
                       f"The unit will work tickets LIVE until the queue is empty or you press Stop.')")
        ap_html = (
            '<span class=tbdiv></span>'
            '<div class="tbap off">'
            '<span class="apdot-sm off"></span>'
            '<span class=tbaplabel>Autopilot</span>'
            f'<form method=post action=/api/autopilot class=tbf '
            f'onsubmit="{_conf_choose}">'
            f'<input type=hidden name=action value=start>'
            f'<input type=hidden name=app value="{ap_appq}">'
            '<input type=hidden name=mode value=choose>'
            f'<button class="aptbtn start" {ap_dis} '
            'title="Pick specific tickets to develop (opens the ticket picker)">'
            '&#127915;&nbsp;Choose tickets</button></form>'
            f'<form method=post action=/api/autopilot class=tbf '
            f'onsubmit="{_conf_drain}">'
            f'<input type=hidden name=action value=start>'
            f'<input type=hidden name=app value="{ap_appq}">'
            '<input type=hidden name=mode value=drain>'
            f'<button class="aptbtn start" {ap_dis} '
            'title="Drain the backlog automatically until empty">'
            '&#9654;&nbsp;Auto-drain</button></form>'
            '</div>')

    # EU-106: global 'Open logs' button — macOS only (Darwin `open` command opens Finder).
    # Calls /api/open-logs with the configured log folder so a single click reveals ALL run logs.
    open_logs_html = ""
    if is_mac:
        log_folder = str(getattr(cfg, "log_folder", None) or "logs/")
        open_logs_html = (
            f'<a class="btn" href="/api/open-logs?path={html.escape(quote(log_folder))}" '
            f'title="Open the run-logs folder in Finder">&#128194; Open logs</a>'
        )

    # Render plan-limit banner BEFORE the control bar (if active)
    plan_banner = _plan_limit_banner(_state)
    return tab_bar + plan_banner + f"""
<style>
/* Control bar — consumes the EU-39 design tokens (palette/radius/elevation/ring) from
   the War Room's :root{{}}, so a re-skin there flows through here too. */
.tbar{{display:flex;gap:9px;align-items:center;flex-wrap:wrap;padding:11px 26px;border-bottom:1px solid var(--line);background:var(--panel)}}
.tbar .btn{{display:inline-flex;align-items:center;gap:7px;background:var(--panel2);border:1px solid var(--line2);color:var(--ink);border-radius:var(--r-md);padding:9px 13px;font:inherit;font-size:13px;font-weight:600;cursor:pointer;text-decoration:none;white-space:nowrap;transition:background var(--t-fast),border-color var(--t-fast)}}
.tbar .btn:hover{{background:var(--line)}}
.tbar .btn.primary{{background:var(--accent);border-color:var(--accent);color:#fff}}
.tbar .btn.primary:hover{{background:#2f5ce0}}
.tbar .btn:focus-visible,.tbar summary:focus-visible,.tbar .panel a:focus-visible,.tbar .panel button:focus-visible{{outline:none;box-shadow:var(--ring)}}
.tbar details.menu{{position:relative}}
.tbar details.menu>summary{{list-style:none}}
.tbar details.menu>summary::-webkit-details-marker{{display:none}}
.tbar details.menu>summary::after{{content:" \\25BE";color:var(--dim);font-size:10px}}
.tbar details[open]>summary{{background:var(--line);border-color:var(--accent)}}
.tbar .panel{{position:absolute;top:calc(100% + 7px);left:0;z-index:30;min-width:212px;background:var(--panel);border:1px solid var(--line2);border-radius:var(--r-lg);padding:6px;display:flex;flex-direction:column;gap:2px;box-shadow:var(--shadow-3)}}
.tbar .panel.right{{left:auto;right:0}}
.tbar .panel a,.tbar .panel form>button{{display:flex;align-items:center;gap:9px;width:100%;text-align:left;background:none;border:0;color:var(--ink);border-radius:var(--r-md);padding:9px 11px;font:inherit;font-size:13px;font-weight:500;cursor:pointer;text-decoration:none;white-space:nowrap}}
.tbar .panel a:hover,.tbar .panel form>button:hover{{background:var(--line)}}
.tbar .panel form{{margin:0}}
.tbar .panel.form{{min-width:312px;gap:9px;padding:13px}}
.tbar .panel.form select,.tbar .panel.form input[type=text]{{background:var(--bg);border:1px solid var(--line2);color:var(--ink);border-radius:var(--r-md);padding:8px 10px;font:inherit;width:100%}}
.tbar .panel.form .row{{display:flex;gap:8px;align-items:center}}
.tbar .panel.form button{{display:block;width:100%;background:var(--accent);color:#fff;border:0;border-radius:var(--r-md);padding:9px;font-weight:650;cursor:pointer}}
.tbar .panel.form button:disabled{{background:#222a37;color:var(--faint);cursor:not-allowed}}
.tbar .panel .sep{{height:1px;background:var(--line);margin:5px 4px}}
.tbar .panel .ph{{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--faint);padding:6px 11px 3px}}
.tbar .tbnote{{font-size:12px;margin-left:2px}}.tbar .tbnote.run{{color:var(--warn)}}.tbar .tbnote.bad{{color:var(--bad)}}.tbar .tbnote.ok{{color:var(--ok);font-weight:600}}
.tbar .btn.deploy{{background:#1f7a45;border-color:#2c9a5f;color:#fff}}.tbar .btn.deploy:hover{{background:#1a6b3c}}.tbar .btn.deploy .cbadge{{background:#0c3a22}}
.tbar .btn.ship{{background:#7c3aed;border-color:#8b5cf6;color:#fff}}.tbar .btn.ship:hover{{background:#6d28d9}}.tbar .btn.ship .cbadge{{background:#3b1d7a}}
.tbar .tbdiv{{width:1px;height:22px;background:var(--line2);margin:0 7px;align-self:center;display:inline-block}}
.tbar .grow{{flex:1}}
.tbar .chatbtn{{display:inline-flex;align-items:center;gap:6px}}
.tbar .cbadge{{background:var(--bad);color:#fff;font-size:10px;font-weight:800;border-radius:var(--r-pill);padding:1px 6px}}
.tbar form.tbf{{margin:0;display:inline-flex}}
.tbar .btn:disabled{{opacity:.5;cursor:not-allowed}}
.tbar .panel a{{display:flex;align-items:center}}
.tbar .panel .mfresh{{margin-left:auto;padding-left:14px;color:var(--faint);font-size:11px;font-weight:400}}
/* EU-103 — per-project Autopilot section */
.tbar .tbap{{display:inline-flex;align-items:center;gap:6px;padding:5px 8px 5px 10px;border:1px solid var(--line2);border-radius:var(--r-md);background:var(--panel2)}}
.tbar .tbap.on{{border-color:var(--okline);background:var(--okbg)}}
.tbar .tbap.on.ext{{border-color:var(--infoline);background:var(--infobg)}}
.tbar .tbap.stopping{{border-color:var(--warnline);background:var(--warnbg)}}
.tbar .apdot-sm{{width:7px;height:7px;border-radius:50%;background:var(--faint);flex:none}}
.tbar .apdot-sm.on{{background:var(--ok);animation:pulse2 1.3s infinite}}
.tbar .apdot-sm.stop{{background:var(--warn)}}
.tbar .tbaplabel{{font-size:12px;color:var(--ink);white-space:nowrap}}
.tbar .tbaplabel .ext{{font-size:10px;color:var(--info);font-weight:600;margin-left:4px}}
.tbar .aptbtn{{border:0;border-radius:var(--r-md);padding:5px 11px;font:inherit;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap}}
.tbar .aptbtn.start{{background:var(--accent);color:#fff}}.tbar .aptbtn.start:hover{{background:#2f5ce0}}
.tbar .aptbtn.start:disabled{{background:#222a37;color:var(--faint);cursor:not-allowed}}
.tbar .aptbtn.drain{{background:var(--warn);color:#1a1205}}.tbar .aptbtn.drain:hover{{background:#c99020}}
.tbar .aptbtn.stop{{background:var(--bad);color:#fff}}.tbar .aptbtn.stop:hover{{background:#c74c50}}
.deploybar{{display:flex;align-items:center;gap:13px;padding:11px 26px;background:var(--accentbg);border-bottom:1px solid var(--accentline)}}
.deploybar .dspin{{width:18px;height:18px;border:3px solid var(--accentline);border-top-color:var(--accent);border-radius:50%;animation:dsp .9s linear infinite;flex:none}}
.deploybar .dmsg{{color:#cfe0ff;font-size:13px;font-weight:650}}
.deploybar .dsub{{color:var(--dim);font-weight:400;font-size:12px}}
.deploybar .dprog{{flex:1;max-width:300px;height:6px;background:var(--bg);border-radius:var(--r-pill);overflow:hidden;border:1px solid var(--accentline)}}
.deploybar .dprogfill{{display:block;width:38%;height:100%;background:linear-gradient(90deg,var(--accent),var(--info));border-radius:var(--r-pill);animation:dsl 1.4s ease-in-out infinite}}
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
  <a class="btn primary" href="/tickets{('?app=' + html.escape(nav_app)) if nav_app else ''}">&#127915; Choose a ticket</a>

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
  <a class="btn chatbtn" href="/needs">&#128276; Needs you{needs_badge}</a>
  <a class="btn" href="/roster-doc" title="Officers, soldiers &amp; duties — the full unit roster">&#128101; Roster</a>
  {promote_html}
  {ship_html}
  {ap_html}
  {open_logs_html}

  <details class=menu>
    <summary class=btn>&#128202; Reports</summary>
    <div class="panel right">
      <a href="/tasks">&#128203; Task log{fr_tasks}</a>
      <a href="/council">&#128172; Daily muster &amp; meetings{fr_council}</a>
      <a href="/memory">&#128221; Unit memory{fr_mem}</a>
      <a href="/usage">&#128202; Token usage{fr_usage}</a>
      <a href="/budget">&#128176; Budget monitor</a>
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
               ".ctabs{max-width:780px;margin:0 auto 14px;display:flex;gap:6px;border-bottom:1px solid var(--line)}"
               ".ctab{padding:9px 14px;color:var(--dim);font-size:13px;font-weight:600;border-bottom:2px solid transparent;text-decoration:none}"
               ".ctab.on{color:var(--ink);border-bottom-color:var(--accent)}.ctab:hover{color:var(--ink)}"
               ".ctab:focus-visible{outline:none;box-shadow:var(--ring);border-radius:var(--r-sm)}"
               ".cbadge{background:var(--bad);color:#fff;font-size:10px;font-weight:800;border-radius:var(--r-pill);padding:1px 6px;margin-left:5px}"
               ".aim{max-width:780px;margin:0 auto 10px;color:#9be7bd;font-size:13px}.aim a{color:var(--info)}"
               ".pcard{background:var(--warnbg);border:1px solid var(--warnline);border-radius:var(--r-xl);padding:14px 16px;margin-bottom:12px}"
               ".pcard .ph2{color:var(--warn);font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:7px}"
               ".pcard .pq{color:var(--ink);font-size:13px;white-space:pre-wrap;max-height:260px;overflow:auto;font-family:var(--mono);line-height:1.5}"
               ".preply{display:flex;gap:8px;margin-top:11px}.preply input{flex:1}"
               ".thread{display:flex;flex-direction:column;gap:9px;margin:16px 0 96px}"
               ".msg{display:flex;flex-direction:column;max-width:80%}"
               ".msg.you{align-self:flex-end;align-items:flex-end}.msg.unit{align-self:flex-start}"
               ".who{font-size:10px;color:var(--faint);margin:0 6px 2px}"
               ".bub{padding:9px 13px;border-radius:var(--r-xl);font-size:13px;line-height:1.5;white-space:pre-wrap}"
               ".msg.unit .bub{background:var(--panel2);border:1px solid var(--line2);border-bottom-left-radius:4px}"
               ".msg.you .bub{background:#1e3a5f;border-bottom-right-radius:4px;color:#eaf1fb}"
               ".cempty{color:var(--dim);padding:30px 8px;text-align:center;font-size:13px}"
               ".composer{position:fixed;bottom:0;left:0;right:0;background:var(--bg);border-top:1px solid var(--line);padding:12px 30px}"
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


def _dual_provider_gauge(cfg: Config, claude_usage: dict, glm_usage: dict | None = None) -> str:
    """EU-122: Dual-provider budget gauge — shows Claude and GLM side-by-side with % remaining.

    Each provider gets its own card with:
    - Provider name + brand label
    - Utilization percentage (visual bar + number)
    - Low-watermark indicator (green → amber → red based on threshold)
    - Reset time (if available)

    Pattern mirrors EU-118 plan_limit_banner for consistent alert styling.
    """
    from . import usage as _usg

    # Low-watermark thresholds (configurable, with safe defaults)
    warn_threshold = float(getattr(cfg, "budget_alert_pct", 0.8) or 0.8)
    bad_threshold = float(getattr(cfg, "budget_bad_threshold", 0.95) or 0.95)

    def _gauge_tone(util: float) -> tuple[str, str]:
        """Returns (tone_class, aria_label) for a utilization value."""
        if util >= bad_threshold:
            return "bad", "critical"
        if util >= warn_threshold:
            return "warn", "warning"
        return "ok", "normal"

    def _provider_card(name: str, brand: str, data: dict, is_placeholder: bool = False) -> str:
        """Render a single provider's budget gauge card."""
        if is_placeholder or not data:
            # Placeholder for when GLM isn't configured yet
            return (
                '<div class=provcard>'
                '<div class=phead>'
                f'<span class=pname>{html.escape(name)}</span>'
                f'<span class=pbrand>unconfigured</span>'
                '</div>'
                '<div class=pnote>This provider isn\'t set up yet. Add it to config.yaml to track its quota.</div>'
                '</div>'
            )

        util = float(data.get("utilization", 0.0))
        pct = int(util * 100)
        wpct = min(100, max(0, pct))
        tone, aria_label = _gauge_tone(util)
        tone_cls = {"ok": "g", "warn": "a", "bad": "r"}.get(tone, "g")

        # Calculate remaining percentage
        remaining = max(0, 100 - pct)

        # Reset time (if available)
        reset = data.get("resets_in", "")
        reset_meta = ""
        if reset and reset not in ("now", ""):
            reset_meta = f'<div class=pmeta>resets in {html.escape(reset)}</div>'
        elif reset == "now":
            reset_meta = '<div class=pmeta>resetting now</div>'

        # Status indicator (low-watermark)
        if util >= bad_threshold:
            status_icon = "&#9888;"  # warning icon
            status_text = "critical"
        elif util >= warn_threshold:
            status_icon = "&#9888;"
            status_text = "low"
        else:
            status_icon = "&#10003;"  # checkmark
            status_text = "ok"

        aria = f"{html.escape(brand)} {pct}% used, {remaining}% remaining"

        return (
            '<div class=provcard>'
            '<div class=phead>'
            f'<span class=pname>{html.escape(name)}</span>'
            f'<span class=pbrand>{html.escape(brand)}</span>'
            '</div>'
            '<div class=pstatus>'
            f'<span class="picon {tone}">{status_icon}</span>'
            f'<span class=pstat>{html.escape(status_text)}</span>'
            f'<span class=ppct>{pct}% used</span>'
            '</div>'
            '<div class=pgauge>'
            f'<div class=pgbar role=progressbar aria-valuemin=0 aria-valuemax=100 '
            f'aria-valuenow={wpct} aria-label="{aria}">'
            f'<span class="pgfill {tone_cls}" style="width:{wpct}%"></span>'
            '</div>'
            f'<div class=premain>{remaining}% remaining</div>'
            '</div>'
            + reset_meta +
            '</div>'
        )

    # Build Claude card from plan_usage data
    claude_card = ""
    if claude_usage.get("available"):
        # Find the most critical limit to display (highest utilization)
        limits = claude_usage.get("limits", [])
        if limits:
            # Sort by utilization descending, pick the worst one
            worst_limit = max(limits, key=lambda l: float(l.get("utilization", 0.0)))
            claude_card = _provider_card(
                "Claude",
                "Max subscription",
                worst_limit,
                is_placeholder=False
            )
    else:
        # Claude data unavailable - show fallback
        claude_card = _provider_card(
            "Claude",
            "Max subscription",
            {"utilization": 0.0, "resets_in": ""},
            is_placeholder=False
        )

    # Build GLM card (placeholder if not configured)
    glm_card = _provider_card(
        "GLM",
        "Secondary provider",
        glm_usage or {},
        is_placeholder=(glm_usage is None or not glm_usage)
    )

    # Combine both cards in a side-by-side layout
    return (
        "<style>"
        ".dualprov{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin:6px 0 20px}"
        ".provcard{background:#12161f;border:1px solid #232936;border-radius:12px;padding:16px 18px}"
        ".phead{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px}"
        ".pname{color:#e9ecf1;font-size:15px;font-weight:700}"
        ".pbrand{color:#6b7480;font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.06em}"
        ".pstatus{display:flex;align-items:center;gap:8px;margin-bottom:10px}"
        ".picon{font-size:14px}.picon.ok{color:#3fb950}.picon.warn{color:#d99a2b}.picon.bad{color:#f0676b}"
        ".pstat{color:#8a929f;font-size:12px;font-weight:500;text-transform:uppercase}"
        ".ppct{color:#e9ecf1;font-size:13px;font-weight:600;margin-left:auto}"
        ".pgauge{margin:12px 0}"
        ".pgbar{height:10px;background:#0d1119;border-radius:6px;overflow:hidden;border:1px solid #222a38}"
        ".pgfill{display:block;height:100%;transition:width .3s ease}"
        ".pgfill.g{background:#3fb950}.pgfill.a{background:#d99a2b}.pgfill.r{background:#f0676b}"
        ".premain{color:#6b7480;font-size:11px;margin-top:6px;font-family:ui-monospace,Menlo,monospace}"
        ".pmeta{color:#6b7480;font-size:11px;margin-top:8px;font-family:ui-monospace,Menlo,monospace}"
        ".pnote{color:#8a929f;font-size:12px;margin-top:8px}"
        "@media(max-width:680px){.dualprov{grid-template-columns:1fr}}"
        "</style>"
        '<div class=dualprov>'
        f'{claude_card}'
        f'{glm_card}'
        '</div>'
    )
