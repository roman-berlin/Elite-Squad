"""The single "Needs you" surface — everything awaiting the Commander, aggregated.

EU-102: all four Commander-inbox streams are now merged into one flat ``rows`` list so the
badge count and the list length are always identical.  Each row carries ``category`` and
``why`` so consumers can group or filter without re-inspecting raw fields.

Streams in ``rows`` (everything the badge counts — count == len(rows) == total, always):
  • decision   — the CTO's open questions (pending_decisions.json)
  • errored    — runs that ended errored / escalated / awaiting decision, minus dismissed
  • parked     — tickets the autopilot is skipping (blocked_tickets.json)
  • pr         — runs that ended with a PR opened and need Commander review
  • approval   — officer recommendations awaiting a decision (drill / adjutant reports)
  • proposal   — queued ticket batches awaiting approve/deny
  • specialist — specialist-provisioning rosters awaiting approve/decline

The same items are ALSO exposed under their original keys (``decisions``, ``approvals``,
``proposals``, ``specialist_approvals``, ``tasks``) so server.py can render each section with its
bespoke action form.  EU-102 (iter-3): ``specialist_approvals`` are now folded into ``rows`` too, so
there is exactly ONE number everywhere — ``count() == len(rows) == total`` — and no stream can raise
the badge without also rendering in the inbox.

Dedup: a ticket in blocked_tickets.json yields exactly ONE row, category ``parked`` (autopilot is
skipping it), even when its latest run also errored — the errored/PR loop skips blocked ticket ids.

Defensive end-to-end: any missing/half-written source degrades to an empty stream, never a crash.
"""
from __future__ import annotations

from .config import Config

# Outcomes from dashboard._NEEDS_YOU that are NOT a PR — shown under category "errored".
_FAILED_OUTCOMES = {"errored", "escalated", "awaiting decision"}


def summary(cfg: Config) -> dict:
    """Unified inbox: a flat ``rows`` list (typed, with category + why) plus backward-compat keys.

    Each row is a copy of the source item extended with:
      ``category`` — one of: decision | errored | parked | pr | approval | proposal | specialist
      ``why``      — one-line human reason string (question text, note, or fallback label)

    ``total`` == ``len(rows)`` == ``count()`` — one number for every Needs-you surface (EU-102).
    """
    import json
    from pathlib import Path

    rows: list[dict] = []

    # ── 0. Blocked/parked ticket ids — resolved up front so the errored/PR loop can SKIP them.
    # Dedup invariant (EU-102 iter-3): a ticket in blocked_tickets.json must produce exactly ONE
    # row — category 'parked' (section 3), because the autopilot is skipping it — never also an
    # 'errored'/'pr' duplicate. Tolerates a dict {id: reason} or a list of ids/objects, like
    # warroom._load_blocked, and degrades to an empty set on any read/parse failure.
    blocked_ids: set[str] = set()
    try:
        _blocked_path = Path(cfg.audit_path).with_name("blocked_tickets.json")
        _raw_blocked = json.loads(_blocked_path.read_text(encoding="utf-8"))
        if isinstance(_raw_blocked, dict):
            blocked_ids = {str(k) for k in _raw_blocked}
        elif isinstance(_raw_blocked, list):
            for _b in _raw_blocked:
                blocked_ids.add(str(_b.get("ticket_id") or _b.get("id") or _b)
                                if isinstance(_b, dict) else str(_b))
    except (OSError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
        pass

    # ── 1. Pending decisions ─────────────────────────────────────────────────
    decisions_items: list[dict] = []
    try:
        from . import decisions as _dec
        decisions_items = _dec.load(cfg) or []
    except Exception:  # noqa: BLE001
        pass
    for d in decisions_items:
        rows.append({
            **d,
            "category": "decision",
            "why": str(d.get("question") or d.get("summary") or "pending decision"),
        })
    # Review fix (2026-07-05): a ticket parked THROUGH a decision (budget breach, exhaustion —
    # decisions.add also lands it in blocked_tickets.json) must surface as ONE row. The decision
    # row wins: it carries the question and the reply hint. Base ids only — a decision id can be
    # suffixed ("EU-81#out-of-scope").
    decision_ids = {str(d.get("id") or "").split("#", 1)[0] for d in decisions_items if d.get("id")}

    # ── 2 & 4. latest_needs_you() → errored | escalated → "errored", PR → "pr" ──
    task_items: list[dict] = []
    try:
        from . import dashboard as _D
        _tasks = _D.load_tasks(cfg.audit_path)
        _dismissed = _D.load_dismissed(cfg.audit_path)
        # One row per ticket (latest run); stale / dismissed runs are already filtered out.
        task_items = _D.latest_needs_you(_tasks, _dismissed)
    except Exception:  # noqa: BLE001
        pass
    for t in task_items:
        # Dedup: a blocked ticket is surfaced as its 'parked' row (section 3) ONLY — skip it here so
        # it can never also appear as 'errored'/'pr'. 'parked' wins because autopilot is skipping it.
        if str(t.get("ticket_id") or "") in blocked_ids:
            continue
        outcome = t.get("outcome") or ""
        if outcome == "PR / needs you":
            rows.append({
                **t,
                "category": "pr",
                "why": str(t.get("note") or t.get("pr_url") or "PR opened — review needed"),
            })
        elif outcome in _FAILED_OUTCOMES:
            rows.append({
                **t,
                "category": "errored",
                "why": str(t.get("note") or outcome),
            })

    # ── 3. Parked/blocked tickets — blocked_tickets.json via latest_parked() ─
    # Uses the same ``blocked_ids`` resolved in section 0, so the dedup skip above and the parked
    # rows here are driven by ONE source — they can't disagree on which tickets are blocked.
    parked_items: list[dict] = []
    try:
        from . import dashboard as _D
        if blocked_ids:
            _all_tasks = _D.load_tasks(cfg.audit_path)
            parked_items = _D.latest_parked(_all_tasks, blocked_ids)
    except Exception:  # noqa: BLE001
        pass
    for t in parked_items:
        # Review fix (2026-07-05): skip parked rows already represented by their decision row —
        # one blocked ticket, one row, one badge count.
        if str(t.get("ticket_id") or "") in decision_ids:
            continue
        rows.append({
            **t,
            "category": "parked",
            "why": str(t.get("note") or "blocked — autopilot skipping"),
        })

    # ── 5. Officer recommendations + 6. ticket proposals + 7. specialist rosters ──
    # All three need the Commander, so all three ARE part of the unified inbox (rows + badge count).
    # EU-102 (iter-3): specialist_approvals are now folded into ``rows`` as well, so total == count
    # == len(rows) everywhere and a pending roster can never raise the badge without rendering a row.
    approvals_items: list[dict] = []
    proposal_items: list[dict] = []
    specialist_approval_items: list[dict] = []
    try:
        from . import approvals as _ap
        approvals_items = _ap.pending(cfg) or []
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import approvals as _ap
        proposal_items = _ap.pending_proposals(cfg) or []
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import hr as _hr
        specialist_approval_items = _hr.pending_specialist_approvals(cfg) or []
    except Exception:  # noqa: BLE001
        pass

    for a in approvals_items:
        rows.append({
            **a,
            "category": "approval",
            "why": str(a.get("label") or a.get("kind") or "officer recommendation"),
        })
    for p in proposal_items:
        _n = len(p.get("proposals") or [])
        rows.append({
            **p,
            "category": "proposal",
            "why": str(p.get("source") or f"{_n} ticket(s) to file"),
        })
    for sp in specialist_approval_items:
        _tid = str(sp.get("ticket_id") or "")
        _dom = str(sp.get("domain") or "")
        rows.append({
            **sp,
            "category": "specialist",
            "why": (f"Provision {_dom} specialist squad for {_tid}".strip()
                    if (_dom or _tid) else "specialist roster awaiting approval"),
        })

    return {
        "rows": rows,
        # Per-stream keys — server.py /needs renders each section with its own action form.
        "decisions": decisions_items,
        "approvals": approvals_items,
        "proposals": proposal_items,
        "specialist_approvals": specialist_approval_items,
        "tasks": task_items,
        # ONE number everywhere: count == len(rows) == total (the badge invariant). Every stream that
        # raises ``total`` also appends a row, so the badge can never point at an empty inbox (EU-102).
        "total": len(rows),
    }


def count(cfg: Config) -> int:
    """Badge number — the unified inbox row count (decisions + errored + parked + PRs +
    officer approvals + ticket proposals + specialist rosters).  count == len(summary()['rows'])
    == summary()['total'], always."""
    return len(summary(cfg)["rows"])
