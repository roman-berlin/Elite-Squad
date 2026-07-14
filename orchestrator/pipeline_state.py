"""Pure derivation of each ticket's CURRENT pipeline stage from its raw audit events (EU-312).

EU-136 bug this fixes: the cockpit used to infer a ticket's stage from event *order in the log*
(effectively "last event of the run, assumed forward-only"), so a `gate` event followed by a later
`build` retry still displayed "Gate" — stuck — when the ticket had actually moved back into Building.
`derive_pipeline_stage` instead picks the deciding event by LATEST TIMESTAMP among the relevant
kinds, never assuming forward-only progression. Timestamps only resolve to the second, so when two
relevant events share one timestamp the deciding event is the one appearing LATER in input/list
order (it was appended later) — a gate->build retry logged in the same second reports Building.

Pure / no I/O: callers (dashboard, warroom, needs-you) feed it the same event dicts already produced
by `dashboard._load_tasks_uncached`'s underlying audit lines (keys: `ticket_id`, `event`, `ts`, ...).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from .dashboard import _parse_ts

# A stage record older than this is flagged `is_stale` — later tickets (e.g. the needs-you panel)
# use this to stop presenting an old block/gate/etc. as the ticket's live current state. 24h: no
# single build/gate/review cycle should legitimately sit unchanged this long.
STALE_THRESHOLD_SECONDS = 24 * 60 * 60

# The event kinds that can decide a ticket's current stage. Anything else (e.g. per-pass detail
# events not in this set) is ignored for stage purposes.
_RELEVANT_KINDS = {"ticket_start", "build", "gate", "review", "merged", "needs_human", "blocked"}

# kind of the deciding event -> displayed stage name.
_STAGE_BY_KIND = {
    "ticket_start": "Queued",
    "build": "Building",
    "gate": "Gate",
    "review": "Review",
    "merged": "Merged",
    "needs_human": "Needs-you",
    "blocked": "Blocked",
}


def _naive(dt: datetime) -> datetime:
    """Strip tzinfo so aware/naive parsed timestamps (format-dependent — see _parse_ts) can be
    subtracted safely, same tolerance dashboard._started_dt already relies on."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def derive_pipeline_stage(
    tasks: list[dict[str, Any]], now: Optional[datetime] = None
) -> dict[str, dict[str, Any]]:
    """Group raw audit events by ticket_id and derive each ticket's CURRENT stage.

    For each ticket, the "deciding event" is the LATEST event among the relevant kinds
    ({ticket_start, build, gate, review, merged, needs_human, blocked}), ordered by
    (timestamp, list-position): when two relevant events carry the SAME second-resolution
    timestamp the one appearing later in the input list wins (it was appended later), so a
    gate->build retry recorded in the same second reports "Building", not "Gate". A ticket with
    events only up through `ticket_start` (no build/gate/review/terminal event yet) reports
    "Queued". Unparseable timestamps and events with no ticket_id are skipped (fail safe, never
    raises). A ticket with no relevant events at all is simply absent from the result.

    Returns ``{ticket_id: {"ticket_id", "stage", "since_ts", "age_seconds", "is_stale"}}``.
    """
    now = _naive(now) if now is not None else datetime.now()

    # Keep each relevant event's original list index alongside its (naive) timestamp so ties on an
    # identical timestamp break by input order (later wins) rather than by max()'s first-seen rule.
    by_ticket: dict[str, list[tuple[datetime, int, dict[str, Any]]]] = {}
    for idx, ev in enumerate(tasks or []):
        tid = ev.get("ticket_id")
        kind = ev.get("event")
        if not tid or kind not in _RELEVANT_KINDS:
            continue
        ts = _parse_ts(ev.get("ts", ""))
        if ts is None:
            continue
        by_ticket.setdefault(str(tid), []).append((_naive(ts), idx, ev))

    result: dict[str, dict[str, Any]] = {}
    for tid, events in by_ticket.items():
        since_ts, _idx, deciding = max(events, key=lambda e: (e[0], e[1]))
        stage = _STAGE_BY_KIND.get(deciding.get("event"), "Queued")
        age_seconds = (now - since_ts).total_seconds()
        result[tid] = {
            "ticket_id": tid,
            "stage": stage,
            "since_ts": since_ts,
            "age_seconds": age_seconds,
            "is_stale": age_seconds > STALE_THRESHOLD_SECONDS,
        }
    return result
