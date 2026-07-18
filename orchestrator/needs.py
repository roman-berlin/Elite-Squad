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

The same items are ALSO exposed under their original keys (``decisions``, ``approvals``,
``proposals``, ``tasks``) so server.py can render each section with its bespoke action form. There
is exactly ONE number everywhere — ``count() == len(rows) == total`` — and no stream can raise the
badge without also rendering in the inbox.

Dedup: a ticket in blocked_tickets.json yields exactly ONE row, category ``parked`` (autopilot is
skipping it), even when its latest run also errored — the errored/PR loop skips blocked ticket ids.

Defensive end-to-end: any missing/half-written source degrades to an empty stream, never a crash.

EU-129 — per-project scoping + a single audit parse per render:

  Before this fix ``summary()``/``count()`` took no ``app_name`` and always aggregated EVERY
  project, so the Needs-you badge/KPI card showed the SAME number on every cockpit tab. Data is
  already app-tagged (decisions/approvals/proposals carry an ``app`` field; audit task rows carry
  ``app`` too), so both entry points now take an ``app_name`` parameter and filter rows to the
  active project — by the ``app`` field for decisions/approvals/proposals, and by ticket-key
  prefix (``<PREFIX>-123`` -> ``PREFIX``) for errored/parked/PR rows, which don't carry an ``app``
  field of their own. ``ALL_PROJECTS`` (a sentinel, matching
  ``cockpit_state.ALL_PROJECTS_SENTINEL``) preserves the old "aggregate everything" behaviour.

  Separately, ``summary()`` used to call ``dashboard.load_tasks(cfg.audit_path)`` TWICE per call
  (once for the errored/PR pass, once for the parked pass) — a full, uncached parse of the audit
  log each time — and the cockpit board calls ``summary()`` twice per render (KPI card + side
  panel), so one tab switch could parse the audit up to 4x. ``load_tasks`` is now called exactly
  ONCE per ``summary()`` call and the same list is reused for both passes, and the whole ``summary()``
  result is memoized for a few seconds per ``(audit signature, app_name)`` so the KPI card and the
  side panel share one computation per render instead of recomputing it twice.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .config import Config

# Outcomes from dashboard._NEEDS_YOU that are NOT a PR — shown under category "errored".
_FAILED_OUTCOMES = {"errored", "escalated", "awaiting decision"}

# The "All projects" sentinel — mirrors cockpit_state.ALL_PROJECTS_SENTINEL ("*"). Accepted here
# too (not just imported) so needs.py has no hard dependency on cockpit_state at import time.
ALL_PROJECTS = "*"

# EU-129: short-lived memo of summary() results, keyed on (audit signature, app_name), so a burst
# of renders for the SAME tab (KPI card + side panel, or a fast poll) share one computation instead
# of re-parsing the audit + re-reading every source file on each call. TTL is intentionally short —
# long enough to collapse the handful of calls in a single render, short enough that a fresh
# decision/approval/run is never stale for more than a moment.
_SUMMARY_CACHE: "dict[tuple, tuple[float, dict]]" = {}
_SUMMARY_TTL = 3.0
# EU-362: the cache key embeds the audit signature + per-source-file mtimes, so every audit append
# orphans ALL previously cached entries — their keys can never be looked up again — while nothing
# evicted them: a long-lived serve process retained one dead entry per audit append per open tab
# (2026-07-16 total audit, item 3). Bounds, enforced on every write: expired entries are purged
# (past the TTL they can never be served), and the cache is hard-capped, dropping oldest-stamped
# first. The cap only needs to cover the handful of (audit signature, app scope) keys live inside
# one TTL window — one per open tab plus the "*" aggregate — so 16 is generous.
_SUMMARY_CACHE_MAX = 16


def _ticket_prefix(ticket_id) -> str:
    """The project-key prefix of a ticket id, e.g. 'AUTO-123' -> 'AUTO'. Empty when unparseable."""
    s = str(ticket_id or "").strip().upper()
    if "-" not in s:
        return ""
    return s.split("-", 1)[0]


def _row_matches_app(row: dict, app_name: str, app_prefix: str) -> bool:
    """Whether ``row`` belongs to ``app_name`` — by the row's own ``app``/``app_name`` field when
    present (decisions/approvals/proposals/tasks carry ``app``; specialist rosters carry
    ``app_name``), else by ticket-key prefix (covers rows that key on ``ticket_id`` but carry
    neither field)."""
    row_app = row.get("app") or row.get("app_name")
    if row_app:
        return str(row_app) == app_name
    tid = row.get("ticket_id") or row.get("id") or ""
    return bool(app_prefix) and _ticket_prefix(tid) == app_prefix


def _cache_key(cfg: Config, app_name: Optional[str]) -> tuple:
    """Memoization key: the audit signature (so any audit change busts the cache) plus every other
    per-app source file's mtime signature, plus the app scope itself."""
    from . import dashboard as _D

    paths = _D._audit_paths(cfg.audit_path)
    sig = _D._audit_sig(paths)
    extra_sigs = []
    for fname in ("pending_decisions.json", "blocked_tickets.json",
                  "approvals.json", "proposals.json"):
        fp = Path(cfg.audit_path).with_name(fname)
        try:
            st = fp.stat()
            extra_sigs.append((fname, st.st_size, st.st_mtime_ns))
        except OSError:
            extra_sigs.append((fname, None, None))
    return (sig, tuple(extra_sigs), app_name or ALL_PROJECTS)


def summary(cfg: Config, app_name: Optional[str] = None) -> dict:
    """Unified inbox: a flat ``rows`` list (typed, with category + why) plus backward-compat keys.

    Each row is a copy of the source item extended with:
      ``category`` — one of: decision | errored | parked | pr | approval | proposal
      ``why``      — one-line human reason string (question text, note, or fallback label)

    ``total`` == ``len(rows)`` == ``count()`` — one number for every Needs-you surface (EU-102).

    ``app_name`` (EU-129) scopes every stream to ONE project — by ``app`` field where the row
    carries one, else by ticket-key prefix. Pass ``None``/``""``/``ALL_PROJECTS`` (``"*"``) for the
    old unit-wide aggregate (the "All projects" view). Memoized for a few seconds per
    (audit signature, app_name) so repeated calls in the same render (KPI card + side panel) don't
    re-parse the audit or re-read the source files more than once (EU-129 perf fix).
    """
    import time

    key = _cache_key(cfg, app_name)
    now = time.time()
    hit = _SUMMARY_CACHE.get(key)
    if hit is not None and now - hit[0] < _SUMMARY_TTL:
        return hit[1]
    result = _summary_uncached(cfg, app_name)
    # EU-362: bound the cache on the write path. Purge entries past the TTL first (signature-pinned
    # keys make them unreachable, not just stale), then LRU-cap what survives so a burst of audit
    # appends can't grow the dict between purges.
    for k in [k for k, (ts, _) in _SUMMARY_CACHE.items() if now - ts >= _SUMMARY_TTL]:
        _SUMMARY_CACHE.pop(k, None)
    while len(_SUMMARY_CACHE) >= _SUMMARY_CACHE_MAX:
        oldest = min(_SUMMARY_CACHE.items(), key=lambda kv: kv[1][0])[0]
        _SUMMARY_CACHE.pop(oldest, None)
    _SUMMARY_CACHE[key] = (now, result)
    return result


def _summary_uncached(cfg: Config, app_name: Optional[str]) -> dict:
    import json

    scoped = bool(app_name) and app_name != ALL_PROJECTS
    # Ticket-key prefix match for rows with no 'app' field of their own (errored/parked/PR
    # rows key on ticket_id, e.g. 'AUTO-123' / 'EU-129') — most app names ARE their
    # Jira project key, so the raw (uppercased) app_name doubles as the prefix to match against.
    app_prefix = str(app_name).strip().upper() if scoped else ""

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
    if scoped:
        decisions_items = [d for d in decisions_items if _row_matches_app(d, app_name, app_prefix)]
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

    # ── 2, 3 & 4. Load the audit-derived task list EXACTLY ONCE (EU-129 perf fix) ──
    # dashboard.load_tasks() is a full parse of the (potentially multi-thousand-line) audit log.
    # It used to be called separately for the errored/PR pass (below) AND the parked pass (section
    # 3) — two full parses per summary() call, times two summary() calls per render (KPI + side
    # panel) = up to 4x per tab switch. Loaded once here and reused for both passes below.
    all_tasks: list[dict] = []
    dismissed: dict = {}
    try:
        from . import dashboard as _D
        all_tasks = _D.load_tasks(cfg.audit_path)
        dismissed = _D.load_dismissed(cfg.audit_path)
    except Exception:  # noqa: BLE001
        pass

    # ── 2 & 4. latest_needs_you() → errored | escalated → "errored", PR → "pr" ──
    task_items: list[dict] = []
    try:
        from . import dashboard as _D
        # One row per ticket (latest run); stale / dismissed runs are already filtered out.
        task_items = _D.latest_needs_you(all_tasks, dismissed)
    except Exception:  # noqa: BLE001
        pass
    if scoped:
        task_items = [t for t in task_items if _row_matches_app(t, app_name, app_prefix)]
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
    # Reuses ``all_tasks`` loaded above — no second load_tasks() call (EU-129 perf fix).
    parked_items: list[dict] = []
    try:
        from . import dashboard as _D
        if blocked_ids:
            parked_items = _D.latest_parked(all_tasks, blocked_ids)
    except Exception:  # noqa: BLE001
        pass
    if scoped:
        parked_items = [t for t in parked_items if _row_matches_app(t, app_name, app_prefix)]
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

    # ── 5. Officer recommendations + 6. ticket proposals ──
    # Both need the Commander, so both ARE part of the unified inbox (rows + badge count), so
    # total == count == len(rows) everywhere and a pending item can never raise the badge without
    # rendering a row.
    # EU-129: officer recommendations (drill/adjutant) are unit-wide, NOT per-project — they cover
    # doctrine/personnel actions, not a single app's tickets — so they are intentionally NOT filtered
    # by app_name and always show on every tab (same as "All projects"). Proposals DO carry a project
    # scope and are filtered.
    approvals_items: list[dict] = []
    proposal_items: list[dict] = []
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
    if scoped:
        proposal_items = [p for p in proposal_items if _row_matches_app(p, app_name, app_prefix)]

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

    return {
        "rows": rows,
        # Per-stream keys — server.py /needs renders each section with its own action form.
        "decisions": decisions_items,
        "approvals": approvals_items,
        "proposals": proposal_items,
        "tasks": task_items,
        # ONE number everywhere: count == len(rows) == total (the badge invariant). Every stream that
        # raises ``total`` also appends a row, so the badge can never point at an empty inbox (EU-102).
        "total": len(rows),
    }


def clear_cache() -> None:
    """Drop the summary() memo cache — test seam / call after mutating source files in a test that
    needs the very next summary()/count() call to be a guaranteed fresh (uncached) read."""
    _SUMMARY_CACHE.clear()


def count(cfg: Config, app_name: Optional[str] = None) -> int:
    """Badge number — the unified inbox row count (decisions + errored + parked + PRs +
    officer approvals + ticket proposals).  count == len(summary()['rows'])
    == summary()['total'], always.

    EU-129: ``app_name`` scopes the count to ONE project (``None``/``ALL_PROJECTS`` aggregates
    every project, preserving the old behaviour)."""
    return len(summary(cfg, app_name)["rows"])
