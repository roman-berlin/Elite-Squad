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
    "--ring:0 0 0 2px var(--bg),0 0 0 4px rgba(77,124,255,.6);--t-fast:.15s ease;"
    # 8pt spacing scale (EU-296) — mirrors _PAGE's :root block, kept byte-identical.
    "--s-1:4px;--s-2:8px;--s-3:16px;--s-4:24px;--s-5:32px;--s-6:48px;"
    # modular type scale (EU-296) — px-equivalents/usage documented on the _PAGE mirror.
    "--t-xs:11px;--t-sm:12.5px;--t-md:14px;--t-lg:18px;--t-xl:24px;--t-2xl:32px;"
    # semantic color-role aliases (EU-296) — map onto the existing palette above.
    "--surface:var(--panel);--border:var(--line);--text:var(--ink);"
    "--positive:var(--ok);--critical:var(--bad)}")


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


# ── REUSABLE PARTIALS (EU-297 / EU-285b) ───────────────────────────────────────
# The first slice that actually *consumes* the EU-296 foundation tokens (--s-*/--t-*/
# --surface/--r-xl) instead of just defining them. Each partial inlines its tokens
# directly on the element it returns, so the partial's OWN output always carries them —
# independent of whichever stylesheet happens to be loaded around a given call site.
# Full rollout to every card/panel/button on every screen is a later slice; these three
# are wired into one real call site each as proof-of-integration (see _kpi_html /
# _actbtn here, and the "Talk to the unit" panel in warroom.py).

def _btn(label: str, *, cls: str = "", tag: str = "button", attrs: str = "") -> str:
    """Reusable toolbar-button partial: a shared ``.btn`` base carrying ``--s-*`` padding,
    ``--r-xl`` radius and ``--t-md`` type, so toolbar screens stop hand-rolling their own inline
    button CSS. ``cls`` layers extra tone/colour classes (e.g. ``actbtn``) on top of the base."""
    classes = ("btn " + cls).strip()
    return (f'<{tag} class="{classes}" style="border-radius:var(--r-xl);'
            f'padding:var(--s-2) var(--s-3);font-size:var(--t-md)"{attrs}>{label}</{tag}>')


def _kpi_metric(value, label, tone: str = "", href: str | None = None, extra: str = "") -> str:
    """Reusable KPI-numeral partial: renders a headline number + its label at the EU-296
    ``--t-2xl`` type-scale token instead of a raw 30px/40px inline font-size literal — reducing
    numeral weight per the parent ticket's direction. ``extra`` appends trusted HTML (e.g. a
    gauge/sparkline) inside the wrapping tag before it closes. The distinctive ``kpim`` marker
    class proves a call site renders through this partial rather than duplicating the numeral
    markup inline."""
    tag, attr, link = ("a", f' href="{html.escape(href)}"', " link") if href else ("div", "", "")
    return (
        f'<{tag} class="kpim kpi {tone}{link}"{attr}>'
        f'<div class=kv style="font-size:var(--t-2xl);font-weight:500">{html.escape(str(value))}</div>'
        f'<div class=kl>{html.escape(str(label))}</div>{extra}</{tag}>'
    )


def _card(title: str, body: str, freshness: str | None = None) -> str:
    """Reusable card/panel partial: consistent padding (the EU-296 ``--s-*`` spacing scale),
    border, ``--r-xl`` radius and ``--surface`` background, so KPI cards and panels stop being
    hand-rolled per screen. ``body`` is trusted HTML (caller-controlled, same contract as the
    other partials in this module); ``freshness`` renders an optional small caption (e.g.
    'updated 2m ago') under the body."""
    fresh_html = (f'<div class=card-fresh style="font-size:var(--t-xs);color:var(--dim);'
                  f'margin-top:var(--s-2)">{html.escape(freshness)}</div>' if freshness else "")
    return (
        '<div class=card style="background:var(--surface);border:1px solid var(--border);'
        'border-radius:var(--r-xl);padding:var(--s-4)">'
        f'<div class=card-title style="font-size:var(--t-lg);font-weight:600;'
        f'margin-bottom:var(--s-2)">{html.escape(title)}</div>'
        f'<div class=card-body>{body}</div>{fresh_html}</div>'
    )


def _actbtn(action: str, label: str, app: str = "", confirm: str = "") -> str:
    """A single on-page officer-action button (POST form). These actions used to live in the
    'Unit' toolbar menu; they now sit on the page that shows their result, so each activity
    appears exactly once."""
    hidden = f'<input type=hidden name=app value="{html.escape(app)}">' if app else ""
    onsub = f" onsubmit=\"return confirm('{confirm}')\"" if confirm else ""
    return (f"<form method=post action={action} style='margin:0'{onsub}>{hidden}"
            f"{_btn(label, cls='actbtn')}</form>")


def _actbar(*items: str) -> str:
    """A row of on-page officer-action controls. Buttons render via ``_btn`` (EU-297); this
    style block adds only the ``actbtn`` tone/colour on top of the shared ``.btn`` base."""
    return ("<style>.actbar{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 18px}"
            ".btn{font:inherit;font-weight:600;cursor:pointer;text-decoration:none;"
            "display:inline-block;transition:background var(--t-fast)}"
            ".actbtn{background:var(--panel2);border:1px solid var(--line2);color:var(--ink)}"
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


def _plan_limit_banner(state: dict, cfg=None) -> str:
    """Plan-limit warning banner: shown when a Claude or GLM plan limit is hit.

    Prominent red banner that persists until the limit resets (not a one-shot like
    ``_result_banner``). Displays which limit was hit and when it resets. EU-191: when an alternate
    backend (e.g. GLM) is configured, it also offers a one-click "Continue on <backend>" button that
    switches the model backend and resumes the paused ticket, so an Opus/Claude limit doesn't stall
    the drain until reset. EU-202: bidirectional — offers Opus→GLM AND GLM→Opus.
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

    # EU-191 + EU-202: offer a one-click switch to an available alternate backend instead of only
    # waiting for the limit to reset. Works for BOTH directions: Opus→GLM AND GLM→Opus.
    # The button switches the sticky backend and auto-resumes the last-run ticket (tracked in
    # state['last_run']).
    continue_offer = ""
    backend_label = "Claude"  # Default, updated below
    try:
        from . import backends as _bk, backend_pref as _bp
        _cur = _bk.normalize(_bp.active(cfg) if cfg is not None else _bk.NATIVE)
        backend_label = "Claude (Opus)" if _cur == _bk.NATIVE else "GLM (Z.ai)"
        _alts = _bk.alternates(_cur)
        if _alts:
            _alt = _alts[0]
            _label = "GLM (Z.ai)" if _alt == _bk.GLM else "Claude (Opus)"
            _last = state.get("last_run") or {}
            _tickets = [t for t in (_last.get("tickets") or []) if t]
            _resume = (" &amp; resume " + html.escape(", ".join(_tickets))) if _tickets else ""
            # EU-223: name the affected app (the paused run's project) so the switch — and the
            # server-side handler — flips only THAT app's backend, not the whole unit.
            _affected_app = (_last.get("app") or "").strip() or None
            continue_offer = (
                "<form method=post action=/api/continue-on-alternate style='margin:8px 0 0'>"
                f"<input type=hidden name=backend value='{html.escape(_alt)}'>"
                + (f"<input type=hidden name=app value='{html.escape(_affected_app)}'>"
                   if _affected_app else "")
                + "<button style='font-size:13px;font-weight:650;padding:6px 14px;border-radius:7px;"
                "background:#1f6feb;color:#fff;border:none;cursor:pointer'>"
                f"Continue on {_label}{_resume}</button>"
                "<span style='font-size:12px;color:#e7ebf2;font-weight:400;margin-left:10px'>"
                "&#8212; keep the drain moving without waiting for the reset</span></form>")
    except Exception:  # noqa: BLE001 — the offer must never break the banner
        continue_offer = ""

    return (
        "<div style='background:#2a1417;border-bottom:2px solid #5a1f22;color:#f0676b;"
        "padding:16px 26px;font-size:14px;font-weight:650;display:flex;align-items:flex-start;gap:11px'>"
        "<span style='font-size:20px'>&#9888;</span>"
        "<div>"
        f"<div style='font-size:15px;margin-bottom:4px'>&#9888; {html.escape(backend_label)} plan limit reached &#8212; implementation paused</div>"
        f"<div style='font-size:13px;color:#e7ebf2;font-weight:400'>Resets at {html.escape(reset_text)}. "
        "New builds will wait until the limit renews.</div>"
        f"{continue_offer}"
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


# ── EU-314: per-project pipeline board ──────────────────────────────────────────
# One consolidated board for the ACTIVE project tab only — every in-flight/recent ticket with its
# current stage (dashboard.derive_pipeline_stage) and age — so the Commander stops cross-reading
# KPI cards / /tasks / /needs to see "what's happening right now". Scoped exactly like
# needs.summary(cfg, app_name) already scopes per app (EU-129 pattern): NOT a single global list.
_MERGED_RECENCY_SECS = 30 * 60   # a just-merged ticket lingers ~30m on the board, then drops off

# EU-315: per-tone row colour, keyed off dashboard.pipeline_stage_tone(task) — reuses the same
# palette vars _token_css() already defines, so Blocked / Needs-you / Errored are each visually
# distinct at a glance instead of identical --dim grey text.
_STAGE_TONE_COLOR = {
    "bad": "var(--bad)", "warn": "var(--warn)", "blocked": "var(--info)", "ok": "var(--ok)",
}


def _pipeline_board(cfg: Config, tasks: list[dict], app_name: str | None,
                    blocked_set: set[str] | None = None) -> str:
    """Render the active tab's pipeline board: one row per ticket — id, current stage, age.

    ``tasks`` is the caller's already-loaded ``dashboard.load_tasks(cfg.audit_path)`` result (one
    entry per RUN, newest run first) — never reloaded here, mirroring ``needs.py``'s "load once,
    filter many" contract. Filtered to ``app_name`` with the SAME rule ``needs.py``'s
    ``_row_matches_app`` uses (the row's own ``app`` field, else a ticket-key prefix match) — pass
    a falsy ``app_name`` to show every project (unscoped).

    ``blocked_set`` (EU-315): the REAL parked set — the ticket ids in blocked_tickets.json
    (``warroom._load_blocked``). A row whose ticket is a member renders with the distinct Blocked
    tone/label and, once past ``warroom.STALE_BLOCK_CUTOFF_S``, greyed as ``(stale)``. Blocked-ness
    is membership in this set, NOT a run ``outcome`` (there is no ``blocked`` outcome). When
    ``None``, the board loads the set itself from ``cfg`` so a direct caller still gets the real
    state; pass an explicit set (even empty) to override that self-load.

    Age-filter rule: every non-terminal / needs-you row always shows; a ``merged→dev`` row shows
    only while recent (< ``_MERGED_RECENCY_SECS`` since it ended), so a just-landed ticket lingers
    briefly instead of vanishing the instant it merges. ``cfg`` is accepted (used to self-load the
    parked set) and otherwise matches the ``needs.summary(cfg, app_name)`` convention this mirrors.
    """
    from datetime import datetime

    from . import needs as _needs

    if blocked_set is None:
        blocked_set = set()
        if cfg is not None:
            try:
                from . import warroom as _wr
                blocked_set = {str(b) for b in _wr._load_blocked(cfg)}
            except Exception:  # noqa: BLE001 - the board must never break on a bad parked-set read
                blocked_set = set()
    else:
        blocked_set = {str(b) for b in blocked_set}

    app_prefix = str(app_name).strip().upper() if app_name else ""
    now_naive = datetime.now()

    def _age_secs(ref) -> float | None:
        if ref is None:
            return None
        now = datetime.now(ref.tzinfo) if getattr(ref, "tzinfo", None) else now_naive
        return max((now - ref).total_seconds(), 0.0)

    rows = []
    for t in tasks:
        if app_name and not _needs._row_matches_app(t, app_name, app_prefix):
            continue
        age_secs = _age_secs(t.get("ended") or t.get("started"))
        if age_secs is None:
            continue  # no timestamp at all — nothing to show an age for
        if (t.get("outcome") or "") == "merged→dev" and age_secs >= _MERGED_RECENCY_SECS:
            continue  # merged AND old — the one row kind the board drops (the recency rule)
        raw_tid = str(t.get("ticket_id") or "")
        is_blocked = raw_tid in blocked_set
        tid = html.escape(raw_tid)
        stage_text = D.derive_pipeline_stage(t, is_blocked=is_blocked)
        age = html.escape(D._human_dur(age_secs))
        # EU-315: freshness hardening — a parked (blocked) row past the staleness cutoff is NEVER
        # shown as a plain active Blocked row; it stays visible (greyed), not silently dropped, so
        # the Commander can still see it aged out rather than losing the signal entirely. Blocked-
        # ness + its staleness are both derived from the real parked set (blocked_set membership),
        # never from a run outcome.
        stale = D.is_blocked_stale(t, is_blocked=is_blocked)
        if stale:
            tone_cls, color = "pbstale", "var(--faint)"
            stage_text += " (stale)"
        else:
            tone = D.pipeline_stage_tone(t, is_blocked=is_blocked)
            tone_cls = f"pbstage-{tone}" if tone else "pbstage-default"
            color = _STAGE_TONE_COLOR.get(tone, "var(--dim)")
        stage = html.escape(stage_text)
        rows.append(
            '<div class=pbrow style="display:flex;align-items:center;gap:var(--s-3);'
            'padding:var(--s-2) 0;border-top:1px solid var(--border)">'
            f'<span class="mono pbid" style="font-weight:600">{tid}</span>'
            f'<span class="pbstage {tone_cls}" style="color:{color};flex:1">{stage}</span>'
            f'<span class=pbage style="color:var(--faint);font-size:var(--t-xs)">{age}</span>'
            '</div>'
        )
    body = ("".join(rows) if rows else
            '<div class=pbempty style="color:var(--dim)">No in-flight tickets right now.</div>')
    return _card("Pipeline", body)


def backend_control(cfg, app_name: str | None = None) -> str:
    """EU-190/EU-223: the STICKY model-backend selectors for the cockpit control bar.

    The GLOBAL selector (unchanged): persists via POST /api/model with no `app` and applies to
    every project that has no per-app override; the active backend is shown at a glance. GLM is
    offered only when configured (GLM_AUTH_TOKEN present). If the persisted choice is GLM but the
    key is now missing, it's shown flagged and runs are blocked at launch — no silent fallback.

    EU-223 adds an optional PER-PROJECT selector for ``app_name`` — defaults to "Inherit global"
    (selected when the app has no override) and posts ``app=<app_name>`` alongside ``backend=`` so
    only THAT project's pref changes — the two parallel drains (EU-103) can each pin a backend.
    Never renders the token value.

    EU-236: ALSO lists every user-defined model-registry backend as an additional ``<option>`` in
    the global selector, alongside Claude/GLM — sourced live from ``ModelRegistry(cfg)`` (cfg-
    anchored, same hermetic tmp-store convention as ``model_registry.py``) so a backend added via
    the cockpit shows up immediately, with no code change or restart. Never renders a credential."""
    from . import backend_pref
    from . import backends as _bk
    from .model_registry import ModelRegistry
    active = backend_pref.active(cfg)
    glm_ok = _bk.available("glm")
    show_glm = glm_ok or active == _bk.GLM     # keep a stale GLM choice visible even if key vanished
    # EU-236: enumerate the selectable backends through backends.list_backends (the single source of
    # truth — hardcoded opus/glm + every registry record), then render each; opus/glm keep their
    # availability + third-party notes, registry records render as a plain custom option.
    opts = ""
    for _entry in _bk.list_backends(registry=ModelRegistry(cfg)):
        _bid = _entry["id"]
        if _bid == _bk.NATIVE:
            opts += (f"<option value='opus' {'selected' if active == _bk.NATIVE else ''}>"
                     "Opus (Claude)</option>")
        elif _bid == _bk.GLM:
            if not show_glm:
                continue
            glm_label = "GLM (Z.ai)" if glm_ok else "GLM (Z.ai) — key missing"
            opts += (f"<option value='glm' {'selected' if active == _bk.GLM else ''} "
                     f'title="Sends prompts (code, tickets, diffs) to Z.ai — a third-party provider">'
                     f"{glm_label}</option>")
        else:
            _label = html.escape(_entry.get("label") or _bid)
            opts += (f"<option value='{html.escape(_bid)}' {'selected' if active == _bid else ''} "
                     f'title="Custom model backend — {_label}">{_label}</option>')
    if active == _bk.GLM and not glm_ok:
        note = ("<span class=\"tbnote bad\" title=\"Set GLM_AUTH_TOKEN and restart\">"
                "&#9888; GLM key missing — runs blocked</span>")
    elif active == _bk.GLM:
        note = "<span class=tbnote style=\"color:#8a909c\">&#8599; prompts go to Z.ai</span>"
    else:
        note = ""
    per_project = ""
    if app_name:
        override = backend_pref.get_apps(cfg).get(app_name)
        inherit_label = "Opus" if active == _bk.NATIVE else "GLM"
        popts = (f"<option value='inherit' {'selected' if not override else ''}>"
                 f"Inherit global ({inherit_label})</option>"
                 f"<option value='opus' {'selected' if override == _bk.NATIVE else ''}>"
                 f"Opus (Claude)</option>")
        if glm_ok or override == _bk.GLM:
            glm_plabel = "GLM (Z.ai)" if glm_ok else "GLM (Z.ai) — key missing"
            popts += (f"<option value='glm' {'selected' if override == _bk.GLM else ''}>"
                      f"{glm_plabel}</option>")
        per_project = (
            f'<form method=post action=/api/model class=tbf '
            f'title="Model for {html.escape(app_name)} only">'
            f'<input type=hidden name="app" value="{html.escape(app_name)}">'
            '<span style="font-size:12px;color:#8a909c;margin-right:4px">This project</span>'
            f'<select name=backend onchange="this.form.submit()" style="font-size:13px">{popts}</select>'
            '</form>')
    return (
        '<form method=post action=/api/model class=tbf title="Model backend — applies to all runs">'
        '<span style="font-size:12px;color:#8a909c;margin-right:4px">Model</span>'
        f'<select name=backend onchange="this.form.submit()" style="font-size:13px">{opts}</select>'
        f'</form>{note}{per_project}')


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

    # ── Per-project "Ship <app>" DEV→MAIN button removed per EU-206 — the button no longer renders.
    # The underlying ship functionality remains intact: routes, sync module, and ship-preview page
    # are still available, only the per-project button was removed from the control bar.
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
        #  · 'Resume implementing' picks up pending work (In-Progress first, then To-Do) on the active backend.
        _conf_choose = (f"return confirm('Open the ticket picker for "
                        f"{html.escape(app0 or '')} to choose specific tickets to develop?')")
        _conf_drain = (f"return confirm('Resume implementing for "
                       f"{html.escape(app0 or '')}? "
                       f"The unit will work In-Progress tickets first, then To-Do, until the queue is empty or you press Stop.')")
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
            'title="Resume implementing — work In-Progress tickets first, then To-Do, until empty">'
            '&#9654;&nbsp;Resume implementing</button></form>'
            '</div>')

    # EU-106: global 'Open logs' button — macOS only (Darwin `open` command opens Finder).
    # Calls /api/open-logs with the configured log folder so a single click reveals ALL run logs.
    open_logs_html = ""
    if is_mac:
        log_folder = str(getattr(cfg, "log_folder", None) or "logs/")
        open_logs_html = _btn(
            "&#128194; Open logs", tag="a",
            attrs=(f' href="/api/open-logs?path={html.escape(quote(log_folder))}" '
                   f'title="Open the run-logs folder in Finder"'))

    # Render plan-limit banner BEFORE the control bar (if active)
    plan_banner = _plan_limit_banner(_state, cfg)

    # EU-190: prominent model-backend alert — a bad GLM setup (missing/incorrect token, wrong URL)
    # surfaces here with what to fix or re-onboard, so it's never missed. A transient
    # _state['model_alert'] (set by a failed /api/model connection test) shows once; a persistent
    # static-config problem re-derives every render while the active backend is GLM.
    _malert = _state.pop("model_alert", None)
    if not _malert:
        from . import backends as _bk, backend_pref as _bp
        if _bp.active(cfg) == _bk.GLM:
            _iss = _bk.glm_config_issues()
            if _iss:
                _malert = ("GLM is selected but not usable — " + "; ".join(_iss)
                           + ". Fix it in .env and restart, or set the Model back to Opus.")
    model_banner = ""
    if _malert:
        model_banner = (
            '<div role=alert style="background:#3a1113;border:1px solid #7f1d1d;color:#fecaca;'
            'padding:9px 13px;border-radius:8px;margin:0 0 8px;font-size:13px;display:flex;'
            'align-items:center;gap:12px;flex-wrap:wrap">'
            f'<span>&#9888;&#65039; {html.escape(_malert)}</span>'
            '<form method=post action=/api/model style="margin:0">'
            '<input type=hidden name=backend value=opus>'
            '<button style="font-size:12px;padding:3px 9px;border-radius:6px;cursor:pointer">'
            'Switch to Opus</button></form></div>')
    return tab_bar + plan_banner + model_banner + f"""
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
/* EU-299 (EU-285d) — toolbar clusters: group the flat button row into labeled sections
   (build · QA · nav) instead of one flat emoji row. Spacing uses the EU-296 --s-* scale;
   labels use the --t-xs type token — no new ad-hoc px literals. */
.tbar .tclu{{display:inline-flex;align-items:center;gap:var(--s-2)}}
.tbar .tclabel{{font-size:var(--t-xs);font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--faint);margin-right:var(--s-1)}}
.tbar .tcrow{{display:inline-flex;align-items:center;gap:var(--s-2);flex-wrap:wrap}}
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
  <div class=tclu>
    <span class=tclabel>build</span>
    <div class=tcrow>
      <details class=menu>
        {_btn("&#43; New task", tag="summary")}
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
      {backend_control(cfg, app0)}
      {ap_html}
      {ship_html}
    </div>
  </div>

  <div class=tclu>
    <span class=tclabel>QA</span>
    <div class=tcrow>
      <form method=post action=/api/patrol class=tbf onsubmit="return confirm('Run a patrol? QA Engineer + Security Engineer + Release Manager will inspect DEV and FILE findings as Jira tickets assigned to you.')"><input type=hidden name=app value="{html.escape(app0)}">{_btn("&#128225; Patrol", attrs=f' {busy("patrolling")}' if busy("patrolling") else "")}</form>
      <form method=post action=/api/ship-review class=tbf><input type=hidden name=app value="{html.escape(app0)}">{_btn("&#128640; Ship review", attrs=f' {busy("shipreview")}' if busy("shipreview") else "")}</form>
    </div>
  </div>

  <div class=tclu>
    <span class=tclabel>nav</span>
    <div class=tcrow>
      {_btn("&#128268; Jira", tag="a", attrs=f' href="/jira?app={html.escape(app0)}" title="Pick or connect the Jira this project uses"')}
      <a class="btn" href="/roster-doc" title="Officers &amp; duties — the full unit roster">&#128101; Roster</a>
      {open_logs_html}
      <details class=menu>
        {_btn("&#128202; Reports", tag="summary")}
        <div class="panel right">
          <a href="/tasks">&#128203; Task log{fr_tasks}</a>
          <a href="/council">&#128172; Daily muster &amp; meetings{fr_council}</a>
          <a href="/memory">&#128221; Unit memory{fr_mem}</a>
          <a href="/usage">&#128202; Token usage{fr_usage}</a>
          <a href="/budget">&#128176; Budget monitor</a>
          <a href="/forensics">&#129513; Failure forensics{fr_fx}</a>
          <a href="/roster-doc">&#128101; Unit roster</a>
        </div>
      </details>
    </div>
  </div>

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


def _chat_inner(cfg: Config, limit: int = 20, offset: int = 0) -> str:
    """Render the CTO chat thread, windowed to the last ``limit`` regular messages (EU-304).

    ``offset`` counts how many of the most-recent regular messages to skip before taking the next
    ``limit``-sized window going backwards — 0 (the default) is "the last ``limit`` messages" (used
    for the normal page render and the 5s auto-refresh poll); ``offset=limit`` is the next-older
    batch the 'load earlier' control fetches, ``offset=2*limit`` the one after that, and so on.

    When ``offset`` is non-zero this returns a BARE fragment (just ``.msg`` bubbles, no pinned
    cards, no ``.thread``/composer wrapper) meant to be prepended into the existing thread by the
    client — an empty string means there are no older messages left. The default (``offset=0``)
    render is unchanged in shape: pinned cards on top, then the ``.thread`` wrapper, plus a
    'load earlier' control when older messages exist.
    """
    from . import council, decisions
    all_bubbles = _chat_bubbles(_safe_chat_transcript(cfg))
    total = len(all_bubbles)
    window_limit = limit if limit and limit > 0 else total
    end = max(total - max(offset, 0), 0)
    start = max(end - window_limit, 0)
    window = all_bubbles[start:end]
    bubbles = ""
    for i, (who, text) in enumerate(window):
        # EU-305: stamp each bubble with its absolute index in the full transcript so the
        # auto-refresh poll can append-only (diff on data-seq) instead of replacing #cinner
        # wholesale. Stable across the default window and any 'load earlier' offset batch,
        # since it's always start + i into the same all_bubbles list.
        seq = start + i
        label = "You" if who == "you" else "CTO"
        bubbles += (f'<div class="msg {who}" data-seq="{seq}"><div class=who>{label}</div>'
                    f'<div class=bub>{html.escape(text)}</div></div>')

    if offset:
        # 'load earlier' batch fetch — just the older bubbles, nothing else, so the client can
        # prepend them into the live .thread without disturbing pinned cards or the composer.
        return bubbles

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

    if not bubbles and not cards:
        bubbles = ('<div class=cempty>No messages yet. When an officer needs a decision it shows '
                   'up here — or send the CTO a message below.</div>')
    load_earlier = ""
    if start > 0:
        load_earlier = (f'<button type=button class=load-earlier data-offset="{window_limit}" '
                         f'data-limit="{window_limit}" onclick="loadEarlierChat(this)">'
                         '&#8593; Load earlier messages</button>')
    return pending_html + load_earlier + f'<div class=thread>{bubbles}</div>'


def _safe_chat_transcript(cfg: Config) -> str:
    from . import council
    try:
        return council.chat_transcript(cfg, lines=400)
    except Exception:  # noqa: BLE001
        return ""


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
               ".load-earlier{display:block;margin:14px auto 0;background:var(--panel2);"
               "border:1px solid var(--line2);color:var(--dim);border-radius:var(--r-pill);"
               "padding:7px 15px;font:inherit;font-size:12px;font-weight:600;cursor:pointer}"
               ".load-earlier:hover{background:var(--line);color:var(--ink)}"
               ".load-earlier:disabled{opacity:.6;cursor:default}"
               ".thread{display:flex;flex-direction:column;gap:9px;margin:16px 0 96px}"
               ".msg{display:flex;flex-direction:column;max-width:80%}"
               ".msg.you{align-self:flex-end;align-items:flex-end}.msg.unit{align-self:flex-start}"
               ".who{font-size:10px;color:var(--faint);margin:0 6px 2px}"
               ".bub{padding:9px 13px;border-radius:var(--r-xl);font-size:13px;line-height:1.5;white-space:pre-wrap}"
               ".msg.unit .bub{background:var(--panel2);border:1px solid var(--line2);border-bottom-left-radius:4px}"
               ".msg.you .bub{background:#1e3a5f;border-bottom-right-radius:4px;color:#eaf1fb}"
               ".cempty{color:var(--dim);padding:30px 8px;text-align:center;font-size:13px}"
               ".typing{max-width:780px;margin:0 auto 8px;color:var(--faint);font-size:12px;font-style:italic}"
               ".composer{position:fixed;bottom:0;left:0;right:0;background:var(--bg);border-top:1px solid var(--line);padding:12px 30px}"
               ".composer form{max-width:780px;margin:0 auto;display:flex;gap:8px}.composer input{flex:1}"
               ".composer input.cerr{border-color:var(--bad);box-shadow:0 0 0 1px var(--bad)}"
               ".chaterr{display:none;max-width:780px;margin:0 auto 7px;color:var(--bad);font-size:12px;font-weight:600}"
               ".chaterr.on{display:block}"
               "</style>")


def _chat_tabs(active: str, npend: int = 0) -> str:
    badge = f'<span class=cbadge>{npend}</span>' if npend else ""
    g = "on" if active == "general" else ""
    gr = "on" if active == "group" else ""
    return (f'<div class=ctabs><a class="ctab {g}" href="/chat">&#128172; CTO{badge}</a>'
            f'<a class="ctab {gr}" href="/group">&#128101; Group room</a></div>')


def _group_inner(cfg: Config) -> str:
    from . import council
    msgs = council.group_messages(cfg, limit=30)   # EU-287: window to the recent messages, not the whole log
    if not msgs:
        return ('<div class=cempty>No messages yet. Ask the unit anything — the 1–2 relevant officers '
                'weigh in. (The CTO is your 1:1 chat.)</div>')
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
