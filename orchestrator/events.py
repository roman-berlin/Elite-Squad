"""Event reactor — the unit convenes itself.

Called once per autopilot cycle. Based on what just happened — a security block, a pile of
parked tickets, or a quiet queue — it may auto-convene a focused meeting or a corridor
small-talk, so the officers act on their own. Everything is throttled by a cooldown +
probabilities so they never spam, and the whole layer is disable-able via `autonomy_enabled`.
It only fires from Autopilot (the always-on brain) — manual runs never trigger it.
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

from .config import Config


def _state_path(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("autonomy.json")


def _load(cfg: Config) -> dict:
    try:
        return json.loads(_state_path(cfg).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save(cfg: Config, st: dict) -> None:
    try:
        _state_path(cfg).write_text(json.dumps(st))
    except OSError:
        pass


def _is_security(report) -> bool:
    note = (getattr(report, "notes", "") or "").lower()
    return "provost" in note or "security" in note


def _spontaneous_topic(cfg: Config) -> str:
    from .dashboard import load_tasks
    tasks = load_tasks(cfg.audit_path)[:8]
    needs = [t for t in tasks if t.get("outcome") in ("escalated", "PR / needs you", "errored")]
    if needs:
        return f"what should we tackle next — {needs[0]['ticket_id']} is waiting on us"
    return "where is the unit weakest right now, and the one thing to improve this week?"


def _meeting_request(cfg: Config, st: dict):
    """The latest council/meeting's first un-actioned 'MEETING:' request, or None. The unit acts
    on its own deliberations — an officer asking for a huddle gets one (once)."""
    from . import council
    src, topics = council.pending_meeting_requests(cfg)
    if topics and st.get("acted_council") != src:
        return topics[0], src
    return None


async def after_cycle(cfg: Config, reports, audit=None, blocked=None) -> str | None:
    """Maybe convene a session based on the cycle just finished. Returns the kind fired, or None.
    Never raises into the caller — autonomy must never break the autopilot."""
    if not getattr(cfg, "autonomy_enabled", True):
        return None
    now = time.time()
    st = _load(cfg)
    if (now - st.get("last", 0)) < getattr(cfg, "autonomy_cooldown_min", 45) * 60:
        return None   # still cooling down — keep the peace

    reports = list(reports or [])
    sec = [r for r in reports if _is_security(r)]
    blocked_n = len(blocked or [])

    fired = None
    try:
        from . import council
        if sec and getattr(cfg, "meeting_on_security_block", True):
            tid = getattr(sec[0], "ticket_id", "a ticket")
            await council.hold_meeting(
                cfg, f"security block on {tid} — how do we close it cleanly?",
                officers=["provost", "field", "inspector"], rounds=1, audit=audit)
            fired = "security-huddle"
        elif (mr := _meeting_request(cfg, st)):
            topic, src = mr
            await council.hold_meeting(cfg, topic, rounds=1, audit=audit)
            st["acted_council"] = src      # don't re-convene the same request next cycle
            fired = "officer-requested-meeting"
        elif blocked_n >= getattr(cfg, "parks_meeting_threshold", 3):
            await council.hold_meeting(
                cfg, f"{blocked_n} tickets are parked — what's the root cause and the fix?",
                officers=["drill", "adjutant", "inspector"], rounds=1, audit=audit)
            fired = "stuck-meeting"
        elif not reports:   # quiet cycle — room for the unit to have a life
            roll = random.random()
            st_p = float(getattr(cfg, "smalltalk_prob", 0.15))
            rm_p = float(getattr(cfg, "random_meeting_prob", 0.06))
            if roll < st_p:
                await council.small_talk(cfg, audit=audit)
                fired = "smalltalk"
            elif roll < st_p + rm_p:
                await council.hold_meeting(cfg, _spontaneous_topic(cfg), rounds=1, audit=audit)
                fired = "random-meeting"
    except Exception as exc:  # noqa: BLE001
        print(f"  autonomy skipped: {exc}", flush=True)
        return None

    if fired:
        st["last"] = now
        _save(cfg, st)
        if audit is not None:
            audit.record("autonomy", kind=fired)
        print(f"  · the unit convened itself: {fired}", flush=True)
    return fired
