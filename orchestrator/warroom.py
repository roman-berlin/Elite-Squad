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
import os
import time
from datetime import datetime
from urllib.parse import quote
from pathlib import Path
from typing import Any, Optional

from . import dashboard as D

# --------------------------------------------------------------------------- #
# The roster: (key, display name, role line). Order = chain of command.
_OFFICERS = [
    ("general",       "CTO",                "Orchestrator"),
    ("adjutant",      "Engineering Manager", "S-1 · personnel"),
    ("pm",            "Product Manager",    "S-5 · product"),
    ("builder",       "Dev Team Lead",      "Builder"),
    ("reviewer",      "Code Reviewer",      "Reviewer"),
    ("scout",         "QA Engineer",        "S-2 · QA / recon"),
    ("provost",       "Security Engineer",  "Security gate"),
    ("quartermaster", "Release Manager",    "S-4 · deploy"),
    ("sentinel",      "SRE",                "S-3 · integration & rollback"),
    ("drill",         "Engineering Coach",  "Doctrine / training"),
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
    # Merged view: this machine's audit + every synced shared/<host>.jsonl (see dashboard.audit_lines).
    for line in D.audit_lines(audit_path):
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
    _dismissed = D.load_dismissed(cfg.audit_path)
    needs = [t for t in ts if t.get("outcome") in D._NEEDS_YOU and not D._is_dismissed(t, _dismissed)]
    passes = [t["passes"] for t in merged if t.get("passes")]
    avg_passes = round(sum(passes) / len(passes), 1) if passes else 0
    blocked = _load_blocked(cfg)
    sec_blocks = _scan(cfg.audit_path)["count"].get("security_block", 0)

    # Each card deep-links to a view scoped to the count it shows: the /tasks log auto-applies the
    # ?filter= (merged / needs / parked) so the destination honors the click, and Security blocks
    # opens the forensics view scoped to the security-block findings that produced the number.
    cards = [
        {"label": "Merged → DEV today", "value": len(merged_today), "hint": "shipped to QA",
         "href": "/tasks?filter=merged"},
        {"label": "Merged total", "value": len(merged), "hint": "all time", "tone": "ok",
         "href": "/tasks?filter=merged"},
        {"label": "Needs you", "value": len(needs), "hint": "PR · escalated · errored",
         "tone": "warn" if needs else None, "href": "/tasks?filter=needs"},   # -> the tickets that need you
        {"label": "Avg passes / ticket", "value": avg_passes, "hint": "lower is cleaner"},
        {"label": "Parked", "value": len(blocked), "hint": "auto-skipped — stuck",
         "tone": "warn" if blocked else None, "href": "/tasks?filter=parked"},
        {"label": "Security blocks", "value": sec_blocks, "hint": "Security Engineer gate (all time)",
         "tone": "bad" if sec_blocks else None, "href": "/forensics?cat=security_block"},
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

    # roster key -> the officer's council name (so a click consults that exact officer).
    group_name = {"adjutant": "Adjutant", "builder": "Field Engineer", "reviewer": "Inspector General",
                  "scout": "Scout", "provost": "Provost Marshal", "quartermaster": "Quartermaster",
                  "sentinel": "Sentinel", "drill": "Drillmaster"}
    out = []
    for key, name, role in _OFFICERS:
        dt = seen.get(key)
        if key in on_duty:
            dot = "live"
        elif _age_secs(dt) < 600:      # acted in the last 10 minutes
            dot = "recent"
        else:
            dot = "idle"
        # The General is your 1:1 chat; every other officer opens a focused consult with just them.
        href = "/chat" if key == "general" else "/group?officer=" + quote(group_name.get(key, name))
        out.append({"name": name, "role": role, "dot": dot, "last": _rel(dt), "href": href})
    return out


_FEED_META = {
    "merged→dev": ("ok", "merged → DEV · moved to QA"),
    "PR / needs you": ("warn", "opened a PR — needs you"),
    "escalated": ("warn", "escalated to you"),
    "errored": ("bad", "errored"),
    "awaiting decision": ("warn", "awaiting your decision"),
    "dry-run": ("muted", "dry-run landed"),
}

# Terminal outcomes that mean the run STOPPED because it failed (not merged, not still pending a
# human decision). Used to light the phase bar's stopping node red.
_FAILED_OUTCOMES = {"errored", "escalated"}


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
            "text": label + (f" — {D._short(note, 80)}" if note and tone in ("warn", "bad") else ""),
        })
    # Councils (unit-wide, not app-scoped)
    try:
        from . import council
        for h in council.history(cfg, limit=6):
            dt = _parse(h.get("ts", ""))
            if dt:
                items.append({"when": dt, "tone": "info", "ticket": "council",
                              "app": "", "text": D._short(h.get("summary") or "daily council", 90)})
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
    # A terminal-but-FAILED run (errored/escalated) must light its STOPPING phase red, not render
    # the phases behind it as cleanly-done. Derive the phase the run died at so the bar shows where
    # it actually broke instead of implying it sailed through review and just didn't deploy.
    failed_phase = None
    if not live and t.get("outcome") in _FAILED_OUTCOMES:
        verdict = (t.get("verdict") or "").upper()
        if not has_build:
            failed_phase = 0                                   # Build never finished
        elif not has_review:
            failed_phase = 1                                   # built, then died at the Gate
        elif "FAIL" in verdict or "REJECT" in verdict:
            failed_phase = 2                                   # review verdict was a rejection
        else:
            failed_phase = 3                                   # passed review, broke at Security/Land
    return {
        "live": live,
        "ticket": str(t.get("ticket_id") or "—"),
        "app": str(t.get("app") or ""),
        "branch": str(t.get("branch") or ""),
        "passes": t.get("passes") or 0,
        "verdict": t.get("verdict") or "",
        "outcome": t.get("outcome") or "running",
        "cost": t.get("cost") or 0,
        "phases": phases,
        "reached": reached,
        "failed_phase": failed_phase,
    }


def _run_in_flight(cfg, tasks: list[dict], app: Optional[str], within_s: int = 150) -> bool:
    """True when a run is genuinely live RIGHT NOW even though the cockpit didn't start it — e.g. a build
    kicked off by the Needs-you answer box, /unblock, or autopilot in another process. Heuristic: the
    newest run for this project has NO terminal outcome AND the audit shows activity within the last
    `within_s` seconds. Time-bounded so a crashed/interrupted attempt stops reading as live."""
    ts = _scope(tasks, app)
    if not ts or ts[0].get("outcome"):     # no runs, or the newest one already finished
        return False
    try:
        for line in reversed(D.audit_lines(cfg.audit_path)):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            dt = D._parse_ts(ev.get("ts", ""))
            if dt:
                return (datetime.now().timestamp() - dt.timestamp()) < within_s
    except Exception:  # noqa: BLE001
        return False
    return False


def _fmt_dur(secs: float) -> str:
    secs = int(max(0, secs))
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"


# --------------------------------------------------------------------------- #
# Render

def _esc(s: Any) -> str:
    return html.escape(str(s))


def _kpi_html(cards: list[dict]) -> str:
    out = []
    for c in cards:
        tone = c.get("tone") or ""
        href = c.get("href")
        tag, attr, link = ("a", f' href="{href}"', " link") if href else ("div", "", "")
        out.append(
            f'<{tag} class="kpi {tone}{link}"{attr}><div class=kv>{_esc(c["value"])}</div>'
            f'<div class=kl>{_esc(c["label"])}</div>'
            f'<div class=kh>{_esc(c["hint"])}</div></{tag}>')
    return "".join(out)


def _run_html(run: Optional[dict], mode: Optional[str] = None,
              elapsed: Optional[str] = None, manual: bool = False) -> str:
    if not run:
        return ('<div class=runempty><div class=dot2></div>'
                'No runs yet for this project. Launch one from the bar above.</div>')
    bar = []
    failed_phase = run.get("failed_phase")
    for i, ph in enumerate(run["phases"]):
        if failed_phase is not None and i == failed_phase:
            state = "failed"               # the run died here — show it red, not greyed-done
        elif i < run["reached"]:
            state = "done"
        elif i == run["reached"] and run["live"]:
            state = "now"
        else:
            state = ""
        bar.append(f'<div class="ph {state}"><span></span>{_esc(ph)}</div>')
    # Stop is offered only for a manual run in flight — Autopilot has its own Stop in the header.
    stop = ('<form method=post action=/api/stop-run class=stoprun '
            'onsubmit="return confirm(\'Stop this run? It halts at the next safe checkpoint — '
            'no merge, nothing left half-applied.\')">'
            '<button class=stopbtn title="Halt this run at the next checkpoint">&#9632; Stop</button>'
            '</form>') if (run["live"] and manual) else ""
    if run["live"]:
        status = '<span class="b live">● running</span>'
        chip = ('<span class="b mode">live → DEV</span>' if mode == "live"
                else '<span class="b dry">dry-run · no changes</span>' if mode == "dry" else "")
    else:
        # Idle: this is the LAST run, not a live one. Show its real outcome and a muted bar so it
        # never reads as "in progress".
        status = '<span class="b muted">last run</span>'
        oc = run.get("outcome") or ""
        otone = {"merged→dev": "ok", "errored": "bad", "awaiting decision": "warn",
                 "escalated": "warn", "PR / needs you": "warn"}.get(oc, "muted")
        olabel = {"merged→dev": "merged → DEV", "errored": "errored", "escalated": "escalated",
                  "PR / needs you": "PR — needs you", "awaiting decision": "needs you",
                  "running": "interrupted"}.get(oc, oc or "—")
        chip = f'<span class="b {otone}">{_esc(olabel)}</span>'
    verdict = (f'<span class=meta>verdict <b>{_esc(run["verdict"])}</b></span>'
               if run["verdict"] else "")
    extra = ""
    if run["live"] and elapsed:
        extra += f'<span class=meta>elapsed <b>{_esc(elapsed)}</b></span>'
    # $ cost is meaningless on the Max plan — only show it when actually billing via an API key.
    if run.get("cost") and os.environ.get("ANTHROPIC_API_KEY"):
        extra += f'<span class=meta>cost <b>${run["cost"]:.2f}</b></span>'
    return (
        f'<div class=runhead><div><span class=mono>{_esc(run["ticket"])}</span> '
        f'<span class=muted>{_esc(run["app"])}</span></div>'
        f'<div style="display:flex;gap:7px;align-items:center">{chip}{status}{stop}</div></div>'
        f'<div class="phasebar{"" if run["live"] else " idle"}">{"".join(bar)}</div>'
        f'<div class=runmeta><span class=meta>pass <b>{_esc(run["passes"])}</b></span>'
        f'{verdict}{extra}<span class=meta>branch <span class=mono>{_esc(run["branch"] or "—")}</span></span></div>')


def _log_html(lines) -> str:
    if not lines:
        return ('<div class=logempty>No live output yet — start a run and the unit\'s '
                'steps stream here (same as the terminal, minus the noise).</div>')
    out = []
    for ln in lines:
        s = ln.strip()
        low = s.lower()
        cls = ""
        if "merged" in low or "✓" in s or " pass" in low or "ready" in low:
            cls = "lg-ok"
        elif any(w in low for w in ("error", "fail", "park", "block", "✗", "reject")):
            cls = "lg-b"
        elif s.startswith("·") or "builder:" in low or "reviewer:" in low:
            cls = "lg-dim"
        out.append(f'<span class="{cls}">{_esc(ln)}</span>')
    return '<pre class=logbox id=logbox>' + "\n".join(out) + '</pre>'


def _roster_html(rows: list[dict]) -> str:
    out = []
    for r in rows:
        href = r.get("href")
        tag, attr = ("a", f' href="{href}"') if href else ("div", "")
        out.append(
            f'<{tag} class=offrow{attr}><span class="d {r["dot"]}"></span>'
            f'<div class=offmain><div class=offname>{_esc(r["name"])}</div>'
            f'<div class=offrole>{_esc(r["role"])}</div></div>'
            f'<div class=offlast>{_esc(r["last"])}</div></{tag}>')
    return "".join(out)


def _hero_html(run: Optional[dict], elapsed: Optional[str], mode: Optional[str]) -> str:
    """Full-width headline shown only while a run is live — the most important thing, biggest."""
    if not run or not run.get("live"):
        return ""
    phases, reached = run["phases"], run["reached"]
    phase = phases[reached] if reached < len(phases) else phases[-1]
    modechip = ('<span class="hgchip live">live → DEV</span>' if mode == "live"
                else '<span class="hgchip dry">dry-run · no changes</span>' if mode == "dry" else "")
    stats = [f'<div class=hgstat><div class=hgk>elapsed</div><div class=hgv>{_esc(elapsed or "—")}</div></div>',
             f'<div class=hgstat><div class=hgk>pass</div><div class=hgv>{_esc(run["passes"])}</div></div>']
    if run.get("cost") and os.environ.get("ANTHROPIC_API_KEY"):
        stats.append(f'<div class=hgstat><div class=hgk>cost</div><div class=hgv>${run["cost"]:.2f}</div></div>')
    return (
        '<div class=hero><div class=hgrow>'
        '<div class=hgleft><span class=hgdot></span><div>'
        f'<div class=hgtitle><span class=mono>{_esc(run["ticket"])}</span> '
        f'<span class=hgapp>{_esc(run["app"])}</span></div>'
        f'<div class=hgsub>Working · <b>{_esc(phase)}</b> &nbsp;{modechip}</div></div></div>'
        f'<div class=hgstats>{"".join(stats)}</div>'
        '</div></div>')


def _needs_side_html(ns: dict) -> str:
    """The Needs-you panel body for the cockpit's side column — a compact preview of the inbox."""
    if not ns.get("total"):
        return ('<a class=needsok href="/needs"><span class=nok>&#10003;</span> '
                'All clear — nothing needs you</a>')
    rows = []
    for d in ns.get("decisions", [])[:4]:
        q = _esc(str(d.get("question") or d.get("summary") or "question"))[:64]
        rows.append(f'<a class=needrow href="/needs"><span class="nd warn"></span>'
                    f'<div class=ndmain><div class=ndt>{q}</div>'
                    f'<div class=ndr>question · {_esc(str(d.get("id") or ""))}</div></div></a>')
    for a in ns.get("approvals", [])[:4]:
        rows.append(f'<a class=needrow href="/needs"><span class="nd ok"></span>'
                    f'<div class=ndmain><div class=ndt>{_esc(a.get("label") or "recommendation")}</div>'
                    f'<div class=ndr>approval</div></div></a>')
    for t in ns.get("tasks", [])[:4]:
        rows.append(f'<a class=needrow href="/needs"><span class="nd bad"></span>'
                    f'<div class=ndmain><div class=ndt>{_esc(str(t.get("ticket_id") or ""))} — '
                    f'{_esc(str(t.get("outcome") or ""))}</div><div class=ndr>run</div></div></a>')
    return "".join(rows) + '<a class=needall href="/needs">Open inbox &#8594;</a>'


_TALK_HTML = (
    '<div class=talk>'
    '<a class=talkbtn href="/chat"><span class=tki>&#128172;</span>'
    '<div><b>General</b><i>ask the orchestrator 1:1</i></div></a>'
    '<a class=talkbtn href="/group"><span class=tki>&#128101;</span>'
    '<div><b>Group room</b><i>convene all the officers</i></div></a>'
    '</div>')


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


def _liveness(state: dict, active: bool) -> str:
    """A heartbeat chip: green while the unit is printing steps, amber/red if it goes quiet —
    so you can tell 'working' from 'stuck' at a glance."""
    if not active:
        return ""
    la = state.get("last_activity")
    if not la:
        return '<span class="lv work">&#9679; working</span>'
    idle = max(0.0, datetime.now().timestamp() - la)
    if idle < 120:
        secs = int(idle)
        return f'<span class="lv work">&#9679; working · last step {secs}s ago</span>'
    m = max(1, int(idle // 60))
    if idle < 360:
        return f'<span class="lv quiet">&#9680; quiet for {m}m</span>'
    return f'<span class="lv stuck">&#9888; no step for {m}m — may be stuck</span>'


# The synced badge globs + stat()s every peer file; its text is minute-precision ("Xm ago"), so there is
# no point recomputing it on every ~2s SSE frame. Cache the rendered string per audit-path for ~45s.
_SYNC_CACHE: dict[str, tuple[float, str]] = {}   # audit_path -> (fetched_ts, html)
_SYNC_TTL = 45.0


def _sync_html(cfg) -> str:
    """Subtle one-line badge: which machines' audits are merged into this view, and how fresh.
    Empty (no clutter) when the state clone isn't set up yet — i.e. a stand-alone machine."""
    key = str(getattr(cfg, "audit_path", ""))
    now = time.time()
    hit = _SYNC_CACHE.get(key)
    if hit and now - hit[0] < _SYNC_TTL:
        return hit[1]
    html_out = _sync_html_uncached(cfg)
    _SYNC_CACHE[key] = (now, html_out)
    return html_out


def _sync_html_uncached(cfg) -> str:
    try:
        from . import sync
        files = sync.shared_files(cfg)
    except Exception:  # noqa: BLE001
        return ""
    if not files:
        return ""
    peers = ", ".join(sorted(p.stem for p in files))
    ago = ""
    try:
        newest = max(f.stat().st_mtime for f in files)
        ago = " · " + _fmt_dur(datetime.now().timestamp() - newest) + " ago"
    except OSError:
        pass
    return f'<div class=synced>&#8646; synced: {html.escape(peers)}{html.escape(ago)}</div>'


_BACKLOG_CACHE: dict[str, tuple[float, list]] = {}   # scope -> (fetched_ts, [(AppConfig, Ticket)])
_BACKLOG_TTL = 90.0   # the board re-renders every couple seconds; only hit Jira at most once/90s/scope


def _backlog_items(cfg, app: Optional[str]) -> tuple[list, Optional[str]]:
    """Open To-Do / In-Progress tickets assigned to the Commander for ``app`` — or EVERY backlogged app
    when ``app`` is None/'*'. TTL-cached so the SSE poll doesn't call Jira on every frame; serves the
    last good result if a refresh fails, so a Jira blip never blanks the panel. Returns (items, error)."""
    scope = app if (app and app != "*") else "*"
    now = time.time()
    hit = _BACKLOG_CACHE.get(scope)
    if hit and now - hit[0] < _BACKLOG_TTL:
        return hit[1], None
    try:
        from . import intake
        name = None if scope == "*" else scope     # None → from_drain spans all backlogged apps
        items = intake.from_drain(cfg, name, 25)
        _BACKLOG_CACHE[scope] = (now, items)
        return items, None
    except Exception as exc:  # noqa: BLE001 - never let a backlog fetch break the board
        if hit:
            return hit[1], None
        return [], str(exc)[:140]


def _backlog_html(cfg, app: Optional[str]) -> str:
    items, err = _backlog_items(cfg, app)
    scope = "all projects" if (not app or app == "*") else _esc(app)
    # Surface any board the last drain couldn't read — so an unreachable/misconfigured Jira shows as a
    # loud warning here instead of silently looking like "nothing of yours open".
    from . import intake as _intake
    derrs = dict(getattr(_intake, "LAST_DRAIN_ERRORS", {}) or {})
    if app and app != "*":
        derrs = {n: m for n, m in derrs.items() if n == app}
    warn = "".join(
        f'<div class=blempty style="color:var(--bad,#f0676b)">&#9888; {_esc(n)} backlog unreachable — '
        f'{_esc(m)}</div>' for n, m in derrs.items())
    if err:
        return warn + f'<div class=blempty>Backlog unavailable for {scope} — {_esc(err)}</div>'
    if not items:
        return warn + (f'<div class=blempty>&#10003; Nothing of yours open in {scope} '
                       '(In&nbsp;Progress / To&nbsp;Do).</div>')
    multi = (not app or app == "*")
    rows = []
    for a, t in items:
        an = _esc(getattr(a, "name", "") or "")
        tid = _esc(getattr(t, "id", "") or getattr(t, "key", "") or "?")
        summ = _esc(getattr(t, "summary", "") or "(no summary)")
        badge = f'<span class=blapp>{an}</span>' if multi else ""
        rows.append(
            f'<a class=blrow href="/tickets?app={quote(an)}" title="Open the {an} backlog to develop this">'
            f'<span class=blkey>{tid}</span><span class=blsum>{summ}</span>{badge}</a>')
    head = (f'<a class=blmore href="/tickets?app={quote(app) if (app and app!="*") else "*"}">'
            f'{len(items)} open &middot; develop &rarr;</a>')
    return warn + f'<div class=blhead>{head}</div><div class=bllist>{"".join(rows)}</div>'


def render_board(cfg, app: Optional[str], state: dict, log_lines=None) -> str:
    """Inner board (everything that updates on the poll)."""
    ap_on = bool((state.get("autopilot") or {}).get("on"))
    tasks = D.load_tasks(cfg.audit_path)
    # A run is live if the cockpit started it, autopilot is on, OR there's fresh audit activity with no
    # terminal outcome yet — the last case covers builds started by the answer box / /unblock / another
    # process, which otherwise (wrongly) render as "last run · interrupted".
    inflight = (not state.get("active") and not ap_on) and _run_in_flight(cfg, tasks, app)
    active = bool(state.get("active")) or ap_on or inflight
    dry = state.get("dry_run")
    mode = None
    if active:
        mode = "live" if (ap_on or dry is False or inflight) else ("dry" if dry is True else None)
    # Elapsed on the live run; Stop only for a manual run (Autopilot stops from the header).
    elapsed = None
    rs = state.get("run_started")
    if active and rs:
        elapsed = _fmt_dur(datetime.now().timestamp() - rs)
    manual = bool(state.get("active")) and not ap_on
    run_obj = active_run(cfg, tasks, app, active)
    k = _kpi_html(kpis(cfg, tasks, app))
    run = _run_html(run_obj, mode, elapsed, manual)
    hero = _hero_html(run_obj, elapsed, mode)
    fd = _feed_html(feed(cfg, tasks, app))
    try:
        from . import needs as _needs
        ns = _needs.summary(cfg)
    except Exception:  # noqa: BLE001
        ns = {"total": 0, "decisions": [], "approvals": [], "tasks": []}
    needs_body = _needs_side_html(ns)
    ncount = f' · {ns["total"]}' if ns.get("total") else ""
    log_panel = ""
    if log_lines is not None:
        log_panel = (f'<section class=panel><div class=ph>Live feed{_liveness(state, active)}</div>'
                     f'{_log_html(log_lines)}</section>')
    return (
        f'{hero}'
        f'<div class=kpis>{k}</div>'
        f'{_sync_html(cfg)}'
        '<div class=cols>'
        f'<div class=col-main>'
        f'<section class=panel><div class=ph>Active run</div><div class=run>{run}</div></section>'
        f'{log_panel}'
        f'<details class="panel collapse" id=blpanel open>'
        f'<summary class=ph>Tickets to work &middot; '
        f'{"all projects" if (not app or app == "*") else _esc(app)}</summary>'
        f'<div class=backlog id=blbox>{_backlog_html(cfg, app)}</div></details>'
        f'<details class="panel collapse" id=actpanel open><summary class=ph>Activity</summary>'
        f'<div class=feed id=actbox>{fd}</div></details>'
        '</div>'
        f'<div class=col-side>'
        f'<section class="panel needspanel"><div class=ph>Needs you{ncount}</div>'
        f'<div class=needs>{needs_body}</div></section>'
        f'<section class=panel><div class=ph>Talk to the unit</div>{_TALK_HTML}</section>'
        '</div>'
        '</div>')


def project_selector(cfg, app: Optional[str]) -> str:
    sel = app or "*"
    try:
        from . import projects
        rec, disc = projects.recents(cfg), projects.discover_repos(cfg)
    except Exception:  # noqa: BLE001
        rec, disc = [], []
    opts = [f'<option value="*" {"selected" if sel == "*" else ""}>All projects</option>']
    if rec:
        opts.append('<optgroup label="&#9733; Recent">')
        opts += [f'<option value="{_esc(n)}" {"selected" if n == sel else ""}>{_esc(n)}</option>' for n in rec]
        opts.append("</optgroup>")
    opts.append('<optgroup label="Projects">')
    opts += [f'<option value="{_esc(a.name)}" {"selected" if a.name == sel else ""}>{_esc(a.name)}</option>'
             for a in cfg.apps]
    opts.append("</optgroup>")
    if disc:
        # Informational only — a <select> option can't navigate, so don't promise "click to onboard"
        # here. Onboarding lives on the ＋ Product page (these repos are click-to-onboard links there).
        opts.append('<optgroup label="Found nearby (add via &#10133; Product)">')
        opts += [f'<option value="*" disabled>{_esc(r["name"])} &mdash; {_esc(r["path"])}</option>'
                 for r in disc[:12]]
        opts.append("</optgroup>")
    return (f'<select id=proj onchange="proj(this.value)" title="project / app — recent on top; nearby repos listed">'
            f'{"".join(opts)}</select>')


def health_pill(h: dict) -> str:
    probs = [c for c in h.get("checks", []) if c["status"] == "bad"]
    warns = [c for c in h.get("checks", []) if c["status"] == "warn"]
    healthy = h.get("healthy")
    cls = "ok" if healthy else "bad"
    n = len(probs)
    label = "&#9679; System healthy" if healthy else f"&#9679; {n} problem" + ("s" if n != 1 else "")
    if healthy and warns:
        label += f" · {len(warns)} warning" + ("s" if len(warns) != 1 else "")
    items = probs + warns
    if not items:
        return f'<span class="hpill {cls}">{label}</span>'
    rows = "".join(
        f'<div class=hpi><span class="tag {c["status"]}">{"fix" if c["status"] == "bad" else "warn"}</span> '
        f'{_esc(c["name"])}{(" — " + _esc(c["detail"])) if c["detail"] else ""}</div>' for c in items)
    return (f'<details class="hd {cls}"><summary class="hpill {cls}">{label}</summary>'
            f'<div class=hpanel>{rows}'
            '<button class=recheck type=button onclick="location.reload()">Re-check</button></div></details>')


def health_banner(h: dict) -> str:
    # Only shown when something is actually wrong — when healthy, the header pill is enough,
    # so the cockpit stays calm (warnings live behind the pill).
    if h.get("healthy"):
        return ""
    bad = [c for c in h.get("checks", []) if c["status"] == "bad"]
    items = "".join(
        f'<li><span class="tag bad">fix</span> {_esc(c["name"])}'
        f'{(" — " + _esc(c["detail"])) if c["detail"] else ""}</li>' for c in bad)
    return (f'<div class="healthbar bad"><div class=hbrow>'
            f'<div class=hbtitle><span class=hbdot></span>'
            f'{len(bad)} problem{"s" if len(bad) != 1 else ""} to fix before the unit can work tickets</div>'
            '<div class=hbactions><button class=hbbtn type=button onclick="location.reload()">Re-check</button>'
            f'</div></div><ul class=hbissues>{items}</ul></div>')


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
    confirm = ('onsubmit="return confirm(\'Start Autopilot? The unit will work the queue '
               'LIVE — building and merging to DEV until you press Stop.\')"')
    return (f'<form method=post action=/api/autopilot class=apsw {confirm}>'
            '<input type=hidden name=action value=start>'
            f'<input type=hidden name=app value="{appq}">'
            f'<span class="apdot off"></span><span class=aplabel>Autopilot&nbsp;<b>off</b></span>'
            f'<button class="apbtn start" {dis}>Start</button></form>')


def _host_tag(cfg) -> str:
    """A small pill in the header naming the machine this cockpit runs on, so the Mac cockpit and the
    tunnelled server cockpit (both served on localhost:8787) are instantly tellable apart."""
    try:
        from . import sync
        h = sync.host_id(cfg)
    except Exception:  # noqa: BLE001
        return ""
    return f'<span class=hosttag title="this cockpit is running on this machine">{html.escape(h)}</span>'


def render_page(cfg, app: Optional[str], state: dict, control_bar: str, health: dict,
                log_lines=None) -> str:
    return (_PAGE
            .replace("{{HOST}}", _host_tag(cfg))
            .replace("{{PROJ}}", project_selector(cfg, app))
            .replace("{{AUTOPILOT}}", autopilot_switch(state, app, health.get("healthy", False)))
            .replace("{{HEALTHPILL}}", health_pill(health))
            .replace("{{HEALTHBAR}}", health_banner(health))
            .replace("{{BAR}}", control_bar)
            .replace("{{BOARD}}", render_board(cfg, app, state, log_lines))
            .replace("{{APP}}", _esc(app or "*"))
            .replace("{{GEN}}", datetime.now().strftime("%H:%M:%S")))


_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Elite Unit — War Room</title>
<style>
:root{color-scheme:dark;
--bg:#080a0f;--panel:#0f141d;--panel2:#141a25;--line:#1b2230;--line2:#283342;
--ink:#e7ebf2;--dim:#7e8795;--faint:#515a67;
--ok:#34d399;--okbg:#0e2a1e;--warn:#f5b34a;--warnbg:#2c2410;--bad:#f0676b;--badbg:#2a1417;
--info:#6aa9ff;--accent:#4d7cff;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;margin:0;color:var(--ink);
background:radial-gradient(1100px 440px at 80% -10%,rgba(77,124,255,.10),transparent 60%),
radial-gradient(820px 320px at 4% -6%,rgba(52,211,153,.045),transparent 55%),var(--bg);background-attachment:fixed}
a{color:var(--info);text-decoration:none}
.mono{font-family:var(--mono);font-size:.85em}
.muted{color:var(--dim)}
button{font:inherit}
/* header */
header{display:flex;align-items:center;gap:14px;padding:14px 26px;border-bottom:1px solid var(--line);
background:linear-gradient(180deg,#11151e,#0a0c11);position:sticky;top:0;z-index:5;flex-wrap:wrap}
.brand{font-size:15px;font-weight:750;letter-spacing:.4px;white-space:nowrap;text-transform:uppercase}
.hosttag{margin-left:10px;font-size:10.5px;font-weight:700;color:#9fb0cf;background:#1a2333;border:1px solid #2a3850;border-radius:999px;padding:2px 9px;vertical-align:middle;letter-spacing:.06em;text-transform:lowercase}
.brand b{color:var(--accent)}
header select{background:#0d1119;border:1px solid var(--line2);color:var(--ink);border-radius:9px;
padding:8px 12px;font:inherit;cursor:pointer}
.spacer{flex:1}
.gen{font-family:var(--mono);font-size:11px;color:var(--faint);letter-spacing:.02em}
.sdot{display:inline-block;width:7px;height:7px;border-radius:99px;background:var(--faint);margin-right:5px;vertical-align:middle}
.sdot.on{background:var(--ok);box-shadow:0 0 7px var(--ok);animation:pulse2 1.6s infinite}
.sdot.off{background:var(--warn)}
/* health pill */
.hpill{font-size:12px;font-weight:700;padding:6px 13px;border-radius:99px}
.hpill.ok{color:var(--ok);background:var(--okbg);border:1px solid #1c5238}
.hpill.bad{color:var(--bad);background:var(--badbg);border:1px solid #5a1f22}
.hd{position:relative}.hd>summary{list-style:none;cursor:pointer}
.hd>summary::-webkit-details-marker{display:none}
.hpanel{position:absolute;top:calc(100% + 8px);right:0;z-index:40;min-width:320px;background:var(--panel);
border:1px solid var(--line2);border-radius:12px;padding:10px;box-shadow:0 16px 40px rgba(0,0,0,.55)}
.hpi{font-size:12.5px;color:var(--dim);padding:5px 4px}
.recheck{margin-top:9px;background:#1b2230;border:1px solid var(--line2);color:var(--ink);border-radius:8px;
padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer}
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
.tag{font-family:var(--mono);font-size:10px;font-weight:700;text-transform:uppercase;padding:2px 6px;border-radius:5px;margin-right:8px}
.tag.bad{background:var(--badbg);color:var(--bad)}.tag.warn{background:var(--warnbg);color:var(--warn)}
/* kpis */
.kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;padding:22px 24px 8px}
.kpi{position:relative;background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:16px 17px;
overflow:hidden;transition:border-color .15s,transform .15s}
.kpi::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;background:#2a3342}
.kpi:hover{transform:translateY(-1px);border-color:var(--line2)}
a.kpi{display:block;text-decoration:none;color:inherit;cursor:pointer}
a.kpi:hover{border-color:var(--accent)}
.kpi .kv{font-family:var(--mono);font-size:30px;font-weight:600;line-height:1;letter-spacing:-1px;font-variant-numeric:tabular-nums}
.kpi .kl{font-size:11.5px;color:var(--ink);margin-top:9px;font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.kpi .kh{font-size:11px;color:var(--faint);margin-top:3px}
.kpi.ok::before{background:var(--ok)}.kpi.ok .kv{color:var(--ok)}
.kpi.warn::before{background:var(--warn)}.kpi.warn .kv{color:var(--warn)}
.kpi.bad::before{background:var(--bad)}.kpi.bad .kv{color:var(--bad)}
/* layout */
.cols{display:grid;grid-template-columns:1fr 340px;gap:16px;padding:14px 26px 40px}
.col-main{display:flex;flex-direction:column;gap:16px;min-width:0}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.ph{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:var(--dim);
padding:13px 18px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}
.ph::before{content:"";width:6px;height:6px;border-radius:2px;background:var(--accent)}
/* active run */
.run{padding:18px}
.runhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;gap:10px}
.runhead .mono{font-size:15px;color:var(--ink);font-weight:600}
.b{font-size:10px;font-weight:700;padding:4px 10px;border-radius:6px;text-transform:uppercase;letter-spacing:.06em}
.b.live{color:var(--warn);background:var(--warnbg)}.b.muted{color:var(--dim);background:#141a25}
.b.mode{color:var(--ok);background:var(--okbg);box-shadow:0 0 0 1px #1c4d39 inset}
.b.dry{color:var(--info);background:#0f1c30;box-shadow:0 0 0 1px #1e3457 inset}
.b.ok{color:var(--ok);background:var(--okbg)}.b.bad{color:var(--bad);background:var(--badbg)}.b.warn{color:var(--warn);background:var(--warnbg)}
.phasebar{display:flex;gap:0;position:relative}
.phasebar .ph{display:flex;flex-direction:column;align-items:center;gap:9px;flex:1;border:0;padding:0;text-transform:none;
letter-spacing:.02em;font-size:11.5px;font-weight:600;color:var(--faint);position:relative}
.phasebar .ph::before{display:none}
.phasebar .ph::after{content:"";position:absolute;top:6px;left:50%;width:100%;height:2px;background:#222b39;z-index:0}
.phasebar .ph:last-child::after{display:none}
.phasebar .ph span{width:13px;height:13px;border-radius:99px;background:var(--bg);flex:none;border:2px solid #2b3543;z-index:1;position:relative}
.phasebar .ph.done{color:var(--ink)}
.phasebar .ph.done span{background:var(--ok);border-color:var(--ok);box-shadow:0 0 8px rgba(52,211,153,.5)}
.phasebar .ph.done::after{background:var(--ok)}
/* idle = a finished 'last run', not live -> grey the bar so it never reads as in-progress */
.phasebar.idle .ph.done{color:var(--dim)}
.phasebar.idle .ph.done span{background:#39424f;border-color:#39424f;box-shadow:none}
.phasebar.idle .ph.done::after{background:#2b3543}
.phasebar .ph.now{color:var(--warn)}
.phasebar .ph.now span{background:var(--warn);border-color:var(--warn);animation:pulse 1.5s infinite}
/* failed = the run terminated at this phase -> red stopping node, never reads as cleanly-done */
.phasebar .ph.failed,.phasebar.idle .ph.failed{color:var(--bad)}
.phasebar .ph.failed span,.phasebar.idle .ph.failed span{background:var(--bad);border-color:var(--bad);box-shadow:0 0 8px rgba(240,103,107,.5)}
@keyframes pulse{0%,100%{box-shadow:0 0 0 3px rgba(245,179,74,.28)}50%{box-shadow:0 0 0 8px rgba(245,179,74,0)}}
.runmeta{display:flex;gap:24px;margin-top:18px;padding-top:14px;border-top:1px solid var(--line);flex-wrap:wrap}
.meta{font-size:12px;color:var(--dim)}.meta b{color:var(--ink);font-weight:600;font-family:var(--mono)}
.runempty{padding:26px 18px;color:var(--dim);display:flex;align-items:center;gap:10px}
.dot2{width:8px;height:8px;border-radius:99px;background:var(--faint)}
.stoprun{margin:0;display:inline}
.stopbtn{background:var(--badbg);color:var(--bad);border:1px solid #5a1f22;border-radius:6px;
padding:4px 11px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;cursor:pointer}
.stopbtn:hover{background:#3a181b}
/* roster */
.roster{padding:6px 0}
.offrow{display:flex;align-items:center;gap:12px;padding:10px 18px;border-left:2px solid transparent}
.offrow:hover{background:var(--panel2);border-left-color:var(--line2)}
a.offrow{text-decoration:none;color:inherit;cursor:pointer}
.d{width:8px;height:8px;border-radius:99px;flex:none;background:#39424f}
.d.live{background:var(--ok);box-shadow:0 0 8px var(--ok);animation:pulse2 1.4s infinite}
.d.recent{background:var(--info)}.d.idle{background:#39424f}
@keyframes pulse2{0%,100%{box-shadow:0 0 0 0 rgba(52,211,153,.5)}50%{box-shadow:0 0 0 5px rgba(52,211,153,0)}}
.offmain{flex:1;min-width:0}.offname{font-weight:600;font-size:13px}
.offrole{font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.04em}
.offlast{font-family:var(--mono);font-size:11px;color:var(--dim);white-space:nowrap}
/* feed */
.feed{padding:5px 0;height:360px;min-height:120px;max-height:74vh;resize:vertical;overflow:auto}
/* backlog — tickets to work, scoped to the project selector (all projects = every backlogged Jira) */
.backlog{padding:4px 0 6px;height:260px;min-height:110px;max-height:74vh;resize:vertical;overflow:auto}
.blhead{padding:6px 16px 4px}
.blmore{font-size:11.5px;font-weight:700;color:var(--info)}
.bllist{display:flex;flex-direction:column}
.blrow{display:flex;gap:11px;align-items:baseline;padding:9px 16px;border-top:1px solid var(--line);color:var(--ink)}
.blrow:hover{background:var(--panel2)}
.blkey{font-family:var(--mono);font-size:12px;color:var(--info);white-space:nowrap}
.blsum{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.blapp{font-size:10.5px;font-weight:700;color:var(--dim);background:#1a2333;border:1px solid var(--line2);border-radius:999px;padding:1px 8px;white-space:nowrap}
.blempty{padding:14px 16px;color:var(--dim);font-size:13px}
.fitem{display:flex;gap:11px;padding:9px 17px;border-top:1px solid #161b24}
.fitem:first-child{border-top:0}
.fd{width:7px;height:7px;border-radius:99px;margin-top:6px;flex:none;background:var(--faint)}
.fd.ok{background:var(--ok)}.fd.warn{background:var(--warn)}.fd.bad{background:var(--bad)}
.fd.info{background:var(--info)}.fd.muted{background:var(--faint)}
.fbody{font-size:13px;min-width:0}.fmeta{font-family:var(--mono);font-size:11px;color:var(--faint);margin-top:3px}
/* live log */
.logbox{font-family:var(--mono);font-size:11.5px;line-height:1.55;color:#b9c2cf;background:#070a0e;
margin:0;padding:13px 16px;height:380px;min-height:150px;max-height:78vh;resize:vertical;overflow:auto;
white-space:pre-wrap;word-break:break-word}
.logbox .lg-b{color:var(--warn)}.logbox .lg-ok{color:var(--ok)}.logbox .lg-dim{color:var(--faint)}
details.collapse{padding:0}
details.collapse>summary{cursor:pointer;list-style:none;user-select:none}
details.collapse>summary::-webkit-details-marker{display:none}
details.collapse>summary::after{content:"\\25BE";float:right;color:var(--faint);font-size:11px;font-weight:400;transition:transform .15s;margin-top:1px}
details.collapse[open]>summary::after{transform:rotate(180deg)}
details.collapse>summary:hover::after{color:var(--ink)}
details.collapse:not([open])>summary{opacity:.82}
.logempty{padding:16px;color:var(--faint);font-size:12.5px}
.lv{margin-left:auto;font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:.03em;
padding:3px 9px;border-radius:6px;text-transform:none}
.lv.work{color:var(--ok);background:var(--okbg)}
.lv.work::after{content:"";display:inline-block;width:6px;height:6px;border-radius:99px;background:var(--ok);
margin-left:7px;vertical-align:middle;box-shadow:0 0 6px var(--ok);animation:pulse2 1.4s infinite}
.lv.quiet{color:var(--warn);background:var(--warnbg)}
.lv.stuck{color:var(--bad);background:var(--badbg)}
/* synced badge — which machines' audits are merged into this view */
.synced{margin:6px 24px 0;font-size:11px;color:#5b6b86;letter-spacing:.02em}
/* hero — the live-run headline (biggest thing when a run is in flight) */
.hero{margin:18px 24px 0;padding:16px 20px;border:1px solid #243049;border-radius:14px;
background:linear-gradient(120deg,rgba(77,124,255,.14),rgba(245,179,74,.06));position:relative;overflow:hidden}
.hgrow{display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap}
.hgleft{display:flex;align-items:center;gap:14px;min-width:0}
.hgdot{width:13px;height:13px;border-radius:99px;background:var(--warn);animation:pulse 1.5s infinite;flex:none}
.hgtitle{font-size:22px;font-weight:700;letter-spacing:-.3px}
.hgtitle .hgapp{font-size:13px;color:var(--dim);font-weight:500;margin-left:6px}
.hgsub{font-size:13px;color:var(--dim);margin-top:2px}.hgsub b{color:var(--warn)}
.hgchip{font-size:10px;font-weight:700;padding:3px 8px;border-radius:6px;text-transform:uppercase;letter-spacing:.05em}
.hgchip.live{color:var(--ok);background:var(--okbg)}.hgchip.dry{color:var(--info);background:#0f1c30}
.hgstats{display:flex;gap:26px}
.hgstat{text-align:right}.hgk{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--faint)}
.hgv{font-family:var(--mono);font-size:20px;font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums}
/* needs-you side panel + talk-to-the-unit */
.needs{padding:6px 0}
.needsok{display:flex;align-items:center;gap:9px;padding:14px 18px;color:var(--ok);text-decoration:none;font-size:13px}
.nok{font-weight:800}
.needrow{display:flex;align-items:flex-start;gap:11px;padding:10px 18px;text-decoration:none;color:inherit;border-left:2px solid transparent}
.needrow:hover{background:var(--panel2);border-left-color:var(--line2)}
.nd{width:8px;height:8px;border-radius:99px;margin-top:5px;flex:none;background:var(--faint)}
.nd.ok{background:var(--ok)}.nd.warn{background:var(--warn)}.nd.bad{background:var(--bad)}
.ndmain{min-width:0}.ndt{font-size:13px;font-weight:600;color:var(--ink)}
.ndr{font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.04em;margin-top:1px}
.needall{display:block;padding:11px 18px;font-size:12px;font-weight:650;color:var(--info);border-top:1px solid var(--line)}
.needspanel .ph::before{background:var(--warn)}
.talk{padding:10px;display:flex;flex-direction:column;gap:8px}
.talkbtn{display:flex;align-items:center;gap:12px;padding:12px 14px;border:1px solid var(--line2);border-radius:11px;
background:#0d1119;text-decoration:none;color:inherit}
.talkbtn:hover{border-color:var(--accent);background:var(--panel2)}
.tki{font-size:20px}.talkbtn b{display:block;font-size:13.5px}.talkbtn i{font-style:normal;font-size:11.5px;color:var(--dim)}
@media(max-width:1080px){.kpis{grid-template-columns:repeat(3,1fr)}.cols{grid-template-columns:1fr}.hgstats{gap:18px}}
@media(max-width:680px){.kpis{grid-template-columns:repeat(2,1fr)}.hbactions .models{display:none}}
@media(prefers-reduced-motion:reduce){*{animation:none!important}}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:#222b39;border-radius:8px}
</style></head><body>
<header>
  <div class=brand>&#9733; Elite Unit <b>·</b> War Room{{HOST}}</div>
  {{PROJ}}
  <div class=spacer></div>
  {{AUTOPILOT}}
  {{HEALTHPILL}}
  <span class=gen><span id=streamdot class="sdot off" title="live stream"></span>live · {{GEN}}</span>
</header>
{{HEALTHBAR}}
{{BAR}}
<div id=board>{{BOARD}}</div>
<script>
var APP="{{APP}}";
function proj(v){APP=v;location.search="?app="+encodeURIComponent(v);}
document.addEventListener("click",function(e){
  document.querySelectorAll("details[open]").forEach(function(d){
    if(!d.classList.contains("collapse") && !d.contains(e.target)) d.removeAttribute("open");
  });
});
// Collapsible Activity panel + resizable terminal live INSIDE #board, which the SSE feed re-renders
// every frame — so persist their state and re-apply it after each refresh (otherwise it resets).
function saveUi(){try{
  ["actpanel","blpanel"].forEach(function(id){var p=document.getElementById(id);
    if(p)localStorage.setItem("ui.open."+id,p.open?"1":"0");});
  ["logbox","blbox","actbox"].forEach(function(id){var el=document.getElementById(id);
    if(el&&el.style.height)localStorage.setItem("ui.h."+id,el.style.height);});
}catch(e){}}
function applyUi(){try{
  ["actpanel","blpanel"].forEach(function(id){var p=document.getElementById(id);
    if(p){var v=localStorage.getItem("ui.open."+id);
      if(v==="0")p.removeAttribute("open");else if(v==="1")p.setAttribute("open","");
      p.addEventListener("toggle",saveUi);}});
  ["logbox","blbox","actbox"].forEach(function(id){var el=document.getElementById(id);
    if(el){var h=localStorage.getItem("ui.h."+id);if(h)el.style.height=h;
      if(window.ResizeObserver)new ResizeObserver(saveUi).observe(el);}});
}catch(e){}}
function scrollLog(){var lb=document.getElementById("logbox");if(lb)lb.scrollTop=lb.scrollHeight;}
function _atBottom(el){return (el.scrollHeight-el.scrollTop-el.clientHeight)<24;}
function applyBoard(html){
  saveUi();
  var b=document.getElementById("board");if(!b)return;
  // Remember each scroll panel's position so the 2s refresh doesn't yank you around while you read:
  // if you were at the bottom (following live output) we keep you pinned there; otherwise we restore
  // your exact scroll position instead of jumping to the top/bottom.
  var keep={};
  ["logbox","blbox","actbox"].forEach(function(id){var el=document.getElementById(id);
    if(el)keep[id]={top:el.scrollTop,bottom:_atBottom(el)};});
  b.innerHTML=html;
  applyUi();
  ["logbox","blbox","actbox"].forEach(function(id){var el=document.getElementById(id);var k=keep[id];
    if(el&&k)el.scrollTop=k.bottom?el.scrollHeight:k.top;
    else if(el&&id==="logbox")el.scrollTop=el.scrollHeight;});
}
async function tick(){
  try{
    var r=await fetch("/api/board?app="+encodeURIComponent(APP),{cache:"no-store"});
    if(r.ok)applyBoard(await r.text());
  }catch(e){}
}
function setDot(s){var d=document.getElementById("streamdot");if(d)d.className="sdot "+s;}
// Real-time: push board frames over SSE; fall back to the 5s poll if the stream drops.
var _es=null,_poll=null;
function fallback(){if(!_poll)_poll=setInterval(tick,5000);}
function startStream(){
  if(typeof(EventSource)==="undefined"){setDot("off");fallback();return;}
  try{
    _es=new EventSource("/api/stream?app="+encodeURIComponent(APP));
    _es.addEventListener("board",function(e){applyBoard(e.data);setDot("on");});
    _es.onopen=function(){setDot("on");if(_poll){clearInterval(_poll);_poll=null;}};
    _es.onerror=function(){setDot("off");if(_es){_es.close();_es=null;}fallback();setTimeout(startStream,4000);};
  }catch(e){setDot("off");fallback();}
}
applyUi();
scrollLog();
startStream();
</script>
</body></html>"""
