"""The single "Needs you" surface — everything awaiting the Commander, aggregated.

Three streams converge here so there's one place (and one count) to watch instead of three
scattered badges:
  • decisions — the CTO's open questions (pending_decisions.json)
  • approvals — officer recommendations awaiting Approve/Disapprove (drill / adjutant)
  • tasks     — runs that ended needing you (PR / escalated / errored), minus dismissed

Defensive end-to-end: any missing/half-written source degrades to an empty stream, never a crash.
"""
from __future__ import annotations

from .config import Config


def summary(cfg: Config) -> dict:
    """All pending items + a single total. JSON-safe primitives only."""
    decisions_items: list[dict] = []
    approvals_items: list[dict] = []
    task_items: list[dict] = []
    try:
        from . import decisions as _dec
        decisions_items = _dec.load(cfg) or []
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import approvals as _ap
        approvals_items = _ap.pending(cfg) or []
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import dashboard as _D
        tasks = _D.load_tasks(cfg.audit_path)
        dismissed = _D.load_dismissed(cfg.audit_path)
        task_items = _D.latest_needs_you(tasks, dismissed)   # one row per ticket (latest run), not every old run
    except Exception:  # noqa: BLE001
        pass
    return {
        "decisions": decisions_items,
        "approvals": approvals_items,
        "tasks": task_items,
        "total": len(decisions_items) + len(approvals_items) + len(task_items),
    }


def count(cfg: Config) -> int:
    """Just the badge number — cheap to call on every page render."""
    return summary(cfg)["total"]
