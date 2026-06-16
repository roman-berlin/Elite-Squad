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
             "ticket_exception", "no_changes", "needs_human"}
_OUTCOME = {
    "merged": "merged→dev", "pr_opened": "PR / needs you", "escalated": "escalated",
    "dryrun_land": "dry-run", "ship_dryrun": "dry-run",
    "ticket_exception": "errored", "no_changes": "errored",
    "needs_human": "awaiting decision",
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


def load_tasks(audit_path: str | Path) -> list[dict[str, Any]]:
    path = Path(audit_path)
    if not path.exists():
        return []
    tasks: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
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
        t = tasks.get(tid)
        if t is None:
            t = {"ticket_id": tid, "app": ev.get("app"), "branch": ev.get("branch"),
                 "started": None, "ended": None, "passes": 0, "turns": 0, "cost": 0.0,
                 "verdict": None, "outcome": None, "pr_url": None, "dry_run": None,
                 "note": "", "detail": {}}
            tasks[tid] = t
            order.append(tid)
        kind = ev.get("event")
        ts = _parse_ts(ev.get("ts", ""))
        if kind == "ticket_start":
            t["started"] = ts
            t["app"] = ev.get("app", t["app"])
            t["branch"] = ev.get("branch", t["branch"])
            t["dry_run"] = ev.get("dry_run")
        elif kind == "build":
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
    out = []
    for tid in order:
        t = tasks[tid]
        t["duration"] = ((t["ended"] - t["started"]).total_seconds()
                         if t["started"] and t["ended"] else None)
        t["passes_list"] = [dict(d, n=k) for k, d in sorted(t["detail"].items())]
        out.append(t)
    # sort by timestamp (tz-safe: avoids comparing aware vs naive datetimes)
    out.sort(key=lambda x: x["started"].timestamp() if x["started"] else 0.0, reverse=True)
    return out


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


def render_html(tasks: list[dict[str, Any]], show_cost: bool = True) -> str:
    total = len(tasks)
    merged = sum(1 for t in tasks if t["outcome"] == "merged→dev")
    needs = [t for t in tasks if t["outcome"] in _NEEDS_YOU]
    cards = [("Tasks", total), ("Merged → dev", merged), ("Needs you", len(needs))]
    if show_cost:
        cards.append(("Est. cost", f"${sum(t['cost'] for t in tasks):.2f}"))

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
            f'<div class=need><span class=mono>{html.escape(str(t["ticket_id"]))}</span> '
            f'{_badge(t["outcome"])} <span class=muted>{html.escape(t.get("note") or "")}</span>'
            + (f' <a href="{html.escape(t["pr_url"])}" target=_blank>PR ↗</a>' if t.get("pr_url") else "")
            + '</div>'
            for t in needs)
        panel = f'<div class=panel><div class=ph>Needs your attention ({len(needs)})</div>{items}</div>'

    cards_html = "".join(
        f'<div class=card><div class=k>{html.escape(str(v))}</div><div class=l>{html.escape(l)}</div></div>'
        for l, v in cards)
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
.panel{margin:14px 30px;background:#1a160f;border:1px solid #3a2f12;border-radius:12px;padding:14px 18px}
.ph{color:#fbbf24;font-weight:650;font-size:13px;margin-bottom:8px}
.need{padding:5px 0;border-top:1px solid #2a2410;font-size:13px}.need:first-of-type{border-top:0}
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
