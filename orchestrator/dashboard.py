"""Build the monitor / cockpit from the audit log.

`general dashboard` -> a self-contained dashboard.html (open in any browser).
`general status`    -> a quick table in the terminal.
The control panel (`general serve`) reuses render_html for its live view.

Every task shows app, ticket, start/end/duration, passes, turns, verdict, outcome,
branch, PR — and an expandable transcript: per pass the effort used, the Builder's
tool calls + summary, and the Reviewer's verdict + feedback. Full transparency.
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_TERMINAL = {"merged", "pr_opened", "escalated", "dryrun_land", "ship_dryrun",
             "ticket_exception", "no_changes", "needs_human", "pm_triage"}
_OUTCOME = {
    "merged": "merged→dev", "pr_opened": "PR / needs you", "escalated": "escalated",
    "dryrun_land": "dry-run", "ship_dryrun": "dry-run",
    "ticket_exception": "errored", "no_changes": "errored",
    "needs_human": "awaiting decision",
    "pm_triage": "re-queued",   # PM sent it back for one corrective pass — not a Needs-you item
}
_NEEDS_YOU = {"PR / needs you", "escalated", "errored", "awaiting decision"}


def _parse_ts(ts: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _human_dur(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def audit_lines(audit_path: str | Path) -> list[str]:
    """Every audit line for the unified view: this machine's live ``audit.jsonl`` PLUS each synced
    ``shared/<host>.jsonl`` published by the other machines (see orchestrator/sync.py). Exact-duplicate
    lines are collapsed — a host's own events live in both its audit.jsonl and its published copy, so
    they are counted once. Order is local-first then shared; callers that care sort by ts."""
    p = Path(audit_path)
    paths: list[Path] = [p]
    # Synced peers: the state clone's shared/ (orphan unit-state branch); the bare shared/ form is
    # accepted too so tests and any future local-only layout work without the clone.
    for shared in (p.parent / ".unit-state" / "shared", p.parent / "shared"):
        if shared.is_dir():
            paths += sorted(shared.glob("*.jsonl"))
    seen: set[str] = set()
    out: list[str] = []
    for fp in paths:
        try:
            text = fp.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            s = line.strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return out


def load_tasks(audit_path: str | Path) -> list[dict[str, Any]]:
    lines = audit_lines(audit_path)
    if not lines:
        return []
    def _new(tid: str, ev: dict) -> dict[str, Any]:
        return {"ticket_id": tid, "app": ev.get("app"), "branch": ev.get("branch"),
                "started": None, "ended": None, "passes": 0, "turns": 0, "cost": 0.0,
                "verdict": None, "outcome": None, "pr_url": None, "dry_run": None,
                "note": "", "detail": {}}

    # Each `ticket_start` begins a SEPARATE run — so a re-run of the same ticket (e.g. a dry-run
    # then a live run) does NOT merge the earlier run's phases/verdict into the new one.
    runs: list[dict[str, Any]] = []
    cur: dict[str, dict[str, Any]] = {}     # ticket_id -> its currently-open run
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        tid = ev.get("ticket_id")
        if not tid:
            continue
        kind = ev.get("event")
        ts = _parse_ts(ev.get("ts", ""))
        if kind == "ticket_start":
            t = _new(tid, ev)
            t["started"] = ts
            t["dry_run"] = ev.get("dry_run")
            runs.append(t)
            cur[tid] = t
            continue
        t = cur.get(tid)
        if t is None:                       # an event with no preceding ticket_start (legacy/partial)
            t = _new(tid, ev)
            runs.append(t)
            cur[tid] = t
        if kind == "build":
            it = ev.get("iteration", 0)
            t["passes"] = max(t["passes"], it)
            t["turns"] += ev.get("turns", 0) or 0
            t["cost"] += ev.get("cost_usd", 0) or 0
            d = t["detail"].setdefault(it, {})
            d["effort"] = ev.get("effort")
            d["tools"] = ev.get("tools", []) or []
            d["build_summary"] = ev.get("summary", "")
        elif kind == "review":
            it = ev.get("iteration", 0)
            t["verdict"] = ev.get("verdict", t["verdict"])
            t["cost"] += ev.get("cost_usd", 0) or 0
            d = t["detail"].setdefault(it, {})
            d["verdict"] = ev.get("verdict")
            d["review_summary"] = ev.get("summary", "")
            d["required_changes"] = ev.get("required_changes", []) or []
            d["issues"] = ev.get("issues", []) or []
        if kind in _TERMINAL:
            t["ended"] = ts or t["ended"]
            t["outcome"] = _OUTCOME.get(kind, kind)
            t["pr_url"] = ev.get("pr_url", t["pr_url"])
            t["note"] = (ev.get("note") or ev.get("reason") or ev.get("error")
                         or ev.get("question") or t["note"])
    for t in runs:
        t["duration"] = ((t["ended"] - t["started"]).total_seconds()
                         if t["started"] and t["ended"] else None)
        t["passes_list"] = [dict(d, n=k) for k, d in sorted(t["detail"].items())]
    # newest run first (tz-safe: avoids comparing aware vs naive datetimes)
    runs.sort(key=lambda x: x["started"].timestamp() if x["started"] else 0.0, reverse=True)
    return runs


# --------------------------------------------------------------------------- #
# Dismissals — the Commander can clear a 'needs you' item once handled. Per-ticket with a
# timestamp, so a LATER run of the same ticket that fails again reappears.
def _dismissed_file(audit_path: str | Path) -> Path:
    return Path(audit_path).with_name("dismissed.json")


def load_dismissed(audit_path: str | Path) -> dict[str, str]:
    try:
        data = json.loads(_dismissed_file(audit_path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def dismiss(audit_path: str | Path, ticket_id: str) -> None:
    import time
    d = load_dismissed(audit_path)
    d[str(ticket_id)] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        _dismissed_file(audit_path).write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def _is_dismissed(t: dict[str, Any], dismissed: dict | None) -> bool:
    """A needs-you task is hidden if it (or an older run) was dismissed — but a newer run shows."""
    if not dismissed:
        return False
    da = dismissed.get(str(t.get("ticket_id")))
    if not da:
        return False
    dt, st = _parse_ts(da), t.get("started")
    return (st <= dt) if (dt and st) else True


# --------------------------------------------------------------------------- #
def _badge(outcome: Optional[str]) -> str:
    cls = {"merged→dev": "ok", "dry-run": "muted", "PR / needs you": "warn",
           "escalated": "warn", "errored": "bad"}.get(outcome or "", "muted")
    return f'<span class="b {cls}">{html.escape(outcome or "running…")}</span>'


def _detail_html(t: dict[str, Any]) -> str:
    if not t["passes_list"]:
        return '<div class=det><span class=muted>no transcript captured</span></div>'
    blocks = []
    for d in t["passes_list"]:
        tools = ", ".join(html.escape(x) for x in d.get("tools", [])) or "—"
        rc = d.get("required_changes") or []
        issues = d.get("issues") or []
        rc_html = ("<div class=sub><b>Reviewer asked for:</b><ul>"
                   + "".join(f"<li>{html.escape(str(x))}</li>" for x in rc) + "</ul></div>") if rc else ""
        is_html = ("<div class=sub><b>Issues:</b><ul>"
                   + "".join(f'<li><span class=sev>{html.escape(i.get("severity",""))}</span> '
                             f'{html.escape(i.get("area",""))}: {html.escape(i.get("detail",""))}</li>'
                             for i in issues) + "</ul></div>") if issues else ""
        v = d.get("verdict")
        vbadge = f'<span class="b {"ok" if v=="PASS" else "bad" if v=="FAIL" else "muted"}">{html.escape(v or "—")}</span>'
        blocks.append(
            f'<div class=pass><div class=passhead>Pass {d.get("n","?")} '
            f'<span class=eff>effort {html.escape(str(d.get("effort") or "—"))}</span></div>'
            f'<div class=sub><b>Builder:</b> {html.escape(d.get("build_summary") or "—")}</div>'
            f'<div class=sub><b>Tools:</b> <span class=mono>{tools}</span></div>'
            f'<div class=sub><b>Reviewer {vbadge}:</b> {html.escape(d.get("review_summary") or "—")}</div>'
            f'{rc_html}{is_html}</div>'
        )
    return '<div class=det>' + "".join(blocks) + '</div>'


def _finding_str(x: Any) -> str:
    """A reviewer finding may be a plain string or a {severity, area, detail} dict — render either."""
    if isinstance(x, dict):
        bits = [str(x.get("severity") or "").strip(), str(x.get("area") or "").strip()]
        head = " ".join(b for b in bits if b)
        detail = str(x.get("detail") or x.get("message") or "").strip()
        return (f"{head}: {detail}" if head and detail else head or detail or str(x))
    return str(x)


def _short(s: Any, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def brief(text: Any, n: int = 360) -> str:
    """A short, scannable version of a long escalation note for the Needs-you card. Prefers the PM's
    structured lines (BLOCKER/DECISION/OPTIONS/RECOMMENDATION) when present; otherwise the first couple
    of sentences. So the Commander reads the ask, not a wall of reasoning."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    keep = [ln.strip() for ln in raw.splitlines()
            if any(ln.upper().lstrip("*# -").startswith(m)
                   for m in ("BLOCKER", "DECISION", "OPTIONS", "RECOMMENDATION", "THE ASK"))]
    if keep:
        return _short(" · ".join(keep), n)
    import re
    out = ""
    for s in re.split(r"(?<=[.!?])\s+", " ".join(raw.split())):
        if out and len(out) + len(s) > n:
            break
        out += (" " if out else "") + s
    return _short(out or raw, n)


def needs_detail_html(t: dict[str, Any]) -> str:
    """The Needs-you card detail: lead with a BRIEF (the ask), then the build/review passes. The full
    raw note is shown dimmed + capped + scrollable underneath — never an unbounded wall of text."""
    rows: list[str] = []
    if t.get("note"):
        note = str(t["note"])
        b = brief(note)
        rows.append(f'<div class=ndt><b>The ask:</b> {html.escape(b)}</div>')
        # Keep the full note available but contained — only when it adds more than the brief.
        if len(" ".join(note.split())) > len(b) + 40:
            rows.append('<details class=ndfull><summary>full message</summary>'
                        f'<div class="ndt muted" style="white-space:pre-wrap;max-height:200px;'
                        f'overflow:auto;margin-top:6px">{html.escape(_short(note, 1600))}</div></details>')
    for p in (t.get("passes_list") or []):
        n = p.get("n", "?")
        if p.get("build_summary"):
            rows.append(f'<div class=ndt><b>Builder · pass {html.escape(str(n))}:</b> '
                        f'{html.escape(_short(p["build_summary"], 700))}</div>')
        if p.get("review_summary"):
            v = p.get("verdict")
            vlbl = f' · verdict {html.escape(str(v))}' if v else ''
            rows.append(f'<div class=ndt><b>Reviewer · pass {html.escape(str(n))}{vlbl}:</b> '
                        f'{html.escape(_short(p["review_summary"], 700))}</div>')
        for f in (p.get("required_changes") or [])[:8]:
            rows.append(f'<div class="ndt sub">• {html.escape(_short(_finding_str(f), 240))}</div>')
        for f in (p.get("issues") or [])[:8]:
            rows.append(f'<div class="ndt sub">• {html.escape(_short(_finding_str(f), 240))}</div>')
    if not rows:
        rows.append('<div class="ndt muted">No further detail was captured for this run — '
                    'open it with the General to investigate.</div>')
    return "".join(rows)


def needs_chat_summary(t: dict[str, Any]) -> str:
    """A compact plain-text brief of the problem, pre-loaded into the General chat when the Commander
    clicks 'Discuss with the General' — so he can send it as-is (or tweak) instead of retyping."""
    tid = str(t.get("ticket_id") or "this run")
    oc = str(t.get("outcome") or "needs attention")
    parts = [f"{tid} ended '{oc}'."]
    if t.get("note"):
        parts.append(_short(t["note"], 200))
    last = next((p for p in reversed(t.get("passes_list") or [])
                 if p.get("build_summary") or p.get("review_summary")), None)
    if last:
        if last.get("build_summary"):
            parts.append("Builder: " + _short(last["build_summary"], 260))
        if last.get("review_summary"):
            v = last.get("verdict")
            parts.append((f"Reviewer ({v}): " if v else "Reviewer: ") + _short(last["review_summary"], 260))
        findings = (last.get("required_changes") or []) + (last.get("issues") or [])
        if findings:
            parts.append("Open items: " + "; ".join(_short(_finding_str(f), 120) for f in findings[:3]))
    parts.append("What do you want me to do?")
    return " ".join(parts)


def render_html(tasks: list[dict[str, Any]], show_cost: bool = True, dismissed: dict | None = None) -> str:
    total = len(tasks)
    merged = sum(1 for t in tasks if t["outcome"] == "merged→dev")
    needs = [t for t in tasks if t["outcome"] in _NEEDS_YOU and not _is_dismissed(t, dismissed)]
    cards = [("Tasks", total, "all"), ("Merged → dev", merged, "merged"), ("Needs you", len(needs), "needs")]
    if show_cost:
        cards.append(("Est. cost", f"${sum(t['cost'] for t in tasks):.2f}", None))

    head = ["<th></th>", "<th>Status</th>", "<th>Ticket</th>", "<th>App</th>", "<th>Branch</th>",
            "<th>Started</th>", "<th>Dur</th>", "<th class=num>Passes</th>", "<th class=num>Turns</th>"]
    if show_cost:
        head.append("<th class=num>Cost</th>")
    head += ["<th>Verdict</th>", "<th>PR</th>"]
    ncols = len(head)

    rows = []
    for i, t in enumerate(tasks):
        pr = (f'<a href="{html.escape(t["pr_url"])}" target=_blank>PR ↗</a>' if t.get("pr_url") else "")
        started = t["started"].strftime("%b %d %H:%M") if t["started"] else "—"
        cost_cell = f'<td class=num>${t["cost"]:.2f}</td>' if show_cost else ""
        rows.append(
            f'<tr class=row onclick="tog({i})">'
            f'<td class=tw>▸</td>'
            f'<td>{_badge(t["outcome"])}{" <span class=dry>dry</span>" if t.get("dry_run") else ""}</td>'
            f'<td class=mono>{html.escape(str(t["ticket_id"]))}</td>'
            f'<td>{html.escape(str(t.get("app") or ""))}</td>'
            f'<td class=mono>{html.escape(str(t.get("branch") or ""))}</td>'
            f'<td>{started}</td><td>{_human_dur(t["duration"])}</td>'
            f'<td class=num>{t["passes"]}</td><td class=num>{t["turns"]}</td>'
            f'{cost_cell}<td>{html.escape(t["verdict"] or "")}</td><td>{pr}</td></tr>'
            f'<tr id=d{i} class=detrow><td colspan={ncols}>{_detail_html(t)}</td></tr>'
        )

    panel = ""
    if needs:
        items = "".join(
            '<div class=need>'
            f'<span class=needmain onclick="kpick(\'{html.escape(str(t["ticket_id"]))}\')">'
            f'<span class=mono>{html.escape(str(t["ticket_id"]))}</span> {_badge(t["outcome"])} '
            f'<span class=muted>{html.escape(t.get("note") or "")}</span></span>'
            + (f'<a href="{html.escape(t["pr_url"])}" target=_blank>PR ↗</a>' if t.get("pr_url") else "")
            + '<form method=post action=/api/dismiss class=dismiss>'
            f'<input type=hidden name=ticket value="{html.escape(str(t["ticket_id"]))}">'
            '<button class=x title="Dismiss — clear this from Needs you">✕</button></form>'
            '</div>'
            for t in needs)
        panel = f'<div class=panel><div class=ph>Needs your attention ({len(needs)})</div>{items}</div>'

    cards_html = "".join(
        (f'<div class="card clk" onclick="kfilter(\'{kind}\')">' if kind else '<div class=card>')
        + f'<div class=k>{html.escape(str(v))}</div><div class=l>{html.escape(l)}</div></div>'
        for l, v, kind in cards)
    rows_html = "\n".join(rows) or f'<tr><td colspan={ncols} class=muted>No tasks yet — run the General.</td></tr>'
    return (_TEMPLATE.replace("{{CARDS}}", cards_html).replace("{{PANEL}}", panel)
            .replace("{{HEAD}}", "".join(head)).replace("{{ROWS}}", rows_html)
            .replace("{{GEN}}", datetime.now().strftime("%Y-%m-%d %H:%M")))


_TEMPLATE = """<!doctype html><html><head><meta charset=utf-8>
<title>The General — cockpit</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{font:14px/1.55 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:0;background:#0d0f14;color:#e8eaed}
header{padding:22px 30px;border-bottom:1px solid #1e222b;background:linear-gradient(180deg,#141821,#0d0f14)}
h1{margin:0;font-size:19px;letter-spacing:.2px}.sub{color:#8a909c;font-size:12px;margin-top:5px}
.cards{display:flex;gap:14px;padding:20px 30px 6px;flex-wrap:wrap}
.card{background:#151a23;border:1px solid #232936;border-radius:12px;padding:14px 20px;min-width:120px}
.card .k{font-size:24px;font-weight:650}.card .l{color:#8a909c;font-size:12px;margin-top:2px}
.card.clk{cursor:pointer;transition:border-color .15s}.card.clk:hover{border-color:#3b6cff}
.panel{margin:14px 30px;background:#1a160f;border:1px solid #3a2f12;border-radius:12px;padding:14px 18px}
.ph{color:#fbbf24;font-weight:650;font-size:13px;margin-bottom:8px}
.need{display:flex;align-items:center;gap:8px;padding:5px 0;border-top:1px solid #2a2410;font-size:13px}.need:first-of-type{border-top:0}
.needmain{flex:1;cursor:pointer}.needmain:hover{text-decoration:underline}
.dismiss{margin:0}.x{background:none;border:1px solid #3a2f12;color:#8a909c;border-radius:6px;padding:0 8px;cursor:pointer;font-size:12px;line-height:1.7}.x:hover{background:#2a2410;color:#f97a7a}
.wrap{padding:8px 30px 50px}
input{background:#151a23;border:1px solid #232936;color:#e8eaed;border-radius:9px;padding:9px 13px;width:280px;margin:6px 0 14px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:10px 11px;border-bottom:1px solid #191d26}
th{color:#8a909c;font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
.row{cursor:pointer}.row:hover{background:#141821}.tw{color:#5b626f;width:14px}
.num{text-align:right;font-variant-numeric:tabular-nums}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px}
.b{padding:2px 9px;border-radius:99px;font-size:11px;font-weight:650;white-space:nowrap}
.ok{background:#10371f;color:#56d98a}.warn{background:#3a2f10;color:#fbbf24}
.bad{background:#3a1414;color:#f97a7a}.muted{background:#191d26;color:#8a909c}
.dry{color:#8a909c;font-size:11px}a{color:#6aa9ff;text-decoration:none}
.detrow{display:none}.detrow>td{background:#0a0c10;padding:0}
.det{padding:14px 22px}.pass{border-left:2px solid #2a3140;padding:6px 0 12px 14px;margin:4px 0}
.passhead{font-weight:650;font-size:13px;margin-bottom:5px}.eff{color:#8a909c;font-weight:400;font-size:12px;margin-left:6px}
.sub{font-size:13px;color:#c4c9d2;margin:3px 0}.sub b{color:#e8eaed}
.sub ul{margin:4px 0 4px 18px;padding:0}.sev{color:#fbbf24;font-weight:600;text-transform:uppercase;font-size:11px}
</style></head><body>
<header><h1>★ The General — cockpit</h1><div class=sub>generated {{GEN}} · re-run <code>./general dashboard</code> (or use <code>./general serve</code>) · click a row for the full transcript</div></header>
<div class=cards>{{CARDS}}</div>
{{PANEL}}
<div class=wrap>
<input id=f placeholder="filter by ticket / app / branch…" oninput="flt()">
<table id=t><thead><tr>{{HEAD}}</tr></thead><tbody>{{ROWS}}</tbody></table>
</div>
<script>
function tog(i){var e=document.getElementById('d'+i);e.style.display=e.style.display==='table-row'?'none':'table-row';}
function flt(){var q=document.getElementById('f').value.toLowerCase();
document.querySelectorAll('#t tbody tr.row').forEach(function(r){
var m=r.innerText.toLowerCase().includes(q);r.style.display=m?'':'none';
var d=r.nextElementSibling;if(d)d.style.display='none';});}
function kfilter(kind){document.getElementById('f').value='';
document.querySelectorAll('#t tbody tr.row').forEach(function(r){
var st=(r.children[1]?r.children[1].innerText:'').toLowerCase();
var show=kind==='all'||(kind==='merged'&&st.indexOf('merged')>=0)||(kind==='needs'&&(st.indexOf('error')>=0||st.indexOf('escal')>=0||st.indexOf('pr')>=0));
r.style.display=show?'':'none';var d=r.nextElementSibling;if(d)d.style.display='none';});}
function kpick(id){var f=document.getElementById('f');f.value=id;flt();f.scrollIntoView({behavior:'smooth'});}
</script></body></html>"""


def standup(cfg) -> str:
    """A daily-meeting report: shipped today, needs-you, awaiting-decision."""
    from . import decisions
    tasks = load_tasks(cfg.audit_path)
    today = datetime.now().strftime("%Y-%m-%d")
    shipped = [t for t in tasks if t["outcome"] == "merged→dev"
               and t["started"] and t["started"].strftime("%Y-%m-%d") == today]
    needs = [t for t in tasks if t["outcome"] in _NEEDS_YOU]
    pending = decisions.load(cfg)

    lines = [f"🫡 Daily standup — {today}", ""]
    lines.append(f"✅ Shipped to DEV today ({len(shipped)}): "
                 + (", ".join(t["ticket_id"] for t in shipped) or "—"))
    lines.append(f"🟡 Needs you ({len(needs)}): "
                 + (", ".join(f'{t["ticket_id"]} [{t["outcome"]}]' for t in needs) or "—"))
    if pending:
        lines.append("❓ Awaiting your decision:")
        lines += [f'   • {p["id"]}: {p.get("question", "")}' for p in pending]
    else:
        lines.append("❓ Awaiting your decision: —")
    return "\n".join(lines)


def render_status(tasks: list[dict[str, Any]], limit: int = 15, show_cost: bool = True) -> str:
    if not tasks:
        return "No tasks yet — run the General."
    head = f"{'TICKET':<26}{'APP':<12}{'OUTCOME':<14}{'PASSES':<7}{'DUR':<8}" + ("COST" if show_cost else "")
    lines = [head]
    for t in tasks[:limit]:
        row = (f"{str(t['ticket_id'])[:25]:<26}{str(t.get('app') or '')[:11]:<12}"
               f"{str(t.get('outcome') or 'running'):<14}{t['passes']:<7}{_human_dur(t['duration']):<8}")
        if show_cost:
            row += f"${t['cost']:.2f}"
        lines.append(row)
    return "\n".join(lines)
