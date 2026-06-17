"""War Room — the Elite Unit's command cockpit (data + render).

`general serve` mounts this at `/`. It turns the audit log, the blocked-tickets
file, the officers' report files and the council history into a single live
command view: KPIs, the active run with its phase bar, the officer roster, and
the unit's activity feed — scoped to whichever Jira project/app you pick.

Design: this module is defensive end-to-end — every file read is guarded so a
missing or half-written file never breaks the page. Data functions return only
primitives (JSON-safe); HTML lives in the render_* helpers. The board re-renders
server-side on a short poll (see render_page), so the view stays live without a
full reload. True per-event SSE streaming is a planned fast-follow.
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import dashboard as D

# --------------------------------------------------------------------------- #
# The roster: (key, display name, role line). Order = chain of command.
_OFFICERS = [
    ("general",       "The General",     "Orchestrator"),
    ("adjutant",      "Adjutant",        "S-1 · personnel"),
    ("builder",       "Field Engineer",  "Builder"),
    ("reviewer",      "Inspector General", "Reviewer"),
    ("scout",         "Scout",           "S-2 · QA / recon"),
    ("provost",       "Provost Marshal", "Security gate"),
    ("quartermaster", "Quartermaster",   "S-4 · deploy"),
    ("drill",         "Drillmaster",     "Doctrine / training"),
]


def _parse(ts: str) -> Optional[datetime]:
    return D._parse_ts(ts or "")


def _rel(dt: Optional[datetime]) -> str:
    """Human 'time ago'. Tz-safe (compares in the datetime's own tz)."""
    if not dt:
        return "never"
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    secs = (now - dt).total_seconds()
    if secs < 0:
        secs = 0
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _mtime(path: Path) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def _age_secs(dt: Optional[datetime]) -> float:
    if not dt:
        return float("inf")
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return max(0.0, (now - dt).total_seconds())


def _scope(tasks: list[dict], app: Optional[str]) -> list[dict]:
    if not app or app == "*":
        return tasks
    return [t for t in tasks if (t.get("app") or "") == app]


def _scan(audit_path: str | Path) -> dict[str, Any]:
    """One pass over the raw audit: last-seen ts and count per event kind,
    optionally per app. Cheap and used by every panel."""
    last: dict[str, datetime] = {}
    count: dict[str, int] = {}
    p = Path(audit_path)
    if not p.exists():
        return {"last": last, "count": count}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        k = ev.get("event")
        if not k:
            continue
        count[k] = count.get(k, 0) + 1
        dt = _parse(ev.get("ts", ""))
        if dt and (k not in last or last[k] is None or dt > last[k]):
            last[k] = dt
    return {"last": last, "count": count}


def _load_blocked(cfg) -> list[str]:
    """Parked tickets the autopilot is skipping (blocked_tickets.json).
    Tolerates either a dict {id: reason} or a list of ids/objects."""
    p = Path(cfg.audit_path).with_name("blocked_tickets.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        return list(data.keys())
    if isinstance(data, list):
        out = []
        for x in data:
            if isinstance(x, str):
                out.append(x)
            elif isinstance(x, dict):
                out.append(str(x.get("ticket_id") or x.get("id") or x))
        return out
    return []


def _last_council(cfg) -> Optional[datetime]:
    try:
        from . import council
        hist = council.history(cfg, limit=1)
        if hist:
            return _parse(hist[0].get("ts", ""))
    except Exception:  # noqa: BLE001
        pass
    return None


# --------------------------------------------------------------------------- #
# Data (JSON-safe primitives)

def kpis(cfg, tasks: list[dict], app: Optional[str]) -> list[dict]:
    ts = _scope(tasks, app)
    today = datetime.now().strftime("%Y-%m-%d")

    def day(t: dict) -> str:
        s = t.get("ended") or t.get("started")
        return s.strftime("%Y-%m-%d") if s else ""

    merged = [t for t in ts if t.get("outcome") == "merged→dev"]
    merged_today = [t for t in merged if day(t) == today]
    needs = [t for t in ts if t.get("outcome") in D._NEEDS_YOU]
    passes = [t["passes"] for t in merged if t.get("passes")]
    avg_passes = round(sum(passes) / len(passes), 1) if passes else 0
    blocked = _load_blocked(cfg)
    sec_blocks = _scan(cfg.audit_path)["count"].get("security_block", 0)

    cards = [
        {"label": "Merged → DEV today", "value": len(merged_today), "hint": "shipped to QA"},
        {"label": "Merged total", "value": len(merged), "hint": "all time", "tone": "ok"},
        {"label": "Needs you", "value": len(needs), "hint": "PR · escalated · errored",
         "tone": "warn" if needs else None},
        {"label": "Avg passes / ticket", "value": avg_passes, "hint": "lower is cleaner"},
        {"label": "Parked", "value": len(blocked), "hint": "auto-skipped — stuck",
         "tone": "warn" if blocked else None},
        {"label": "Security blocks", "value": sec_blocks, "hint": "Provost gate (all time)",
         "tone": "bad" if sec_blocks else None},
    ]
    return cards


def roster(cfg, tasks: list[dict], active: bool) -> list[dict]:
    scan = _scan(cfg.audit_path)
    last = scan["last"]
    base = Path(cfg.audit_path)
    council_ts = _last_council(cfg)

    def rpt(name: str) -> Optional[datetime]:
        return _mtime(base.with_name(name))

    seen: dict[str, Optional[datetime]] = {
        "general": council_ts,
        "adjutant": rpt("adjutant-report.md"),
        "builder": last.get("build"),
        "reviewer": last.get("review"),
        "scout": rpt("scout-report.md"),
        "provost": last.get("security_block") or rpt("provost-report.md"),
        "quartermaster": rpt("quartermaster-report.md"),
        "drill": rpt("drill-report.md") or council_ts,
    }
    # An active run means the Builder/Reviewer are on duty right now.
    on_duty = {"builder", "reviewer"} if active else set()

    out = []
    for key, name, role in _OFFICERS:
        dt = seen.get(key)
        if key in on_duty:
            dot = "live"
        elif _age_secs(dt) < 600:      # acted in the last 10 minutes
            dot = "recent"
        else:
            dot = "idle"
        out.append({"name": name, "role": role, "dot": dot, "last": _rel(dt)})
    return out


_FEED_META = {
    "merged→dev": ("ok", "merged → DEV · moved to QA"),
    "PR / needs you": ("warn", "opened a PR — needs you"),
    "escalated": ("warn", "escalated to you"),
    "errored": ("bad", "errored"),
    "awaiting decision": ("warn", "awaiting your decision"),
    "dry-run": ("muted", "dry-run landed"),
}


def feed(cfg, tasks: list[dict], app: Optional[str], limit: int = 16) -> list[dict]:
    ts = _scope(tasks, app)
    items: list[dict] = []
    for t in ts:
        out = t.get("outcome")
        when = t.get("ended") or t.get("started")
        if not when:
            continue
        tone, label = _FEED_META.get(out, ("muted", "in progress"))
        note = (t.get("note") or "").strip().replace("\n", " ")
        items.append({
            "when": when, "tone": tone,
            "ticket": str(t.get("ticket_id") or ""),
            "app": str(t.get("app") or ""),
            "text": label + (f" — {note[:80]}" if note and tone in ("warn", "bad") else ""),
        })
    # Councils (unit-wide, not app-scoped)
    try:
        from . import council
        for h in council.history(cfg, limit=6):
            dt = _parse(h.get("ts", ""))
            if dt:
                items.append({"when": dt, "tone": "info", "ticket": "council",
                              "app": "", "text": (h.get("summary") or "daily council").strip()[:90]})
    except Exception:  # noqa: BLE001
        pass
    items.sort(key=lambda x: x["when"].timestamp() if x["when"] else 0.0, reverse=True)
    out = []
    for it in items[:limit]:
        out.append({"when": it["when"].strftime("%b %d %H:%M"), "ago": _rel(it["when"]),
                    "tone": it["tone"], "ticket": it["ticket"], "app": it["app"], "text": it["text"]})
    return out


def active_run(cfg, tasks: list[dict], app: Optional[str], active: bool) -> Optional[dict]:
    """The live run if one is going, else the most recent run as 'last run'."""
    ts = _scope(tasks, app)
    if not ts:
        return None
    # "live" means a run is genuinely in flight (a manual Run or Autopilot), NOT merely
    # that an old audit row lacks a terminal event (e.g. an interrupted ticket). Otherwise a
    # stopped attempt would show as forever-running.
    t = ts[0]                               # newest task (newest-first) = current or last run
    live = bool(active)
    # Approximate phase from what's been recorded so far.
    has_build = any(d.get("build_summary") or d.get("tools") for d in t.get("passes_list", []))
    has_review = t.get("verdict") is not None
    merged = t.get("outcome") == "merged→dev"
    phases = ["Build", "Gate", "Review", "Security", "Land"]
    reached = 0
    if has_build:          # build done; the gate runs next
        reached = 1
    if has_review:         # reviewed → build + gate behind it
        reached = 3
    if merged:             # security passed and landed
        reached = 5
    return {
        "live": live,
        "ticket": str(t.get("ticket_id") or "—"),
        "app": str(t.get("app") or ""),
        "branch": str(t.get("branch") or ""),
        "passes": t.get("passes") or 0,
        "verdict": t.get("verdict") or "",
        "outcome": t.get("outcome") or "running",
        "phases": phases,
        "reached": reached,
    }


# --------------------------------------------------------------------------- #
# Render

def _esc(s: Any) -> str:
    return html.escape(str(s))


def _kpi_html(cards: list[dict]) -> str:
    out = []
    for c in cards:
        tone = c.get("tone") or ""
        out.append(
            f'<div class="kpi {tone}"><div class=kv>{_esc(c["value"])}</div>'
            f'<div class=kl>{_esc(c["label"])}</div>'
            f'<div class=kh>{_esc(c["hint"])}</div></div>')
    return "".join(out)


def _run_html(run: Optional[dict]) -> str:
    if not run:
        return ('<div class=runempty><div class=dot2></div>'
                'No runs yet for this project. Launch one from the bar above.</div>')
    bar = []
    for i, ph in enumerate(run["phases"]):
        state = "done" if i < run["reached"] else ("now" if i == run["reached"] and run["live"] else "")
        bar.append(f'<div class="ph {state}"><span></span>{_esc(ph)}</div>')
    tag = ('<span class="b live">● live</span>' if run["live"]
           else '<span class="b muted">last run</span>')
    verdict = (f'<span class=meta>verdict <b>{_esc(run["verdict"])}</b></span>'
               if run["verdict"] else "")
    return (
        f'<div class=runhead><div><span class=mono>{_esc(run["ticket"])}</span> '
        f'<span class=muted>{_esc(run["app"])}</span></div>{tag}</div>'
        f'<div class=phasebar>{"".join(bar)}</div>'
        f'<div class=runmeta><span class=meta>pass <b>{_esc(run["passes"])}</b></span>'
        f'{verdict}<span class=meta>branch <span class=mono>{_esc(run["branch"] or "—")}</span></span></div>')


def _roster_html(rows: list[dict]) -> str:
    out = []
    for r in rows:
        out.append(
            f'<div class=offrow><span class="d {r["dot"]}"></span>'
            f'<div class=offmain><div class=offname>{_esc(r["name"])}</div>'
            f'<div class=offrole>{_esc(r["role"])}</div></div>'
            f'<div class=offlast>{_esc(r["last"])}</div></div>')
    return "".join(out)


def _feed_html(items: list[dict]) -> str:
    if not items:
        return '<div class=muted style="padding:14px">No activity yet.</div>'
    out = []
    for it in items:
        tk = (f'<span class=mono>{_esc(it["ticket"])}</span> ' if it["ticket"] else "")
        out.append(
            f'<div class=fitem><span class="fd {it["tone"]}"></span>'
            f'<div class=fbody>{tk}{_esc(it["text"])}'
            f'<div class=fmeta>{_esc(it["app"])}{" · " if it["app"] else ""}{_esc(it["ago"])}</div></div></div>')
    return "".join(out)


def render_board(cfg, app: Optional[str], state: dict) -> str:
    """Inner board (everything that updates on the poll)."""
    active = bool(state.get("active")) or bool((state.get("autopilot") or {}).get("on"))
    tasks = D.load_tasks(cfg.audit_path)
    k = _kpi_html(kpis(cfg, tasks, app))
    run = _run_html(active_run(cfg, tasks, app, active))
    ros = _roster_html(roster(cfg, tasks, active))
    fd = _feed_html(feed(cfg, tasks, app))
    return (
        f'<div class=kpis>{k}</div>'
        '<div class=cols>'
        f'<div class=col-main>'
        f'<section class=panel><div class=ph>Active run</div><div class=run>{run}</div></section>'
        f'<section class=panel><div class=ph>Activity</div><div class=feed>{fd}</div></section>'
        '</div>'
        f'<div class=col-side>'
        f'<section class=panel><div class=ph>Roster · 8 officers</div><div class=roster>{ros}</div></section>'
        '</div>'
        '</div>')


def project_selector(cfg, app: Optional[str]) -> str:
    sel = app or "*"
    opts = [f'<option value="*" {"selected" if sel == "*" else ""}>All projects</option>']
    for a in cfg.apps:
        s = "selected" if a.name == sel else ""
        opts.append(f'<option value="{_esc(a.name)}" {s}>{_esc(a.name)}</option>')
    return (f'<select id=proj onchange="proj(this.value)" title="Jira project / app">'
            f'{"".join(opts)}</select>')


def health_pill(h: dict) -> str:
    if h.get("healthy"):
        w = h.get("warnings") or 0
        extra = f" · {w} warning{'s' if w != 1 else ''}" if w else ""
        return f'<span class="hpill ok" title="all critical checks passed">&#9679; System healthy{extra}</span>'
    n = h.get("problems") or 0
    return f'<span class="hpill bad" title="fix before starting work">&#9679; {n} problem{"s" if n != 1 else ""}</span>'


def health_banner(h: dict) -> str:
    recheck = '<button class=hbbtn type=button onclick="location.reload()">Re-check</button>'
    if h.get("healthy"):
        w = h.get("warnings") or 0
        title = ("System is healthy — all critical checks passed"
                 + (f" ({w} warning{'s' if w != 1 else ''})" if w else ""))
        warns = [c for c in h.get("checks", []) if c["status"] == "warn"]
        extra = ""
        if warns:
            extra = "<ul class=hbissues>" + "".join(
                f'<li><span class="tag warn">warn</span> {_esc(c["name"])}'
                f'{(" — " + _esc(c["detail"])) if c["detail"] else ""}</li>' for c in warns) + "</ul>"
        return (f'<div class="healthbar ok"><div class=hbrow>'
                f'<div class=hbtitle><span class=hbdot></span>{title}</div>'
                f'<div class=hbactions><span class=models>officers on {_esc(h.get("models", {}).get("builder", "?"))}</span>'
                f'{recheck}</div></div>{extra}</div>')
    bad = [c for c in h.get("checks", []) if c["status"] == "bad"]
    items = "".join(
        f'<li><span class="tag bad">fix</span> {_esc(c["name"])}'
        f'{(" — " + _esc(c["detail"])) if c["detail"] else ""}</li>' for c in bad)
    return (f'<div class="healthbar bad"><div class=hbrow>'
            f'<div class=hbtitle><span class=hbdot></span>'
            f'{len(bad)} problem{"s" if len(bad) != 1 else ""} to fix before the unit can work tickets</div>'
            f'<div class=hbactions>{recheck}</div></div><ul class=hbissues>{items}</ul></div>')


def autopilot_switch(state: dict, app: Optional[str], healthy: bool) -> str:
    ap = (state or {}).get("autopilot") or {}
    if ap.get("on"):
        scope = _esc(ap.get("app") or "all projects")
        return ('<form method=post action=/api/autopilot class="apsw on">'
                '<input type=hidden name=action value=stop>'
                f'<span class="apdot on"></span><span class=aplabel>Autopilot&nbsp;<b>ON</b> · {scope}</span>'
                '<button class="apbtn stop">Stop</button></form>')
    appq = _esc(app if app and app != "*" else "")
    dis = "" if healthy else "disabled"
    return ('<form method=post action=/api/autopilot class=apsw>'
            '<input type=hidden name=action value=start>'
            f'<input type=hidden name=app value="{appq}">'
            f'<span class="apdot off"></span><span class=aplabel>Autopilot&nbsp;<b>off</b></span>'
            f'<button class="apbtn start" {dis}>Start</button></form>')


def render_page(cfg, app: Optional[str], state: dict, control_bar: str, health: dict) -> str:
    return (_PAGE
            .replace("{{PROJ}}", project_selector(cfg, app))
            .replace("{{AUTOPILOT}}", autopilot_switch(state, app, health.get("healthy", False)))
            .replace("{{HEALTHPILL}}", health_pill(health))
            .replace("{{HEALTHBAR}}", health_banner(health))
            .replace("{{BAR}}", control_bar)
            .replace("{{BOARD}}", render_board(cfg, app, state))
            .replace("{{APP}}", _esc(app or "*"))
            .replace("{{GEN}}", datetime.now().strftime("%H:%M:%S")))


_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Elite Unit — War Room</title>
<style>
:root{color-scheme:dark;
--bg:#0a0c11;--panel:#12161f;--panel2:#161b25;--line:#1f2531;--line2:#2a3343;
--ink:#e9ecf1;--dim:#8a929f;--faint:#5c6573;
--ok:#3ad17f;--okbg:#102a1d;--warn:#f7b955;--warnbg:#2e2510;--bad:#f0676b;--badbg:#2a1416;
--info:#6aa9ff;--accent:#3b6cff}
*{box-sizing:border-box}
body{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;margin:0;background:var(--bg);color:var(--ink)}
a{color:var(--info);text-decoration:none}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:.85em}
.muted{color:var(--dim)}
button{font:inherit}
/* header */
header{display:flex;align-items:center;gap:14px;padding:14px 26px;border-bottom:1px solid var(--line);
background:linear-gradient(180deg,#11151e,#0a0c11);position:sticky;top:0;z-index:5;flex-wrap:wrap}
.brand{font-size:16px;font-weight:750;letter-spacing:.2px;white-space:nowrap}
.brand b{color:var(--accent)}
header select{background:#0d1119;border:1px solid var(--line2);color:var(--ink);border-radius:9px;
padding:8px 12px;font:inherit;cursor:pointer}
.spacer{flex:1}
.gen{font-size:11px;color:var(--faint)}
/* health pill */
.hpill{font-size:12px;font-weight:700;padding:6px 13px;border-radius:99px}
.hpill.ok{color:var(--ok);background:var(--okbg);border:1px solid #1c5238}
.hpill.bad{color:var(--bad);background:var(--badbg);border:1px solid #5a1f22}
/* autopilot switch */
.apsw{display:flex;align-items:center;gap:9px;margin:0;padding:5px 6px 5px 13px;border:1px solid var(--line2);
border-radius:99px;background:#0d1119}
.apsw.on{border-color:#1c5238;background:var(--okbg)}
.apdot{width:8px;height:8px;border-radius:99px;background:var(--faint)}
.apdot.on{background:var(--ok);animation:pulse2 1.3s infinite}
.aplabel{font-size:12px;color:var(--ink)}
.apbtn{border:0;border-radius:99px;padding:6px 13px;font-size:12px;font-weight:700;cursor:pointer}
.apbtn.start{background:var(--accent);color:#fff}
.apbtn.start:disabled{background:#222a37;color:var(--faint);cursor:not-allowed}
.apbtn.stop{background:var(--bad);color:#fff}
/* health banner */
.healthbar{padding:13px 26px}
.healthbar.ok{background:linear-gradient(180deg,rgba(16,42,29,.55),transparent);border-bottom:1px solid #15351f}
.healthbar.bad{background:linear-gradient(180deg,rgba(42,20,22,.6),transparent);border-bottom:1px solid #3a1a1c}
.hbrow{display:flex;align-items:center;gap:14px}
.hbtitle{display:flex;align-items:center;gap:11px;font-weight:650;font-size:14px;flex:1}
.healthbar.ok .hbtitle{color:#9be7bd}.healthbar.bad .hbtitle{color:#f3a6a8}
.hbdot{width:11px;height:11px;border-radius:99px;flex:none}
.healthbar.ok .hbdot{background:var(--ok);box-shadow:0 0 0 4px rgba(58,209,127,.13)}
.healthbar.bad .hbdot{background:var(--bad);animation:pulse3 1.4s infinite}
@keyframes pulse3{0%,100%{box-shadow:0 0 0 0 rgba(240,103,107,.45)}50%{box-shadow:0 0 0 8px rgba(240,103,107,0)}}
.hbactions{display:flex;align-items:center;gap:12px}
.models{font-size:11px;color:var(--faint)}
.hbbtn{background:#1b2230;border:1px solid var(--line2);color:var(--ink);border-radius:8px;padding:6px 13px;
font-size:12px;font-weight:600;cursor:pointer}
.hbissues{margin:11px 0 2px;padding:0;list-style:none;display:grid;gap:6px}
.hbissues li{font-size:12.5px;color:var(--dim)}
.tag{font-size:10px;font-weight:800;text-transform:uppercase;padding:2px 6px;border-radius:5px;margin-right:8px}
.tag.bad{background:var(--badbg);color:var(--bad)}.tag.warn{background:var(--warnbg);color:var(--warn)}
/* kpis */
.kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;padding:20px 26px 6px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:15px 16px;
box-shadow:0 1px 0 rgba(255,255,255,.02) inset}
.kpi .kv{font-size:27px;font-weight:720;line-height:1;letter-spacing:-.6px}
.kpi .kl{font-size:12px;color:var(--ink);margin-top:7px;font-weight:600}
.kpi .kh{font-size:11px;color:var(--faint);margin-top:2px}
.kpi.ok .kv{color:var(--ok)}.kpi.warn .kv{color:var(--warn)}.kpi.bad .kv{color:var(--bad)}
/* layout */
.cols{display:grid;grid-template-columns:1fr 340px;gap:16px;padding:14px 26px 40px}
.col-main{display:flex;flex-direction:column;gap:16px;min-width:0}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.ph{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--dim);
padding:13px 17px;border-bottom:1px solid var(--line)}
/* active run */
.run{padding:17px}
.runhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.b{font-size:11px;font-weight:700;padding:3px 10px;border-radius:99px}
.b.live{color:var(--ok);background:var(--okbg)}.b.muted{color:var(--dim);background:#171c26}
.phasebar{display:flex;gap:8px}
.phasebar .ph{display:flex;align-items:center;gap:7px;flex:1;border:0;padding:0;text-transform:none;
letter-spacing:0;font-size:12px;font-weight:600;color:var(--faint)}
.phasebar .ph span{width:9px;height:9px;border-radius:99px;background:#222a37;flex:none;border:2px solid #222a37}
.phasebar .ph.done{color:var(--ink)}
.phasebar .ph.done span{background:var(--ok);border-color:var(--ok)}
.phasebar .ph.now{color:var(--warn)}
.phasebar .ph.now span{background:var(--warn);border-color:var(--warn);animation:pulse 1.3s infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(247,185,85,.5)}50%{box-shadow:0 0 0 5px rgba(247,185,85,0)}}
.runmeta{display:flex;gap:20px;margin-top:15px;padding-top:13px;border-top:1px solid var(--line)}
.meta{font-size:12px;color:var(--dim)}.meta b{color:var(--ink);font-weight:650}
.runempty{padding:26px 18px;color:var(--dim);display:flex;align-items:center;gap:10px}
.dot2{width:8px;height:8px;border-radius:99px;background:var(--faint)}
/* roster */
.roster{padding:6px 0}
.offrow{display:flex;align-items:center;gap:11px;padding:9px 17px}
.offrow:hover{background:var(--panel2)}
.d{width:9px;height:9px;border-radius:99px;flex:none;background:#39424f}
.d.live{background:var(--ok);animation:pulse2 1.3s infinite}
.d.recent{background:var(--info)}.d.idle{background:#39424f}
@keyframes pulse2{0%,100%{box-shadow:0 0 0 0 rgba(58,209,127,.5)}50%{box-shadow:0 0 0 5px rgba(58,209,127,0)}}
.offmain{flex:1;min-width:0}.offname{font-weight:600;font-size:13px}
.offrole{font-size:11px;color:var(--faint)}
.offlast{font-size:11px;color:var(--dim);white-space:nowrap}
/* feed */
.feed{padding:5px 0;max-height:430px;overflow:auto}
.fitem{display:flex;gap:11px;padding:9px 17px;border-top:1px solid #161b24}
.fitem:first-child{border-top:0}
.fd{width:7px;height:7px;border-radius:99px;margin-top:6px;flex:none;background:var(--faint)}
.fd.ok{background:var(--ok)}.fd.warn{background:var(--warn)}.fd.bad{background:var(--bad)}
.fd.info{background:var(--info)}.fd.muted{background:var(--faint)}
.fbody{font-size:13px;min-width:0}.fmeta{font-size:11px;color:var(--faint);margin-top:2px}
@media(max-width:1080px){.kpis{grid-template-columns:repeat(3,1fr)}.cols{grid-template-columns:1fr}}
@media(max-width:680px){.kpis{grid-template-columns:repeat(2,1fr)}.hbactions .models{display:none}}
</style></head><body>
<header>
  <div class=brand>&#9733; Elite Unit <b>·</b> War Room</div>
  {{PROJ}}
  <div class=spacer></div>
  {{AUTOPILOT}}
  {{HEALTHPILL}}
  <span class=gen>updated {{GEN}}</span>
</header>
{{HEALTHBAR}}
{{BAR}}
<div id=board>{{BOARD}}</div>
<script>
var APP="{{APP}}";
function proj(v){APP=v;location.search="?app="+encodeURIComponent(v);}
async function tick(){
  try{
    var r=await fetch("/api/board?app="+encodeURIComponent(APP),{cache:"no-store"});
    if(r.ok){document.getElementById("board").innerHTML=await r.text();}
  }catch(e){}
}
setInterval(tick,5000);
</script>
</body></html>"""
