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
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_TERMINAL = {"merged", "pr_opened", "escalated", "dryrun_land", "ship_dryrun",
             "ticket_exception", "no_changes", "needs_human", "pm_triage"}
_OUTCOME = {
    "merged": "merged→dev", "pr_opened": "PR / needs you", "escalated": "escalated",
    "dryrun_land": "dry-run", "ship_dryrun": "dry-run",
    "ticket_exception": "errored", "no_changes": "escalated",  # EU-116: no_changes -> ESCALATED
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


# --------------------------------------------------------------------------- #
# Audit read cache. render_board (server.py's SSE gen, ≥ every 2s, PER open tab) fans out to the audit
# THREE times per frame — load_tasks, warroom._run_in_flight and warroom._scan (KPIs) each re-read +
# JSON-split the WHOLE merged history (local audit.jsonl + every synced shared/<host>.jsonl peer). With a
# 50k-line audit and duplicate/tunnelled tabs that pins a core on the stream thread. Cache the merged
# split and the assembled task list, keyed on the source files' (size, mtime_ns) so it auto-invalidates
# the instant any audit (local or peer) is appended to / rewritten; a short TTL is a coarse-mtime
# backstop. A burst of SSE frames and K tabs then share ONE read/parse per interval.
_AUDIT_TTL = 1.5
_audit_cache: dict[str, tuple[tuple, float, list[str]]] = {}              # path -> (sig, ts, lines)
_tasks_cache: dict[str, tuple[tuple, float, list[dict[str, Any]]]] = {}   # path -> (sig, ts, runs)

# Instrumentation (see tests/cockpit_cache_test.py): total audit_lines() calls vs. real disk reads. The
# acceptance is ≤1 read per cache interval no matter how many frames/tabs call it.
audit_lines_calls = 0
audit_lines_reads = 0


def _audit_paths(audit_path: str | Path) -> list[Path]:
    """The files merged into the unified audit view: this machine's audit.jsonl plus every synced
    ``shared/<host>.jsonl`` peer. Synced peers live in the state clone's shared/ (orphan unit-state
    branch); the bare shared/ form is accepted too so tests / any local-only layout work without it."""
    p = Path(audit_path)
    paths: list[Path] = [p]
    # The state clone lives at the REPO ROOT: sync._repo_root strips a ``state/`` subdir, so when
    # audit.jsonl is under ``state/`` the synced shared/ is ``<root>/.unit-state/shared`` — NOT
    # ``<state>/.unit-state/shared``. Probe the state-stripped root too, or peer/server audits never
    # merge into the view (the cockpit silently shows nothing from other hosts).
    root = p.parent
    if root.name == "state":
        root = root.parent
    seen: set[Path] = set()
    for shared in (root / ".unit-state" / "shared", p.parent / ".unit-state" / "shared", p.parent / "shared"):
        if shared in seen or not shared.is_dir():
            continue
        seen.add(shared)
        paths += sorted(shared.glob("*.jsonl"))
    return paths


def _audit_sig(paths: list[Path]) -> tuple:
    """A cheap fingerprint of the source files — (path, size, mtime_ns) each. Changes the instant any
    file is appended to or rewritten, so the cache can never serve stale audit data. Globbing + stat() is
    far cheaper than re-reading + JSON-parsing tens of thousands of lines on every frame."""
    sig: list[tuple] = []
    for fp in paths:
        try:
            st = fp.stat()
            sig.append((str(fp), st.st_size, st.st_mtime_ns))
        except OSError:
            continue
    return tuple(sig)


def audit_lines(audit_path: str | Path) -> list[str]:
    """Every audit line for the unified view: this machine's live ``audit.jsonl`` PLUS each synced
    ``shared/<host>.jsonl`` published by the other machines (see orchestrator/sync.py). Exact-duplicate
    lines are collapsed — a host's own events live in both its audit.jsonl and its published copy, so
    they are counted once. Order is local-first then shared; callers that care sort by ts.

    TTL/mtime-cached so a burst of SSE board frames (and K open tabs) share a single read+merge instead
    of re-parsing the whole history several times a second."""
    global audit_lines_calls, audit_lines_reads
    audit_lines_calls += 1
    key = str(audit_path)
    paths = _audit_paths(audit_path)
    sig = _audit_sig(paths)
    now = time.time()
    hit = _audit_cache.get(key)
    if hit is not None and hit[0] == sig and (now - hit[1]) < _AUDIT_TTL:
        return hit[2]
    audit_lines_reads += 1
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
    _audit_cache[key] = (sig, now, out)
    return out


def load_tasks(audit_path: str | Path) -> list[dict[str, Any]]:
    # Same (size, mtime_ns)-keyed TTL cache as audit_lines, so the per-frame JSON parse + run assembly is
    # done once per interval and shared across SSE frames / tabs. Read-only for every caller.
    key = str(audit_path)
    sig = _audit_sig(_audit_paths(audit_path))
    now = time.time()
    hit = _tasks_cache.get(key)
    if hit is not None and hit[0] == sig and (now - hit[1]) < _AUDIT_TTL:
        return hit[2]
    runs = _load_tasks_uncached(audit_path)
    _tasks_cache[key] = (sig, now, runs)
    return runs


def _load_tasks_uncached(audit_path: str | Path) -> list[dict[str, Any]]:
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
    from . import locking

    # 2026-07-05 audit §7.4: two concurrent /api/dismiss handlers (Flask runs threaded) did a
    # bare load→mutate→write_text, so the second write dropped the first ticket's dismissal and
    # its needs-you card silently reappeared. locked_rmw makes the read-modify-write atomic.
    def _mut(d):
        d = d if isinstance(d, dict) else {}
        d[str(ticket_id)] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return d
    try:
        locking.locked_rmw(_dismissed_file(audit_path), _mut, default={}, corrupt_to_default=True)
    except OSError:
        pass


def _is_dismissed(t: dict[str, Any], dismissed: dict | None) -> bool:
    """Hide a needs-you run ONLY if its ticket was dismissed AND we can confirm THIS run started at/before
    that dismissal — a newer run for the same ticket shows again. Fail-safe by design: any uncertainty
    (missing/unparseable timestamps) SHOWS the item, and the comparison never raises. The old version
    compared a tz-aware dismissal time against a naive run time, which raised TypeError — and because
    needs.summary() swallows exceptions, a SINGLE dismissal then wiped the entire panel (every run, even
    the one you still needed to answer)."""
    if not dismissed:
        return False
    da = dismissed.get(str(t.get("ticket_id")))
    if not da:
        return False
    dt, st = _started_dt({"started": da}), _started_dt(t)   # both naive — robust to datetime objs / formats
    if dt is None or st is None:
        return False                       # can't confirm it's old → never hide on uncertainty
    return st <= dt


def _started_dt(t: dict[str, Any]) -> Optional[datetime]:
    """A run's 'started' as a naive datetime — tolerates a datetime object OR an ISO string with a 'T'
    or space separator and a +HH:MM / +HHMM offset (load_tasks emits the space+colon form). None if blank.
    Used for BOTH the dismissal compare and latest-run dedupe, so neither mis-orders on format drift."""
    s = t.get("started")
    if isinstance(s, datetime):
        return s.replace(tzinfo=None)
    if not isinstance(s, str) or not s:
        return None
    dt = _parse_ts(s)
    if dt is None:
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    return dt.replace(tzinfo=None)


def _started_key(t: dict[str, Any]) -> datetime:
    return _started_dt(t) or datetime.min


def latest_needs_you(tasks: list[dict[str, Any]], dismissed: dict | None = None) -> list[dict[str, Any]]:
    """The Needs-you list: ONE row per ticket — its most recent run — kept only if that latest run still
    needs you and isn't dismissed. Stops the panel showing every historical errored run of a ticket (the
    AUTO-14×5 duplicates) or a stale failure for a ticket that has since succeeded on a later run."""
    latest: dict[str, dict[str, Any]] = {}
    for t in tasks:
        tid = str(t.get("ticket_id") or "")
        if not tid:
            continue
        if tid not in latest or _started_key(t) >= _started_key(latest[tid]):
            latest[tid] = t
    return [t for t in latest.values()
            if t.get("outcome") in _NEEDS_YOU and not _is_dismissed(t, dismissed)]


def latest_parked(tasks: list[dict[str, Any]], blocked_set: set[str]) -> list[dict[str, Any]]:
    """The Parked list: ONE row per blocked ticket — its most recent run.

    Mirrors latest_needs_you's deduplication so the parked view shows ONE row per ticket
    instead of the full run history. Tickets in blocked_set with no audit history (never
    ran, or log rotated — the 'ghost' scenario) get stub rows so they are still visible
    and Unblockable. Returned newest-first by 'started'."""
    _stub: dict[str, Any] = {"outcome": None, "started": None, "ended": None,
                              "passes": 0, "turns": 0, "cost": 0.0, "verdict": None,
                              "note": "", "branch": "", "app": "", "passes_list": [],
                              "duration": None, "dry_run": None, "detail": {}, "pr_url": None}
    latest: dict[str, dict[str, Any]] = {}
    for t in tasks:
        tid = str(t.get("ticket_id") or "")
        if not tid or tid not in blocked_set:
            continue
        if tid not in latest or _started_key(t) >= _started_key(latest[tid]):
            latest[tid] = t
    # Ghost tickets — in blocked but no audit history; include as stub rows so Unblock works.
    for tid in blocked_set:
        if tid not in latest:
            latest[tid] = {"ticket_id": tid, **_stub}
    return sorted(latest.values(),
                  key=lambda t: _started_key(t) or datetime.min, reverse=True)


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
            f'<div class="sub pre"><b>Builder:</b> {html.escape(d.get("build_summary") or "—")}</div>'
            f'<div class=sub><b>Tools:</b> <span class=mono>{tools}</span></div>'
            f'<div class="sub pre"><b>Reviewer {vbadge}:</b> {html.escape(d.get("review_summary") or "—")}</div>'
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
    if len(s) <= n:
        return s
    cut = s[: n - 1]
    # back up to the previous word boundary unless we already cut exactly at one
    if not s[n - 1].isspace():
        sp = cut.rfind(" ")
        if sp > 0:
            cut = cut[:sp]
    return cut.rstrip() + "…"


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


def bullets(text: Any, limit: int = 3, width: int = 200) -> str:
    """Render an officer summary as ≤`limit` tight "• …" lines for a hand-off comment (EU-79).

    The officers now LEAD their summary with bullets (what was done / the gap / what changed), so we
    reuse those lines directly; when the source carries no explicit bullets we fall back to brief()
    split into its first sentences. Each line is whitespace-collapsed and word-boundary trimmed to
    `width`, so the result is always a short, scannable bullet list — never the wall of prose Roman
    flagged on the QA hand-off comment. Always returns at least one non-empty bullet."""
    import re
    raw = str(text or "").strip()
    if not raw:
        return "• (no summary)"
    items = [m.group(1).strip() for ln in raw.splitlines()
             if (m := re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.*\S)\s*$", ln))]
    if not items:
        # No explicit bullets — split the brief into its first sentences instead.
        items = [s.strip() for s in re.split(r"(?<=[.!?])\s+", brief(raw, n=width * limit)) if s.strip()]
    lines = [_short(it, width) for it in items[:limit] if it.strip()]
    return "\n".join(f"• {it}" for it in lines) or f"• {_short(raw, width)}"


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
                    'open it with the CTO to investigate.</div>')
    return "".join(rows)


def needs_chat_summary(t: dict[str, Any]) -> str:
    """A compact plain-text brief of the problem, pre-loaded into the CTO chat when the Commander
    clicks 'Discuss with the CTO' — so he can send it as-is (or tweak) instead of retyping."""
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


def render_html(tasks: list[dict[str, Any]], show_cost: bool = True, dismissed: dict | None = None,
                active_filter: str | None = None, blocked: list[str] | None = None,
                needs_count: int | None = None) -> str:
    # Cards summarize the FULL run set, regardless of any active scope filter.
    total = len(tasks)
    merged = sum(1 for t in tasks if t["outcome"] == "merged→dev")
    needs = latest_needs_you(tasks, dismissed)   # one row per ticket (latest run), not every old run
    # needs_count may be supplied by the caller (server.py) as the full cross-stream total from
    # needs.summary() — decisions + approvals + proposals + tasks.  Fall back to the task-only
    # len(needs) when the caller hasn't provided it (e.g. the static `general dashboard` command).
    _needs_display = needs_count if needs_count is not None else len(needs)
    cards = [("Tasks", total, "all"), ("Merged → dev", merged, "merged"), ("Needs you", _needs_display, "needs")]
    if show_cost:
        cards.append(("Est. cost", f"${sum(t['cost'] for t in tasks):.2f}", None))

    # A KPI card deep-links here with ?filter=<scope>; scope the visible rows so the destination
    # honors the click ("show me these"). 'parked' matches the auto-skipped blocked_tickets set.
    flt = (active_filter or "").strip().lower()
    blocked_set = {str(b) for b in (blocked or [])}
    _FILTER_LABEL = {"merged": "Merged → dev", "needs": "Needs you", "parked": "Parked"}
    if flt == "merged":
        tasks = [t for t in tasks if t["outcome"] == "merged→dev"]
    elif flt == "needs":
        tasks = [t for t in tasks if t["outcome"] in _NEEDS_YOU and not _is_dismissed(t, dismissed)]
    elif flt == "parked":
        # One row per parked ticket (latest run) — same dedup as latest_needs_you.
        # Ghost tickets (in blocked but no audit history) get stub rows so Unblock works.
        tasks = latest_parked(tasks, blocked_set)
    else:
        flt = ""

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
    if flt == "parked":
        # Parked panel: one row per blocked ticket with Unblock action.
        # Replaces the needs panel when viewing the parked filter so the Commander
        # has a clear action on every row — no parked ticket is un-clearable (EU-78).
        if tasks:
            items = "".join(
                '<div class=need>'
                f'<span class=needmain onclick="kpick(\'{html.escape(str(t["ticket_id"]))}\')">'
                f'<span class=mono>{html.escape(str(t["ticket_id"]))}</span> {_badge(t["outcome"])} '
                f'<span class=muted>{html.escape(_short(str(t.get("note") or ""), 80))}</span></span>'
                + (f'<a href="{html.escape(t["pr_url"])}" target=_blank>PR &#8599;</a>'
                   if t.get("pr_url") else "")
                + ('<a class=x style="text-decoration:none" href="/needs" '
                   'title="Go to Needs you to answer this ticket">Answer</a>'
                   if t.get("outcome") in _NEEDS_YOU else "")
                + '<form method=post action=/api/unblock class=dismiss>'
                f'<input type=hidden name=ticket value="{html.escape(str(t["ticket_id"]))}">'
                '<button class=x title="Unblock — remove from parked, autopilot will retry">Unblock</button></form>'
                '</div>'
                for t in tasks)
            panel = (f'<div class=panel><div class=ph>Parked ({len(tasks)}) '
                     f'&#8212; auto-skipped by autopilot</div>{items}</div>')
        else:
            panel = ('<div class=panel><div class=ph>Parked</div>'
                     '<div class=need><span class=muted>&#10003; No parked tickets &#8212; all clear.</span>'
                     '</div></div>')
    elif needs:
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
    banner = ""
    if flt:
        banner = (f'<div class=fltbar>Showing <b>{html.escape(_FILTER_LABEL.get(flt, flt))}</b> only '
                  f'· <a href="/tasks">show all</a></div>')
    empty = "No tasks match this filter." if flt else "No tasks yet — run the CTO."
    rows_html = "\n".join(rows) or f'<tr><td colspan={ncols} class=muted>{empty}</td></tr>'
    return (_TEMPLATE.replace("{{CARDS}}", cards_html).replace("{{PANEL}}", panel)
            .replace("{{FILTER}}", banner)
            .replace("{{HEAD}}", "".join(head)).replace("{{ROWS}}", rows_html)
            .replace("{{GEN}}", datetime.now().strftime("%Y-%m-%d %H:%M")))


_TEMPLATE = """<!doctype html><html><head><meta charset=utf-8>
<title>CTO — cockpit</title>
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
.sub.pre{white-space:pre-wrap}
.sub ul{margin:4px 0 4px 18px;padding:0}.sev{color:#fbbf24;font-weight:600;text-transform:uppercase;font-size:11px}
.fltbar{margin:6px 30px 0;color:#c4c9d2;font-size:13px}
</style></head><body>
<header><h1>★ CTO — cockpit</h1><div class=sub>generated {{GEN}} · re-run <code>./general dashboard</code> (or use <code>./general serve</code>) · click a row for the full transcript</div></header>
<div class=cards>{{CARDS}}</div>
{{FILTER}}
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
    """The deterministic core of the morning daily: what shipped YESTERDAY, what needs you now, and
    what awaits a decision. The daily fires ~08:30, so 'yesterday' is the completed work the Commander
    wants to see; 'today so far' is added only once same-day merges exist. No cumulative/all-time
    history — that is deliberately out (the Commander does not want it in the daily)."""
    from . import decisions
    from datetime import timedelta
    tasks = load_tasks(cfg.audit_path)
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    def _day(t) -> str | None:                              # when it landed (merge time), else run start —
        d = t.get("ended") or t.get("started")              # .astimezone() normalizes an audit ts written on
        return d.astimezone().strftime("%Y-%m-%d") if d else None  # another host (EU-181) to the reader's local
        #                                                     day, so today/yday boundaries agree cross-host
    shipped_y = [t for t in tasks if t["outcome"] == "merged→dev" and _day(t) == yday]
    shipped_t = [t for t in tasks if t["outcome"] == "merged→dev" and _day(t) == today]
    needs = [t for t in tasks if t["outcome"] in _NEEDS_YOU]
    pending = decisions.load(cfg)

    lines = [f"🫡 Daily stand-up — {today}", ""]
    lines.append(f"✅ Shipped to DEV yesterday ({len(shipped_y)}): "
                 + (", ".join(t["ticket_id"] for t in shipped_y) or "—"))
    if shipped_t:
        lines.append(f"✅ …and today so far ({len(shipped_t)}): "
                     + ", ".join(t["ticket_id"] for t in shipped_t))
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
        return "No tasks yet — run the CTO."
    head = f"{'TICKET':<26}{'APP':<12}{'OUTCOME':<14}{'PASSES':<7}{'DUR':<8}" + ("COST" if show_cost else "")
    lines = [head]
    for t in tasks[:limit]:
        row = (f"{str(t['ticket_id'])[:25]:<26}{str(t.get('app') or '')[:11]:<12}"
               f"{str(t.get('outcome') or 'running'):<14}{t['passes']:<7}{_human_dur(t['duration']):<8}")
        if show_cost:
            row += f"${t['cost']:.2f}"
        lines.append(row)
    return "\n".join(lines)
