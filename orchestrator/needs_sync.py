"""Jira → Needs-you reconciliation (2026-07-19, Commander order).

The Commander answers/moves tickets IN JIRA, but the cockpit's Needs-you only shrank via the
autopilot's in-drain scans — with no drain running, parked entries, pending decisions and
errored rows outlived the tickets they pointed at ("Needs you is much bigger than Blocked in
Jira"). This module reconciles the cockpit's needs stores against the CURRENT Jira status:

  · status is DONE-like (Done/Closed/Resolved/QA…)     → the work is over: unpark, drop the
    pending decision, dismiss the errored/PR row.
  · status is ACTIVE-like (To Do/In Progress/Backlog…) → the Commander re-queued it himself:
    unpark (a lingering park makes the drain SKIP a ticket he wants built), drop the pending
    decision (he took the call in Jira) and dismiss the errored/PR card — it describes a DEAD
    run of a ticket that is back in the queue. dismiss() is timestamped, so a NEW failing run
    after the sync surfaces again (dashboard._is_dismissed only hides runs at/before it).
  · status is BLOCKED-like                             → genuinely waiting: keep everything.
    (A NEW Jira comment on a blocked ticket is the answer path — consumed by the autopilot's
    _resumable_answered scan, which re-runs the ticket; this module never starts builds.)

Runs three ways: a throttled background pass on every /needs load, the same throttled pass at
each autopilot cycle, and force=True from the cockpit's "Sync with Jira" button. Best-effort
throughout — an unreachable Jira just leaves the stores untouched.
"""
from __future__ import annotations

import threading
import time

_DONE_LIKE = {"done", "closed", "resolved", "qa", "in qa", "ready for qa", "manual qa",
              "released", "won't do", "wont do", "cancelled", "canceled"}
# EU-406: a substring match for the Blocked family catches renamed/custom waiting columns a bare
# exact-match set would miss ('On Hold - Client', 'Waiting for Customer', 'Needs Human Approval').
_BLOCKED_LIKE = {"blocked", "on hold", "waiting", "needs human"}
_BLOCKED_KEYWORDS = ("blocked", "on hold", "waiting", "needs human")
# EU-406 (AC2): the ONLY statuses that mean "the Commander re-queued this himself". Anything not
# in the done / blocked / active sets is 'unknown' — KEPT and surfaced, never guessed 'active'
# (the old default silently un-parked a ticket parked behind a renamed/custom blocked-ish column
# and permanently dropped its pending decision).
_ACTIVE_LIKE = {"to do", "in progress", "backlog", "open", "selected for development",
                "ready for development", "reopened"}

# EU-406 (AC3): run outcomes whose parks are Jira-INVISIBLE — they never transition Jira to
# 'Blocked', so the ticket sits 'In Progress' the whole time it is parked. For such a park an
# 'active' (In Progress) Jira status is its resting state, NOT a Commander re-queue; clearing it
# would make the drain rebuild the already-reviewed/exhausted ticket (the park↔unpark oscillation
# the 2026-07-21 production audit found). PR_OPENED and the run-budget ESCALATED are the two
# kinds — every other parked outcome either transitions Jira itself (the error-park path) or owns
# a pending decision (decisions.add → 'Blocked'), so it IS Jira-visible.
_INVISIBLE_PARK_OUTCOMES = {"PR / needs you", "escalated"}

_last_run_ts = 0.0
_run_lock = threading.Lock()


def _classify(status: str | None) -> str:
    """'done' | 'blocked' | 'active' | 'unknown' for a Jira status name.

    EU-406 (AC2): the fail-open default is INVERTED. An UNRECOGNIZED status is 'unknown' (→ kept
    + surfaced), not 'active' — a renamed/custom blocked-ish column ('On Hold - Client',
    'Waiting for approval') used to classify 'active' and silently un-park every ticket in it."""
    if not status:
        return "unknown"
    s = status.strip().lower()
    if s in _DONE_LIKE:
        return "done"
    if s in _BLOCKED_LIKE or any(k in s for k in _BLOCKED_KEYWORDS):
        return "blocked"
    if s in _ACTIVE_LIKE:
        return "active"
    return "unknown"


def _app_for(cfg, ticket_id: str):
    """The AppConfig whose backlog project key matches the ticket's prefix, else None."""
    prefix = str(ticket_id or "").split("-", 1)[0].upper()
    for app in getattr(cfg, "apps", None) or []:
        if getattr(app, "backlog_backend", "") != "jira":
            continue
        pk = str((getattr(app, "backlog", None) or {}).get("project_key", "") or "").upper()
        if pk and pk == prefix:
            return app
    return None


def reconcile(cfg, audit=None, *, ttl_s: float = 300.0, force: bool = False) -> dict:
    """Reconcile the needs stores against live Jira statuses. Returns a summary dict
    {checked, cleared: [(id, reason)], kept, skipped_no_jira, unknown: [(id, raw_status)]} —
    or {"throttled": True} when inside the TTL window and not forced. Never raises; never
    starts a build.

    EU-406 hardening (2026-07-21 production audit):
      · the parked-set write is a locked read-modify-write that removes ONLY the ids this pass
        decided to clear (``autopilot.remove_blocked``), never a snapshot overwrite — so a park
        a concurrent drain makes during the minutes-long Jira scan survives (AC1);
      · a pending decision is dropped ONLY on an explicit done/active classification — an
        unrecognized status keeps everything and is surfaced in ``unknown`` + an audit event
        instead of guessed 'active' and silently cleared (AC2);
      · a Jira-INVISIBLE park (PR_OPENED, run-budget ESCALATED — outcomes that never transition
        Jira to 'Blocked') is KEPT on 'active' (its 'In Progress' is a resting state, not a
        re-queue) and only cleared on an explicit done (AC3)."""
    global _last_run_ts
    with _run_lock:
        now = time.time()
        if not force and now - _last_run_ts < ttl_s:
            return {"throttled": True}
        _last_run_ts = now

    from . import autopilot, decisions
    from . import dashboard as D
    from .backlog.base import make_backlog

    cleared: list[tuple[str, str]] = []
    kept = 0
    skipped = 0
    unknown_surfaced: list[tuple[str, str]] = []   # AC2: (id, raw_status) kept + surfaced

    parked = autopilot.load_blocked(cfg)
    pending = decisions.load(cfg)
    pending_ids = {str(p.get("id", "")).split("#", 1)[0] for p in pending}
    # errored/PR rows come from the audit; their ids overlap the sets above or stand alone —
    # reconcile the union so a Done ticket clears from EVERY surface at once. task_outcome carries
    # each ticket's latest run outcome so AC3 can tell a Jira-invisible park (PR/budget) from a
    # Jira-visible one (error-park / decision-backed escalation).
    task_outcome: dict[str, str] = {}
    try:
        from . import needs as _needs
        for r in _needs.summary(cfg).get("tasks", []):
            tid = str(r.get("ticket_id") or "")
            if tid:
                task_outcome[tid] = str(r.get("outcome") or "")
    except Exception:  # noqa: BLE001
        pass
    ids = sorted({i for i in (set(parked) | pending_ids | set(task_outcome)) if i and "-" in i})

    adapters: dict[str, object] = {}
    statuses: dict[str, str | None] = {}
    for tid in ids:
        app = _app_for(cfg, tid)
        if app is None:
            skipped += 1
            continue
        try:
            bl = adapters.get(app.name)
            if bl is None:
                bl = make_backlog(app)
                adapters[app.name] = bl
            statuses[tid] = bl._current_status(tid)
        except Exception:  # noqa: BLE001 - one unreachable Jira must not kill the pass
            statuses[tid] = None

    # AC1: collect the ids to remove; apply them in ONE locked read-modify-write against the
    # CURRENT store AFTER the scan. NEVER write the pre-scan snapshot back (a park made during
    # the scan would be erased — the EU-218 lost-park class).
    to_unpark: set[str] = set()
    decisions_to_drop: set[str] = set()
    rows_to_dismiss: set[str] = set()

    for tid in ids:
        cls = _classify(statuses.get(tid))
        if cls in ("blocked", "unknown"):
            # AC2: surface an unrecognized status (not a None/unreachable one) so a renamed/custom
            # column is visible instead of silently guessed. Kept either way — never clears on doubt.
            if cls == "unknown" and statuses.get(tid):
                unknown_surfaced.append((tid, str(statuses.get(tid))))
            kept += 1
            continue
        if cls == "active":
            # AC3: a Jira-INVISIBLE park (no pending decision AND its latest run is a PR_OPENED or
            # a run-budget ESCALATED — outcomes that never moved Jira to 'Blocked') sits 'In
            # Progress' by design. That is NOT a Commander re-queue; keep it (only done ends it).
            has_decision = tid in pending_ids
            jira_invisible = (not has_decision) and task_outcome.get(tid, "") in _INVISIBLE_PARK_OUTCOMES
            if jira_invisible:
                kept += 1
                continue
        # done, OR active for a Jira-VISIBLE park (error-park / decision-backed escalation the
        # Commander genuinely re-queued): clear from every surface.
        reason = "done in Jira" if cls == "done" else "re-queued in Jira"
        touched = False
        if tid in parked:
            to_unpark.add(tid)
            touched = True
        for p in pending:
            if str(p.get("id", "")).split("#", 1)[0] == tid:
                decisions_to_drop.add(p["id"])
                touched = True
        # done OR re-queued: either way the errored/awaiting card describes a run that is over
        # for a ticket Jira says is finished or back in the queue — not "needs you" anymore.
        # (First live sync 2026-07-19 proved the done-only version wrong: 12 To-Do tickets kept
        # their dead cards while Jira showed ZERO blocked.)
        if tid in task_outcome:
            rows_to_dismiss.add(tid)
            touched = True
        if touched:
            cleared.append((tid, reason))

    # AC1: remove exactly the decided ids from the CURRENT store (concurrent parks survive).
    if to_unpark:
        autopilot.remove_blocked(cfg, to_unpark)
    for eid in decisions_to_drop:
        try:
            decisions.commit(cfg, eid)   # idempotent drop-by-id (EU-372 primitive)
        except Exception:  # noqa: BLE001
            pass
    for tid in rows_to_dismiss:
        try:
            D.dismiss(cfg.audit_path, tid)
        except Exception:  # noqa: BLE001
            pass

    if cleared and audit is not None:
        try:
            audit.record("needs_sync_cleared",
                         tickets=[t for t, _ in cleared],
                         reasons={t: r for t, r in cleared})
        except Exception:  # noqa: BLE001
            pass
    # AC2: surface unrecognized statuses — an audit event plus a one-line operator note. A
    # renamed/custom Jira column parked behind these must not be invisible.
    if unknown_surfaced and audit is not None:
        try:
            audit.record("needs_sync_unknown_status",
                         tickets=[t for t, _ in unknown_surfaced],
                         statuses={t: s for t, s in unknown_surfaced})
        except Exception:  # noqa: BLE001
            pass
    if unknown_surfaced:
        preview = ", ".join(f"{t}='{s}'" for t, s in unknown_surfaced[:8])
        print(f"  ⚠ needs_sync: {len(unknown_surfaced)} ticket(s) in an unrecognized Jira status "
              f"(kept, not cleared): {preview}", flush=True)
    return {"checked": len(statuses), "cleared": cleared, "kept": kept,
            "skipped_no_jira": skipped, "unknown": unknown_surfaced}
