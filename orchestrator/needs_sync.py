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
_BLOCKED_LIKE = {"blocked", "on hold", "waiting", "needs human"}

_last_run_ts = 0.0
_run_lock = threading.Lock()


def _classify(status: str | None) -> str:
    """'done' | 'blocked' | 'active' | 'unknown' for a Jira status name."""
    if not status:
        return "unknown"
    s = status.strip().lower()
    if s in _DONE_LIKE:
        return "done"
    if s in _BLOCKED_LIKE:
        return "blocked"
    return "active"


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
    {checked, cleared: [(id, reason)], kept, skipped_no_jira} — or {"throttled": True} when
    inside the TTL window and not forced. Never raises; never starts a build."""
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

    parked = autopilot.load_blocked(cfg)
    pending = decisions.load(cfg)
    pending_ids = {str(p.get("id", "")).split("#", 1)[0] for p in pending}
    # errored/PR rows come from the audit; their ids overlap the sets above or stand alone —
    # reconcile the union so a Done ticket clears from EVERY surface at once.
    try:
        from . import needs as _needs
        task_ids = {str(r.get("ticket_id") or "") for r in _needs.summary(cfg).get("tasks", [])}
    except Exception:  # noqa: BLE001
        task_ids = set()
    ids = sorted({i for i in (set(parked) | pending_ids | task_ids) if i and "-" in i})

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

    still_parked = set(parked)
    for tid in ids:
        cls = _classify(statuses.get(tid))
        if cls in ("blocked", "unknown"):
            kept += 1
            continue
        reason = "done in Jira" if cls == "done" else "re-queued in Jira"
        touched = False
        if tid in still_parked:
            still_parked.discard(tid)
            touched = True
        for p in pending:
            if str(p.get("id", "")).split("#", 1)[0] == tid:
                try:
                    decisions.commit(cfg, p["id"])   # idempotent drop-by-id (EU-372 primitive)
                except Exception:  # noqa: BLE001
                    pass
                touched = True
        # done OR re-queued: either way the errored/awaiting card describes a run that is over
        # for a ticket Jira says is finished or back in the queue — not "needs you" anymore.
        # (First live sync 2026-07-19 proved the done-only version wrong: 12 To-Do tickets kept
        # their dead cards while Jira showed ZERO blocked.)
        if tid in task_ids:
            try:
                D.dismiss(cfg.audit_path, tid)
            except Exception:  # noqa: BLE001
                pass
            touched = True
        if touched:
            cleared.append((tid, reason))

    if len(still_parked) != len(parked):
        autopilot.save_blocked(cfg, still_parked)
    if cleared and audit is not None:
        try:
            audit.record("needs_sync_cleared",
                         tickets=[t for t, _ in cleared],
                         reasons={t: r for t, r in cleared})
        except Exception:  # noqa: BLE001
            pass
    return {"checked": len(statuses), "cleared": cleared, "kept": kept,
            "skipped_no_jira": skipped}
