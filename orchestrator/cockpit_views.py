"""Cockpit templates / view helpers — the inline-HTML builders.

Split out of ``server.py`` (F16: decompose templates/routes/state) so the presentation
layer (page chrome, the control bar, the chat renderer, action buttons) lives
apart from the Flask route handlers. These functions take plain data + ``Config`` and
return HTML strings — they never touch ``request``/``redirect``. ``server`` re-exports
them, so ``server._control_bar`` / ``server._wrap`` etc. stay valid for callers and tests.
"""
from __future__ import annotations

import html
import os
import time
from pathlib import Path

from . import dashboard as D
from .cockpit_state import _state, get_autopilot_status, get_state
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
    "--positive:var(--ok);--critical:var(--bad)}"
    # 2026-07-19 theme pass — mirrors _PAGE's extra tokens + light override (kept in sync by
    # the live extraction below; this fallback only serves tests / offline previews).
    ":root{--well:#0d1119;--console:#070a0e;--console-ink:#b9c2cf;--accent-hover:#2f5ce0}"
    ":root[data-theme=light]{color-scheme:light;"
    "--bg:#eef1f6;--panel:#ffffff;--panel2:#f2f4f9;--line:#dde3ec;--line2:#c7d1e0;"
    "--ink:#1c2536;--dim:#5a6578;--faint:#8b95a7;"
    "--ok:#0f9d63;--okbg:#e2f5ec;--okline:#aadfc6;"
    "--warn:#a8720f;--warnbg:#faf0d9;--warnline:#e8d5a5;"
    "--bad:#cf3a40;--badbg:#fae5e6;--badline:#efbfc1;"
    "--info:#2563c9;--infobg:#e7effc;--infoline:#c2d6f3;"
    "--accent:#3b62d9;--accentbg:#e8edfb;--accentline:#c4d1f1;"
    "--well:#e7ebf3;--console:#f7f9fc;--console-ink:#33415c;--accent-hover:#2f54c4}")

# Applies the saved theme BEFORE first paint on every page that injects the tokens, so
# sub-pages follow the War Room header's toggle with no flash.
_THEME_BOOT = ("<script>try{document.documentElement.dataset.theme="
               "localStorage.getItem('ui.theme')||'dark'}catch(e){}</script>")


def _token_css() -> str:
    """The slice-1 design tokens as a ``<style>:root{…}</style>`` block, so every standalone
    cockpit page shares ONE palette source with the War Room (EU-39). Read live from
    ``warroom._PAGE``; falls back to ``_TOKENS_FALLBACK`` when unavailable."""
    try:
        import re

        from . import warroom
        # 2026-07-19: grab the WHOLE token region — the dark :root, the extra-token :root, and
        # the [data-theme=light] override — up to the END THEME TOKENS sentinel, so light mode
        # flows to every standalone page from the one source in _PAGE.
        m = re.search(r":root\{.*?/\* END THEME TOKENS \*/", warroom._PAGE, re.S)
        if not m:
            m = re.search(r":root\{[^}]*\}", warroom._PAGE)
        if m:
            return "<style>" + m.group(0) + "</style>" + _THEME_BOOT
    except Exception:  # noqa: BLE001 - tests / preview render without the War Room module loaded
        pass
    return "<style>" + _TOKENS_FALLBACK + "</style>" + _THEME_BOOT


def _back_home() -> str:
    """The cockpit URL to return to — carries the active ?app= so 'back to cockpit' lands on the project
    you were in, not 'All projects'. Reads it from the request when there is one; falls back to '/'."""
    try:
        from flask import request
        appq = (request.args.get("app") or "").strip()
    except Exception:  # noqa: BLE001 - rendered outside a request context (tests / previews)
        appq = ""
    return f"/?app={html.escape(appq)}" if appq else "/"


def _back_btn(home_url: str) -> str:
    """Shared floating back-to-cockpit button (EU-542).

    Renders a small ``position:fixed`` arrow-back link above all page content so it stays
    visible at ANY scroll depth — exactly what the Commander asked for ("a back to cockpit
    arrow button that will move with the chat"). Theme tokens only (no hex literals), so it
    re-skins from the single :root block and is correct in dark AND light. Keyboard
    accessible: a real ``<a>``, tab-focusable, Enter navigates, ``--ring`` on focus-visible.

    The PM's EU-542 placement call is baked in:
      * a subtle token-based translucent backdrop (color-mix over ``--panel2`` + blur, gated
        behind @supports with a solid token fallback) so any mid-scroll overlap stays legible;
      * below 560px width it collapses to an icon-only 38px disc so the message column is
        never covered;
      * offsets and the caller-side clearance both include ``env(safe-area-inset-*)`` so a
        notched device never overlaps the button OR the content under it.
    Callers clear the footprint below the button: ``_wrap`` pads the body's top, the /tasks
    route indents the dashboard header.
    """
    return (
        "<style>"
        ".backbtn{position:fixed;z-index:100;"
        "top:calc(16px + env(safe-area-inset-top,0px));"
        "left:calc(16px + env(safe-area-inset-left,0px));"
        "display:inline-flex;align-items:center;gap:9px;padding:11px 16px;"
        "background:var(--panel2);border:1px solid var(--line2);border-radius:var(--r-pill);"
        "color:var(--ink);font-size:13.5px;font-weight:650;text-decoration:none;"
        "transition:all var(--t-fast);box-shadow:var(--shadow-2);animation:bbeu542 .22s ease-out}"
        "@keyframes bbeu542{from{opacity:0;transform:translateY(-5px)}to{opacity:1;transform:none}}"
        # Translucent token backdrop — the @supports gate keeps older browsers on the solid
        # token background above instead of a fully transparent (unreadable) button.
        "@supports(background:color-mix(in srgb,red 50%,blue)){"
        ".backbtn{background:color-mix(in srgb,var(--panel2) 85%,transparent);"
        "backdrop-filter:blur(9px);-webkit-backdrop-filter:blur(9px)}}"
        ".backbtn svg{width:17px;height:17px;transition:transform var(--t-fast);flex:none}"
        ".backbtn:hover{background:var(--line);border-color:var(--accent);color:var(--accent);"
        "transform:translateX(-3px);box-shadow:var(--shadow-3)}"
        ".backbtn:hover svg{transform:translateX(-2px)}"
        ".backbtn:active{transform:translateX(-1px)}"
        ".backbtn:focus-visible{outline:none;box-shadow:var(--ring)}"
        # Narrow screens: icon-only 38px disc tucked into the corner, hugging the safe area.
        "@media(max-width:560px){"
        ".backbtn{top:calc(10px + env(safe-area-inset-top,0px));"
        "left:calc(10px + env(safe-area-inset-left,0px));width:38px;height:38px;padding:0;"
        "justify-content:center}"
        ".backbtn .bbl{display:none}"
        ".backbtn svg{width:19px;height:19px}}"
        "@media(prefers-reduced-motion:reduce){.backbtn{animation:none;transition:none}}"
        "</style>"
        f"<a class='backbtn' href='{home_url}' aria-label='Back to cockpit'>"
        f"<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.5' "
        f"stroke-linecap='round' stroke-linejoin='round' aria-hidden=true>"
        f"<path d='M19 12H5M12 19l-7-7 7-7'/></svg><span class=bbl>cockpit</span></a>"
    )


def _wrap(title: str, inner: str) -> str:
    """Page chrome with a **floating** back-to-cockpit button (EU-542)."""
    # body top-padding clears the fixed button's footprint (PM EU-542 call): 66px ≥ the
    # desktop pill's bottom edge (16px offset + ~46px box), and still ≥ the 38px mobile disc
    # (10px offset) — plus the safe-area inset on notched devices, so the first heading /
    # chat bubble / timestamp always starts BELOW the button and is never obscured.
    return ("<!doctype html><meta charset=utf-8><title>" + html.escape(title) + "</title>"
            + _token_css() +
            "<style>*{box-sizing:border-box}"
            "body{background:radial-gradient(1100px 440px at 80% -10%,rgba(77,124,255,.08),transparent 60%),"
            "var(--bg);color:var(--ink);font:14px/1.6 -apple-system,BlinkMacSystemFont,"
            "\"Segoe UI\",Inter,sans-serif;margin:0;"
            "padding:calc(66px + env(safe-area-inset-top,0px)) 30px}a{color:var(--info)}"
            ".rep{white-space:pre-wrap;background:var(--panel);border:1px solid var(--line);"
            "border-radius:var(--r-lg);padding:16px}"
            "textarea,select,input{background:var(--panel);border:1px solid var(--line2);color:var(--ink);"
            "border-radius:var(--r-md);padding:8px;font:inherit}"
            "button{background:var(--accent);border:0;color:#fff;border-radius:var(--r-md);padding:9px 16px;"
            "font-weight:650;cursor:pointer}"
            "a:focus-visible,button:focus-visible,select:focus-visible,textarea:focus-visible,"
            "input:focus-visible{outline:none;box-shadow:var(--ring)}</style>"
            + _back_btn(_back_home())
            + f"<h2>{html.escape(title)}</h2>{inner}")


def _working(msg: str, secs: int = 5) -> str:
    """A live 'working…' panel: spinner + indeterminate progress bar + auto-refresh, so a long
    engineer task (drill/council/scribe/standup) shows progress instead of a dead 'reload later'."""
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
    """A single on-page engineer-action button (POST form). These actions used to live in the
    'Unit' toolbar menu; they now sit on the page that shows their result, so each activity
    appears exactly once."""
    hidden = f'<input type=hidden name=app value="{html.escape(app)}">' if app else ""
    onsub = f" onsubmit=\"return confirm('{confirm}')\"" if confirm else ""
    return (f"<form method=post action={action} style='margin:0'{onsub}>{hidden}"
            f"{_btn(label, cls='actbtn')}</form>")


def _actbar(*items: str) -> str:
    """A row of on-page engineer-action controls. Buttons render via ``_btn`` (EU-297); this
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
    EU-289: the toolbar's '+ New task → Bug' panel was the other caller until it was removed
    (intake is Jira-only); the /report form + /api/report are what still route through here."""
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


# EU-677: removed — superseded by _result_strip (non-destructive peek+dismiss).
# Both index() and the board/SSE path now use _result_strip; no live callers need pop semantics.
# Legacy text references below (_plan_limit_banner docstring, _result_strip docstring) mention it
# for historical context; see Documentation/PEEK_DISMISS_MIGRATION.md for the full migration story.

def _peek_last_result(state: dict) -> dict | None:
    """Read (without popping) ``last_result_record`` from *state*.

    EU-670: non-destructive peek so the board can render the result strip repeatedly
    across SSE ticks / polls. Returns None when nothing is pending.
    """
    rec = state.get("last_result_record")
    if isinstance(rec, dict):
        return rec
    return None


def _result_strip(state: dict) -> str:
    """Render the last-run result as a thin, tone-styled strip with a dismiss button.

    Non-destructive — never pops state. Returns ``""`` when there is no pending result record.
    Tone colours mirror ``_result_banner`` (only "error" → bad).

    Fallback: if no ``last_result_record`` exists, reads the legacy ``last_result`` string
    directly (backward-compatible with EU-31 callers that write plain strings).

    EU-670: rendered inside the live board (prepended by ``warroom.render_board``);
    the JS hook ``data-dismiss-result`` is wired in EU-675. The full-page GET / shows it via
    the board embedded in the page (EU-673 removed the duplicate bar strip from index()), so
    initial render, 5s poll and SSE stream all carry the exact same strip.

    EU-713: when the record carries a ``timestamp`` (epoch seconds), the strip also emits
    an empty ``.result-age`` span with ``data-ts``; the board JS ticker in warroom paints
    it as a relative string ("2m ago") and re-renders it every 30s — purely client-side.
    """
    rec = _peek_last_result(state)
    if not rec:
        # Legacy fallback: plain-string writers that don't set last_result_record.
        msg = (state.get("last_result") or "").strip()
        if not msg:
            return ""
        fg, border, bg = ("var(--bad)", "var(--badline)", "var(--badbg)")
        return (f"<div style='background:{bg};border-bottom:1px solid {border};"
                f"color:{fg};padding:8px 26px;font-size:13px;display:flex;"
                f"justify-content:space-between;align-items:center'>"
                f"{html.escape(msg)}"
                f"<button data-dismiss-result style='margin-left:12px;padding:2px 10px;"
                f"cursor:pointer;border:1px solid var(--okline);background:transparent;"
                f"color:inherit;border-radius:4px'>Dismiss</button>"
                f"</div>")
    msg = (rec.get("text", "") or "").strip()
    if not msg:
        return ""
    bad = rec.get("tone") == "error"
    fg, border, bg = (("var(--bad)", "var(--badline)", "var(--badbg)") if bad
                      else ("var(--ok)", "var(--okline)", "var(--okbg)"))
    # EU-713: emit the record's timestamp (epoch seconds) as data-ts on an empty
    # .result-age span; the board JS ticker in warroom fills it with a relative string
    # ("just now" → "2m ago") and keeps it ageing purely client-side — no endpoint
    # calls. Records without a usable timestamp (legacy writers) render no span.
    age = ""
    try:
        ts = float(rec.get("timestamp"))
    except (TypeError, ValueError):
        ts = 0.0
    if ts > 0:
        age = (f"<span class=result-age data-ts='{ts:.3f}' style='margin-left:10px;"
               f"font-size:11px;color:var(--dim);white-space:nowrap'></span>")
    return (f"<div style='background:{bg};border-bottom:1px solid {border};"
            f"color:{fg};padding:8px 26px;font-size:13px;display:flex;"
            f"justify-content:space-between;align-items:center'>"
            f"<span style='flex:1;min-width:0'>{html.escape(msg)}{age}</span>"
            f"<button data-dismiss-result style='margin-left:12px;padding:2px 10px;"
            f"cursor:pointer;border:1px solid var(--okline);background:transparent;"
            f"color:inherit;border-radius:4px'>Dismiss</button>"
            f"</div>")


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
        _last = state.get("last_run") or {}
        # EU-223: name the affected app (the paused run's project) so the switch — and the
        # server-side handler — flips only THAT app's backend, not the whole unit.
        _affected_app = (_last.get("app") or "").strip() or None
        # EU-242: resolve the CURRENT backend through the affected app, not the global pref. The
        # switch this banner renders is app-scoped (it posts `app=` and the handler calls
        # set_active(..., app_name=app_param), server.py:1183-1186), so a global-derived _cur made
        # the offer disagree with the thing being switched whenever that app carried an EU-223
        # override: global=GLM + app=Opus offered "Continue on Opus" to an app already on Opus,
        # which cleared the banner and auto-resumed straight back into the same limit. active()
        # falls through to the global pref when there's no override, so the no-override path is
        # byte-for-byte unchanged.
        _cur = _bk.normalize(_bp.active(cfg, _affected_app) if cfg is not None else _bk.NATIVE)
        backend_label = "Claude (Opus)" if _cur == _bk.NATIVE else "GLM (Z.ai)"
        _alts = _bk.alternates(_cur)
        if _alts:
            _alt = _alts[0]
            _label = "GLM (Z.ai)" if _alt == _bk.GLM else "Claude (Opus)"
            _tickets = [t for t in (_last.get("tickets") or []) if t]
            _resume = (" &amp; resume " + html.escape(", ".join(_tickets))) if _tickets else ""
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
        note = "<span class=\"tbnote dim\" title=\"GLM is a third-party provider — prompts (code, tickets, diffs) leave Anthropic\">&#8599; prompts go to Z.ai</span>"
    else:
        note = ""
    # 2026-07-19 (Commander order): the toolbar speaks MAIN + SECONDARY, not global/inherit.
    # The per-app override (EU-223, for dual drains) still exists in backend_pref/api — it just
    # no longer renders here; the toolbar's job is the simple mental model:
    #   Main model      — what the unit runs on.
    #   Secondary       — the stand-in it switches to (loudly) when the main can't run
    #                     (Claude plan limit, GLM token missing). None = pause instead (old world).
    #   ＋ Add model    — the /models registry page (add a backend + API key, test connection).
    secondary = backend_pref.get_secondary(cfg)
    mode = backend_pref.get_mode(cfg)   # 'hybrid' | 'backup' — only shown when a Secondary is set
    sec_opts = f"<option value='none' {'selected' if not secondary else ''}>None</option>"
    for _entry in _bk.list_backends(registry=ModelRegistry(cfg)):
        _bid = _entry["id"]
        if _bid == active:
            continue                      # the main model can't be its own stand-in
        if _bid == _bk.GLM and not show_glm:
            continue
        _lbl = ("Opus (Claude)" if _bid == _bk.NATIVE
                else ("GLM (Z.ai)" if _bid == _bk.GLM
                      else html.escape(_entry.get("label") or _bid)))
        sec_opts += (f"<option value='{html.escape(_bid)}' "
                     f"{'selected' if secondary == _bid else ''}>{_lbl}</option>")
    fb_note = ""
    try:
        _run_bk, _fb_why = _bk.resolve_for_run(cfg, app_name)
        if _fb_why:
            fb_note = ('<span class="tbnote run" title="' + html.escape(_fb_why) + '">'
                       '&#8644; running on secondary</span>')
    except Exception:  # noqa: BLE001
        pass
    return (
        '<form method=post action=/api/model class=tbf '
        'title="Main model — what the unit runs on (all projects)">'
        '<span class=tbsel-label>Main model</span>'
        f'<select name=backend onchange="this.form.submit()" style="font-size:13px">{opts}</select>'
        f'</form>{note}'
        '<form method=post action=/api/model class=tbf '
        'title="Secondary model — the unit switches to it (loudly) when the main can&#39;t run: '
        'Claude plan limit hit, or GLM key missing. None = pause and wait instead.">'
        '<span class=tbsel-label>Secondary</span>'
        f'<select name=secondary onchange="this.form.submit()" style="font-size:13px">{sec_opts}</select>'
        f'</form>{fb_note}'
        # 2026-07-19 (Commander order): with TWO models, choose HOW they work — Hybrid (both,
        # per task: heavy thinking on Main, building on Secondary) or Backup (everything on the
        # Main; the Secondary wakes only when the Main hits its limit / breaks). The selector
        # appears only when a Secondary exists; one model = nothing to choose.
        + (('<form method=post action=/api/model class=tbf '
            'title="Hybrid — both models work per task: plan/PRD, review and debug judgment on '
            'the Main; regular building on the Secondary. '
            'Backup — everything runs on the Main; the Secondary takes over only when the Main '
            'can&#39;t run (plan limit hit, key broken).">'
            '<span class=tbsel-label>Mode</span>'
            '<select name=mode onchange="this.form.submit()" style="font-size:13px">'
            f"<option value='hybrid' {'selected' if mode == 'hybrid' else ''}>Hybrid — both, per task</option>"
            f"<option value='backup' {'selected' if mode == 'backup' else ''}>Backup — if main runs out</option>"
            '</select></form>'
            + ('<span class="tbnote dim" title="Plan/PRD, review and judgment on the Main model; '
               'regular building on the Secondary.">&#9878; plan on main &middot; build on secondary</span>'
               if mode == 'hybrid' else
               '<span class="tbnote dim" title="Everything runs on the Main model; the Secondary '
               'only takes over when the Main hits its limit or breaks.">'
               '&#128737; standby: takes over at the limit</span>'))
           if secondary else "")
        + '<a class=btn href="/models" style="height:30px;font-size:12px;padding:0 10px" '
        'title="Add / manage model backends (API key, base URL, connection test)">&#10133; Add model</a>'
        )



def _control_bar(cfg: Config, current_app: str | None = None, healthy: bool = True,
                 is_mac: bool = False) -> str:
    """Render the cockpit's top control bar.

    EU-106 / EU-632: global '📂 Open logs' → real nav to ``/logs/days?app=<app>`` (works on
    every OS). When ``is_mac=True``, a small secondary "reveal in Finder" link still fires
    ``/api/open-logs`` via fetch() behind the scenes.
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
    # EU-289: the app/effort <select> options and the run_dis gate lived only in the "+ New task"
    # panel, which is gone (intake is Jira-only) — so they went with it. The /api/run route itself
    # stays for scripted use; only the affordance was removed.
    # EU-646: surface per-project start/stop messages. Read the active app's own ``last_msg``
    # FIRST (set by ``_claim_cockpit_run``, ``run_selected_api``, ``autopilot_api``, etc.);
    # render it once, then pop so it doesn't repeat across unrelated GET / requests. Fall back
    # to the global ``_state["last_msg"]`` only when the per-app slot is empty.
    app_st = get_state(app0) if app0 else _state
    status = ""
    app_msg = (app_st.get("last_msg") or "").strip() if app0 else None
    global_msg = (_state.get("last_msg") or "").strip()
    if app_msg:
        # Per-app message has priority — render it, then clear so it shows exactly once.
        # EU-646 iter-2: tone from the stored record (written by ``server.set_last_msg``), NOT
        # from substring matches on the message text — the SAME contract as the global fallback
        # branch below (EU-656): the writer states the tone, the view only maps it. A missing
        # record (legacy raw ``st['last_msg'] = ...`` writer) defaults to "dim" (neutral).
        _m = app_msg
        rec = app_st.get("last_msg_record")
        if rec and isinstance(rec, dict):
            rtone = rec.get("tone", "")
            _tone_map = {"error": "bad", "warn": "dim", "ok": "ok"}
            _tone = _tone_map.get(rtone, "dim")
        else:
            _tone = "dim"  # legacy / raw assignment without set_last_msg → neutral
        status = f'<span class="tbnote {_tone}">{html.escape(_m)}</span>'
        # Clear immediately after rendering — one-shot, no persistence. Both keys go back to
        # their _new_state defaults so a stale record can never re-tone a later message.
        app_st["last_msg"] = ""
        app_st["last_msg_record"] = None
    elif global_msg:
        # Fallback to the global last_msg (unit-level actions like ship/promote/patrol).
        # EU-656: tone from stored record, NOT content. Zero substring checks remain here.
        _m = global_msg
        rec = _state.get("last_msg_record")
        if rec and isinstance(rec, dict):
            rtone = rec.get("tone", "")
            _tone_map = {"error": "bad", "warn": "dim", "ok": "ok"}
            _tone = _tone_map.get(rtone, "dim")
        else:
            _tone = "dim"  # legacy / raw assignment without set_last_msg → neutral
        status = f'<span class="tbnote {_tone}">{html.escape(_m)}</span>'
    else:
        status = ""

    def busy(k: str) -> str:
        return "disabled" if _state.get(k) else ""

    # ── Per-project "Ship <app>" DEV→MAIN button removed per EU-206 — the button no longer renders.
    # The underlying ship functionality remains intact: routes, sync module, and ship-preview page
    # are still available, only the per-project button was removed from the control bar.
    ship_html = ""

    # QA report card (EU-581): shown when _state["qa_dismissed"] is False AND there's actual data.
    # Replaces transient last_msg with a persistent card that stays until dismissed.
    qa_findings = list(_state.get("qa_findings") or [])
    qa_verdict = (_state.get("qa_verdict") or "").strip()
    _qrd = _state.get("qa_dismissed")
    qa_report_html = ""
    if not _qrd and (qa_findings or qa_verdict):
        base_url = D._jira_base_for(cfg, app0)
        findings_list = ""
        if qa_findings:
            n = len(qa_findings)
            link_items = "".join(
                f'<a href="{html.escape(base_url)}/browse/{html.escape(k)}" '
                f'target=_blank rel=noopener>{html.escape(k)}</a>'
                for k in qa_findings
            )
            findings_list = (f'{n} finding{"s" if n != 1 else ""} filed: ' + link_items)
        else:
            findings_list = "no new findings"
        verdict_cls = ("ok" if "go" in (qa_verdict or "").lower() else "bad")
        qa_report_html = (
            '<div class="qa-report" role="status"'
            ' style="background:var(--panel);border:1px solid var(--line);'
            'border-radius:var(--r-lg);padding:var(--s-3) var(--s-4);margin:0 0 14px">'
            '<div class="qa-report-title" style="font-size:var(--t-md);font-weight:700;'
            'color:var(--ink);margin-bottom:var(--s-2)"'
            '>&#128203; Findings Report</div>'
            f'<div class="qa-report-findings" style="margin-bottom:var(--s-2)">{findings_list}</div>'
            '<div class="qa-report-verdict" style="font-size:var(--t-md);font-weight:700;'
            f'color:var(--{verdict_cls})">{html.escape(qa_verdict or "(no verdict)")}'
            '</div>'
            '<form method=post action=/api/qa-dismiss style="margin-top:var(--s-2);margin-bottom:0">'
            '<button type=submit style="font-size:var(--t-xs);font-weight:600;padding:'
            '4px 10px;border-radius:var(--r-sm);background:var(--panel2);border:1px solid var(--line)'
            ';color:var(--dim);cursor:pointer">&#10003; Dismiss</button></form>'
            '</div>')

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
        # 2026-07-19: "Update memory" (the scribe) writes the LIVING log (UNIT.live.md), not the
        # Commander doctrine (UNIT.md) — the stamp read only the doctrine, so it stayed "22d ago"
        # after every update. Show the NEWER of the two so the stamp tracks either write.
        _mt = [d for d in (_wr._mtime(_mem.UNIT_PATH), _wr._mtime(_mem.LIVE_PATH)) if d]
        fr_mem = _fresh(max(_mt)) if _mt else ""
    except Exception:  # noqa: BLE001
        fr_mem = ""

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

    # QA progress strip: live two-phase indicator while a QA run is in flight.
    # Reuses the same poll-and-reload pattern as deploy_strip; points at /api/qa-status.
    # Server-side rendered so a mid-run refresh shows the correct current phase.
    qa_strip = ""
    if _state.get("qa"):
        _qs = _state.get("qa_started") or 0
        _qelapsed = int(time.time() - _qs) if isinstance(_qs, (int, float)) else 0
        _mm, _ss = divmod(_qelapsed, 60)
        # None+active = claim-time race window → show phase 1.
        # qlabel carries a LITERAL '&': the single html.escape() below encodes it once
        # (a pre-escaped '&amp;' here would double-escape to '&amp;amp;' in the browser).
        if _state.get("qa_phase") == "ship_review":
            qlabel = "Phase 2/2 — ship verdict"
        else:
            qlabel = "Phase 1/2 — inspecting dev & filing findings"
        # NOTE: the inline script is ONE self-contained snippet — keep each JS string
        # literal inside a single Python string (iter-1 split 'Phase 1/2…' across two
        # Python literals, which produced unparseable JS), and end with exactly one
        # IIFE close `})();`. The '&' inside the JS string literal stays literal too:
        # <script> content is raw text, HTML entities are NOT decoded there.
        qa_strip = (
            f'<div class="qa-strip"><span id=qaphase>{html.escape(qlabel)}</span>'
            f' <span id=qaelapsed>{_mm:02d}:{_ss:02d}</span></div>'
            "<script>(function(){var el=document.getElementById('qaphase');"
            "var ta=document.getElementById('qaelapsed');function p(){fetch('/api/qa-status')"
            ".then(function(r){return r.json()}).then(function(d){"
            "if(el){el.textContent=(d.phase==='ship_review')"
            "?'Phase 2/2 — ship verdict':'Phase 1/2 — inspecting dev & filing findings'}"
            "var m=Math.floor(d.elapsed_s/60);var s=d.elapsed_s%60;"
            "if(ta){ta.textContent=String(m).padStart(2,'0')+':'+String(s).padStart(2,'0')}"
            "if(!d.active){location.reload()}else{setTimeout(p,1500)}})"
            ".catch(function(){setTimeout(p,2500)})}setTimeout(p,1500)})();</script>")

    # QA failure state (EU-582): shown when a QA run completed with an error phase,
    # but NO active run is in flight (the progress strip covers it while qa is truthy).
    # Displays plain-language failure description + any partial findings + one-click Retry.
    qa_failure_html = ""
    _ep = _state.get("qa_error_phase")
    if _ep and not _state.get("qa"):   # error exists AND no active run running
        # Map internal phase name → human label matching the progress strip wording.
        if _ep == "ship_review":
            _phase_label = "Phase 2 (ship verdict)"
        else:
            # Everything else (including bare "patrol" or whatever future phases get added)
            # maps to Phase 1 with the same wording as the progress strip.
            _phase_label = "Phase 1 (inspecting dev & filing findings)"
        _error_msg = html.escape(_state.get("last_result", "") or "")
        qa_failure_html = (
            '<div role=status style="background:var(--badbg);border:1px solid var(--badline);'
            'border-radius:var(--r-lg);padding:var(--s-3) var(--s-4);margin:0 0 14px">'
            '<div style="font-weight:700;color:var(--bad);margin-bottom:var(--s-2)"'
            f'>&#9888; QA failed during {_phase_label}: {_error_msg}</div>'
        )
        # Render partial findings (already filed by a surviving phase).
        _partial_findings = list(_state.get("qa_findings") or [])
        if _partial_findings:
            _base_url = D._jira_base_for(cfg, app0)
            _f_links = "".join(
                f'<a href="{html.escape(_base_url)}/browse/{html.escape(k)}" '
                f'target=_blank rel=noopener>{html.escape(k)}</a>'
                for k in _partial_findings
            )
            _n = len(_partial_findings)
            qa_failure_html += (
                f'<div style="margin-bottom:var(--s-2);font-size:var(--t-sm)">'
                f'{_n} finding{"s" if _n != 1 else ""} already filed: {_f_links}</div>')
        # One-click retry form: POST /api/qa with hidden app field.
        _retry_app = _state.get("qa_app") or app0
        qa_failure_html += (
            '<form method=post action=/api/qa style="margin-top:var(--s-2);margin-bottom:0">'
            f'<input type=hidden name=app value="{html.escape(_retry_app)}">'
            '<button type=submit style="font-size:var(--t-xs);font-weight:600;padding:'
            '4px 10px;border-radius:var(--r-sm);background:var(--bad);color:var(--badtxt);'
            'border:none;cursor:pointer">&#128260; Retry</button></form>'
            '</div>')

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
        # Drain in progress: show the "finishing…" label WITH a Stop button so a long run
        # can be cut short mid-drain — EU-689. The stop action reuses /api/autopilot which
        # already handles app-scoped stop_event.set() + autopilot_on=False for this app.
        ap_html = (
            '<div class="tbap stopping" title="Finishing current ticket, then standing down">'
            '<span class="apdot-sm stop"></span>'
            '<span class=tbaplabel>&#9203;&nbsp;Stopping&hellip;</span>'
            f'<form method=post action=/api/autopilot class=tbf>'
            f'<input type=hidden name=action value=stop>'
            f'<input type=hidden name=app value="{ap_appq}">'
            '<button class="aptbtn stop" '
            'title="Mark autopilot off now — the in-flight build still finishes in the background">'
            'Stop</button></form>'
            '</div>')
    elif ap_on:
        # Autopilot running: offer graceful drain or hard stop.
        # EU-120: when external daemon is running, mark it as external in the label.
        ap_label = f'Autopilot&nbsp;<b>ON</b>&nbsp;<span class=ext>(external)</span>&nbsp;&middot;&nbsp;{ap_appq}' if ap_external else f'Autopilot&nbsp;<b>ON</b>&nbsp;&middot;&nbsp;{ap_appq}'
        ap_class = "tbap on ext" if ap_external else "tbap on"
        ap_html = (
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
        # 2026-07-19 (Commander order): NO confirm popups — these are explicit clicks with
        # visible, stoppable outcomes (the picker is pure navigation; a drain has a Stop button).
        ap_html = (
            '<div class="tbap off">'
            '<span class="apdot-sm off"></span>'
            '<span class=tbaplabel>Autopilot</span>'
            f'<form method=post action=/api/autopilot class=tbf>'
            f'<input type=hidden name=action value=start>'
            f'<input type=hidden name=app value="{ap_appq}">'
            '<input type=hidden name=mode value=choose>'
            f'<button class="aptbtn start" {ap_dis} '
            'title="Pick specific tickets to develop (opens the ticket picker)">'
            '&#127915;&nbsp;Choose tickets</button></form>'
            f'<form method=post action=/api/autopilot class=tbf>'
            f'<input type=hidden name=action value=start>'
            f'<input type=hidden name=app value="{ap_appq}">'
            '<input type=hidden name=mode value=drain>'
            f'<button class="aptbtn start" {ap_dis} '
            'title="Resume implementing — work In-Progress tickets first, then To-Do, until empty">'
            '&#9654;&nbsp;Resume implementing</button></form>'
            '</div>')

    # EU-632: primary "Open logs" → REAL NAVIGATION to the in-cockpit day-list view
    # (/logs/days?app=<app>) — a page to browse, not a fire-and-forget fetch().  Works on every
    # OS, so the EU-106 Mac gate is GONE (the old /api/open-logs primary path 403'd off-Mac).
    # Routed through the _btn partial like the rest of the nav cluster (EU-299 AC3).
    # EU-106 remnant: a tiny secondary "Finder" reveal via fetch('/api/open-logs') stays,
    # macOS-only (Darwin `open`) — a fallback, never the primary path.
    app_q = html.escape(app0 or "")
    open_logs_html = _btn(
        "&#128194; Open logs", tag="a",
        attrs=(' href="/logs/days?app=' + app_q + '" '
               'title="Browse this project\'s run logs by day"'))
    if is_mac:
        open_logs_html += (
            ' <a style="font-size:11px;color:var(--info)" '
            'href="/api/open-logs" '
            'onclick="fetch(this.href);return false" '
            'title="Reveal the logs folder in Finder (macOS only)">Finder</a>'
        )

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
            '<div role=alert style="background:var(--badbg);border:1px solid var(--badline);color:var(--bad);'
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
.tbar{{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:var(--s-3);align-items:stretch;padding:var(--s-3) 26px;border-bottom:1px solid var(--line);background:var(--panel)}}
/* Row 1: run | build (stretches to absorb slack) | QA.  Row 2: the nav strip, full width —
   so both rows run edge-to-edge and nothing floats in dead space. */
.tbar>.tclu:nth-of-type(4){{grid-column:1 / -1;flex-direction:row;align-items:center;gap:var(--s-3)}}
/* 2026-07-19: the NAV row stretches its buttons edge-to-edge so it fills the width like the
   action row above — no dead space to the right. The label stays natural-width; the button row
   grows, and each button shares the space evenly. */
.tbar>.tclu:nth-of-type(4)>.tcrow{{flex:1;flex-wrap:nowrap}}
.tbar>.tclu:nth-of-type(4)>.tcrow>.btn{{flex:1;justify-content:center}}
.tbar>.tbnote{{grid-column:1 / -1}}
.tbar>.grow{{display:none}}
/* 2026-07-19 redesign: each cluster is a quiet card — label as an overline INSIDE the group —
   so the bar reads as run · build · QA · nav sections instead of scattered buttons. */
.tbar .tclu{{display:flex;flex-direction:column;gap:var(--s-1);justify-content:center;background:var(--panel2);border:1px solid var(--line);border-radius:var(--r-lg);padding:var(--s-2) var(--s-3)}}
.tbar .btn{{display:inline-flex;align-items:center;gap:7px;height:34px;background:var(--panel);border:1px solid var(--line2);color:var(--ink);border-radius:var(--r-md);padding:0 13px;font:inherit;font-size:13px;font-weight:600;cursor:pointer;text-decoration:none;white-space:nowrap;transition:background var(--t-fast),border-color var(--t-fast)}}
.tbar .btn:hover{{border-color:var(--accent)}}
.tbar .btn.primary{{background:var(--accent);border-color:var(--accent);color:#fff}}
.tbar .btn.primary:hover{{background:var(--accent-hover)}}
.tbar select{{height:34px;background:var(--panel);border:1px solid var(--line2);color:var(--ink);border-radius:var(--r-md);padding:0 8px;font:inherit;font-size:13px;cursor:pointer}}
.tbar select:hover{{border-color:var(--accent)}}
.tbar .tbsel-label{{font-size:var(--t-xs);color:var(--faint);margin-right:2px}}
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
.tbar .panel.form button:disabled{{background:var(--line);color:var(--faint);cursor:not-allowed}}
.tbar .panel .sep{{height:1px;background:var(--line);margin:5px 4px}}
.tbar .panel .ph{{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--faint);padding:6px 11px 3px}}
.tbar .tbnote{{font-size:12px;margin-left:2px}}.tbar .tbnote.dim{{color:var(--faint)}}.tbar .tbnote.run{{color:var(--warn)}}.tbar .tbnote.bad{{color:var(--bad)}}.tbar .tbnote.ok{{color:var(--ok);font-weight:600}}
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
.tbar .tclabel{{font-size:var(--t-xs);font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--faint)}}
.tbar .tcrow{{display:inline-flex;align-items:center;gap:var(--s-2);flex-wrap:wrap}}
/* EU-103 — per-project Autopilot section */
.tbar .tbap{{display:inline-flex;align-items:center;gap:8px;padding:0;border:0;background:none}}
.tbar .tbap.on{{border-color:var(--okline);background:var(--okbg)}}
.tbar .tbap.on.ext{{border-color:var(--infoline);background:var(--infobg)}}
.tbar .tbap.stopping{{border-color:var(--warnline);background:var(--warnbg)}}
.tbar .apdot-sm{{width:7px;height:7px;border-radius:50%;background:var(--faint);flex:none}}
.tbar .apdot-sm.on{{background:var(--ok);animation:pulse2 1.3s infinite}}
.tbar .apdot-sm.stop{{background:var(--warn)}}
.tbar .tbaplabel{{font-size:12px;color:var(--ink);white-space:nowrap}}
.tbar .tbaplabel .ext{{font-size:10px;color:var(--info);font-weight:600;margin-left:4px}}
.tbar .aptbtn{{border:0;height:34px;border-radius:var(--r-md);padding:0 14px;font:inherit;font-size:13px;font-weight:700;cursor:pointer;white-space:nowrap}}
.tbar .aptbtn.start{{background:var(--accent);color:#fff}}.tbar .aptbtn.start:hover{{background:var(--accent-hover)}}
.tbar .aptbtn.start:disabled{{background:var(--line);color:var(--faint);cursor:not-allowed}}
.tbar .aptbtn.drain{{background:var(--warn);color:#1a1205}}.tbar .aptbtn.drain:hover{{background:#c99020}}
.tbar .aptbtn.stop{{background:var(--bad);color:#fff}}.tbar .aptbtn.stop:hover{{background:#c74c50}}
.deploybar{{display:flex;align-items:center;gap:13px;padding:11px 26px;background:var(--accentbg);border-bottom:1px solid var(--accentline)}}
.qa-strip{{display:flex;align-items:center;gap:10px;padding:9px 26px;background:var(--accentbg);border-bottom:1px solid var(--accentline);color:#cfe0ff;font-size:13px;font-weight:650}}
.qa-strip #qaelapsed{{font-variant-numeric:tabular-nums;color:var(--dim);font-weight:400}}
.deploybar .dspin{{width:18px;height:18px;border:3px solid var(--accentline);border-top-color:var(--accent);border-radius:50%;animation:dsp .9s linear infinite;flex:none}}
.deploybar .dmsg{{color:#cfe0ff;font-size:13px;font-weight:650}}
.deploybar .dsub{{color:var(--dim);font-weight:400;font-size:12px}}
.deploybar .dprog{{flex:1;max-width:300px;height:6px;background:var(--bg);border-radius:var(--r-pill);overflow:hidden;border:1px solid var(--accentline)}}
.deploybar .dprogfill{{display:block;width:38%;height:100%;background:linear-gradient(90deg,var(--accent),var(--info));border-radius:var(--r-pill);animation:dsl 1.4s ease-in-out infinite}}
@keyframes dsp{{to{{transform:rotate(360deg)}}}}
@keyframes dsl{{0%{{margin-left:-38%}}100%{{margin-left:100%}}}}
@media(max-width:1150px){{
.tbar{{grid-template-columns:1fr}}
.tbar>.tclu:nth-of-type(4){{flex-direction:column;align-items:stretch}}
}}
@media(max-width:820px){{
.tbar{{gap:7px;padding:10px 14px}}
.tbar .btn{{padding:8px 10px;font-size:12px}}
.tbar .tbnote{{order:99;flex-basis:100%;margin:4px 0 0}}
.tbar .panel.form{{min-width:0;width:min(320px,92vw)}}
}}
</style>
<div class=tbar>
  <div class=tclu>
    <span class=tclabel>run</span>
    <div class=tcrow>
      {ap_html}
      {ship_html}
    </div>
  </div>

  <div class=tclu>
    <span class=tclabel>build</span>
    <div class=tcrow>
      {backend_control(cfg, app0)}
    </div>
  </div>

  <div class=tclu>
    <span class=tclabel>QA</span>
    <div class=tcrow>
      <form method=post action=/api/qa class=tbf><input type=hidden name=app value="{html.escape(app0)}">{_btn("&#128269; Run QA", attrs=f' {busy("qa")}' if busy("qa") else "")}</form>
    </div>
  </div>

  <div class=tclu>
    <span class=tclabel>nav</span>
    <div class=tcrow>
      {_btn("&#128268; Jira", tag="a", attrs=f' href="/jira?app={html.escape(app0)}" title="Pick or connect the Jira this project uses"')}
      {open_logs_html}
      {_btn(f"&#128203; Task log{fr_tasks}", tag="a", attrs=' href="/tasks" title="Every run — Today / week / month scoping, transcripts, Jira links"')}
      {_btn(f"&#128172; Daily{fr_council}", tag="a", attrs=' href="/council" title="The daily muster — DONE / NEXT / NEEDS YOU + failure causes"')}
      {_btn(f"&#128221; Memory{fr_mem}", tag="a", attrs=' href="/memory" title="Squad memory — doctrine + the living lessons log"')}
    </div>
  </div>

  <span class=grow></span>
  {status}
</div>
{deploy_strip}{qa_strip}{qa_report_html}{qa_failure_html}"""


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
        bubbles = ('<div class=cempty>No messages yet. When an engineer needs a decision it shows '
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
    return f'<div class=ctabs><a class="ctab {g}" href="/chat">&#128172; CTO{badge}</a></div>'


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

        # EU-540: plan-probe rows carry 'utilization' (fraction); glm_budget_status()/budget_status()
        # carry 'pct' as a fraction (used/cap) with no 'utilization' key. Prefer 'utilization' when
        # present (plan rows), else fall back to 'pct' so the GLM gauge reflects real ledger burn
        # instead of a dead 0%. Both are 0.0–1.0 fractions here.
        util = float(data.get("utilization", data.get("pct", 0.0)) or 0.0)
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
        ".provcard{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px}"
        ".phead{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px}"
        ".pname{color:var(--ink);font-size:15px;font-weight:700}"
        ".pbrand{color:var(--dim);font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.06em}"
        ".pstatus{display:flex;align-items:center;gap:8px;margin-bottom:10px}"
        ".picon{font-size:14px}.picon.ok{color:#3fb950}.picon.warn{color:#d99a2b}.picon.bad{color:#f0676b}"
        ".pstat{color:var(--dim);font-size:12px;font-weight:500;text-transform:uppercase}"
        ".ppct{color:var(--ink);font-size:13px;font-weight:600;margin-left:auto}"
        ".pgauge{margin:12px 0}"
        ".pgbar{height:10px;background:var(--well);border-radius:6px;overflow:hidden;border:1px solid var(--line2)}"
        ".pgfill{display:block;height:100%;transition:width .3s ease}"
        ".pgfill.g{background:#3fb950}.pgfill.a{background:#d99a2b}.pgfill.r{background:#f0676b}"
        ".premain{color:var(--dim);font-size:11px;margin-top:6px;font-family:ui-monospace,Menlo,monospace}"
        ".pmeta{color:var(--dim);font-size:11px;margin-top:8px;font-family:ui-monospace,Menlo,monospace}"
        ".pnote{color:var(--dim);font-size:12px;margin-top:8px}"
        "@media(max-width:680px){.dualprov{grid-template-columns:1fr}}"
        "</style>"
        '<div class=dualprov>'
        f'{claude_card}'
        f'{glm_card}'
        '</div>'
    )
