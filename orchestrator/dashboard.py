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
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

_TERMINAL = {"merged", "pr_opened", "escalated", "dryrun_land", "ship_dryrun",
             "ticket_exception", "no_changes", "needs_human", "pm_triage", "scrum_split"}
_OUTCOME = {
    "merged": "merged→dev", "pr_opened": "PR / needs you", "escalated": "escalated",
    "dryrun_land": "dry-run", "ship_dryrun": "dry-run",
    "ticket_exception": "errored", "no_changes": "escalated",  # EU-116: no_changes -> ESCALATED
    "needs_human": "awaiting decision",
    "pm_triage": "re-queued",   # PM sent it back for one corrective pass — not a Needs-you item
    "scrum_split": "split",     # too big → decomposed into sub-tickets, parent closed (a terminal outcome,
                                # not a run still in flight — else the parent shows "running…" forever)
}
# EU-315: "blocked" is NOT a run outcome — no audit event ever reconstructs to it (that was the
# iteration-1 defect: a phantom outcome the pipeline never produced). A ticket is "blocked/parked"
# when it is a MEMBER of blocked_tickets.json (warroom._load_blocked / autopilot.load_blocked),
# orthogonal to whatever terminal outcome its last run recorded (usually escalated / PR). The board
# derives Blocked tone + freshness from that membership via the ``is_blocked`` flag below, never
# from ``outcome``.
_NEEDS_YOU = {"PR / needs you", "escalated", "errored", "awaiting decision"}

# EU-787: predicate shared by standup(), kpis() and the dashboard card counters to decide whether a
# TaskRow counts as "shipped" (outcome == "merged→dev" AND no superseded label).  The "superseded"
# label field may be absent on audit-derived TaskRows; when absent the check is skipped so existing
# merge-only logic is preserved and the predicate stays backward-compatible.
def _is_merged_and_not_superseded(t: dict[str, object]) -> bool:
    if t.get("outcome") != "merged→dev":
        return False
    labels = t.get("labels")
    if labels is not None and "superseded" in (labels if isinstance(labels, list) else (labels or {}).values()):
        return False
    return True


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
# EU-428 AC3: keyed by (path, local_only) — the local-only view (forensics WRITE path) and the
# peer-inclusive view (display) cache independently.
_audit_cache: dict[tuple[str, bool], tuple[tuple, float, list[str]]] = {}              # (path, local_only) -> (sig, ts, lines)
# One assembled run row (load_tasks): stable str keys, heterogeneous values (str / int / float /
# bool / None / nested dict), and ghost-stub rows carry a SUBSET of a real row's keys — so the
# honest shared type is a str-keyed object-valued mapping rather than a fixed TypedDict.
TaskRow = dict[str, object]
_tasks_cache: dict[tuple[str, bool], tuple[tuple, float, list[TaskRow]]] = {}   # (path, local_only) -> (sig, ts, runs)

# Instrumentation (see tests/cockpit_cache_test.py): total audit_lines() calls vs. real disk reads. The
# acceptance is ≤1 read per cache interval no matter how many frames/tabs call it.
audit_lines_calls = 0
audit_lines_reads = 0


def _audit_paths(audit_path: str | Path, local_only: bool = False) -> list[Path]:
    """The files merged into the unified audit view: this machine's audit.jsonl plus every synced
    ``shared/<host>.jsonl`` peer. Synced peers live in the state clone's shared/ (orphan unit-state
    branch); the bare shared/ form is accepted too so tests / any local-only layout work without it.

    ``local_only=True`` returns JUST this host's own audit.jsonl — the peer files are dropped. The
    WRITE side of the forensics subsystem (EU-428 AC3: post-mortem + crash-signature auto-filing)
    reads through ``forensics.scan`` with this flag, so a row a host merely *received* over sync can
    never drive a ticket filing / parking / write on that host — peer rows are display + liveness
    only. The DISPLAY side (board, needs, signals) keeps the peer-inclusive default so the cockpit
    still mirrors what the other machine built."""
    p = Path(audit_path)
    paths: list[Path] = [p]
    if local_only:
        return paths
    # The state clone lives at the REPO ROOT: sync._repo_root strips a ``state/`` subdir, so when
    # audit.jsonl is under ``state/`` the synced shared/ is ``<root>/.unit-state/shared`` — NOT
    # ``<state>/.unit-state/shared``. Probe the state-stripped root too, or peer/server audits never
    # merge into the view (the cockpit silently shows nothing from other hosts).
    root = p.parent
    if root.name == "state":
        root = root.parent
    # This host's OWN published copy must never be merged back in. sync.publish writes
    # shared/<host_id>.jsonl as a MIRROR of the local audit.jsonl already at paths[0], so globbing it
    # too counts every local event twice. Latent until 2026-07-22, when installing the EU-428 audit
    # publisher made the Mac start publishing again: the merge went to 12,392 local + 12,391
    # published rows with 12,315 overlapping keys, silently doubling every count the board, the
    # stand-up and the 'shipped in last 24h' digest derive from it. A peer's file is real data; our
    # own is a duplicate of a file we are already reading.
    try:
        from . import sync
        own = f"{sync.host_id()}.jsonl"
    except Exception:  # noqa: BLE001 - never let host resolution break the audit view
        own = ""
    seen: set[Path] = set()
    for shared in (root / ".unit-state" / "shared", p.parent / ".unit-state" / "shared", p.parent / "shared"):
        if shared in seen or not shared.is_dir():
            continue
        seen.add(shared)
        paths += [f for f in sorted(shared.glob("*.jsonl")) if f.name != own]
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


# EU-345: incremental per-file line cache. The audit is APPEND-ONLY (a jsonl event log), so on the
# common case — the drain just appended a few events to the local audit.jsonl — we seek to the last
# consumed byte offset and parse ONLY the new bytes, instead of re-reading + splitting the whole 7 MB
# file. Before this, the (size, mtime_ns) sig changed on every append, so the cache missed and the
# dashboard re-parsed the entire history every ~5s auto-refresh while Roman watched a live build
# (measured ~13s/request, "no warm-cache effect"). The offset is always kept at a newline boundary so
# a partial trailing line waits for its newline; a size DECREASE (truncation / rotation, EU-363) falls
# back to a full re-read. keyed by absolute path.
_file_line_cache: dict[str, tuple[int, list[str], bytes]] = {}   # path -> (offset, lines, anchor)
_ANCHOR_N = 64   # bytes ending at the committed offset, re-verified to prove append (not rewrite)


def _read_file_lines(fp: Path) -> list[str]:
    try:
        size = fp.stat().st_size
    except OSError:
        return []
    cached = _file_line_cache.get(str(fp))
    start, prior = 0, []
    if cached is not None and size >= cached[0]:
        # The incremental delta-read is only valid if the file was APPENDED to — a same-path REWRITE
        # (test write_text, or sync overwriting a peer shared/<host>.jsonl via scp) can grow the file
        # with entirely different bytes, which would splice garbage onto the cached lines. Re-verify
        # the bytes ending at the committed offset still match the stored anchor; if not, it's a
        # rewrite → fall through to a full re-read. (A size DECREASE already falls through below.)
        off, plines, anchor = cached
        if size == off and _anchor_ok(fp, off, anchor):
            return plines                 # unchanged since last read
        if size > off and _anchor_ok(fp, off, anchor):
            start, prior = off, plines    # genuine append — read only the delta
    try:
        with fp.open("rb") as fh:
            fh.seek(start)
            chunk = fh.read()
    except OSError:
        return prior
    # Only COMMIT (advance the offset + cache) lines up to the last newline, so a half-written
    # trailing line is re-read next call rather than cached mid-write. But still SURFACE that trailing
    # line in the return, matching splitlines()'s handling of a newline-less final line (backward-
    # compat: the live audit's final event is a complete line, sometimes without a trailing newline).
    nl = chunk.rfind(b"\n")
    if nl == -1:
        trailing = chunk
        committed_lines = []
        commit_len = 0
    else:
        trailing = chunk[nl + 1:]
        committed = chunk[:nl + 1]
        committed_lines = [ln.strip() for ln in committed.decode("utf-8", errors="replace").splitlines()
                           if ln.strip()]
        commit_len = len(committed)
    if commit_len:
        prior = prior + committed_lines
        new_off = start + commit_len
        _file_line_cache[str(fp)] = (new_off, prior, _anchor_at(fp, new_off))
    tail = trailing.decode("utf-8", errors="replace").strip()
    return (prior + [tail]) if tail else prior


def _anchor_at(fp: Path, offset: int) -> bytes:
    """The up-to-_ANCHOR_N bytes ending at ``offset`` — a cheap fingerprint of the committed prefix's
    tail, re-checked before an incremental delta-read to prove the file was appended-to, not rewritten."""
    n = min(_ANCHOR_N, offset)
    if n <= 0:
        return b""
    try:
        with fp.open("rb") as fh:
            fh.seek(offset - n)
            return fh.read(n)
    except OSError:
        return b""


def _anchor_ok(fp: Path, offset: int, anchor: bytes) -> bool:
    return _anchor_at(fp, offset) == anchor


def audit_lines(audit_path: str | Path, local_only: bool = False) -> list[str]:
    """Every audit line for the unified view: this machine's live ``audit.jsonl`` PLUS each synced
    ``shared/<host>.jsonl`` published by the other machines (see orchestrator/sync.py). Exact-duplicate
    lines are collapsed — a host's own events live in both its audit.jsonl and its published copy, so
    they are counted once. Order is local-first then shared; callers that care sort by ts.

    ``local_only=True`` (EU-428 AC3) collapses the view to this host's own audit only — used by the
    forensics WRITE path so peer rows can't drive filings on the receiver.

    TTL/mtime-cached so a burst of SSE board frames (and K open tabs) share a single read+merge instead
    of re-parsing the whole history several times a second. On a cache miss the per-file reader
    (``_read_file_lines``) reads only newly-appended bytes (EU-345), so an active drain no longer forces
    a full-history re-read every request."""
    global audit_lines_calls, audit_lines_reads
    audit_lines_calls += 1
    key = (str(audit_path), local_only)
    paths = _audit_paths(audit_path, local_only=local_only)
    sig = _audit_sig(paths)
    now = time.time()
    hit = _audit_cache.get(key)
    if hit is not None and hit[0] == sig and (now - hit[1]) < _AUDIT_TTL:
        return hit[2]
    audit_lines_reads += 1
    seen: set[str] = set()
    out: list[str] = []
    for fp in paths:
        for s in _read_file_lines(fp):
            if s not in seen:
                seen.add(s)
                out.append(s)
    _audit_cache[key] = (sig, now, out)
    return out


def load_tasks(audit_path: str | Path, local_only: bool = False) -> list[TaskRow]:
    # Same (size, mtime_ns)-keyed TTL cache as audit_lines, so the per-frame JSON parse + run assembly is
    # done once per interval and shared across SSE frames / tabs. Read-only for every caller.
    # local_only (EU-428 AC3): scope to this host's own audit so peer rows can't drive the forensics
    # WRITE path; display callers keep the peer-inclusive default.
    key = (str(audit_path), local_only)
    sig = _audit_sig(_audit_paths(audit_path, local_only=local_only))
    now = time.time()
    hit = _tasks_cache.get(key)
    if hit is not None and hit[0] == sig and (now - hit[1]) < _AUDIT_TTL:
        return hit[2]
    runs = _load_tasks_uncached(audit_path, local_only=local_only)
    _tasks_cache[key] = (sig, now, runs)
    return runs


def _load_tasks_uncached(audit_path: str | Path, local_only: bool = False) -> list[TaskRow]:
    lines = audit_lines(audit_path, local_only=local_only)
    if not lines:
        return []
    def _new(tid: str, ev: dict) -> TaskRow:
        return {"ticket_id": tid, "app": ev.get("app"), "branch": ev.get("branch"),
                "started": None, "ended": None, "passes": 0, "turns": 0, "cost": 0.0,
                "verdict": None, "outcome": None, "pr_url": None, "dry_run": None,
                "note": "", "detail": {},
                # EU-136/EU-315: latest structural phase ("build"/"gate") + the last gate's
                # pass/fail, so a build that follows a FAILED gate can be recognised as a retry
                # instead of the stage getting stuck showing a bare 'Gate' state.
                "phase": None, "gate_passed": None, "gate_retry": False}

    # Each `ticket_start` begins a SEPARATE run — so a re-run of the same ticket (e.g. a dry-run
    # then a live run) does NOT merge the earlier run's phases/verdict into the new one.
    runs: list[TaskRow] = []
    cur: dict[str, TaskRow] = {}     # ticket_id -> its currently-open run
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
            # EU-136: a build that arrives while the last recorded gate for this ticket was a
            # FAILURE is a retry attempt — sticky for the run, so the board keeps showing the
            # retry indicator even once this build itself moves on to a later phase.
            if t.get("gate_passed") is False:
                t["gate_retry"] = True
            t["phase"] = "build"
            it = ev.get("iteration", 0)
            t["passes"] = max(t["passes"], it)
            t["turns"] += ev.get("turns", 0) or 0
            t["cost"] += ev.get("cost_usd", 0) or 0
            d = t["detail"].setdefault(it, {})
            d["effort"] = ev.get("effort")
            d["tools"] = ev.get("tools", []) or []
            d["build_summary"] = ev.get("summary", "")
        elif kind == "gate":
            # EU-136: record the gate's phase + result only — consumers decide what to show,
            # and a subsequent "build" event (the normal retry-after-fail path) always wins
            # over a stale gate phase.
            t["phase"] = "gate"
            t["gate_passed"] = ev.get("passed")
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


def _is_dismissed(t: TaskRow, dismissed: dict | None) -> bool:
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


def _started_dt(t: dict[str, object]) -> Optional[datetime]:
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


def _started_key(t: dict[str, object]) -> datetime:
    return _started_dt(t) or datetime.min


def latest_needs_you(tasks: list[TaskRow], dismissed: dict | None = None) -> list[TaskRow]:
    """The Needs-you list: ONE row per ticket — its most recent run — kept only if that latest run still
    needs you and isn't dismissed. Stops the panel showing every historical errored run of a ticket (the
    AUTO-14×5 duplicates) or a stale failure for a ticket that has since succeeded on a later run."""
    latest: dict[str, TaskRow] = {}
    for t in tasks:
        tid = str(t.get("ticket_id") or "")
        if not tid:
            continue
        if tid not in latest or _started_key(t) >= _started_key(latest[tid]):
            latest[tid] = t
    return [t for t in latest.values()
            if t.get("outcome") in _NEEDS_YOU and not _is_dismissed(t, dismissed)]


def latest_parked(tasks: list[TaskRow], blocked_set: set[str]) -> list[TaskRow]:
    """The Parked list: ONE row per blocked ticket — its most recent run.

    Mirrors latest_needs_you's deduplication so the parked view shows ONE row per ticket
    instead of the full run history. Tickets in blocked_set with no audit history (never
    ran, or log rotated — the 'ghost' scenario) get stub rows so they are still visible
    and Unblockable. Returned newest-first by 'started'."""
    _stub: TaskRow = {"outcome": None, "started": None, "ended": None,
                              "passes": 0, "turns": 0, "cost": 0.0, "verdict": None,
                              "note": "", "branch": "", "app": "", "passes_list": [],
                              "duration": None, "dry_run": None, "detail": {}, "pr_url": None}
    latest: dict[str, TaskRow] = {}
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


def _detail_html(t: TaskRow) -> str:
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


def _finding_str(x: object) -> str:
    """A reviewer finding may be a plain string or a {severity, area, detail} dict — render either."""
    if isinstance(x, dict):
        bits = [str(x.get("severity") or "").strip(), str(x.get("area") or "").strip()]
        head = " ".join(b for b in bits if b)
        detail = str(x.get("detail") or x.get("message") or "").strip()
        return (f"{head}: {detail}" if head and detail else head or detail or str(x))
    return str(x)


def _short(s: object, n: int) -> str:
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


def brief(text: object, n: int = 360) -> str:
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


def bullets(text: object, limit: int = 3, width: int = 200) -> str:
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


def needs_detail_html(t: TaskRow) -> str:
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


def needs_chat_summary(t: TaskRow) -> str:
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


_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def _jira_base_for(cfg, app_name: str) -> str:
    """Resolve an app's Jira browse base URL — from `backlog.base_url` in config, else the cockpit
    connection store (`connections.for_app`). '' when the app isn't Jira-backed or has no URL.
    Never raises: a link is a nice-to-have, never worth a 500 on the log page."""
    if not cfg or not app_name:
        return ""
    try:
        app = next((a for a in getattr(cfg, "apps", []) if a.name == app_name), None)
        if app is None or getattr(app, "backlog_backend", "") != "jira":
            return ""
        base = str((getattr(app, "backlog", None) or {}).get("base_url") or "").rstrip("/")
        if not base:
            from . import connections
            conn = connections.for_app(app_name, cfg)
            base = str((conn or {}).get("base_url") or "").rstrip("/")
        return base
    except Exception:  # noqa: BLE001
        return ""


def _jira_link(base: str, ticket_id: object, *, stop_prop: bool = False) -> str:
    """A compact 'Jira ↗' anchor to {base}/browse/{KEY}, or '' when there's no base URL or the id
    isn't a Jira key (e.g. an ephemeral run). ``stop_prop`` guards it inside a row whose own click
    toggles the detail drawer — the link must open Jira without also expanding the row."""
    tid = str(ticket_id or "").strip()
    if not base or not _JIRA_KEY_RE.match(tid):
        return ""
    onclick = ' onclick="event.stopPropagation()"' if stop_prop else ""
    return (f' <a class=jira href="{html.escape(base)}/browse/{html.escape(tid)}" target=_blank '
            f'rel=noopener{onclick} title="Open {html.escape(tid)} in Jira">Jira &#8599;</a>')


_PERIOD_LABEL = {"today": "Today", "week": "This week", "month": "This month", "all": "Total"}


def filter_tasks_since(tasks: list, period: str) -> list:
    """2026-07-19 (Commander order): scope the task log to Today / This week / This month / Total.
    'week' = the last 7 days, 'month' = the last 30 — rolling windows, deterministic, no TZ
    gymnastics. Unknown/blank periods return the list untouched (Total)."""
    period = (period or "all").strip().lower()
    if period not in ("today", "week", "month"):
        return tasks
    from datetime import timedelta
    now = datetime.now()
    if period == "today":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        cutoff = now - timedelta(days=7)
    else:
        cutoff = now - timedelta(days=30)
    out = []
    for t in tasks:
        ts = t.get("ended") or t.get("started")
        try:
            if ts is not None and ts.replace(tzinfo=None) >= cutoff:
                out.append(t)
        except (TypeError, AttributeError):
            continue
    return out


def render_html(tasks: list[TaskRow], show_cost: bool = True, dismissed: dict | None = None,
                active_filter: str | None = None, blocked: list[str] | None = None,
                needs_count: int | None = None, cfg=None, app_name: str | None = None,
                period: str = "all") -> str:
    # 2026-07-19 (Commander order): the task log is PER-PROJECT — everything on the page (cards,
    # filters, table) is scoped to the chosen project. Rows with no app stamp (e.g. ghost-parked
    # stubs) are kept so their Unblock action never disappears behind a scope.
    if app_name:
        tasks = [t for t in tasks if not t.get("app") or str(t.get("app")) == app_name]
    # Cards summarize the FULL scoped run set, regardless of any active ?filter= narrowing.
    total = len(tasks)
    merged = sum(1 for t in tasks if _is_merged_and_not_superseded(t))
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
        tasks = [t for t in tasks if _is_merged_and_not_superseded(t)]
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

    # Resolve each app's Jira base URL once per render (rows share an app on the scoped log page).
    _jira_bases: dict[str, str] = {}

    def _jira_for(app: str) -> str:
        if app not in _jira_bases:
            _jira_bases[app] = _jira_base_for(cfg, app)
        return _jira_bases[app]

    rows = []
    for i, t in enumerate(tasks):
        pr = (f'<a href="{html.escape(t["pr_url"])}" target=_blank>PR ↗</a>' if t.get("pr_url") else "")
        started = t["started"].strftime("%b %d %H:%M") if t["started"] else "—"
        cost_cell = f'<td class=num>${t["cost"]:.2f}</td>' if show_cost else ""
        _jl = _jira_link(_jira_for(str(t.get("app") or app_name or "")), t["ticket_id"], stop_prop=True)
        rows.append(
            f'<tr class=row onclick="tog({i})">'
            f'<td class=tw>▸</td>'
            f'<td>{_badge(t["outcome"])}{" <span class=dry>dry</span>" if t.get("dry_run") else ""}</td>'
            f'<td class=mono>{html.escape(str(t["ticket_id"]))}{_jl}</td>'
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
                + _jira_link(_jira_for(str(t.get("app") or app_name or "")), t["ticket_id"])
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
            + _jira_link(_jira_for(str(t.get("app") or app_name or "")), t["ticket_id"])
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
    # 2026-07-19: the Today / This week / This month / Total segmented control — scopes the cards
    # AND the rows (tasks arrive pre-filtered from tasks_page; the chips just re-request).
    _p = (period or "all").strip().lower()
    if _p not in _PERIOD_LABEL:
        _p = "all"
    _appq = f"&app={html.escape(app_name)}" if app_name else ""
    chips = "".join(
        f'<a class="pseg{" on" if key == _p else ""}" href="/tasks?since={key}{_appq}">{label}</a>'
        for key, label in _PERIOD_LABEL.items())
    cards_html = f'<div class=psegs>{chips}</div>' + cards_html
    banner = ""
    _appq = f"&app={html.escape(app_name)}" if app_name else ""
    if flt:
        banner = (f'<div class=fltbar>Showing <b>{html.escape(_FILTER_LABEL.get(flt, flt))}</b> only '
                  f'· <a href="/tasks?filter=all{_appq}">show all runs</a></div>')
    empty = "No tasks match this filter." if flt else "No tasks yet — run the CTO."
    rows_html = "\n".join(rows) or f'<tr><td colspan={ncols} class=muted>{empty}</td></tr>'

    # (The EU-314 per-project pipeline board was removed 2026-07-19 with the task-log redesign —
    # in-flight state lives on the cockpit board; this page is the per-project run LOG.)
    _title = f"★ Task log — {html.escape(app_name)}" if app_name else "★ CTO — cockpit"
    # 2026-07-19 theme pass: prepend the shared design tokens (dark + light + boot script) so this
    # page follows the War Room's theme toggle. Lazy import — cockpit_views imports this module.
    try:
        from .cockpit_views import _token_css
        _tokens = _token_css()
    except Exception:  # noqa: BLE001 - the static `general dashboard` render must never break
        _tokens = ""
    return (_TEMPLATE.replace("<style>", _tokens + "<style>", 1)
            .replace("★ CTO — cockpit", _title, 1)
            .replace("{{CARDS}}", cards_html).replace("{{BOARD}}", "")
            .replace("{{PANEL}}", panel)
            .replace("{{FILTER}}", banner)
            .replace("{{HEAD}}", "".join(head)).replace("{{ROWS}}", rows_html)
            .replace("{{GEN}}", datetime.now().strftime("%Y-%m-%d %H:%M")))


_TEMPLATE = """<!doctype html><html><head><meta charset=utf-8>
<title>CTO — cockpit</title>
<style>
*{box-sizing:border-box}
body{font:14px/1.55 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:0;background:var(--bg);color:var(--ink)}
header{padding:22px 30px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,var(--panel),var(--bg))}
h1{margin:0;font-size:19px;letter-spacing:.2px}.sub{color:var(--dim);font-size:12px;margin-top:5px}
.cards{display:flex;gap:14px;padding:12px 30px 6px;flex-wrap:wrap;align-items:center}
.psegs{display:inline-flex;background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:3px;gap:2px;flex-basis:100%;width:max-content;margin-bottom:4px}
.pseg{padding:7px 16px;border-radius:8px;font-size:13px;font-weight:600;color:var(--dim);text-decoration:none}
.pseg:hover{color:var(--ink)}
.pseg.on{background:var(--accent);color:#fff}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 20px;min-width:120px}
.card .k{font-size:24px;font-weight:650}.card .l{color:var(--dim);font-size:12px;margin-top:2px}
.card.clk{cursor:pointer;transition:border-color .15s}.card.clk:hover{border-color:var(--accent)}
.panel{margin:14px 30px;background:var(--warnbg);border:1px solid var(--warnline);border-radius:12px;padding:14px 18px}
.ph{color:var(--warn);font-weight:650;font-size:13px;margin-bottom:8px}
.need{display:flex;align-items:center;gap:8px;padding:5px 0;border-top:1px solid var(--warnline);font-size:13px}.need:first-of-type{border-top:0}
.needmain{flex:1;cursor:pointer}.needmain:hover{text-decoration:underline}
.dismiss{margin:0}.x{background:none;border:1px solid var(--warnline);color:var(--dim);border-radius:6px;padding:0 8px;cursor:pointer;font-size:12px;line-height:1.7}.x:hover{background:var(--badbg);color:var(--bad)}
.wrap{padding:8px 30px 50px}
input{background:var(--panel);border:1px solid var(--line);color:var(--ink);border-radius:9px;padding:9px 13px;width:280px;margin:6px 0 14px}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:13.5px;background:var(--panel);
border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:13px 14px;border-bottom:1px solid var(--line)}
tbody tr:last-child td{border-bottom:0}
th{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.06em;background:var(--panel2)}
.row{cursor:pointer;transition:background .12s}.row:hover{background:var(--panel2)}.tw{color:var(--faint);width:14px}
.num{text-align:right;font-variant-numeric:tabular-nums}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px}
.b{display:inline-block;padding:3px 11px;border-radius:99px;font-size:11px;font-weight:700;white-space:nowrap;letter-spacing:.02em}
.ok{background:var(--okbg);color:var(--ok)}.warn{background:var(--warnbg);color:var(--warn)}
.bad{background:var(--badbg);color:var(--bad)}.muted{background:var(--line);color:var(--dim)}
.dry{color:var(--dim);font-size:11px}a{color:var(--info);text-decoration:none}
a.jira{display:inline-flex;align-items:center;gap:3px;margin-left:8px;padding:1px 7px;border:1px solid var(--line2);border-radius:6px;font-size:11px;font-weight:600;color:var(--dim);vertical-align:middle}
a.jira:hover{border-color:var(--accent);color:var(--info);background:var(--accentbg)}
.detrow{display:none}.detrow>td{background:var(--console);padding:0}
.det{padding:14px 22px}.pass{border-left:2px solid var(--line2);padding:6px 0 12px 14px;margin:4px 0}
.passhead{font-weight:650;font-size:13px;margin-bottom:5px}.eff{color:var(--dim);font-weight:400;font-size:12px;margin-left:6px}
.sub{font-size:13px;color:var(--ink);margin:3px 0}.sub b{color:var(--ink)}
.sub.pre{white-space:pre-wrap}
.sub ul{margin:4px 0 4px 18px;padding:0}.sev{color:var(--warn);font-weight:600;text-transform:uppercase;font-size:11px}
.fltbar{margin:6px 30px 0;color:var(--ink);font-size:13px}
</style></head><body>
<header><h1>★ CTO — cockpit</h1><div class=sub>generated {{GEN}} · re-run <code>./general dashboard</code> (or use <code>./general serve</code>) · click a row for the full transcript</div></header>
<div class=cards>{{CARDS}}</div>
{{BOARD}}
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


# EU-245: the daily's "Needs you" line used to be a raw scan of load_tasks() — EVERY historical run
# of a ticket, forever, with no dedup and no "is this actually resolved now" check. A ticket rerun 5x
# over weeks showed up 5x (AUTO-14 x5 in the 2026-07-11 case), and a ticket resolved weeks ago never
# aged out. needs.summary() (orchestrator/needs.py) already solves exactly this for the cockpit's
# Needs-you panel — one row per ticket (latest run only), resolved/dismissed tickets dropped,
# blocked_tickets.json + pending_decisions.json merged in — so the digest now sources from THAT single
# live inbox instead of re-deriving its own (divergent) view of "needs you" from the raw audit.
#
# Categories kept: 'errored' (errored/escalated/awaiting-decision — latest run) and 'pr' (PR opened,
# needs review) and 'parked' (blocked_tickets.json). 'decision' is deliberately EXCLUDED here — the
# Commander's open questions already get their own "Awaiting your decision" section below, so folding
# them in here too would double them up. 'approval'/'proposal' are officer-level asks, not a single
# ticket, so they don't belong in a per-ticket digest line either.
_NEEDS_YOU_CATEGORIES = {"errored", "parked", "pr"}
_NEEDS_YOU_MAX = 10   # bound the digest; anything past this collapses into an "…and N more" tail

# EU-336: the shipped headline is a ROLLING window, not a calendar day. A 24/7 unattended unit does
# most of its work overnight, which lands after midnight — under the old yesterday/today split the
# freshest merges (the EU-294 collapse landed 01:00–04:00 on 2026-07-15) fell into a conditional
# "…and today so far" afterthought while the headline reported yesterday. The daily fires ~08:30, so
# a 24h window is also gap-free and dupe-free brief-to-brief.
_SHIPPED_WINDOW_HOURS = 24
_DECISION_MAX = 120   # one capped line per decision in the brief; the full text lives in the cockpit

# EU-382: the daily's base-state signal is RECENCY-BOUNDED. The 2026-07-17 brief's FOCUS read
# "Break the 'base dev RED' logjam (24+ tickets stuck on it)" while dev was GREEN (tip 74678bb,
# 382/382, 9/10 consecutive green runs that day) — the last red_base_block was 2026-07-16 10:26,
# 30+ hours stale, with merges landed since. The fuel was the loop's red-base pending decisions
# (loop.py:1430 "Base branch '…' is RED before any build …") rendered into the brief with no
# "is this still true" check — the same unbounded-history class EU-358 bounded to 48h for
# _recent_no_changes_ticket_ids and EU-336 bounded for the shipped window. A red_base_block
# headlines ONLY while it is the LATEST base signal: any later green proof (a `merged` land, or a
# `dev_gate` with passed=true — the same proof EU-376 publishes into the base-gate cache) resolves
# it, and a red older than 48h never headlines at all (the audit never rotates; one ancient red
# must not dominate forever). A false "everything's blocked" headline is worse than silence.
_BASE_RED_WINDOW_H = 48.0
# The stable lead of the red-base decision question loop.py writes — the render-side filter key for
# stale entries. tests/eu382_daily_base_recency_test.py pins it against loop.py's actual text.
_RED_BASE_MARKER = "is RED before any build"


def _event_dt(e: dict[str, object]) -> Optional[datetime]:
    """An audit event's ``ts`` as an AWARE local datetime (audit.py:33 writes %z offsets; a naive
    peer ts is taken as local — the same EU-181 .astimezone() normalization the shipped window uses)."""
    dt = _parse_ts(str(e.get("ts") or ""))
    return dt.astimezone() if dt is not None else None


def _active_base_red(cfg) -> Optional[dict[str, object]]:
    """The CURRENT red-base signal for the daily, or None when the base is (or must be presumed)
    green. Returns {'ticket_id': latest blocked ticket, 'tickets': distinct tickets blocked in the
    current red episode, 'age_s': seconds since the latest red_base_block} — see the EU-382 note on
    _BASE_RED_WINDOW_H for why every bound errs toward silence, not alarm."""
    from datetime import timedelta
    latest_red: Optional[dict[str, object]] = None
    latest_red_dt: Optional[datetime] = None
    latest_green_dt: Optional[datetime] = None
    reds: list[tuple[datetime, str]] = []
    for line in audit_lines(cfg.audit_path):
        try:
            e = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        ev = e.get("event")
        if ev == "red_base_block":
            d = _event_dt(e)
            if d is None:
                continue        # undateable red → too old to trust as "current" (EU-358 lineage)
            reds.append((d, str(e.get("ticket_id") or "")))
            if latest_red_dt is None or d > latest_red_dt:
                latest_red, latest_red_dt = e, d
        elif ev == "merged" or (ev == "dev_gate" and e.get("passed") is True):
            # Green proofs only: a real land or a green dev-gate suite run. dryrun_land/ship_dryrun
            # never ran the gate on the base, and dev_gate passed=false proves nothing green.
            d = _event_dt(e)
            if d is not None and (latest_green_dt is None or d > latest_green_dt):
                latest_green_dt = d
    if latest_red is None or latest_red_dt is None:
        return None
    if latest_green_dt is not None and latest_green_dt >= latest_red_dt:
        return None             # base has since PROVEN green — the red is resolved, not a blocker
    now = datetime.now().astimezone()
    if latest_red_dt < now - timedelta(hours=_BASE_RED_WINDOW_H):
        return None             # EU-358 precedent: unbounded history must not headline the daily
    episode = {tid for d, tid in reds
               if tid and (latest_green_dt is None or d > latest_green_dt)}
    return {"ticket_id": str(latest_red.get("ticket_id") or "?"),
            "tickets": max(len(episode), 1),
            "age_s": max((now - latest_red_dt).total_seconds(), 0.0)}


def _one_line(text: str, limit: int = _DECISION_MAX) -> str:
    """Collapse an officer's (often multi-paragraph) decision text to ONE capped line for the brief.
    Ends with '…' iff anything was dropped, so the caller can tell a clipped item from a short one.

    EU-336: the stored decision keeps the FULL question — this is render-side only, the same split
    EU-337 (77c2216) drew for the reviewer-findings phone ping. Before this, the 2026-07-15 brief
    rendered EU-139's entry as a wall of reviewer analysis ("Looking at the evidence: 1. EU-139's
    actual fix works: … 2. The two 'failing' tests pass in isolation…") — unreadable on a phone."""
    rows = [s for s in (l.strip().lstrip("*# ").strip() for l in str(text or "").splitlines()) if s]
    if not rows:
        return "(no question text)"
    first, dropped = rows[0], len(rows) > 1
    if len(first) > limit:
        first, dropped = first[:limit - 1].rstrip(), True
    return first + ("…" if dropped else "")


def _needs_you_label(row: dict[str, object]) -> str:
    """The bracketed tag for one Needs-you row, e.g. 'AUTO-14 [errored]' / 'AUTO-9 [blocked]'."""
    if row.get("category") == "parked":
        return "blocked"
    return str(row.get("outcome") or row.get("category") or "needs you")


def _filing_precision_line(cfg) -> str | None:
    """Best-effort 📉 line for officers below precision bar; returns None when none qualify."""
    from . import consolidate as _consolidate
    precisions = _consolidate.filing_precision(cfg)
    alerts: list[dict] = []
    for row in precisions:
        if row["precision"] < 0.5:
            alerts.append(row)
    if not alerts:
        return None
    # Surface the worst 1–2; format one-capped line per AC(3).
    caps = alerts[:2]
    parts = [f"{r['officer']} {round(r['precision'] * 100)}% ({r['total']} outcomes)" for r in caps]
    return "📉 Filing precision below bar: " + ", ".join(parts)


def _needs_you_rows(cfg) -> list[dict[str, object]]:
    """The deduped, currently-actionable Needs-you rows for the daily digest — one row per ticket,
    oldest (longest-waiting) first. Sourced from needs.summary(cfg), the same live inbox the cockpit's
    Needs-you panel renders — never a raw scan of the whole audit log's outcome field."""
    from . import needs as _needs
    live = _needs.summary(cfg).get("rows") or []
    seen: set[str] = set()
    out: list[dict[str, object]] = []
    for row in live:
        if row.get("category") not in _NEEDS_YOU_CATEGORIES:
            continue
        tid = str(row.get("ticket_id") or "")
        if not tid or tid in seen:      # belt-and-suspenders: needs.summary() already dedupes by
            continue                     # ticket per-category, this guards a cross-category repeat too
        seen.add(tid)
        out.append(row)
    out.sort(key=_started_key)
    return out


def standup(cfg) -> str:
    """The deterministic core of the morning daily: what shipped in the LAST 24H, what needs you now,
    and what awaits a decision. The shipped window is deliberately rolling rather than a
    yesterday/today calendar split (EU-336) — the unit works overnight, so its freshest merges land
    after midnight and a calendar split demoted exactly the work the Commander most wants to see. No
    cumulative/all-time history — that is deliberately out (the Commander does not want it in the daily)."""
    from . import decisions
    from datetime import timedelta
    tasks = load_tasks(cfg.audit_path)
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    cutoff = now.astimezone() - timedelta(hours=_SHIPPED_WINDOW_HOURS)

    def _landed(t) -> datetime | None:                      # when it landed (merge time), else run start —
        d = t.get("ended") or t.get("started")              # .astimezone() normalizes an audit ts written on
        return d.astimezone() if d else None                # another host (EU-181) to the reader's clock, so
        #                                                     the window's edge agrees cross-host
    def _in_window(t) -> bool:
        d = _landed(t)
        return d is not None and d > cutoff   # undateable run → not counted; the count stays exact
    shipped = [t for t in tasks if _is_merged_and_not_superseded(t) and _in_window(t)]
    needs_rows = _needs_you_rows(cfg)
    base_red = _active_base_red(cfg)
    pending = decisions.load(cfg)
    stale_base_red: list[dict[str, object]] = []
    if base_red is None:
        # EU-382: the base is not currently red, so the loop's red-base pending decisions ("Base
        # branch '…' is RED before any build …", loop.py:1430) are STALE — they fueled the
        # 2026-07-17 false "24+ tickets stuck" FOCUS headline a day after dev went green. Hold them
        # out of the BRIEF only (render-side, the EU-336/337 split): the stored decisions survive
        # untouched for the cockpit's /needs inbox, and the ♻️ note below keeps them discoverable.
        kept: list[dict[str, object]] = []
        for p in pending:
            (stale_base_red if _RED_BASE_MARKER in str(p.get("question") or "") else kept).append(p)
        pending = kept

    lines = [f"🫡 Daily stand-up — {today}", ""]
    lines.append(f"✅ Shipped to DEV (last 24h) ({len(shipped)}): "
                 + (", ".join(t["ticket_id"] for t in shipped) or "—"))
    shown = needs_rows[:_NEEDS_YOU_MAX]
    needs_line = ", ".join(f'{r["ticket_id"]} [{_needs_you_label(r)}]' for r in shown) or "—"
    overflow = len(needs_rows) - len(shown)
    if overflow > 0:
        needs_line += f" …and {overflow} more (cockpit → /needs)"
    lines.append(f"🟡 Needs you ({len(needs_rows)}): " + needs_line)
    if base_red is not None:
        # EU-382: the base state is now an EXPLICIT deterministic fact — the CTO synthesis reads it
        # from here instead of inferring "everything's blocked" from however many decision rows the
        # red episode left behind. Appears ONLY while the red is the latest base signal (≤48h).
        lines.append(f"⛔ Base RED: gate fails on the clean base — {base_red['tickets']} ticket(s) "
                     f"blocked, latest {base_red['ticket_id']} "
                     f"{_human_dur(base_red['age_s'])} ago")
    if pending:
        # EU-336: one capped line per item — never the officer's raw multi-paragraph text. The
        # "(cockpit → /needs)" pointer rides the header only when something was actually clipped,
        # mirroring the "…and N more (cockpit → /needs)" overflow idiom on the Needs-you line above.
        # 2026-07-19 (Commander order): PRACTICAL, not a wall — top 5 one-liners, the rest is a
        # count + the /needs pointer (the inbox is where answering actually happens).
        # 2026-07-21 (Commander: the daily showed a leaked officer banner as a "question"):
        # bullets go through the same brief pipeline the /needs cards use — structured summary
        # when the question carries options, else the banner-stripped one-line summarizer.
        from . import decisions as _dec

        def _q_brief(q):
            try:
                po = _dec.parse_options(q) or _dec.synthesize_options(q)
                return _one_line((po or {}).get("summary") or _dec.summarize_question(q))
            except Exception:  # noqa: BLE001 - a brief failure must never sink the daily
                return _one_line(q)
        rendered = [(p["id"], _q_brief(p.get("question", ""))) for p in pending[:5]]
        more = len(pending) - len(rendered)
        lines.append("❓ Awaiting your decision" + (f" ({len(pending)})" if more > 0 else "")
                     + ": (answer: cockpit → /needs)")
        lines += [f"   • {pid}: {q}" for pid, q in rendered]
        if more > 0:
            lines.append(f"   …and {more} more (cockpit → /needs)")
    else:
        lines.append("❓ Awaiting your decision: —")
    if stale_base_red:
        # EU-382: the held-out red-base decisions must not become invisible — one muted line says
        # the base recovered and where the tickets wait, without re-arming the blocker headline.
        lines.append(f"♻️ Base went green again — {len(stale_base_red)} stale base-red decision(s) "
                     "left out of this brief; re-queue those tickets from cockpit → /needs")
    # 2026-07-19 (Commander order): the daily answers DONE / TO DO / NEXT. "Next up" peeks at the
    # top of each board queue (best-effort — an unreachable Jira just drops the line), and the
    # failure-cause summary moves INTO the daily (the forensics nav link left the cockpit bar).
    try:
        from . import intake as _intake
        nxt = []
        for _app in (getattr(cfg, "apps", None) or [])[:3]:
            try:
                for _a, _tk in _intake.from_drain(cfg, _app.name, 3):
                    nxt.append(f"{_tk.id}")
            except Exception:  # noqa: BLE001 - one unreachable board must not kill the daily
                continue
        if nxt:
            lines.append("🔜 Next up (top of the queue): " + ", ".join(nxt[:6]))
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import forensics as _fx
        _tax = _fx.taxonomy(cfg)[:3]
        if _tax:
            lines.append("🧩 Failure causes (top): "
                         + " · ".join(f"{r['label']} ×{r['count']}" for r in _tax)
                         + " (details: cockpit → /forensics)")
    except Exception:  # noqa: BLE001
        pass
    try:
        _fp = _filing_precision_line(cfg)
        if _fp:
            lines.append(_fp)
    except Exception:  # noqa: BLE001 - best-effort; a read failure must not sink the daily
        pass
    return "\n".join(lines)


def render_status(tasks: list[TaskRow], limit: int = 15, show_cost: bool = True) -> str:
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
