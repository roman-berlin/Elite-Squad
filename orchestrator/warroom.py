"""War Room — the Elite Unit's command cockpit (data + render).

`general serve` mounts this at `/`. It turns the audit log, the blocked-tickets
file, the engineers' report files and the council history into a single live
command view: KPIs, the active run with its phase bar, the engineer roster, and
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
from datetime import date, datetime, timedelta
from urllib.parse import quote
from pathlib import Path
from typing import Optional, TypedDict

from . import cockpit_views as CV
from . import dashboard as D
from .officers import display as _display
from .phases import BUILD, GATE, LAND, PHASES, REVIEW

# --------------------------------------------------------------------------- #
# Cockpit roster key -> internal officers.OFFICER_NAMES key. Most match 1:1; a few cockpit keys differ
# from the engineer key (builder=field_engineer, reviewer=inspector). Display names are
# NEVER hard-coded below — they're resolved from the single source of truth via display(), so renaming an
# engineer is one edit in officers.OFFICER_NAMES and the board, roster and group-room labels all follow.
_OFFICER_KEY = {
    "general": "general", "pm": "pm",
    "builder": "field_engineer", "reviewer": "inspector", "scout": "scout",
    "provost": "provost", "quartermaster": "quartermaster", "sentinel": "sentinel",
}

# (cockpit key, role line). Order = chain of command. The display-name column is built from the SOT below.
_OFFICER_ROLES = [
    ("general",       "Orchestrator"),
    ("pm",            "S-5 · product"),
    ("builder",       "Builder"),
    ("reviewer",      "Reviewer"),
    ("scout",         "S-2 · QA / recon"),
    ("provost",       "Security gate"),
    ("quartermaster", "S-4 · deploy"),
    ("sentinel",      "S-3 · integration & rollback"),
]

# The roster: (key, display name, role line). Display name resolved from officers.OFFICER_NAMES (SOT).
_OFFICERS = [(key, _display(_OFFICER_KEY[key]), role) for key, role in _OFFICER_ROLES]

# cockpit key -> the engineer's display name (the council name a click consults). Derived from the SAME
# source of truth, so it can never drift from the board labels above. (general opens /chat, not /group.)
_GROUP_NAME = {key: _display(ik) for key, ik in _OFFICER_KEY.items() if key != "general"}


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


def _day_keys(days: int) -> list[str]:
    """The last `days` calendar dates as 'YYYY-MM-DD' strings (local), oldest→newest.

    EU-76 fix: built with calendar-date arithmetic (``date.today() - timedelta(days=k)``) rather than
    subtracting fixed 86400s chunks from a unix clock. A spring-forward / fall-back DST day is 23h or
    25h long, so stepping back 86400s at a time drifts across local midnight and can land two keys on
    the same calendar day — collapsing/skipping a bucket. Counting whole calendar days avoids that.
    """
    today = date.today()
    return [(today - timedelta(days=k)).isoformat() for k in range(days - 1, -1, -1)]


def _merges_per_day(cfg, days: int = 14) -> list[int]:
    """Daily count of 'merged' audit events for the last `days` calendar days, oldest→newest.

    EU-76 perf fix: the per-day buckets are produced by the single ``_scan()`` pass (memoized on the
    audit's (size, mtime_ns) signature), so rendering this sparkline no longer adds an extra
    full-history JSON parse on every SSE board frame — it just slices an already-computed dict. Days
    with no merges are zero; any failure returns all-zeros rather than breaking the board.
    """
    keys = _day_keys(days)
    try:
        by_day = _scan(cfg.audit_path).get("merged_by_day", {})
        return [int(by_day.get(k, 0)) for k in keys]
    except Exception:  # noqa: BLE001 — never break the board on aggregation failure
        return [0] * days


def _daily_token_burn(cfg, days: int = 14) -> list[float]:
    """Daily token COST (USD) for the last `days` calendar days, oldest→newest.

    EU-76 perf fix: delegates to ``usage.daily_burn_series``, which reads ``usage_ledger.jsonl`` at most
    once per (size, mtime_ns) change and caches the computed series — so the board's burn sparkline no
    longer calls ``read_text()`` on the whole ledger every render. Day buckets use calendar-date
    arithmetic there too. Returns all-zeros when usage metering or the ledger is unavailable.
    """
    try:
        from . import usage as _usage
        return _usage.daily_burn_series(cfg, days=days)
    except Exception:  # noqa: BLE001 — never break the board on metering failure
        return [0.0] * days


# One-pass audit scan, memoized on the audit's (size, mtime_ns) signature — the same fingerprint
# dashboard._audit_cache keys on — so a burst of SSE board frames / open tabs share ONE JSON parse of
# the merged history instead of re-parsing it every frame. The cached dict is read-only for all callers
# (kpis, roster, _merges_per_day); none mutate it, so sharing one instance is safe.
class _ScanResult(TypedDict):
    """The memoized single-pass audit scan shape (_scan): last-seen ts + count per event
    kind, plus per-calendar-day merged counts for the KPI trend sparkline."""
    last: dict[str, datetime]
    count: dict[str, int]
    merged_by_day: dict[str, int]


_scan_cache: dict[str, tuple[tuple, _ScanResult]] = {}   # audit path -> (sig, scan result)


def _scan(audit_path: str | Path) -> _ScanResult:
    """One pass over the raw audit: last-seen ts + count per event kind, and a per-calendar-day count
    of 'merged' events (``merged_by_day``) for the KPI trend sparkline. Cheap and used by every panel.

    EU-76: memoized on the audit's (size, mtime_ns) signature, so the full parse runs at most once per
    change. The merges sparkline now reads ``merged_by_day`` from this shared pass instead of triggering
    its own extra full-history parse on every board frame."""
    key = str(audit_path)
    sig = D._audit_sig(D._audit_paths(audit_path))
    hit = _scan_cache.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    last: dict[str, datetime] = {}
    count: dict[str, int] = {}
    merged_by_day: dict[str, int] = {}
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
        # EU-76: bucket 'merged' events by local calendar day in this same pass so the merges
        # sparkline (see _merges_per_day) costs no extra full-history parse.
        if k == "merged" and dt:
            ds = dt.strftime("%Y-%m-%d")
            merged_by_day[ds] = merged_by_day.get(ds, 0) + 1
    result = {"last": last, "count": count, "merged_by_day": merged_by_day}
    _scan_cache[key] = (sig, result)
    return result


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

# EU-313: freshness window for the "Security blocks" KPI card — a security_block event
# older than this (with no newer event for its ticket) no longer counts as an active block.
STALE_BLOCK_CUTOFF_S = 24 * 3600

# EU-298: freshness window for the "synced: <peers> · <age> ago" footer. A state clone that
# stopped syncing days ago rendered in the same neutral grey as a 2-minute-old one (the observed
# case was "synced: mac · 156h 38m ago"), so a dead sync read as current. Matches the block
# cutoff — a peer silent for a day has stopped publishing, not merely paused.
STALE_SYNC_CUTOFF_S = 24 * 3600

# Event types that resolve/supersede an earlier security_block for the same ticket_id: another
# security_block (a fresh gate hit), any dashboard-terminal outcome (merged/PR/escalated/etc — the
# ticket moved on), or a plain "build" (the Builder re-attempted the ticket).
_RESOLVING_EVENTS = D._TERMINAL | {"build", "security_block"}


def _load_security_blocks(cfg) -> list[dict]:
    """Load all security_block events from the audit log, newest first.

    Each dict includes: ticket_id, reason, ts (timestamp), app (if available).
    """
    blocks = []
    for line in D.audit_lines(cfg.audit_path):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("event") == "security_block":
            blocks.append({
                "ticket_id": ev.get("ticket_id", ""),
                "reason": ev.get("reason", "")[:2500],  # Match loop.py truncation
                "ts": ev.get("ts", ""),
                "iteration": ev.get("iteration", 1),
            })
    # Sort newest first
    blocks.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return blocks


def _active_security_blocks(cfg, blocks: list[dict]) -> list[dict]:
    """Subset of `blocks` (from _load_security_blocks) still counted as "active" (EU-313).

    A block is active when both hold:
      - it is within STALE_BLOCK_CUTOFF_S of now (freshness window), AND
      - no later event for the SAME ticket_id has superseded it (see _RESOLVING_EVENTS).

    Fixes the KPI card counting a block from days ago as if it were still live: previously
    `sec_block_count` was simply `len(_load_security_blocks(cfg))` — every security_block ever
    recorded, all-time. Preserves the input order (newest-first, inherited from the caller).
    """
    if not blocks:
        return []
    # Latest resolving-event ts per ticket_id, in one pass over the audit log. ts strings are all
    # written by audit.py with the same "%Y-%m-%dT%H:%M:%S%z" format, so lexical comparison orders
    # them correctly — same convention _load_security_blocks already relies on for its sort.
    latest_resolving: dict[str, str] = {}
    for line in D.audit_lines(cfg.audit_path):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("event") not in _RESOLVING_EVENTS:
            continue
        tid = ev.get("ticket_id")
        ts = ev.get("ts", "")
        if not tid or not ts:
            continue
        if ts > latest_resolving.get(tid, ""):
            latest_resolving[tid] = ts

    active = []
    for b in blocks:
        if _age_secs(_parse(b.get("ts", ""))) >= STALE_BLOCK_CUTOFF_S:
            continue  # outside the freshness window
        if latest_resolving.get(b.get("ticket_id", ""), "") > b.get("ts", ""):
            continue  # superseded by a later event for this ticket
        active.append(b)
    return active


def kpis(cfg, tasks: list[dict], app: Optional[str]) -> list[dict]:
    ts = _scope(tasks, app)
    today = datetime.now().strftime("%Y-%m-%d")

    def day(t: dict) -> str:
        s = t.get("ended") or t.get("started")
        return s.strftime("%Y-%m-%d") if s else ""

    merged = [t for t in ts if t.get("outcome") == "merged→dev"]
    merged_today = [t for t in merged if day(t) == today]
    sec_blocks = _load_security_blocks(cfg)  # all-time — the historical record (see /forensics)
    active_sec_blocks = _active_security_blocks(cfg, sec_blocks)  # EU-313: only still-live blocks count
    sec_block_count = len(active_sec_blocks)
    # EU-313: age of the most recent still-active block, for the card to surface inline (e.g.
    # "1 · 3h ago") — kept as its own field rather than "hint" since EU-152 retired hint-line
    # rendering for every KPI card (incl. Security blocks) and that decision stays in force here.
    sec_block_age = None
    if active_sec_blocks:
        # newest-first order is inherited from _load_security_blocks, so [0] is the most recent.
        sec_block_age = _rel(_parse(active_sec_blocks[0].get("ts", "")))

    # EU-76: pre-compute sparkline series for the Merged→DEV and cost/burn KPI cards.
    # Both helpers are best-effort: an empty audit or absent ledger yields all-zeros, which
    # _kpi_sparkline_svg then silently drops (< 2 non-zero points → returns "").
    merges_series = _merges_per_day(cfg, days=14)   # list[int], oldest→newest
    burn_series = _daily_token_burn(cfg, days=14)    # list[float] USD, oldest→newest

    # Each card deep-links to a view scoped to the count it shows: the /tasks log auto-applies the
    # ?filter= (merged / needs / parked) so the destination honors the click, and Security blocks
    # opens the forensics view scoped to the security-block findings that produced the number.
    # EU-150: "Merged total" card is retired — use "Merged → DEV today". EU-159: it now links to the
    # dedicated /merge-stats page (today's aggregated numbers) instead of the flat /tasks?filter=merged log.
    # 2026-07-19 (Commander order): the "Needs you" KPI card RETURNS — it was removed in 5a882a6
    # (2026-07-01) in favour of the side panel, but the Commander wants the number in the big KPI
    # row. Clicking opens /needs, where answering a decision comments the Jira ticket and sends it
    # back to To Do (the EU-337 structured-decision flow).
    try:
        from . import needs as _needs
        _needs_n = _needs.count(cfg, app)
    except Exception:  # noqa: BLE001
        _needs_n = 0
    cards = [
        {"label": "Merged → DEV today", "value": len(merged_today), "hint": "shipped to QA",
         "href": "/merge-stats", "sparkline": merges_series},  # EU-159: deep-link to the merge-stats page
        {"label": "Needs you", "value": _needs_n, "hint": "decisions · errored · parked",
         "tone": "warn" if _needs_n else None,
         "href": f"/needs?app={app}" if app else "/needs"},
        {"label": "Security blocks", "value": sec_block_count, "hint": "Security Engineer gate",
         "tone": "bad" if sec_block_count else None, "href": "/forensics?cat=security_block",
         # EU-145 passes the findings themselves so the card can render an interactive response
         # form. EU-313 filtered the COUNT to active blocks but kept handing the ALL-TIME list
         # here, so the card read "0" while still rendering the 2026-06-29 EU-116 block — the only
         # security_block ever recorded — expanded, with a live "Record response" box. The gate
         # that produced it was deleted in the Phase-2 collapse (c24f459), so nothing it reports
         # can be unblocked: a permanent false "needs-you" signal. EU-290: the list now tracks the
         # count. The historical record still lives at /forensics?cat=security_block.
         "security_block_findings": active_sec_blocks,
         "active_age": sec_block_age},  # EU-313: age of the most recent active block (None if none)
    ]

    # EU-145 — merged token KPI card (today + week in one, UX best practice).
    # Shows today prominently with weekly in the hint, plus the daily-cap gauge and trend sparkline.
    try:
        from . import usage as _usage
        bs = _usage.budget_status(cfg)
        w = _usage.windows(cfg)
        sess_total, sess_calls = w["today"]["total"], w["today"]["calls"]
        week_total, week_calls = w["week"]["total"], w["week"]["calls"]
        tok_tone = "bad" if bs.get("over") else "warn" if bs.get("alert") else None
        # Merge today and week tokens into a single card.
        # 2026-07-22 (Commander: "the design text is different between KPIs — make it the same"):
        # every other tile is a BARE VALUE in .kv over a short caps label in .kl ("16" /
        # "MERGED -> DEV TODAY"). This one used to pack prose into .kv ("91.8M today · 18%"),
        # which renders that sentence in the 30px tabular-nums MONO display face the other tiles
        # only ever put digits in — so it read as a different typeface even though the CSS is
        # identical. The qualifier belongs in the label, where the other tiles keep theirs.
        if bs["on"] and bs.get("over"):
            # Budget exhausted — show paused state
            tok_value = "⛔ paused"
            tok_hint = "daily cap reached · resets midnight"
        elif bs["on"]:
            # The % is NOT repeated here: the gauge bar directly below this value already renders it,
            # and the hint carries the exact cap. Duplicating it in the value is what put prose in a
            # digits-only display face in the first place.
            tok_value = _fmt_tokens(sess_total)
            pct_str = f"{round(bs['pct'] * 100)}%"
            tok_hint = (f"{pct_str} of today's cap · {_fmt_tokens(week_total)} this week · "
                        f"{week_calls} calls · cap {_fmt_tokens(bs['cap'])}")
        else:
            tok_value = _fmt_tokens(sess_total)
            tok_hint = f"{_fmt_tokens(week_total)} this week · {sess_calls + week_calls} calls total"
        # EU-540: compact Qwen Token-Plan quota line. Computed HERE (with cfg) rather than in
        # _kpi_html so the warroom tile and the /usage page resolve the SAME probe path — both
        # call qwen_quota_status(cfg) → Path(cfg.audit_path).parent/"qwen_quota.json". Best-effort:
        # None (or any failure) simply hides the line.
        try:
            qwen_quota = _usage.qwen_quota_status(cfg)
        except Exception:  # noqa: BLE001
            qwen_quota = None
        cards.append({
            "label": "Tokens",
            "value": tok_value,
            "hint": tok_hint,
            "tone": tok_tone,
            # gauge = fill fraction 0-1; None when no cap is configured (bar stays hidden)
            "gauge": bs["pct"] if bs["on"] else None,
            "href": "/usage",
            "sparkline": burn_series,  # EU-76: 14-day daily token-burn trend
            "qwen_quota": qwen_quota,  # EU-540: e.g. "5h 62% · 7d 41%" or None
        })
    except Exception:  # noqa: BLE001 — never let usage metering break the board
        pass

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
        "builder": last.get("build"),
        "reviewer": last.get("review"),
        "scout": rpt("scout-report.md"),
        "provost": last.get("security_block") or rpt("provost-report.md"),
        "quartermaster": rpt("quartermaster-report.md"),
    }
    # An active run means the Builder/Reviewer are on duty right now.
    on_duty = {"builder", "reviewer"} if active else set()

    # roster key -> the engineer's council name (so a click consults that exact engineer). Read from the
    # single source of truth (see _GROUP_NAME) so it never drifts from the board / roster labels.
    group_name = _GROUP_NAME
    out = []
    for key, name, role in _OFFICERS:
        dt = seen.get(key)
        if key in on_duty:
            dot = "live"
        elif _age_secs(dt) < 600:      # acted in the last 10 minutes
            dot = "recent"
        else:
            dot = "idle"
        # The CTO is your 1:1 chat; every other engineer opens a focused consult with just them.
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
        # The feed is a one-line-per-ticket activity strip: flatten any newlines and
        # word-boundary-trim the note to keep each row tight (EU-33). Engineer-report bullets
        # are rendered in full on the cockpit transcript (dashboard._detail_html), not here.
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


def _detect_prebuild_triage(cfg, ticket_id: str, has_build: bool) -> Optional[dict]:
    """Read audit log to detect if ticket is in prebuild triage phase.

    Returns a dict with triage_phase and triage_verdict if in triage, else None.
    Only returns triage state when NO build has occurred yet (has_build=False).
    """
    if has_build:
        return None

    # Scan audit log for prebuild events for this ticket
    triage_events = []
    close_events = []
    refile_events = []

    for line in D.audit_lines(cfg.audit_path):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        event = ev.get("event")
        ev_ticket = ev.get("ticket_id")

        if ev_ticket != ticket_id:
            continue

        if event == "prebuild_triage":
            triage_events.append(ev)
        elif event == "prebuild_close":
            close_events.append(ev)
        elif event == "prebuild_refile":
            refile_events.append(ev)
        elif event in ("build", "security_block", "merged"):
            # Any build-phase event means we're past triage
            return None

    # Determine triage state from events found
    # Most recent triage determines the verdict
    if triage_events:
        latest_triage = triage_events[-1]  # newest
        verdict = latest_triage.get("verdict", "UNKNOWN")

        # Check if there's a matching close/refile outcome
        if close_events:
            return {
                "triage_phase": "closed",
                "triage_verdict": verdict,
                "triage_reason": close_events[-1].get("reason", "closed"),
            }
        elif refile_events:
            latest_refile = refile_events[-1]
            return {
                "triage_phase": "refiled",
                "triage_verdict": verdict,
                "triage_new_tickets": [
                    {"key": r.get("new_ticket_key"), "title": r.get("title")}
                    for r in refile_events
                ],
            }
        else:
            # In triage but not yet resolved
            return {
                "triage_phase": "triaging",
                "triage_verdict": verdict,
            }

    return None


def _detect_current_triage(cfg, current_ticket_id: Optional[str] = None,
                           since_ts: Optional[float] = None) -> Optional[dict]:
    """Read audit log to detect the CURRENT prebuild triage activity.

    EU-130: When autopilot is running and processing tickets through prebuild triage,
    this function finds the most recent triage event, which represents what the unit
    is doing RIGHT NOW — not a stale completed pipeline from a previous ticket.

    Args:
        cfg: Config object
        current_ticket_id: The ticket_id of the current task (if any). Used to filter
            out stale triage events from already-completed tickets.
        since_ts: Only consider events after this timestamp (unix timestamp). Used to
            filter for recent activity when autopilot is active.

    Returns:
        A dict with ticket_id, triage_phase, triage_verdict, and optionally
        triage_reason or triage_new_tickets if current triage activity is found.
        Returns None if no current triage activity is detected.
    """
    # Scan audit log for the most recent prebuild_triage event
    latest_triage = None
    latest_triage_ts = 0.0
    latest_close = None
    latest_refile = None

    for line in D.audit_lines(cfg.audit_path):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        event = ev.get("event")
        ticket_id = ev.get("ticket_id")

        if not ticket_id:
            continue

        # Parse timestamp
        ts_str = ev.get("ts", "")
        try:
            dt = D._parse_ts(ts_str)
            if dt:
                ev_ts = dt.timestamp()
            else:
                continue
        except Exception:
            continue

        # Filter by timestamp if provided (only recent activity)
        if since_ts is not None and ev_ts < since_ts:
            continue

        # Track the most recent triage event
        if event == "prebuild_triage":
            if ev_ts > latest_triage_ts:
                latest_triage_ts = ev_ts
                latest_triage = ev
        elif event == "prebuild_close":
            if ticket_id == (latest_triage or {}).get("ticket_id"):
                latest_close = ev
        elif event == "prebuild_refile":
            if ticket_id == (latest_triage or {}).get("ticket_id"):
                latest_refile = ev
        elif event in ("build", "security_block", "merged", "ticket_start"):
            # If we hit a build event for this ticket, triage is over for this ticket
            # But we still want to check if there's NEWER triage activity for other tickets
            ticket_of_build = ticket_id
            # If this build event is for the latest triage ticket, that triage is stale
            if latest_triage and ticket_of_build == latest_triage.get("ticket_id"):
                latest_triage = None
                latest_triage_ts = 0.0

    # If no recent triage activity, return None
    if not latest_triage:
        return None

    triage_ticket_id = latest_triage.get("ticket_id")
    verdict = latest_triage.get("verdict", "UNKNOWN")

    # If the current task's ticket_id matches the triage ticket, let the normal
    # _detect_prebuild_triage path handle it (it will be called with the right ticket_id)
    if current_ticket_id and current_ticket_id == triage_ticket_id:
        return None

    # Build the triage state result
    result = {
        "ticket_id": triage_ticket_id,
        "triage_phase": "triaging",
        "triage_verdict": verdict,
    }

    # Check if there's a matching close/refile outcome
    if latest_close:
        result["triage_phase"] = "closed"
        result["triage_reason"] = latest_close.get("reason", "closed")
    elif latest_refile:
        result["triage_phase"] = "refiled"
        result["triage_new_tickets"] = [
            {"key": latest_refile.get("new_ticket_key"), "title": latest_refile.get("title")}
        ]

    return result


# The freshness window EVERY "is this run live right now" heuristic shares (EU-477): a run
# with no audit activity inside it reads as crashed/interrupted, not live. `_run_in_flight`
# (the newest-run boolean) and `live_runs` (the full per-ticket live subset) both default to
# it so the two can never drift apart.
_LIVE_WITHIN_S = 150


def _synthetic_triage_run(t: dict, current_triage: dict, fallback_ticket_id: str) -> dict:
    """The EU-130 synthetic run object showing CURRENT prebuild triage activity.

    Moved verbatim out of ``active_run`` (EU-477) so ``live_runs`` — the shared live-subset
    computation — can pre-empt the live set with it exactly the way ``active_run`` always
    did: when autopilot is triaging new tickets (EU-113, EU-114) while the task list still
    shows an old completed run (EU-109 that merged at 16:16), the cockpit shows the CURRENT
    triage instead of a stale 'Working · Land' card. ``t`` is the newest scoped task (its
    app seeds the synthetic row); ``fallback_ticket_id`` is its ticket id, used when the
    triage event carries none.
    """
    triage_ticket_id = current_triage.get("ticket_id") or fallback_ticket_id
    triage_phase = current_triage.get("triage_phase", "triaging")
    triage_verdict = current_triage.get("triage_verdict", "UNKNOWN")

    # Build a minimal run object that _run_html can render with triage UI
    # Use live=True so it renders as an active run, not "last run"
    result = {
        "live": True,
        "ticket": triage_ticket_id,
        "app": str(t.get("app") or ""),  # Use the current app
        "branch": "",  # No branch yet in triage
        "passes": 0,
        "verdict": "",
        "outcome": None,  # No terminal outcome - triage is in progress
        "cost": 0.0,
        "phases": [],  # Empty phases - triage replaces the phase bar
        "reached": 0,
        "failed_phase": None,
        "sparkline": [],  # No sparkline for triage state
        "triage_phase": triage_phase,
        "triage_verdict": triage_verdict,
        # EU-477 compat keys (see _run_obj_for): every run object live_runs() emits carries
        # the raw-task id + start time the EU-486 multi-card loop reads off each element.
        "ticket_id": triage_ticket_id,
        "started": None,
    }
    if current_triage.get("triage_reason"):
        result["triage_reason"] = current_triage["triage_reason"]
    if current_triage.get("triage_new_tickets"):
        result["triage_new_tickets"] = current_triage["triage_new_tickets"]

    return result


def _run_obj_for(cfg, t: dict, ts: list[dict], live: bool) -> dict:
    """Build the Active-run panel dict for ONE scoped task (EU-477).

    The exact run-object shape ``active_run`` has always produced per run — ``ticket``,
    ``app``, ``phases``/``reached``, ``failed_phase``, ``sparkline``, optional triage
    fields — extracted so ``live_runs`` can build one per live task and ``active_run`` is
    a thin wrapper over ``live_runs()[0]``.  ``t`` is the task to render; ``ts`` is the
    full scoped history (newest-first) the sparkline is derived from; ``live`` selects the
    EU-448 live-phase derivation (True) vs the idle/last-run structural path (False).
    """
    ticket_id = str(t.get("ticket_id") or "")

    # Approximate phase from what's been recorded so far.
    has_build = any(d.get("build_summary") or d.get("tools") for d in t.get("passes_list", []))
    has_review = t.get("verdict") is not None
    merged = t.get("outcome") == "merged→dev"

    # EU-130: Detect prebuild triage phase before build events occur (for the current ticket)
    triage_state = None
    if ticket_id and ticket_id != "—":
        triage_state = _detect_prebuild_triage(cfg, ticket_id, has_build)
    # One source of truth with the terminal bar: the PHASES labels + order (imported from
    # phases.py, pinned by the _EXPECTED assertion below) are what loop._bar and this web bar
    # share, so the two can never drift (EU-55). The CURRENT-phase index is NOT shared —
    # loop._bar advances inside loop.run at each real transition, while the web bar re-derives
    # it here from the audit (same build/gate events the terminal bar advances on, via
    # load_tasks' `t["phase"]`). `reached` doubles as the count of completed phases AND the index
    # of the current/next phase.
    phases = list(PHASES)
    reached = 0
    if live:
        # EU-448: a LIVE run's bar must show the phase it is IN right now, not the next one.
        # `has_build` flips True the moment pass 1's `build` event exists and never resets, so it
        # read "Gate" for the entire build — including PM-driven continuation passes (pm_review /
        # pm_decided emit no gate event) — a confident display lie (same class as AUTO-198).
        # Instead derive from `t["phase"]`, the LATEST build/gate event load_tasks recorded:
        # "gate" once the gate phase actually began (loop fires `gate` before `deterministic_gate`),
        # "build" while the Builder is still working or after a retry build reset it (EU-136 retry
        # semantics). Do NOT use `gate_passed` — it stays non-None during a post-gate-fail retry
        # build and would re-introduce the lie; `phase` correctly resets to "build" on retry.
        if merged:
            reached = len(PHASES)                       # reviewed + landed → every phase complete
        elif has_review:
            # 2026-07-19 (AUTO-198): a live run whose latest verdict is FAIL/REJECT is REBUILDING
            # (pass N+1) — show Build, not "Land next to verdict FAIL".
            _v = (t.get("verdict") or "").upper()
            reached = BUILD if ("FAIL" in _v or "REJECT" in _v) else LAND
        elif t.get("phase") == "gate":
            reached = GATE                               # the gate phase actually began
        else:
            reached = BUILD                              # builder working; gate not yet reached
    else:
        # Idle / last-run structural path (unchanged): the run is over, so the audit's structural
        # facts (built? reviewed? merged?) describe where it landed. Keeps phase_fail_test.py and
        # the idle EU-55 cases exactly as they were — EU-448 only fixes the LIVE derivation.
        if has_build:          # build done → the Gate runs next
            reached = GATE
        if has_review:
            _v = (t.get("verdict") or "").upper()
            if "FAIL" in _v or "REJECT" in _v:
                reached = REVIEW
            else:
                reached = LAND     # passing review → Land runs next
        if merged:             # reviewed and landed → every phase complete
            reached = len(PHASES)
    # A terminal-but-FAILED run (errored/escalated) must light its STOPPING phase red, not render
    # the phases behind it as cleanly-done. Derive the phase the run died at so the bar shows where
    # it actually broke instead of implying it sailed through review and just didn't deploy.
    failed_phase = None
    if not live and t.get("outcome") in _FAILED_OUTCOMES:
        verdict = (t.get("verdict") or "").upper()
        if not has_build:
            failed_phase = BUILD                               # Build never finished
        elif not has_review:
            failed_phase = GATE                                # built, then died before review (the Gate)
        elif "FAIL" in verdict or "REJECT" in verdict:
            failed_phase = REVIEW                              # review verdict was a rejection
        else:
            failed_phase = LAND                                # passed review, broke at Land
    # EU-55 / F12 audit: phases.py is the single source of truth. Ordering confirmed:
    #   PHASES[BUILD]="Build", PHASES[GATE]="Gate", PHASES[REVIEW]="Review",
    #   PHASES[LAND]="Land".  (Phase-2 §2: the Security and Tests phases were removed.)
    # A live run reads Build→Gate→Review→Land off the gate-phase signal (EU-448); LAND (idx 3)
    # is not used as a live "reached" value — a passing review sets reached=LAND but the merge
    # lands it at len(PHASES). len(PHASES) marks all phases complete after merge. ✓
    _EXPECTED = ("Build", "Gate", "Review", "Land")
    if PHASES != _EXPECTED:
        raise AssertionError(
            f"phases.py PHASES order drifted from index constants — "
            f"got {PHASES!r}, expected {_EXPECTED!r}"
        )

    # EU-76: collect pass-count history across the 10 most recent completed runs
    # (oldest→newest) so _run_html() can render a trend sparkline mini-chart.
    # Only runs with a terminal outcome are included; in-flight runs lack `passes` data.
    completed = [t2 for t2 in ts if t2.get("outcome")]  # newest-first
    sparkline = [t2.get("passes") or 0 for t2 in reversed(completed[:10])]  # oldest→newest

    result = {
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
        "sparkline": sparkline,  # EU-76: list[int] oldest→newest, for trend chart
        # EU-477 compat keys: render_board's EU-486 multi-card loop reads the raw-task id
        # (`lt.get("ticket_id")` to scope each card's history) and the raw start time
        # (`run.get("started")` for the per-card elapsed timer) off every element
        # live_runs() emits, so each run object carries them alongside the render fields.
        "ticket_id": str(t.get("ticket_id") or ""),
        "started": t.get("started"),
    }

    # EU-130: Add triage phase fields if detected
    if triage_state:
        result["triage_phase"] = triage_state.get("triage_phase")
        result["triage_verdict"] = triage_state.get("triage_verdict")
        if triage_state.get("triage_reason"):
            result["triage_reason"] = triage_state["triage_reason"]
        if triage_state.get("triage_new_tickets"):
            result["triage_new_tickets"] = triage_state["triage_new_tickets"]

    return result


def live_runs(cfg, tasks: list[dict], app: Optional[str], active: bool,
              within_s: int = _LIVE_WITHIN_S) -> list[dict]:
    """The FULL set of genuinely-live runs for *app* as run-object dicts, newest-first,
    capped at ``max_concurrent_builders`` (config.py).

    EU-477 (generalises the EU-486 detection helper): the single shared live-subset
    computation behind ``active_run()`` — a thin wrapper over ``live_runs()[0]`` — and
    ``render_board``'s multi-card loop.  Each element carries the SAME run-object shape
    ``active_run`` has always produced per run (``ticket``, ``app``, ``phases``/``reached``,
    ``sparkline``, optional triage fields — plus the ``ticket_id``/``started`` compat keys
    the multi-card loop reads), so downstream renderers don't need to know about the
    multi-run case.

    A task counts as live when it has NO terminal outcome (``outcome`` unset) AND its OWN
    audit activity falls within the shared ``within_s`` freshness window — the
    ``_run_in_flight`` ~150 s idle-cutoff heuristic applied per-ticket instead of only to
    ``ts[0]``.  Stale rows drop out of the live set — INCLUDING the newest scoped task:
    a run silent for >150 s reads as crashed/interrupted (the written AC of EU-477), and
    ``active_run`` renders it as an idle 'last run' via its fallback instead.

    EU-130: when the newest scoped run has CURRENT prebuild triage activity for a DIFFERENT
    ticket, the single synthetic triage run pre-empts the live subset — the branch
    ``active_run`` has always run first, moved here unchanged.

    ``active=False`` → ``[]`` (idle boards render the 'last run' via ``active_run``'s
    fallback).  One ``D.audit_lines`` pass — cached on (size, mtime_ns) — so no extra
    audit-file loads per frame.
    """
    ts = _scope(tasks, app)
    if not ts or not active:
        return []

    # EU-130: current prebuild triage pre-empts the live subset. This ran FIRST in the old
    # active_run (before the ts[0] selection), so it runs before the live-subset filter here:
    # it catches autopilot triaging new tickets while the task list still shows an old
    # completed run — and it must fire even when ts[0] is terminal or stale. No time
    # filtering — the function uses event ordering to detect current activity.
    ticket_id = str(ts[0].get("ticket_id") or "")
    if ticket_id and ticket_id != "—":
        current_triage = _detect_current_triage(cfg, ticket_id)
        if current_triage:
            return [_synthetic_triage_run(ts[0], current_triage, ticket_id)]

    now = datetime.now().timestamp()

    # One-pass: last activity timestamp per ticket_id across the whole audit.
    last_ts: dict[str, float] = {}
    try:
        for line in D.audit_lines(cfg.audit_path):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            tid = ev.get("ticket_id")
            dt = D._parse_ts(ev.get("ts", ""))
            if not tid or not dt:
                continue
            tstamp = dt.timestamp()
            prev = last_ts.get(tid, 0.0)
            if tstamp > prev:
                last_ts[tid] = tstamp
    except Exception:  # noqa: BLE001
        pass

    # Filter: keep tasks with no terminal outcome AND fresh activity of their OWN —
    # no always-include-ts[0] exception (EU-477 AC: a stale newest run drops out too).
    # Dedupe by ticket_id: `last_ts` is a per-ticket MAX, so EVERY row sharing a ticket_id
    # inherits the SAME freshness score. Without this, a re-run or restart-orphaned GHOST row
    # (e.g. a 25h-old EU-474 verify that never got a terminal outcome) passes the same gate as
    # the current run and the ticket renders TWICE. `ts` is newest-first, so the first row is
    # the live run; later rows of that ticket are stale duplicates. The drain's sibling-mutex
    # guarantees a ticket builds at most once at a time → exactly one live card per ticket.
    result: list[tuple[float, dict]] = []          # (score, task) for sorting
    seen_tids: set[str] = set()
    for t in ts:
        tid = str(t.get("ticket_id") or "")
        if tid and tid in seen_tids:
            continue                                  # older run of an already-represented ticket
        if tid:
            seen_tids.add(tid)
        if t.get("outcome"):
            continue                                  # newest run of this ticket is terminal → not live
        score = last_ts.get(tid, 0.0)
        if (now - score) < within_s:
            result.append((score, t))

    # Sort newest-first by last-activity timestamp (descending).
    result.sort(key=lambda x: x[0], reverse=True)
    # Cap so stale/orphaned rows can't flood callers.
    cap = max(1, int(getattr(cfg, "max_concurrent_builders", 1) or 1))
    return [_run_obj_for(cfg, t, ts, live=True) for _, t in result[:cap]]


def active_run(cfg, tasks: list[dict], app: Optional[str], active: bool) -> Optional[dict]:
    """The live run if one is going, else the most recent run as 'last run'.

    EU-477: a THIN wrapper over ``live_runs()`` — the single shared live-subset
    computation.  When at least one run is genuinely live it returns ``live_runs()[0]``
    (the newest live run, full run-object shape — EU-130 triage synthesis and EU-448
    live-phase derivation included, unchanged, now computed inside ``live_runs`` /
    ``_run_obj_for``).  When nothing is live it falls back to the newest scoped run
    rendered as an idle 'last run' (``live=False``) — the historical contract the cockpit
    has always relied on for its last-run panel.  ``None`` only when the app has no runs
    at all.
    """
    ts = _scope(tasks, app)
    if not ts:
        return None
    lives = live_runs(cfg, tasks, app, active)
    if lives:
        return lives[0]
    # No genuinely-live run: show the newest one as 'last run' (idle structural path).
    return _run_obj_for(cfg, ts[0], ts, live=False)


def _run_in_flight(cfg, tasks: list[dict], app: Optional[str], within_s: int = _LIVE_WITHIN_S) -> bool:
    """True when a run is genuinely live RIGHT NOW even though the cockpit didn't start it — e.g. a build
    kicked off by the Needs-you answer box, /unblock, or autopilot in another process. Heuristic: the
    newest run for this project has NO terminal outcome AND the audit shows activity within the last
    `within_s` seconds. Time-bounded so a crashed/interrupted attempt stops reading as live.

    EU-477: ``live_runs()`` generalises exactly this heuristic to the FULL per-ticket live subset
    (and shares the ``_LIVE_WITHIN_S`` window); this cheap newest-run boolean stays as the
    ``render_board`` inflight flag for runs the cockpit didn't start."""
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


def _fmt_tokens(n: int) -> str:
    """Format a token count into a compact human-readable string (e.g. 1.2M, 340k, 852)."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


# --------------------------------------------------------------------------- #
# Render

def _esc(s: object) -> str:
    return html.escape(str(s))


def _security_issues_html(findings: list[dict]) -> str:
    """Render the interactive security issues list with reply forms (EU-145).

    Each issue shows:
    - Ticket ID (link to /tasks?filter=<ticket_id>)
    - The security finding text (truncated for readability)
    - Timestamp
    - A reply form to record a response to the audit log
    """
    if not findings:
        return '<div class=secempty>No security blocks recorded yet.</div>'

    out = []
    for i, finding in enumerate(findings[:10]):  # Show at most 10 issues
        ticket_id = _esc(finding.get("ticket_id", ""))
        reason = _esc(finding.get("reason", "")[:480])  # Truncate for display
        ts_str = finding.get("ts", "")
        ts = _parse(ts_str)
        ago = _rel(ts) if ts else "—"
        iteration = finding.get("iteration", 1)

        # Build the issue HTML with reply form
        out.append(f'''
<div class=secissue>
  <div class=sechead>
    <span class=secticket><a href="/tasks?filter={quote(ticket_id)}">{ticket_id}</a></span>
    <span class=secmeta>iteration {iteration} · {ago}</span>
  </div>
  <div class=secbody>{reason}</div>
  <form method=post action=/api/security-reply class=secreply>
    <input type=hidden name=ticket_id value="{_esc(ticket_id)}">
    <input type=hidden name=iteration value="{iteration}">
    <textarea name=response placeholder="Add your response (creates Jira ticket for CRITICAL/HIGH issues)…"
              rows=2 maxlength=2000 required></textarea>
    <button type=submit class=secbtn>Record response</button>
  </form>
</div>''')

    if len(findings) > 10:
        out.append(f'<div class=secmore>{len(findings) - 10} more — view all in <a href="/forensics?cat=security_block">forensics</a></div>')

    return "\n".join(out)


def _kpi_html(cards: list[dict]) -> str:
    out = []
    for c in cards:
        tone = c.get("tone") or ""
        href = c.get("href")
        label = _esc(c.get("label", ""))
        # EU-145: Security blocks card is interactive with expandable details
        is_security_card = label == "Security blocks"
        findings = c.get("security_block_findings") or []
        if is_security_card:
            # Always render interactive card, even when empty
            issues_html = _security_issues_html(findings) if findings else '<div class=secempty>No security blocks recorded yet.</div>'
            # EU-313: surface the age of the most recent still-active block (e.g. "1 · 3h ago") right
            # in the headline number, so freshness is visible at a glance. Deliberately NOT a separate
            # class=kh hint line — EU-152 retired third-line hints from every KPI card and that
            # decision stays in force; this appends inline into the existing kv div instead.
            age = c.get("active_age")
            kv_text = f'{_esc(c["value"])} · {_esc(age)}' if age else _esc(c["value"])
            # EU-290: only expand when there is something to act on. A zero-count card sitting
            # permanently open around "No security blocks recorded yet." is the same fixed
            # false "needs-you" signal, just with the number fixed.
            open_attr = " open" if findings else ""
            out.append(
                f'<details class="kpi {tone}"{open_attr}>'
                f'<summary class=kpisum><div class=kv>{kv_text}</div>'
                f'<div class=kl>{_esc(c["label"])}</div></summary>'
                f'{issues_html}'
                f'</details>')
            continue

        # Optional inline gauge bar (EU-75 tokens-today/cap card). Rendered via inline
        # style so the existing CSS block is untouched; bar colour tracks the card tone.
        gauge_html = ""
        g = c.get("gauge")
        if g is not None:
            pct = min(1.0, max(0.0, float(g))) * 100
            gcol = ("var(--bad)" if tone == "bad" else
                    "var(--warn)" if tone == "warn" else
                    "var(--ok)")
            gauge_html = (
                '<div style="margin-top:8px;height:3px;background:var(--line2);'
                'border-radius:3px;overflow:hidden">'
                f'<div style="height:100%;width:{pct:.1f}%;background:{gcol};'
                'border-radius:3px;transition:width .4s ease"></div></div>')
        # EU-76 / EU-540: optional inline sparkline SVG beneath the gauge.
        # Colour tracks the card tone: ok→green, warn→amber, bad→red, else info-blue.
        spark_html = ""
        sp = c.get("sparkline")
        if sp is not None:
            sp_stroke = (
                "var(--ok)" if tone == "ok" else
                "var(--warn)" if tone == "warn" else
                "var(--bad)" if tone == "bad" else
                "var(--info)"
            )
            spark_html = _kpi_sparkline_svg(sp, stroke=sp_stroke)
        # EU-540: Tokens tile — show visible % text (same bs['pct'] as gauge) + compact Qwen quota line.
        # The Qwen quota string is precomputed in kpis() WITH cfg (c["qwen_quota"]) so this tile and
        # the /usage page read the same probe file — never re-query here with a None cfg.
        extra = ""
        if label == "Tokens":
            pct_text = ""
            if g is not None:
                pct_val = min(100, max(0, round(float(g) * 100)))
                pct_text = (f'<span style="display:inline-block;margin-top:6px;font-size:11px;'
                            f'color:var(--dim);font-weight:500;font-family:var(--mono);'
                            f'font-variant-numeric:tabular-nums">{pct_val}%</span>')
            qwen_extra = ""
            qw = c.get("qwen_quota")
            if qw:
                qwen_extra = (f'<div style="margin-top:4px;font-size:11px;color:var(--dim);'
                              f'font-family:var(--mono)">Qwen Token-Plan: {html.escape(str(qw))}</div>')
            extra = f"{pct_text}\n{qwen_extra}"
        # EU-297: numeral + label render via the shared cockpit_views._kpi_metric partial
        # (--t-2xl token scale) instead of duplicating the <div class=kv>/<div class=kl> markup
        # inline per card.
        out.append(CV._kpi_metric(c["value"], c.get("label", ""), tone=tone, href=href,
                                   extra=f"{gauge_html}{spark_html}\n{extra}"))
    return "".join(out)


def _sparkline_svg(values: list[int], width: int = 88, height: int = 28) -> str:
    """Render a tiny inline-SVG polyline for a pass-count trend series (EU-76).

    Each point represents one completed run; x-axis is time (oldest left, newest right),
    y-axis is pass count (higher = more passes = worse quality trend).
    Returns an empty string when fewer than 2 data points are available.
    """
    if len(values) < 2:
        return ""
    n = len(values)
    max_v = max(values) or 1
    min_v = min(values)
    span = float(max_v - min_v) or 1.0
    # SVG y-axis is top-down; invert so a high pass count (bad) appears near the top.
    # x is spread across [2, width-2] to keep points clear of the SVG edge.
    pts = " ".join(
        f"{2.0 + i * (width - 4) / (n - 1):.1f},"
        f"{3.0 + (1.0 - (v - min_v) / span) * (height - 6):.1f}"
        for i, v in enumerate(values)
    )
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" aria-hidden="true" style="flex:none">'
        f'<polyline points="{pts}" fill="none" stroke="var(--info)" '
        f'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" opacity=".65"/>'
        f'</svg>'
    )


def _kpi_sparkline_svg(
    values: list[float],
    width: int = 60,
    height: int = 20,
    stroke: str = "var(--info)",
) -> str:
    """Render a tiny inline SVG polyline for a KPI trend series (EU-76).

    Plots `values` as a `width`×`height` px polyline — no axes, labels, or ticks,
    just the shape so the eye can read the trend at a glance. Older values are on
    the left, newer on the right. Returns an empty string when fewer than 2 points
    are available (nothing meaningful to draw). All-equal series still render as a
    flat mid-line so the card always has a consistent visual footprint once data
    arrives.
    """
    if len(values) < 2:
        return ""
    n = len(values)
    max_v = max(values)
    min_v = min(values)
    span = float(max_v - min_v)
    # SVG y-axis is top-down; invert so higher values sit visually higher (more merges → peak, more
    # cost → also a peak). A flat series (span == 0) has no trend to show, so every point maps to the
    # vertical middle (fraction 0.5) — a centred mid-line, matching the docstring — rather than being
    # pinned to an edge (the pre-EU-76 `span or 1.0` form drove flat series to the bottom).
    def _y(v: float) -> float:
        frac = 0.5 if span == 0 else (v - min_v) / span
        return 3.0 + (1.0 - frac) * (height - 6)
    pts = " ".join(
        f"{2.0 + i * (width - 4) / (n - 1):.1f},{_y(v):.1f}"
        for i, v in enumerate(values)
    )
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" aria-hidden="true" '
        f'style="display:block;margin-top:7px;flex:none">'
        f'<polyline points="{pts}" fill="none" stroke="{stroke}" '
        f'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" opacity=".55"/>'
        f'</svg>'
    )


def _run_html(run: Optional[dict], mode: Optional[str] = None,
              elapsed: Optional[str] = None, manual: bool = False,
              log_path: Optional[str] = None, log_ticket: Optional[str] = None) -> str:
    """Render the Active Run panel body.

    EU-76: this is the single authoritative slot for the in-flight (or most-recent) run.
    The standalone hero div has been retired; when a run is live the header inside this
    panel is promoted to a hero-style layout — pulsing dot, large ticket ID, current phase
    name, mode chip — so the most important information stays prominent without the
    duplication of a separate .hero card above.

    EU-106: ``log_path`` — when the BE subtask stores a path in state['log_path'], a small
    '📂 open log' link is rendered right after the phase bar so you can jump to the MQL5-style
    local run log without leaving the cockpit.

    EU-487: ``log_ticket`` — set ONLY on the multi-card path, where every card tails the SAME
    shared drain log and must see only its own run's lines. The card's runhead then carries
    ``data-log-ticket="<id>"`` (the runlog JS appends ``&ticket=`` to the stream URL from the
    first/newest card's attribute) and the open-log link carries ``&ticket=<id>`` as well.
    Default None → the single-run path emits neither, keeping that output unchanged.
    """
    if not run:
        return ('<div class=runempty><div class=dot2></div>'
                'No runs yet for this project. Launch one from the bar above.</div>')
    # EU-55 / F12: when a run is live, inject a scoped rule that highlights the connector
    # segment going right from the active ("now") phase dot — gradient from the active
    # colour into grey so the eye lands on the right segment, not just the pulsing dot.
    # Placed here (not the CSS block) so the rule is only emitted when the bar is live.
    now_css = ""
    if run.get("live") and run.get("reached", -1) < len(run.get("phases") or []):
        now_css = (
            '<style>.phasebar .ph.now::after{'
            'background:linear-gradient(90deg,var(--warn),var(--line2));opacity:.45}'
            '</style>'
        )
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
    # 2026-07-19 (Commander order): the verdict must tell the CURRENT story, not a stale fact —
    # a live run whose latest review FAILED is rebuilding with the feedback, so say exactly that.
    _v = str(run.get("verdict") or "")
    if _v and run.get("live") and ("FAIL" in _v.upper() or "REJECT" in _v.upper()):
        try:
            _next_pass = int(run.get("passes") or 1) + 1
        except (TypeError, ValueError):
            _next_pass = 2
        verdict = (f'<span class=meta>review pass {_esc(run["passes"])} <b>FAIL</b> '
                   f'&#8594; rebuilding with the feedback (pass {_next_pass})</span>')
    else:
        verdict = (f'<span class=meta>verdict <b>{_esc(_v)}</b></span>' if _v else "")
    extra = ""
    if run["live"] and elapsed:
        extra += f'<span class=meta>elapsed <b>{_esc(elapsed)}</b></span>'
    # $ cost is meaningless on the Max plan — only show it when actually billing via an API key.
    if run.get("cost") and os.environ.get("ANTHROPIC_API_KEY"):
        extra += f'<span class=meta>cost <b>${run["cost"]:.2f}</b></span>'
    # EU-76: trend sparkline — pass-count history rendered below the run-meta row.
    spark_html = ""
    sparkline = run.get("sparkline") or []
    if len(sparkline) >= 2:
        svg = _sparkline_svg(sparkline)
        if svg:
            lbl = f"last {len(sparkline)} runs"
            tip = "passes per run · ↓ fewer passes = cleaner tickets"
            spark_html = (
                '<div style="display:flex;align-items:center;gap:10px;margin-top:12px;'
                'padding-top:11px;border-top:1px solid var(--line)">'
                '<span style="font-size:10.5px;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:.08em;color:var(--faint)" title="{_esc(tip)}">'
                'passes trend</span>'
                f'{svg}'
                f'<span style="font-family:var(--mono);font-size:10.5px;color:var(--dim)">'
                f'{_esc(lbl)}</span>'
                '</div>'
            )
    # EU-487: per-card drain-log filter attribute — present only when the multi-card path
    # passes THIS card's ticket id. The runlog JS reads it off the newest card and scopes
    # the shared stream to it; the single-run path never sets it, so its HTML is unchanged.
    log_ticket_attr = f' data-log-ticket="{_esc(log_ticket)}"' if log_ticket else ""
    # ── Panel header: hero-style when live, compact when idle ─────────────────
    app_span = f'<span class=runtapp>{_esc(run["app"])}</span>' if run["app"] else ""
    if run["live"]:
        # EU-76 merged hero: promote ticket ID + current phase to a hero-level header directly
        # inside the Active Run panel.  No separate .hero card above — this IS the hero.
        # Reuses .hgdot and .hgchip (already defined in the CSS block) for visual consistency.
        phases_list = run.get("phases") or []
        reached = run.get("reached", 0)
        phase_name = (phases_list[reached] if reached < len(phases_list)
                      else (phases_list[-1] if phases_list else "—"))
        modechip = ('<span class="hgchip live">live → DEV</span>' if mode == "live"
                    else '<span class="hgchip dry">dry-run · no changes</span>' if mode == "dry"
                    else "")
        runhead = (
            f'<div class="runhead runlive"{log_ticket_attr}>'
            '<div style="display:flex;align-items:center;gap:12px;min-width:0">'
            '<span class=hgdot></span>'
            '<div style="min-width:0">'
            f'<div class=runtitle><span style="font-family:var(--mono)">{_esc(run["ticket"])}</span>'
            f'{app_span}</div>'
            f'<div class=runsub>Working &middot; <b>{_esc(phase_name)}</b>'
            f'{(" &nbsp;" + modechip) if modechip else ""}</div>'
            '</div></div>'
            f'<div style="display:flex;gap:7px;align-items:center">{stop}</div>'
            '</div>'
        )
    else:
        # Idle: compact header — ticket ID + outcome chip + "last run" badge.
        oc = run.get("outcome") or ""
        otone = {"merged→dev": "ok", "errored": "bad", "awaiting decision": "warn",
                 "escalated": "warn", "PR / needs you": "warn"}.get(oc, "muted")
        olabel = {"merged→dev": "merged → DEV", "errored": "errored", "escalated": "escalated",
                  "PR / needs you": "PR — needs you", "awaiting decision": "needs you",
                  "running": "interrupted"}.get(oc, oc or "—")
        chip = f'<span class="b {otone}">{_esc(olabel)}</span>'
        runhead = (
            f'<div class=runhead{log_ticket_attr}>'
            f'<div class=runtitle><span style="font-family:var(--mono)">{_esc(run["ticket"])}</span>'
            f'{app_span}</div>'
            f'<div style="display:flex;gap:7px;align-items:center">'
            f'{chip}<span class="b muted">last run</span></div></div>'
        )
    # EU-130: Render triage state UI when the run is in prebuild triage phase.
    # This takes precedence over the normal phase bar when triage_phase is set,
    # ensuring the card shows the live triage verdict (ANSWER/CLOSE/REFILE)
    # instead of a stale pipeline display.
    triage_html = ""
    triage_phase = run.get("triage_phase")
    if triage_phase:
        # Build the triage status UI
        verdict = run.get("triage_verdict", "UNKNOWN")
        verdict_tone = "warn"  # default amber for in-triage
        status_label = "Triaging"
        status_detail = f"verdict: {_esc(verdict)}"

        if triage_phase == "closed":
            verdict_tone = "ok" if verdict == "ANSWER" else "muted"
            status_label = "Closed"
            reason = run.get("triage_reason", "")
            if reason:
                # Show both verdict and reason
                status_detail = f"{_esc(verdict)} &mdash; {_esc(reason)}"
            else:
                status_detail = f"{_esc(verdict)}"
        elif triage_phase == "refiled":
            verdict_tone = "info"
            status_label = "Refiled"
            new_tickets = run.get("triage_new_tickets", [])
            if new_tickets:
                ticket_list = ", ".join(_esc(t.get("key", "")) for t in new_tickets[:3])
                status_detail = f"new tickets: {ticket_list}"
                if len(new_tickets) > 3:
                    status_detail += f" +{len(new_tickets) - 3}"
        elif triage_phase == "triaging":
            verdict_tone = "warn"
            status_label = "Triaging"
            status_detail = f"verdict: {_esc(verdict)}"

        triage_html = (
            f'<div class="phasebar triage-bar">'
            f'<div class="ph triage-done"><span></span>Prebuild</div>'
            f'<div class="ph triage-now"><span></span>{_esc(status_label)}</div>'
            f'</div>'
            f'<div class=runmeta>'
            f'<span class="meta triage-meta" style="color:var(--{verdict_tone})">'
            f'<b>{_esc(status_label)}</b> &mdash; {status_detail}'
            f'</span></div>'
        )
        # When in triage, don't show the normal phase bar or metadata
        # (prevents showing stale 'Working · Land' for already-merged tickets)
        bar = []
        verdict = ""
        extra = ""
        spark_html = ""

    # EU-106: per-run 'open log' link — shown only when log_path is set by the BE subtask.
    # Rendered as a small anchor right after the phase bar so it's near the run context.
    log_link = ""
    if log_path:
        # EU-487: the card's own ticket id rides along as the log's filter param, same as
        # the runhead attribute — the shared drain log is scoped per card, never split.
        ticket_q = f"&ticket={quote(str(log_ticket))}" if log_ticket else ""
        # 2026-07-19: fetch(), never navigate — the same fix as the toolbar Open-logs button;
        # as a plain link this replaced the cockpit tab with the endpoint's raw JSON.
        log_link = (
            f'<div style="margin-top:9px;padding-bottom:2px">'
            f'<a href="/api/open-logs?path={quote(str(log_path))}{ticket_q}" '
            f'onclick="fetch(this.href);return false" '
            f'style="font-size:11.5px;color:var(--info);font-family:var(--mono);font-weight:600" '
            f'title="Open this run log in Finder">&#128194; open log</a></div>'
        )

    # Build the phase bar and metadata based on whether we're in triage or normal run
    if triage_html:
        phasebar_content = triage_html
        runmeta_content = ""
    else:
        phasebar_content = f'<div class="phasebar{"" if run["live"] else " idle"}">{"".join(bar)}</div>'
        runmeta_content = f'<div class=runmeta><span class=meta>pass <b>{_esc(run["passes"])}</b></span>{verdict}{extra}<span class=meta>branch <span class=mono>{_esc(run["branch"] or "—")}</span></span></div>'

    return (
        f'{now_css}'
        f'{runhead}'
        f'{phasebar_content}'
        f'{log_link}'
        f'{runmeta_content}'
        f'{spark_html}')


def _log_html(lines) -> str:
    if not lines:
        return ('<div class=logempty>No live output yet — start a run and the unit\'s '
                'steps stream here.</div>')
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
    """The Needs-you panel body for the cockpit's side column — a compact preview of the inbox.

    EU-102: iterates the unified ``rows`` list from needs.summary(); each row carries a
    ``category`` badge (decision | errored | parked | pr | proposal) and a
    human ``why`` string.  Gated on ``rows`` — the same list the badge counts — so the panel is never
    "All clear" while the badge is non-zero, and never non-empty while the badge is zero.
    """
    if not ns.get("rows"):
        return ('<a class=needsok href="/needs"><span class=nok>&#10003;</span> '
                'All clear — nothing needs you</a>')
    # Map category → dot tone so the visual urgency still matches the kind of item. Unknown
    # categories fall back to "warn" via .get(cat, "warn") below.
    _CAT_TONE = {
        "decision": "warn",
        "errored": "bad",
        "parked": "warn",
        "pr": "warn",
        "proposal": "warn",
    }
    rows = []
    for row in (ns.get("rows") or [])[:8]:
        cat = str(row.get("category") or "")
        rid = _esc(str(row.get("ticket_id") or row.get("id") or ""))
        why = _esc(str(row.get("why") or cat or "needs you"))[:80]
        tone = _CAT_TONE.get(cat, "warn")
        meta = f"{rid} &middot; {_esc(cat)}" if rid else _esc(cat)
        rows.append(
            f'<a class=needrow href="/needs"><span class="nd {tone}"></span>'
            f'<div class=ndmain><div class=ndt>{why}</div>'
            f'<div class=ndr>{meta}</div></div></a>'
        )
    return "".join(rows) + '<a class=needall href="/needs">Open inbox &#8594;</a>'


_TALK_HTML = (
    '<div class=talk>'
    '<a class=talkbtn href="/chat"><span class=tki>&#128172;</span>'
    '<div class=tkbody><b>CTO</b><i>ask the orchestrator 1:1</i></div>'
    '<span class=tkarrow>&#8250;</span></a>'
    '<a class=talkbtn href="/group"><span class=tki>&#128101;</span>'
    '<div class=tkbody><b>Group room</b><i>convene all the engineers</i></div>'
    '<span class=tkarrow>&#8250;</span></a>'
    '</div>')


def _md_to_html(text: str) -> str:
    """Convert light markdown in engineer text to HTML.

    Lines starting with '• ' or '- ' are grouped into <ul class=fbullets><li>
    elements so bullet-formatted engineer reports render as proper lists rather
    than a wall of plain text. Other line-breaks become <br>. Blank lines flush
    the current paragraph/bullet group.
    """
    if "\n" not in text and not text.startswith(("• ", "- ")):
        return _esc(text)
    out: list[str] = []
    bullets: list[str] = []
    prose: list[str] = []

    def _emit_prose() -> None:
        if prose:
            if out:
                out.append("<br>")
            out.append("<br>".join(_esc(s) for s in prose))
            prose.clear()

    def _emit_bullets() -> None:
        if bullets:
            lis = "".join(f"<li>{_esc(b)}</li>" for b in bullets)
            out.append(f"<ul class=fbullets>{lis}</ul>")
            bullets.clear()

    for line in text.splitlines():
        s = line.strip()
        if s.startswith(("• ", "- ")):
            _emit_prose()
            bullets.append(s[2:].strip())
        elif s:
            _emit_bullets()
            prose.append(s)
        else:                   # blank line — flush both buffers
            _emit_prose()
            _emit_bullets()

    _emit_prose()
    _emit_bullets()
    return "".join(out)


def _render_feed_text(text: str) -> str:
    """Render a feed item's text field as HTML with markdown bullet support.

    Feed text has the shape "label" or "label — body". The em-dash prefix
    (the tone label) is kept as plain escaped text; only the body after the
    first ' — ' separator gets markdown conversion via _md_to_html().
    """
    _SEP = " — "   # " — "
    if _SEP in text:
        prefix, _, body = text.partition(_SEP)
        return _esc(prefix) + _esc(_SEP) + _md_to_html(body)
    return _md_to_html(text)


def _feed_html(items: list[dict]) -> str:
    if not items:
        return '<div class=muted style="padding:14px">No activity yet.</div>'
    out = []
    for it in items:
        tk = (f'<span class=mono>{_esc(it["ticket"])}</span> ' if it["ticket"] else "")
        out.append(
            f'<div class=fitem><span class="fd {it["tone"]}"></span>'
            f'<div class=fbody>{tk}{_render_feed_text(it["text"])}'
            f'<div class=fmeta>{_esc(it["app"])}{" · " if it["app"] else ""}{_esc(it["ago"])}</div></div></div>')
    return "".join(out)


# What the live-log placeholder calls the CURRENT phase. Deliberately NOT phases.PHASES: that tuple
# is ("Build","Gate","Review","Land") — the *stage bar's* model, which has no PLANNING entry at all,
# even though the Planner routinely runs 11+ minutes (EU-440: ticket_start 14:27:18 -> first planner
# line 14:38:52). The Commander asked "if it's planning I'm supposed to see 'planning', no?" — and he
# was right: there was nowhere for that word to come from.
_PHASE_BY_EVENT = {
    "ticket_start": "Planning", "prior_attempts_injected": "Planning",
    "ticket_transition": "Planning", "squad_selected": "Planning",
    "planner": "Building", "build": "Building", "pm_review": "Building",
    "pm_decided": "Building", "agent_call": None,     # None = uninformative, keep looking back
    "gate": "Gate", "deterministic_gate": "Gate", "dev_gate": "Gate",
    "review": "Review",
    "land_pushed": "Landing", "merged": "Landed",
}


def _live_phase(audit_path: str | Path, ticket_id: str | None = None) -> str:
    """Best-effort label for what the run is doing RIGHT NOW, read from the audit tail.

    The run state carries no phase field (see cockpit_state's blank dict — active/last_activity/
    run_started/log_path and no more), so the first version of the placeholder asked it for one and
    silently got nothing: the panel rendered "3m 34s elapsed" with no stage at all. The audit is the
    one place that actually knows.

    ``ticket_id`` scopes the lookup to ONE ticket. Under a concurrent drain (N=2) two tickets emit
    phase events into the same audit, so the unscoped "newest phase event anywhere" showed the OTHER
    ticket's stage on this panel — the Commander saw "Gate" over EU-444 while EU-444 was still
    building and EU-443 had reached its gate. With a ticket_id we only count that ticket's events;
    without one (a single-run cockpit) the old newest-anywhere behaviour is kept.

    Walks BACKWARDS to the newest event that names a phase, skipping uninformative ones (agent_call
    fires for every engineer). Returns "" when nothing is recognisable — the caller then just omits
    the stage rather than guessing."""
    try:
        p = Path(audit_path)
        if not p.exists():
            return ""
        with p.open("rb") as fh:                      # bounded tail — this runs on every board render
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 60_000))
            lines = fh.read().decode("utf-8", "replace").splitlines()[-200:]
        import json as _json
        for line in reversed(lines):
            try:
                rec = _json.loads(line)
            except (ValueError, TypeError):
                continue
            if ticket_id and str(rec.get("ticket_id") or "") != str(ticket_id):
                continue                              # not this panel's ticket — skip under concurrency
            label = _PHASE_BY_EVENT.get(rec.get("event"))
            if label:
                return label
    except Exception:  # noqa: BLE001 — a status label must never break the board
        return ""
    return ""


def _runlog_placeholder(state: dict, active: bool, audit_path: str = "./state/audit.jsonl",
                        ticket_id: str | None = None) -> str:
    """What the live-log panel shows BEFORE any output arrives (2026-07-22).

    The panel used to render a bare "Waiting for run output…" and sit on it. That is honest but
    useless: a run's first ~10 minutes are genuinely silent — the Planner makes ONE long model call
    and prints nothing until it returns (EU-440: ticket_start 14:27:18, first planner line 14:38:52,
    11m30s of nothing). Indistinguishable from a broken panel, which is exactly how the Commander
    read it three separate times.

    So say what IS known instead of what is missing: the stage, how long the run has been going, and
    when the process last did anything. Every field already exists in the run state — this only
    stops throwing it away."""
    if not active:
        return "<div class=logempty>No active run.</div>"
    import time as _t
    bits: list[str] = []
    # the audit knows the phase; the run state does not carry one at all
    stage = _live_phase(audit_path, ticket_id)
    if stage:
        bits.append(f"<b>{_esc(stage)}</b>")
    started = state.get("run_started")
    if started:
        try:
            el = max(0, int(_t.time() - float(started)))
            bits.append(f"{el // 60}m {el % 60:02d}s elapsed")
        except (TypeError, ValueError):
            pass
    last = state.get("last_activity")
    if last:
        try:
            ago = max(0, int(_t.time() - float(last)))
            bits.append(f"last step {ago}s ago" if ago < 90
                        else f"last step {ago // 60}m ago")
        except (TypeError, ValueError):
            pass
    head = " &middot; ".join(bits) if bits else "run starting"
    return (f'<div class=logempty>&#9654; {head}<br><br>'
            f'<span style="opacity:.7">No output yet — an engineer\'s model call prints nothing '
            f'until it returns. The first pass is normally silent for several minutes.</span></div>')


def _liveness(state: dict, active: bool) -> str:
    """A heartbeat chip: green while the unit is printing steps, amber/red if it goes quiet —
    so you can tell 'working' from 'stuck' at a glance.

    Reads ``last_activity`` from the passed-in ``state`` snapshot.  EU-104 per-project scoping
    is the CALLER's responsibility: ``server._view_state(app)`` hands this function the tab's
    own ``get_state(app)`` snapshot, so ``state['last_activity']`` is already THIS project's
    heartbeat — never a different concurrent project's.  Reading the argument (rather than
    reaching into the global run-state) also keeps the chip a pure function of its inputs, the
    contract the EU-84 regression guard pins.
    """
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
    # EU-298: flag a footer whose newest peer publish is past STALE_SYNC_CUTOFF_S. The age text
    # was always there but rendered in the same neutral grey as a fresh sync, so "156h 38m ago"
    # read as current. The marker is deliberately textual as well as coloured — a colour-only
    # signal is invisible to colour-blind/monochrome readers. The stat() failure path below keeps
    # its original bare output: with no mtime there is no age to judge staleness against.
    cls = "synced"
    try:
        newest = max(f.stat().st_mtime for f in files)
        age = datetime.now().timestamp() - newest
        ago = " · " + _fmt_dur(age) + " ago"
        if age >= STALE_SYNC_CUTOFF_S:
            cls = "synced stale"
            ago += " · stale"
    except OSError:
        pass
    # The fresh case keeps its original unquoted single-class wrapper byte-for-byte; only the
    # stale case needs the quoted two-class form.
    wrap = "<div class=synced>" if cls == "synced" else f'<div class="{cls}">'
    return f'{wrap}&#8646; synced: {html.escape(peers)}{html.escape(ago)}</div>'


_BACKLOG_CACHE: dict[str, tuple[float, list]] = {}   # scope -> (fetched_ts, [(AppConfig, Ticket)])
_BACKLOG_TTL = 90.0   # the board re-renders every couple seconds; only hit Jira at most once/90s/scope

# EU-104: TTL cache for the per-ticket Jira status guard.  Each entry is (fetched_ts, status_str|None).
# The TTL is shorter than the backlog TTL so a ticket that lands in 'Done' is detected within half a
# minute — without hammering the Jira REST API on every 2s SSE board frame.
_TICKET_STATUS_CACHE: "dict[tuple, tuple[float, Optional[str]]]" = {}   # (app, key) -> (ts, status)
_TICKET_STATUS_TTL = 30.0


def _ticket_done(cfg, app: Optional[str], ticket_key: Optional[str]) -> bool:
    """Return True if ``ticket_key`` is in a terminal Done/Closed status in Jira (EU-104 guard).

    TTL-cached per (app, key) so the SSE board renders don't hit Jira on every 2s frame.
    Best-effort: any error (missing backlog, bad creds, network) returns False so the board
    never breaks — the active flag + run-loop correctly clear state on normal termination;
    this guard is the backstop for interrupted/crashed runs whose Jira ticket is already done.
    Only checks apps with ``backlog_backend: jira``; all other backends return False immediately.
    """
    if not app or not ticket_key or ticket_key in ("—", ""):
        return False
    cache_key = (app, ticket_key)
    now = time.time()
    hit = _TICKET_STATUS_CACHE.get(cache_key)
    if hit is not None and now - hit[0] < _TICKET_STATUS_TTL:
        status = hit[1]
    else:
        status = None
        try:
            app_cfg = cfg.app(app)
            if getattr(app_cfg, "backlog_backend", None) == "jira":
                from .backlog.base import make_backlog
                bl = make_backlog(app_cfg)
                # JiraAdapter._current_status fetches the Jira issue's status field.
                # getattr guard keeps us safe if a future adapter omits this private method.
                fn = getattr(bl, "_current_status", None)
                if fn is not None:
                    status = fn(ticket_key)
        except Exception:  # noqa: BLE001 — never let a Jira probe break the board
            pass
        _TICKET_STATUS_CACHE[cache_key] = (now, status)
    return bool(status and status.lower() in ("done", "closed"))


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
        f'<div class=blempty style="color:var(--bad,var(--bad))">&#9888; {_esc(n)} backlog unreachable — '
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


def _render_run_card_data(cfg, app, run, tasks, *, active, mode, manual):
    """Compute elapsed / stage / pass-count for a single run card.

    Extracted from ``render_board`` (EU-485).  Takes one scoped task dict (the newest run on
    the project), the full task list (passed through only because ``active_run`` derives the
    sparkline / triage detection from scoped history), and renders the same values the inline
    block produced — but now reusable when we later loop over multiple cards.

    Returns ``{run_obj, mode, elapsed, manual, active}`` so the caller can unpack exactly as
    before.  ``run_obj`` carries ``stage`` (``reached``), ``pass-count`` (``passes``,
    ``phases``, ``sparkline``) that downstream templates read directly.
    """
    # Elapsed: only when a run is genuinely in flight.  EU-147 uses task-specific start time.
    elapsed = None
    if active and run is not None and run.get("started"):
        started_dt = run["started"]
        if isinstance(started_dt, datetime):
            elapsed = _fmt_dur(datetime.now().timestamp() - started_dt.timestamp())
        elif isinstance(started_dt, str):
            parsed_ts = D._parse_ts(started_dt)
            if parsed_ts:
                elapsed = _fmt_dur(datetime.now().timestamp() - parsed_ts.timestamp())

    # --- keep these in sync with the caller's assignments ---
    # (mode and active are passed in; ghost-suppress may reset them below)

    run_obj = active_run(cfg, tasks, app, active)
    # EU-104: re-validate live ticket against Jira — suppress the ghost 'Working' card if
    # the ticket is already Done/Closed.
    if run_obj and run_obj.get("live") and _ticket_done(cfg, app, run_obj.get("ticket")):
        run_obj = dict(run_obj)       # shallow copy — never mutate the cached object
        run_obj["live"] = False
        active = False
        mode = None
        elapsed = None
        manual = False
    return {"run_obj": run_obj, "mode": mode, "elapsed": elapsed,
            "manual": manual, "active": active}


def render_board(cfg, app: Optional[str], state: dict) -> str:
    """Inner board (everything that updates on the poll).

    EU-106: lean board — only the Active Run panel (with _liveness chip + per-run open-log link),
    Needs-you, and Talk-to-the-unit are rendered.  The Live Feed, Activity, and Tickets-to-work
    panels have been retired; the _liveness heartbeat chip moved from the Live Feed header into
    the Active Run header so the health signal stays visible.
    """
    _ap = state.get("autopilot") or {}
    # EU-103: a per-project autopilot lights the board via THIS project's own run-state flag
    # (``state["autopilot_on"]`` — set when its autopilot loop claims the run), so a cockpit-started
    # per-project autopilot renders as autopilot, not a manual run, and only on its OWN tab.
    # EU-73: the legacy unit-wide flag + the live daemon PID check still cover the external-daemon case.
    ap_on = bool(state.get("autopilot_on") or _ap.get("on") or _ap.get("daemon_running", False))
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
    # EU-147: Use task-specific start time, not project-level run_started, so elapsed time
    # reflects only the current task's duration.
    ts = _scope(tasks, app)
    run = ts[0] if ts else None
    manual = bool(state.get("active")) and not ap_on

    # EU-486: loop over live runs to produce one Active-run card per concurrent build.
    # 0 or 1 live → TODAY's code path byte-for-byte (identity guaranteed).
    # 2+ live → per-ticket scoping: _render_run_card_data + _run_html per card.
    lives = live_runs(cfg, tasks, app, active)
    if len(lives) <= 1:
        # Single-run / idle path: unchanged from pre-EU-486.
        card = _render_run_card_data(cfg, app, run, tasks, active=active, mode=mode, manual=manual)
        run_obj = card["run_obj"]
        mode = card["mode"]
        elapsed = card["elapsed"]
        manual = card["manual"]
        active = card["active"]
        k = _kpi_html(kpis(cfg, tasks, app))
        log_path = state.get("log_path")
        run = _run_html(run_obj, mode, elapsed, manual, log_path=log_path)
    else:
        # Multi-card path: one panel per live ticket, ordered newest-first.
        manual = bool(state.get("active")) and not ap_on  # stop button policy
        # Build a ticket→tasks map so each card's helper sees only its own ticket's history.
        # This makes ``active_run`` derive THAT ticket's stage/passes/sparkline correctly
        # on boards with multiple concurrent cards (sparkline becomes ticket-scoped here).
        tid_tasks: dict[str, list[dict]] = {}
        for d in tasks:
            tid = str(d.get("ticket_id", ""))
            tid_tasks.setdefault(tid, []).append(d)

        k = _kpi_html(kpis(cfg, tasks, app))
        cards_html = []
        for lt in lives:
            ticket_tasks = tid_tasks.get(str(lt.get("ticket_id", "")), [lt])
            c = _render_run_card_data(cfg, app, lt, ticket_tasks,
                                      active=True, mode=mode, manual=manual)
            # EU-487: each card tails the SAME shared drain log, filtered to its OWN ticket
            # (the run obj's ticket — what the card actually shows — falling back to the
            # audit task's id). _run_html emits the per-card data-log-ticket attribute from
            # this; the single-run path above passes nothing, staying byte-identical.
            card_ticket = str((c["run_obj"] or {}).get("ticket")
                              or lt.get("ticket_id") or "") or None
            cards_html.append(_run_html(c["run_obj"], c["mode"], c["elapsed"],
                                        c["manual"], log_path=state.get("log_path"),
                                        log_ticket=card_ticket))
        run = "\n".join(cards_html)

    # EU-76 dedup: the live run was rendered TWICE on the board — once as the top `_hero_html`
    # headline and again in the "Active run" panel below it (same ticket/phase/elapsed/pass). The
    # Active-run panel is the canonical slot: it carries the EU-55/F12 phase bar and the pass-trend
    # sparkline, and it renders in BOTH the live and idle (last-run) states, so the hero was pure
    # duplication. Keep the single panel; `_hero_html` stays a public helper (unit-tested directly)
    # but is no longer emitted here.
    # EU-106: _liveness chip moves from the retired Live Feed header into the Active Run header
    # so the heartbeat stays visible.  EU-84 contract is preserved: _liveness reads THIS tab's
    # own per-app snapshot (passed as `state`), never a different project's heartbeat.
    liveness = _liveness(state, active)
    # EU-106: name the project on its status panel.  The lean board is per-project (one tab = one
    # project — EU-63/EU-64), so the Active-run panel header carries the project label.  This is also
    # what makes a per-tab board identifiable in the HTML, the invariant the EU-63 per-tab board tests
    # pin (``/api/board?app=<proj>`` must render THAT project's board).  Omitted when app is None
    # (the legacy all-projects render path used by some older tests).
    proj_tag = f'<span class=boardproj>{_esc(app)}</span>' if app else ""
    # EU-200: Get the current run log path for the live log panel
    log_stream_path = state.get("log_path")
    # 2026-07-22: a CONCURRENT drain never sets state['log_path'] — loop.py's EU-380 branch writes
    # a pointer note instead of opening a per-ticket handle. That rendered data-log-path="", and the
    # panel's JS bails on an empty attribute (`if(!logPath){...return;}`) WITHOUT ever opening the
    # EventSource — so the operator watched "Waiting for run output…" for the whole run while the
    # build streamed to the process stdout the note points at. The server-side fallback in
    # /api/run-log-stream could never help, because the browser never connected. Point the attribute
    # at the shared drain stream so the panel connects; the endpoint then tails it from its current
    # end. Per-project log files are EU-444 — this makes the panel honest in the meantime.
    if not log_stream_path:
        try:
            from . import run_logger as _rl
            log_stream_path = _rl.drain_log_path() or ""
        except Exception:  # noqa: BLE001 — the board must render even if the probe fails
            log_stream_path = ""

    return (
        f'<div class=kpis>{k}</div>'
        f'{_sync_html(cfg)}'
        '<div class=cols>'
        f'<div class=col-main>'
        f'<section class=panel>'
        f'<div class=ph>Active run{proj_tag}{liveness}</div>'
        f'<div class=run>{run}</div></section>'
        f'<section class=panel>'
        f'<div class=ph>&#128190; Live run log</div>'
        f'<div class=runlog id=runlog data-log-path="{_esc(str(log_stream_path or ""))}">'
        f'{_runlog_placeholder(state, active, getattr(cfg, "audit_path", "./state/audit.jsonl"), (ts[0].get("ticket_id") if ts else None))}</div></section>'
        '</div>'
        f'<div class=col-side>'
        # EU-297: proof-of-integration call site for the cockpit_views._card partial —
        # consistent --s-* padding / --r-xl radius / --surface background instead of a
        # hand-rolled <section class=panel><div class=ph>…</div>…</section> pair.
        f'{CV._card("Talk to the unit", _TALK_HTML)}'
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
    if warns:
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


def total_live_run_count(cfg=None, tasks: Optional[list[dict]] = None) -> int:
    """The true concurrent-BUILD total across every active project, computed on demand (EU-479).

    The header badge's number.  For each app with a claimed run
    (``cockpit_state.active_runs()``) this sums ``len(live_runs(cfg, tasks, app, True))``
    — so two tickets building concurrently in the SAME project under
    ``max_concurrent_builders ≥ 2`` count as 2, not the flat per-app boolean of
    ``active_run_count()``.  A claimed run always contributes at least 1 (the run exists
    even before its first audit task lands), so the common cross-app case — N distinct
    projects with one run each — sums to exactly N, the pre-EU-479 total (no regression).

    Deliberately cache-free: the total is recomputed from live audit + run-state on every
    call, independent of which project's board tab happens to be open or polling.  Cheap
    in practice — ``D.audit_lines`` / ``D.load_tasks`` are TTL/mtime-cached, so each
    per-app ``live_runs`` pass reuses the same in-memory lines the board render reads,
    and the app count is bounded by the max-parallel-runs cap.  ``cfg=None`` (legacy /
    test call sites without a config) degrades to ``active_run_count()`` — the old
    distinct-project count.
    """
    from . import cockpit_state as _cs
    keys = _cs.active_runs()
    if not keys:
        return 0
    if cfg is None:
        return len(keys)
    if tasks is None:
        tasks = D.load_tasks(getattr(cfg, "audit_path", "./state/audit.jsonl"))
    return sum(max(1, len(live_runs(cfg, tasks, key, True))) for key in keys)


def autopilot_switch(state: dict, app: Optional[str], healthy: bool,
                     cfg=None, tasks: Optional[list[dict]] = None) -> str:
    """Header roll-up: concurrent-run count badge across all projects (EU-103, EU-479).

    The per-project start/stop controls now live in the control bar (cockpit_views._control_bar).
    This header slot shows a system-wide summary — a pulsing green badge when any build is
    running, or nothing when all are idle — with a dual count: actual concurrent BUILDS
    (``total_live_run_count``, computed on demand over every active app) plus the distinct
    PROJECT count (``active_run_count``).  The split keeps the label honest under same-app
    concurrency: two tickets building in one project read "2 builds running · 1 project",
    never "2 projects", while two projects with one run each still read "2 … 2 projects".

    ``cfg`` (passed by ``render_page``) enables the on-demand per-app ``live_runs`` tally;
    without it the badge degrades to the legacy distinct-project count.  ``state``, ``app``
    and ``healthy`` are kept for call-site compatibility but are no longer used in the output.
    """
    try:
        n = total_live_run_count(cfg, tasks)  # EU-479: true total, incl. same-app concurrency
    except Exception:  # noqa: BLE001 — the badge must never break a page render
        n = 0
    if n == 0:
        return ""
    try:
        from . import cockpit_state as _cs
        p = _cs.active_run_count()     # distinct projects drive the project-side label
    except Exception:  # noqa: BLE001
        p = n
    bld = "build" if n == 1 else "builds"
    prj = "project" if p == 1 else "projects"
    return (f'<div class="apsw on" title="{n} {bld} running · {p} {prj}">'
            f'<span class="apdot on"></span>'
            f'<span class=aplabel>{n}&nbsp;{html.escape(bld)}&nbsp;running&nbsp;&middot;&nbsp;{p}&nbsp;{html.escape(prj)}</span>'
            '</div>')


def _host_tag(cfg) -> str:
    """A small pill in the header naming the machine this cockpit runs on, so the Mac cockpit and the
    tunnelled server cockpit (both served on localhost:8787) are instantly tellable apart."""
    try:
        from . import sync
        h = sync.host_id(cfg)
    except Exception:  # noqa: BLE001
        return ""
    return f'<span class=hosttag title="this cockpit is running on this machine">{html.escape(h)}</span>'


def render_page(cfg, app: Optional[str], state: dict, control_bar: str, health: dict) -> str:
    """Render the full War Room page.

    EU-106: ``log_lines`` parameter removed — the Live Feed panel is gone and logs are
    accessed via the per-run 'open log' link in the Active Run panel.
    """
    return (_PAGE
            .replace("{{HOST}}", _host_tag(cfg))
            .replace("{{PROJ}}", project_selector(cfg, app))
            .replace("{{AUTOPILOT}}", autopilot_switch(state, app, health.get("healthy", False), cfg=cfg))
            .replace("{{HEALTHPILL}}", health_pill(health))
            .replace("{{HEALTHBAR}}", health_banner(health))
            .replace("{{BAR}}", control_bar)
            .replace("{{BOARD}}", render_board(cfg, app, state))
            .replace("{{APP}}", _esc(app or "*"))
            .replace("{{GEN}}", datetime.now().strftime("%H:%M:%S")))


_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>SQUAD — HQ</title>
<style>
/* ── DESIGN TOKENS (EU-39) ───────────────────────────────────────────────────────
   The single source of truth for the cockpit's look. Every surface below — and the
   other cockpit pages — pulls colour, radius, elevation, ring and motion from here,
   so a re-skin is one edit in this block, never a hunt through scattered literals. */
:root{color-scheme:dark;
/* palette */
--bg:#080a0f;--panel:#0f141d;--panel2:#141a25;--line:#1b2230;--line2:#283342;
--ink:#e7ebf2;--dim:#7e8795;--faint:#515a67;
--ok:#34d399;--okbg:#0e2a1e;--okline:#1c5238;
--warn:#f5b34a;--warnbg:#2c2410;--warnline:#5a4a1c;
--bad:#f0676b;--badbg:#2a1417;--badline:#5a1f22;
--info:#6aa9ff;--infobg:#0a1f2e;--infoline:#1a3a5c;--accent:#4d7cff;--accentbg:#0f1c30;--accentline:#1e3457;--brand:#ff7a59;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
/* corner radii */
--r-sm:6px;--r-md:9px;--r-lg:13px;--r-xl:14px;--r-pill:999px;
/* elevation (board-surface depth hierarchy) */
--shadow-1:0 1px 2px rgba(0,0,0,.35);
--shadow-2:0 8px 24px rgba(0,0,0,.45);
--shadow-3:0 16px 40px rgba(0,0,0,.55);
/* keyboard-focus ring + motion — shared so focus & transitions are uniform (a11y) */
--ring:0 0 0 2px var(--bg),0 0 0 4px rgba(77,124,255,.6);
--t-fast:.15s ease;
/* 8pt spacing scale (EU-296) — additive foundation; nothing is rewired to consume these yet */
--s-1:4px; /* micro gap: icon-to-label spacing, tight inline gaps */
--s-2:8px; /* small gap: control padding, chip/tag spacing */
--s-3:16px; /* base gap: card padding, row gaps, standard margins */
--s-4:24px; /* medium gap: section padding, panel gutters */
--s-5:32px; /* large gap: section margin, major block spacing */
--s-6:48px; /* xl gap: page-level section margin, hero spacing */
/* modular type scale (EU-296) — will replace raw inline font-size numerals (e.g. the
   30/40px KPI numerals) in a later slice; additive only for now */
--t-xs:11px; /* micro labels, meta text, tags, timestamps */
--t-sm:12.5px; /* secondary body text, chips, list meta */
--t-md:14px; /* base body copy (matches body font-size) */
--t-lg:18px; /* section/run titles */
--t-xl:24px; /* group headers */
--t-2xl:32px; /* hero KPI numerals (e.g. the 30-40px inline KPI stat figures) */
/* semantic color-role aliases (EU-296) — map onto the existing palette so consumers
   read intent, not raw color; --warn already exists above and is reused as-is */
--surface:var(--panel); /* default elevated surface background */
--border:var(--line); /* default hairline border */
--text:var(--ink); /* default body text color */
--positive:var(--ok); /* success / good-state accent */
--critical:var(--bad)} /* error / bad-state accent */
/* deep-inset backgrounds that were hardcoded hexes before the 2026-07-19 theme pass */
:root{--well:#0d1119;--console:#070a0e;--console-ink:#b9c2cf;--accent-hover:#2f5ce0}
/* ── LIGHT THEME (2026-07-19) — toggled via <html data-theme=light>; persisted in
   localStorage('ui.theme') by the header toggle; every page consumes these through
   cockpit_views._token_css() so the whole cockpit follows one switch. ── */
:root[data-theme=light]{color-scheme:light;
--bg:#eef1f6;--panel:#ffffff;--panel2:#f2f4f9;--line:#dde3ec;--line2:#c7d1e0;
--ink:#1c2536;--dim:#5a6578;--faint:#8b95a7;
--ok:#0f9d63;--okbg:#e2f5ec;--okline:#aadfc6;
--warn:#a8720f;--warnbg:#faf0d9;--warnline:#e8d5a5;
--bad:#cf3a40;--badbg:#fae5e6;--badline:#efbfc1;
--info:#2563c9;--infobg:#e7effc;--infoline:#c2d6f3;
--accent:#3b62d9;--accentbg:#e8edfb;--accentline:#c4d1f1;--brand:#e8590c;
--shadow-1:0 1px 2px rgba(23,32,54,.08);
--shadow-2:0 8px 24px rgba(23,32,54,.12);
--shadow-3:0 16px 40px rgba(23,32,54,.16);
--ring:0 0 0 2px var(--bg),0 0 0 4px rgba(59,98,217,.5);
--well:#e7ebf3;--console:#f7f9fc;--console-ink:#33415c;--accent-hover:#2f54c4}
/* END THEME TOKENS */
*{box-sizing:border-box}
/* Keyboard focus is visible on every interactive board surface (a11y): mouse clicks
   stay clean (:focus-visible), but Tab navigation lands on a clear accent ring. */
a.kpi:focus-visible,.offrow:focus-visible,.blrow:focus-visible,.blmore:focus-visible,
.needrow:focus-visible,.needsok:focus-visible,.needall:focus-visible,.talkbtn:focus-visible,
.recheck:focus-visible,.hbbtn:focus-visible,.apbtn:focus-visible,.stopbtn:focus-visible,
.fitem a:focus-visible,select:focus-visible,summary:focus-visible{outline:none;box-shadow:var(--ring)}
body{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;margin:0;color:var(--ink);
background:radial-gradient(1100px 440px at 80% -10%,rgba(77,124,255,.10),transparent 60%),
radial-gradient(820px 320px at 4% -6%,rgba(52,211,153,.045),transparent 55%),var(--bg);background-attachment:fixed}
a{color:var(--info);text-decoration:none}
.mono{font-family:var(--mono);font-size:.85em}
.muted{color:var(--dim)}
button{font:inherit}
/* header */
header{display:flex;align-items:center;gap:14px;padding:14px 26px;border-bottom:1px solid var(--line);
background:linear-gradient(180deg,var(--panel),var(--bg));position:sticky;top:0;z-index:5;flex-wrap:wrap}
.brand{font-size:15px;font-weight:750;letter-spacing:.4px;white-space:nowrap;text-transform:uppercase}
.hosttag{margin-left:10px;font-size:10.5px;font-weight:700;color:var(--dim);background:var(--panel2);border:1px solid var(--line2);border-radius:999px;padding:2px 9px;vertical-align:middle;letter-spacing:.06em;text-transform:lowercase}
.brand b{color:var(--brand)}
.brand .bmark{color:var(--brand)}
header select{background:var(--well);border:1px solid var(--line2);color:var(--ink);border-radius:9px;
padding:8px 12px;font:inherit;cursor:pointer}
.spacer{flex:1}
.gen{font-family:var(--mono);font-size:11px;color:var(--faint);letter-spacing:.02em}
.sdot{display:inline-block;width:7px;height:7px;border-radius:99px;background:var(--faint);margin-right:5px;vertical-align:middle}
.sdot.on{background:var(--ok);box-shadow:0 0 7px var(--ok);animation:pulse2 1.6s infinite}
.sdot.off{background:var(--warn)}
/* health pill */
.hpill{font-size:12px;font-weight:700;padding:6px 13px;border-radius:99px}
.hpill.ok{color:var(--ok);background:var(--okbg);border:1px solid var(--okline)}
.hpill.bad{color:var(--bad);background:var(--badbg);border:1px solid var(--badline)}
.hd{position:relative}.hd>summary{list-style:none;cursor:pointer}
.hd>summary::-webkit-details-marker{display:none}
.hpanel{position:absolute;top:calc(100% + 8px);right:0;z-index:40;min-width:320px;background:var(--panel);
border:1px solid var(--line2);border-radius:var(--r-lg);padding:10px;box-shadow:var(--shadow-3)}
.hpi{font-size:12.5px;color:var(--dim);padding:5px 4px}
.recheck{margin-top:9px;background:var(--line);border:1px solid var(--line2);color:var(--ink);border-radius:8px;
padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer}
/* autopilot switch */
.apsw{display:flex;align-items:center;gap:9px;margin:0;padding:5px 6px 5px 13px;border:1px solid var(--line2);
border-radius:99px;background:var(--well)}
.apsw.on{border-color:var(--okline);background:var(--okbg)}
.apdot{width:8px;height:8px;border-radius:99px;background:var(--faint)}
.apdot.on{background:var(--ok);animation:pulse2 1.3s infinite}
.aplabel{font-size:12px;color:var(--ink)}
.apbtn{border:0;border-radius:99px;padding:6px 13px;font-size:12px;font-weight:700;cursor:pointer}
.apbtn.start{background:var(--accent);color:#fff}
.apbtn.start:disabled{background:var(--line);color:var(--faint);cursor:not-allowed}
.apbtn.stop{background:var(--bad);color:#fff}
.apbtn.drain{background:var(--warn);color:#1a1205;margin-right:7px}
/* health banner */
.healthbar{padding:13px 26px}
.healthbar.ok{background:linear-gradient(180deg,rgba(16,42,29,.55),transparent);border-bottom:1px solid var(--okline)}
.healthbar.bad{background:linear-gradient(180deg,rgba(42,20,22,.6),transparent);border-bottom:1px solid #3a1a1c}
.hbrow{display:flex;align-items:center;gap:14px}
.hbtitle{display:flex;align-items:center;gap:11px;font-weight:650;font-size:14px;flex:1}
.healthbar.ok .hbtitle{color:var(--ok)}.healthbar.bad .hbtitle{color:var(--bad)}
.hbdot{width:11px;height:11px;border-radius:99px;flex:none}
.healthbar.ok .hbdot{background:var(--ok);box-shadow:0 0 0 4px rgba(58,209,127,.13)}
.healthbar.bad .hbdot{background:var(--bad);animation:pulse3 1.4s infinite}
@keyframes pulse3{0%,100%{box-shadow:0 0 0 0 rgba(240,103,107,.45)}50%{box-shadow:0 0 0 8px rgba(240,103,107,0)}}
.hbactions{display:flex;align-items:center;gap:12px}
.themebtn{background:var(--panel2);border:1px solid var(--line2);color:var(--dim);width:34px;height:34px;
border-radius:var(--r-md);font-size:16px;cursor:pointer;line-height:1;flex:none;margin-right:10px;
transition:color var(--t-fast),border-color var(--t-fast)}
.themebtn:hover{color:var(--ink);border-color:var(--accent)}
.models{font-size:11px;color:var(--faint)}
.hbbtn{background:var(--line);border:1px solid var(--line2);color:var(--ink);border-radius:8px;padding:6px 13px;
font-size:12px;font-weight:600;cursor:pointer}
.hbissues{margin:11px 0 2px;padding:0;list-style:none;display:grid;gap:6px}
.hbissues li{font-size:12.5px;color:var(--dim)}
.tag{font-family:var(--mono);font-size:10px;font-weight:700;text-transform:uppercase;padding:2px 6px;border-radius:5px;margin-right:8px}
.tag.bad{background:var(--badbg);color:var(--bad)}.tag.warn{background:var(--warnbg);color:var(--warn)}
/* kpis */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;padding:22px 24px 8px}
.kpi{position:relative;background:var(--panel);border:1px solid var(--line);border-radius:var(--r-lg);padding:16px 17px;
overflow:hidden;box-shadow:var(--shadow-1);transition:border-color var(--t-fast),transform var(--t-fast),box-shadow var(--t-fast)}
.kpi::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;background:var(--line2)}
.kpi:hover{transform:translateY(-1px);border-color:var(--line2);box-shadow:var(--shadow-2)}
a.kpi{display:block;text-decoration:none;color:inherit;cursor:pointer}
a.kpi:hover{border-color:var(--accent)}
.kpi .kv{font-family:var(--mono);font-size:30px;font-weight:600;line-height:1;letter-spacing:-1px;font-variant-numeric:tabular-nums}
.kpi .kl{font-size:11.5px;color:var(--ink);margin-top:9px;font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.kpi .kh{font-size:11px;color:var(--faint);margin-top:3px}
.kpi.ok::before{background:var(--ok)}.kpi.ok .kv{color:var(--ok)}
.kpi.warn::before{background:var(--warn)}.kpi.warn .kv{color:var(--warn)}
.kpi.bad::before{background:var(--bad)}.kpi.bad .kv{color:var(--bad)}
/* EU-145: Security blocks interactive card */
.kpi{overflow:visible}
.kpi summary{list-style:none;cursor:pointer}
.kpi summary::-webkit-details-marker{display:none}
.kpi summary.kpisum{padding:16px 17px}
/* 2026-07-22 (Commander: "0 needs you and 0 security blocks seem to be on different levels").
   A KPI renders two ways: <a class=kpi> for a plain tile, <details class=kpi><summary class=kpisum>
   for an expandable one (the security card). Both got .kpi's own padding, and the details variant
   then added summary.kpisum's padding INSIDE it — so its number started 16px lower than every
   neighbour and the row of tiles read as misaligned. The summary owns the padding for that
   variant; the container must not pay it twice. */
details.kpi{padding:0}
.kpi[open] summary.kpisum{padding-bottom:8px}
.secissue{border-top:1px solid var(--line);padding:12px 17px}
.sechead{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.secticket{font-family:var(--mono);font-size:12px;font-weight:600;color:var(--info);text-decoration:none}
.secticket:hover{text-decoration:underline}
.secmeta{font-size:10px;color:var(--faint);text-transform:uppercase;letter-spacing:.04em}
.secbody{font-size:12px;color:var(--dim);line-height:1.5;margin-bottom:10px;white-space:pre-wrap}
.secreply{margin-top:8px}
.secreply textarea{width:100%;background:var(--well);border:1px solid var(--line2);color:var(--ink);
  border-radius:var(--r-sm);padding:8px 10px;font:inherit;font-size:11px;resize:vertical;min-height:50px;
  margin-bottom:8px}
.secreply textarea:focus{outline:none;border-color:var(--accent);box-shadow:var(--ring)}
.secbtn{background:var(--accent);color:#fff;border:0;border-radius:var(--r-sm);padding:6px 12px;
  font-size:11px;font-weight:600;cursor:pointer;transition:background var(--t-fast)}
.secbtn:hover{background:#3b5ecc}
.secempty{padding:12px 17px;color:var(--faint);font-size:12px;font-style:italic}
.secmore{padding:8px 17px;font-size:11px;color:var(--dim);border-top:1px solid var(--line2)}
.secmore a{color:var(--info);text-decoration:none}
.secmore a:hover{text-decoration:underline}
/* layout */
.cols{display:grid;grid-template-columns:1fr 340px;gap:16px;padding:14px 26px 40px}
.col-main{display:flex;flex-direction:column;gap:16px;min-width:0}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--r-xl);overflow:hidden;box-shadow:var(--shadow-1)}
.ph{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:var(--dim);
padding:13px 18px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}
.ph::before{content:"";width:6px;height:6px;border-radius:2px;background:var(--accent)}
/* active run */
.run{padding:18px}
.runhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;gap:10px}
.runhead .mono{font-size:15px;color:var(--ink);font-weight:600}
.b{font-size:10px;font-weight:700;padding:4px 10px;border-radius:6px;text-transform:uppercase;letter-spacing:.06em}
.b.live{color:var(--warn);background:var(--warnbg)}.b.muted{color:var(--dim);background:var(--panel2)}
.b.mode{color:var(--ok);background:var(--okbg);box-shadow:0 0 0 1px var(--okline) inset}
.b.dry{color:var(--info);background:var(--accentbg);box-shadow:0 0 0 1px var(--accentline) inset}
.b.ok{color:var(--ok);background:var(--okbg)}.b.bad{color:var(--bad);background:var(--badbg)}.b.warn{color:var(--warn);background:var(--warnbg)}
.phasebar{display:flex;gap:0;position:relative}
.phasebar .ph{display:flex;flex-direction:column;align-items:center;gap:9px;flex:1;border:0;padding:0;text-transform:none;
letter-spacing:.02em;font-size:11.5px;font-weight:600;color:var(--faint);position:relative}
.phasebar .ph::before{display:none}
.phasebar .ph::after{content:"";position:absolute;top:6px;left:50%;width:100%;height:2px;background:var(--line2);z-index:0}
.phasebar .ph:last-child::after{display:none}
.phasebar .ph span{width:13px;height:13px;border-radius:var(--r-pill);background:var(--bg);flex:none;border:2px solid var(--line2);z-index:1;position:relative;transition:background var(--t-fast),border-color var(--t-fast)}
.phasebar .ph.done{color:var(--ink)}
.phasebar .ph.done span{background:var(--ok);border-color:var(--ok);box-shadow:0 0 8px rgba(52,211,153,.5)}
.phasebar .ph.done::after{background:var(--ok)}
/* idle = a finished 'last run', not live -> grey the bar so it never reads as in-progress */
.phasebar.idle .ph.done{color:var(--dim)}
.phasebar.idle .ph.done span{background:var(--line2);border-color:var(--line2);box-shadow:none}
.phasebar.idle .ph.done::after{background:var(--line2)}
.phasebar .ph.now{color:var(--warn)}
.phasebar .ph.now span{background:var(--warn);border-color:var(--warn);animation:pulse 1.5s infinite}
/* failed = the run terminated at this phase -> red stopping node, never reads as cleanly-done */
.phasebar .ph.failed,.phasebar.idle .ph.failed{color:var(--bad)}
.phasebar .ph.failed span,.phasebar.idle .ph.failed span{background:var(--bad);border-color:var(--bad);box-shadow:0 0 8px rgba(240,103,107,.5)}
@keyframes pulse{0%,100%{box-shadow:0 0 0 3px rgba(245,179,74,.28)}50%{box-shadow:0 0 0 8px rgba(245,179,74,0)}}
.runmeta{display:flex;gap:24px;margin-top:18px;padding-top:14px;border-top:1px solid var(--line);flex-wrap:wrap}
.meta{font-size:12px;color:var(--dim)}.meta b{color:var(--ink);font-weight:600;font-family:var(--mono)}
/* EU-76: hero-merged header inside the Active Run panel */
.runlive{align-items:flex-start!important}
.runtitle{font-size:18px;font-weight:700;letter-spacing:-.25px;line-height:1.2;margin-bottom:4px}
.runtapp{font-size:12.5px;color:var(--dim);font-weight:500;margin-left:9px;vertical-align:middle}
/* EU-106: project label on the per-project Active-run panel header (text-transform:none so a name
   like "Elite-Unit" isn't upper-cased by the .ph rule) */
.boardproj{text-transform:none;letter-spacing:0;font-size:11.5px;font-weight:600;color:var(--ink);background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:2px 8px}
.runsub{font-size:12.5px;color:var(--dim)}.runsub b{color:var(--warn)}
.runempty{padding:26px 18px;color:var(--dim);display:flex;align-items:center;gap:10px}
.dot2{width:8px;height:8px;border-radius:99px;background:var(--faint)}
.stoprun{margin:0;display:inline}
.stopbtn{background:var(--badbg);color:var(--bad);border:1px solid var(--badline);border-radius:var(--r-sm);
padding:4px 11px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;cursor:pointer;transition:background var(--t-fast)}
.stopbtn:hover{background:#3a181b}
/* EU-130: triage state bar */
.triage-bar{margin-top:12px}
.triage-bar .ph.triage-done{color:var(--ok)}
.triage-bar .ph.triage-done span{background:var(--ok);border-color:var(--ok);box-shadow:0 0 8px rgba(52,211,153,.5)}
.triage-bar .ph.triage-done::after{background:var(--ok)}
.triage-bar .ph.triage-now{color:var(--warn)}
.triage-bar .ph.triage-now span{background:var(--warn);border-color:var(--warn);animation:pulse 1.5s infinite}
.triage-meta{font-size:12.5px;font-weight:600}
/* roster */
.roster{padding:6px 0}
.offrow{display:flex;align-items:center;gap:12px;padding:10px 18px;border-left:2px solid transparent;transition:background var(--t-fast),border-left-color var(--t-fast)}
.offrow:hover{background:var(--panel2);border-left-color:var(--accent)}
a.offrow{text-decoration:none;color:inherit;cursor:pointer}
.d{width:8px;height:8px;border-radius:99px;flex:none;background:var(--line2)}
.d.live{background:var(--ok);box-shadow:0 0 8px var(--ok);animation:pulse2 1.4s infinite}
.d.recent{background:var(--info)}.d.idle{background:var(--line2)}
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
.blrow{display:flex;gap:11px;align-items:baseline;padding:9px 16px;border-top:1px solid var(--line);color:var(--ink);transition:background var(--t-fast)}
.blrow:hover{background:var(--panel2)}
.blkey{font-family:var(--mono);font-size:12px;color:var(--info);white-space:nowrap}
.blsum{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.blapp{font-size:10.5px;font-weight:700;color:var(--dim);background:var(--panel2);border:1px solid var(--line2);border-radius:var(--r-pill);padding:1px 8px;white-space:nowrap}
.blempty{padding:14px 16px;color:var(--dim);font-size:13px}
.fitem{display:flex;gap:11px;padding:9px 17px;border-top:1px solid var(--line)}
.fitem:first-child{border-top:0}
.fd{width:7px;height:7px;border-radius:99px;margin-top:6px;flex:none;background:var(--faint)}
.fd.ok{background:var(--ok)}.fd.warn{background:var(--warn)}.fd.bad{background:var(--bad)}
.fd.info{background:var(--info)}.fd.muted{background:var(--faint)}
.fbody{font-size:13px;min-width:0}.fmeta{font-family:var(--mono);font-size:11px;color:var(--faint);margin-top:3px}
.fbullets{margin:3px 0 2px 0;padding-left:15px;font-size:12px}.fbullets li{margin:1px 0;line-height:1.4}
/* live log */
.logbox{font-family:var(--mono);font-size:11.5px;line-height:1.55;color:var(--console-ink);background:var(--console);
margin:0;padding:13px 16px;height:380px;min-height:150px;max-height:78vh;resize:vertical;overflow:auto;
white-space:pre-wrap;word-break:break-word}
.logbox .lg-b{color:var(--warn)}.logbox .lg-ok{color:var(--ok)}.logbox .lg-dim{color:var(--faint)}
/* EU-200: live run log panel - same styles as logbox */
.runlog{font-family:var(--mono);font-size:11.5px;line-height:1.55;color:var(--console-ink);background:var(--console);
margin:0;padding:13px 16px;height:380px;min-height:150px;max-height:78vh;resize:vertical;overflow:auto;
white-space:pre-wrap;word-break:break-word}
.runlog .lg-b{color:var(--warn)}.runlog .lg-ok{color:var(--ok)}.runlog .lg-dim{color:var(--faint)}
.runlog .logempty{color:var(--dim);font-style:italic;padding:13px 16px;}
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
/* EU-298: a sync older than STALE_SYNC_CUTOFF_S reads as a warning, not as neutral chrome —
   reuses the EU-285a --warn token rather than a new colour literal */
.synced.stale{color:var(--warn)}
/* hero — the live-run headline (biggest thing when a run is in flight) */
.hero{margin:18px 24px 0;padding:16px 20px;border:1px solid var(--accentline);border-radius:var(--r-xl);
background:linear-gradient(120deg,rgba(77,124,255,.14),rgba(245,179,74,.06));position:relative;overflow:hidden;box-shadow:var(--shadow-2)}
.hgrow{display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap}
.hgleft{display:flex;align-items:center;gap:14px;min-width:0}
.hgdot{width:13px;height:13px;border-radius:99px;background:var(--warn);animation:pulse 1.5s infinite;flex:none}
.hgtitle{font-size:22px;font-weight:700;letter-spacing:-.3px}
.hgtitle .hgapp{font-size:13px;color:var(--dim);font-weight:500;margin-left:6px}
.hgsub{font-size:13px;color:var(--dim);margin-top:2px}.hgsub b{color:var(--warn)}
.hgchip{font-size:10px;font-weight:700;padding:3px 8px;border-radius:6px;text-transform:uppercase;letter-spacing:.05em}
.hgchip.live{color:var(--ok);background:var(--okbg)}.hgchip.dry{color:var(--info);background:var(--accentbg)}
.hgstats{display:flex;gap:26px}
.hgstat{text-align:right}.hgk{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--faint)}
.hgv{font-family:var(--mono);font-size:20px;font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums}
/* needs-you side panel + talk-to-the-unit */
.needs{padding:6px 0}
.needsok{display:flex;align-items:center;gap:9px;padding:14px 18px;color:var(--ok);text-decoration:none;font-size:13px}
.nok{font-weight:800}
.needrow{display:flex;align-items:flex-start;gap:11px;padding:10px 18px;text-decoration:none;color:inherit;border-left:2px solid transparent;transition:background var(--t-fast),border-left-color var(--t-fast)}
.needrow:hover{background:var(--panel2);border-left-color:var(--warn)}
.nd{width:8px;height:8px;border-radius:99px;margin-top:5px;flex:none;background:var(--faint)}
.nd.ok{background:var(--ok)}.nd.warn{background:var(--warn)}.nd.bad{background:var(--bad)}
.ndmain{min-width:0}.ndt{font-size:13px;font-weight:600;color:var(--ink)}
.ndr{font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.04em;margin-top:1px}
.needall{display:block;padding:11px 18px;font-size:12px;font-weight:650;color:var(--info);border-top:1px solid var(--line)}
.needspanel .ph::before{background:var(--warn)}
.talk{padding:6px;display:flex;flex-direction:column;gap:8px}
.talkbtn{display:flex;align-items:center;gap:12px;padding:12px 14px;border:1px solid var(--line);border-radius:var(--r-lg);
background:var(--well);text-decoration:none;color:inherit;transition:border-color var(--t-fast),background var(--t-fast),transform var(--t-fast)}
.talkbtn:hover{border-color:var(--accent);background:var(--panel2);transform:translateX(2px)}
.tki{display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;flex:none;
font-size:17px;background:var(--accentbg);border:1px solid var(--accentline);border-radius:var(--r-md)}
.tkbody{flex:1;min-width:0}
.talkbtn b{display:block;font-size:13.5px}.talkbtn i{font-style:normal;font-size:11.5px;color:var(--dim)}
.tkarrow{color:var(--faint);font-size:18px;flex:none;transition:color var(--t-fast)}
.talkbtn:hover .tkarrow{color:var(--accent)}
@media(max-width:1080px){.cols{grid-template-columns:1fr}.hgstats{gap:18px}}
@media(max-width:680px){.kpis{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}.hbactions .models{display:none}}
@media(prefers-reduced-motion:reduce){*{animation:none!important}}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:var(--line2);border-radius:8px}
</style></head><body>
<header>
  <div class=brand><span class=bmark>&#x2B22;</span> SQUAD <b>·</b> HQ{{HOST}}</div>
  {{PROJ}}
  <div class=spacer></div>
  {{AUTOPILOT}}
  {{HEALTHPILL}}
  <button id=themetoggle class=themebtn type=button title="Toggle light / dark" onclick="uiTheme()">&#9681;</button>
  <span class=gen><span id=streamdot class="sdot off" title="live stream"></span>live · {{GEN}}</span>
</header>
{{HEALTHBAR}}
{{BAR}}
<div id=board>{{BOARD}}</div>
<script>
try{document.documentElement.dataset.theme=localStorage.getItem("ui.theme")||"dark"}catch(e){}
function uiTheme(){try{var r=document.documentElement;
  r.dataset.theme=r.dataset.theme==="light"?"dark":"light";
  localStorage.setItem("ui.theme",r.dataset.theme);}catch(e){}}
var APP="{{APP}}";
function proj(v){APP=v;var p=new URLSearchParams(location.search);p.set("app",v);location.search="?"+p.toString();}
document.addEventListener("click",function(e){
  document.querySelectorAll("details[open]").forEach(function(d){
    if(!d.classList.contains("collapse") && !d.contains(e.target)) d.removeAttribute("open");
  });
});
// Collapsible Activity panel + resizable log panels live INSIDE #board, which the SSE feed re-renders
// every frame — so persist their state and re-apply it after each refresh (otherwise it resets).
function saveUi(){try{
  ["actpanel","blpanel"].forEach(function(id){var p=document.getElementById(id);
    if(p)localStorage.setItem("ui.open."+id,p.open?"1":"0");});
  ["logbox","blbox","actbox"].forEach(function(id){var el=document.getElementById(id);
    if(el)localStorage.setItem("ui.scroll."+id,el.scrollTop);});
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
// EU-200: Live run log streaming
(function(){
  var runlogPanel=document.getElementById("runlog");
  if(!runlogPanel)return;

  var logPath=runlogPanel.getAttribute("data-log-path");
  if(!logPath){
    runlogPanel.innerHTML='<div class=logempty>No active run log to display.</div>';
    return;
  }

  /* EU-487: every Active-run card tails the SAME shared drain log, so the panel scopes
     the stream to the newest card's ticket (cards render newest-first; the attribute
     only exists on the multi-card path). No attribute (single-run board) → no &ticket=
     and the stream URL is exactly what it was before EU-487. */
  function readLogTicket(){
    var hd=document.querySelector("div.run [data-log-ticket]");
    return hd?(hd.getAttribute("data-log-ticket")||""):"";
  }
  var logTicket=readLogTicket();

  var runlogEs=null;
  var runlogBuffer=[];
  var _runlogPoll=null;

  function renderRunlogLines(){
    if(!runlogPanel)return;
    if(runlogBuffer.length===0){
      /* 2026-07-22: do NOT overwrite here. The server renders a live status placeholder
         (stage / elapsed / last step) and refreshes it with the board; blanking it back to
         a static string threw away the only information available during the silent first
         pass of a run. Leave whatever the server put there until real lines arrive. */
      return;
    }

    var linesHtml=runlogBuffer.map(function(line){
      var low=line.toLowerCase();
      var cls="";
      if(low.includes('merged') || line.includes('✓') || low.includes(' pass') || low.includes('ready')){
        cls="lg-ok";
      }else if(/error|fail|park|block|✗|reject/.test(low)){
        cls="lg-b";
      }else if(/^·/.test(line) || /builder:|reviewer:/.test(low)){
        cls="lg-dim";
      }
      return'<span class="'+cls+'">'+line.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")+'</span>';
    }).join("\\n");

    runlogPanel.innerHTML='<pre class=runlog>'+linesHtml+'</pre>';
    runlogPanel.scrollTop=runlogPanel.scrollHeight;
  }

  function startRunlogStream(){
    if(!logPath)return;

    try{
      var runlogUrl="/api/run-log-stream?app="+encodeURIComponent(APP);
      if(logTicket){runlogUrl+="&ticket="+encodeURIComponent(logTicket);}
      runlogEs=new EventSource(runlogUrl);
      runlogEs.addEventListener("log",function(e){
        runlogBuffer.push(e.data);
        // Keep buffer size manageable (last 1000 lines)
        if(runlogBuffer.length>1000){
          runlogBuffer=runlogBuffer.slice(-1000);
        }
        renderRunlogLines();
      });
      runlogEs.addEventListener("done",function(e){
        if(runlogEs){
          runlogEs.close();
          runlogEs=null;
        }
      });
      runlogEs.addEventListener("error",function(e){
        console.error("Run log stream error:",e.data);
      });
      runlogEs.onerror=function(){
        if(runlogEs){
          runlogEs.close();
          runlogEs=null;
        }
      };
    }catch(e){
      console.error("Failed to start run log stream:",e);
    }
  }

  // Start the log stream
  startRunlogStream();

  // Update on board refresh (log path might change)
  var originalApplyBoard=applyBoard;
  applyBoard=function(html){
    originalApplyBoard(html);
    // Restart log stream with new log path — or a new per-card ticket filter (EU-487:
    // the newest card changed, or the board flipped between single- and multi-card).
    var newPanel=document.getElementById("runlog");
    if(newPanel){
      var newPath=newPanel.getAttribute("data-log-path");
      var newTicket=readLogTicket();
      var pathChanged=newPath&&newPath!==logPath;
      var ticketChanged=newTicket!==logTicket;
      if(pathChanged||ticketChanged){
        if(pathChanged)logPath=newPath;
        logTicket=newTicket;
        runlogBuffer=[];
        if(runlogEs){
          runlogEs.close();
          runlogEs=null;
        }
        if(logPath)startRunlogStream();
      }
    }
  };
})();
</script>
</body></html>"""
