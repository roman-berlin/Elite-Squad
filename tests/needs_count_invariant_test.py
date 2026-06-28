"""Invariant tests for needs.count() == len(needs.summary()['rows']) == summary()['total'].

EU-102 (iter-3): count() is the length of the flat ``rows`` list, and EVERY Commander-inbox
stream is folded into ``rows`` — decisions, errored/PR runs, parked tickets, officer approvals,
ticket proposals AND specialist rosters.  There is exactly ONE number everywhere:
``count() == len(rows) == total``.  No stream can raise the badge without rendering a row.

Seven test groups:
  1. count() == len(rows) for a typical mixed state (decision + errored).
  2. A dismissed run item is excluded from both count() and summary() tasks list.
  3. A resolved/answered decision is excluded from count() and decisions list.
  4. A stale/phantom dismissed item does not increment count().
  5. specialist_approvals ARE folded into rows/count() AND keep total == count == len(rows)
     (the single-source design — a non-zero badge always points at a real row).
  6. All four category types (decision | errored | parked | pr) each contribute
     exactly one row when seeded individually — count() == len(rows) in each case.
  7. Dedup regression: a blocked ticket whose latest run outcome == 'errored' yields exactly
     ONE row, category 'parked', and NO 'errored' row (with a realistic outcome, not None).

All network/disk calls are replaced with fixture data — no real files touched.
"""
import json
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

# --- stub the Agent SDK so orchestrator modules import cleanly ---
_sdk = types.ModuleType("claude_agent_sdk")
class _Stub:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
_sdk.__getattr__ = lambda _n: _Stub
sys.modules["claude_agent_sdk"] = _sdk

sys.path.insert(0, ".")

from orchestrator import needs, dashboard
from orchestrator.config import Config, AppConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

results: list[tuple[str, bool, str]] = []

def chk(name: str, cond, detail: str = "") -> None:
    """Soft-check: accumulates results, never raises, so all checks run."""
    results.append((name, bool(cond), str(detail) if not cond else ""))


def _make_cfg(tmp: Path) -> Config:
    """Minimal Config pointing at a fresh temp dir."""
    (tmp / "audit.jsonl").write_text("")
    app = AppConfig(
        name="automatixy",
        repo_path=str(tmp),
        base_branch="DEV",
        protected_branch="MAIN",
        backlog_backend="none",
    )
    return Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)


def _total_from_summary(s: dict) -> int:
    """Re-derive the total from the flat ``rows`` list — the invariant's ground truth.

    EU-102 (iter-3): the primary invariant is count() == len(rows) == total.  EVERY stream
    (decisions, errored/PR, parked, approvals, proposals, specialist rosters) is folded into
    ``rows``, so len(rows) is the single source of truth for the badge.
    """
    return len(s.get("rows", []))


# ---------------------------------------------------------------------------
# Fixture factories
# ---------------------------------------------------------------------------

def _decision_item(decision_id: str = "AUTO-9") -> dict:
    return {
        "id": decision_id,
        "app": "automatixy",
        "question": "DD/MM or MM/DD?",
        "summary": "date format",
    }


def _run_item(
    ticket_id: str = "AUTO-7",
    outcome: str = "errored",
    started: str = "2026-06-20T10:00:00",
) -> dict:
    return {
        "ticket_id": ticket_id,
        "outcome": outcome,
        "app": "automatixy",
        "note": "build blew up",
        "started": started,
    }


# ---------------------------------------------------------------------------
# Test 1 — count() == len(all items in summary()) for a typical mixed state
# ---------------------------------------------------------------------------

def test_count_equals_summary_len_invariant() -> None:
    """Invariant: count() must always equal the sum of all item-list lengths in summary()."""
    tmp = Path(tempfile.mkdtemp())
    cfg = _make_cfg(tmp)

    # Inject one decision, one run that needs-you; zero approvals/proposals.
    (tmp / "pending_decisions.json").write_text(json.dumps([_decision_item()]))
    dashboard.load_tasks = lambda _p: [_run_item()]
    dashboard.load_dismissed = lambda _p: {}

    s = needs.summary(cfg)
    derived = _total_from_summary(s)

    chk(
        "1a. count() equals s['total']",
        needs.count(cfg) == s["total"],
        f"count={needs.count(cfg)}  total={s['total']}",
    )
    chk(
        "1b. s['total'] equals sum of all item lists",
        s["total"] == derived,
        f"total={s['total']}  derived={derived}",
    )
    chk(
        "1c. count() equals sum of all item lists (transitive)",
        needs.count(cfg) == derived,
        f"count={needs.count(cfg)}  derived={derived}",
    )


# ---------------------------------------------------------------------------
# Test 2 — dismissed run item excluded from both count() and tasks list
# ---------------------------------------------------------------------------

def test_dismissed_run_excluded_from_count_and_tasks() -> None:
    """A run dismissed before its 'started' time must not appear in tasks or count."""
    tmp = Path(tempfile.mkdtemp())
    cfg = _make_cfg(tmp)

    # AUTO-7 was dismissed at noon; its run started at 10:00 → older → hidden.
    dismissed_store = {"AUTO-7": "2026-06-20T12:00:00+0000"}

    # AUTO-8 is a second ticket that is NOT dismissed — must remain visible.
    runs = [
        _run_item("AUTO-7", started="2026-06-20T10:00:00"),
        _run_item("AUTO-8", started="2026-06-20T10:00:00"),
    ]
    dashboard.load_tasks = lambda _p: runs
    dashboard.load_dismissed = lambda _p: dict(dismissed_store)

    s = needs.summary(cfg)
    task_ids = [t["ticket_id"] for t in s["tasks"]]

    chk(
        "2a. dismissed ticket (AUTO-7) absent from summary tasks list",
        "AUTO-7" not in task_ids,
        f"tasks={task_ids}",
    )
    chk(
        "2b. non-dismissed ticket (AUTO-8) present in tasks list",
        "AUTO-8" in task_ids,
        f"tasks={task_ids}",
    )
    chk(
        "2c. count() does NOT include the dismissed run",
        needs.count(cfg) == _total_from_summary(s),
        f"count={needs.count(cfg)}  derived={_total_from_summary(s)}",
    )
    chk(
        "2d. count() is exactly 1 (only AUTO-8 survives)",
        needs.count(cfg) == 1,
        f"count={needs.count(cfg)}",
    )


# ---------------------------------------------------------------------------
# Test 3 — resolved/answered decision excluded from count() and decisions list
# ---------------------------------------------------------------------------

def test_resolved_decision_excluded_from_count_and_decisions() -> None:
    """A decision answered (removed from pending_decisions.json) must not appear in
    summary decisions or count.  We simulate resolution by writing only the remaining
    open decision to the fixture file (the resolved one was never written)."""
    tmp = Path(tempfile.mkdtemp())
    cfg = _make_cfg(tmp)

    # Only AUTO-9 is still open; AUTO-1's decision was already answered (absent).
    (tmp / "pending_decisions.json").write_text(json.dumps([_decision_item("AUTO-9")]))
    dashboard.load_tasks = lambda _p: []
    dashboard.load_dismissed = lambda _p: {}

    s = needs.summary(cfg)
    decision_ids = [d["id"] for d in s["decisions"]]

    chk(
        "3a. resolved decision (AUTO-1) absent from decisions list",
        "AUTO-1" not in decision_ids,
        f"decisions={decision_ids}",
    )
    chk(
        "3b. open decision (AUTO-9) present in decisions list",
        "AUTO-9" in decision_ids,
        f"decisions={decision_ids}",
    )
    chk(
        "3c. count() == 1 (only the open decision)",
        needs.count(cfg) == 1,
        f"count={needs.count(cfg)}",
    )
    chk(
        "3d. count() == _total_from_summary() invariant holds after resolution",
        needs.count(cfg) == _total_from_summary(s),
        f"count={needs.count(cfg)}  derived={_total_from_summary(s)}",
    )


# ---------------------------------------------------------------------------
# Test 4 — stale/phantom dismissed item does not increment the count
# ---------------------------------------------------------------------------

def test_phantom_dismissed_item_not_counted() -> None:
    """A stale dismissed entry in the dismissed store for a ticket that has no active
    run must not inflate the count.  The dismissed store is consulted only when filtering
    tasks; if the task list is empty there is nothing to show regardless of the store."""
    tmp = Path(tempfile.mkdtemp())
    cfg = _make_cfg(tmp)

    # Dismissed store contains AUTO-99 (phantom — no corresponding task).
    phantom_dismissed = {"AUTO-99": "2026-06-25T08:00:00+0000"}

    # Also add a run for AUTO-7 that is OLDER than its dismissal → filtered out.
    runs = [_run_item("AUTO-7", started="2026-06-20T10:00:00")]
    dismissed_store = {**phantom_dismissed, "AUTO-7": "2026-06-25T09:00:00+0000"}

    dashboard.load_tasks = lambda _p: runs
    dashboard.load_dismissed = lambda _p: dict(dismissed_store)

    s = needs.summary(cfg)

    chk(
        "4a. phantom stale entry does not add items to tasks",
        "AUTO-99" not in [t["ticket_id"] for t in s["tasks"]],
        f"tasks={[t['ticket_id'] for t in s['tasks']]}",
    )
    chk(
        "4b. stale dismissed run (AUTO-7) does not appear in tasks",
        "AUTO-7" not in [t["ticket_id"] for t in s["tasks"]],
        f"tasks={[t['ticket_id'] for t in s['tasks']]}",
    )
    chk(
        "4c. count() is 0 — no live items",
        needs.count(cfg) == 0,
        f"count={needs.count(cfg)}",
    )
    chk(
        "4d. count() == _total_from_summary() invariant holds for phantom scenario",
        needs.count(cfg) == _total_from_summary(s),
        f"count={needs.count(cfg)}  derived={_total_from_summary(s)}",
    )


# ---------------------------------------------------------------------------
# Test 5 — specialist_approvals are folded into rows/count (single-source design)
# ---------------------------------------------------------------------------

def test_specialist_approvals_folded_into_rows_count() -> None:
    """EU-102 (iter-3) single-source design: a pending specialist roster contributes a row
    (category 'specialist'), is counted by count(), and keeps total == count == len(rows).

    This is the fix for the specialist-only dead-end: with only a specialist approval pending the
    badge is non-zero AND the inbox has a real row — never a non-zero badge over an empty inbox.
    """
    tmp = Path(tempfile.mkdtemp())
    cfg = _make_cfg(tmp)

    # Write a pending specialist-approval entry to the file hr.pending_specialist_approvals reads.
    spec_approvals_file = tmp / "pending_specialist_approvals.json"
    spec_approvals_file.write_text(json.dumps({
        "AUTO-10::security": {
            "status": "pending",
            "ticket_id": "AUTO-10",
            "domain": "security",
            "requested_at": "2026-06-28T09:00:00",
        }
    }), encoding="utf-8")

    # No tasks or decisions; only the specialist approval is pending.
    dashboard.load_tasks = lambda _p: []
    dashboard.load_dismissed = lambda _p: {}

    s = needs.summary(cfg)
    rows = s.get("rows", [])

    chk(
        "5a. specialist_approvals stream still present in summary() (bespoke action form)",
        len(s.get("specialist_approvals", [])) == 1,
        f"specialist_approvals={s.get('specialist_approvals')}",
    )
    chk(
        "5b. rows contains exactly one row, category 'specialist'",
        len(rows) == 1 and rows[0].get("category") == "specialist",
        f"rows={rows}",
    )
    chk(
        "5c. count() is 1 — the specialist roster contributes to the rows badge",
        needs.count(cfg) == 1,
        f"count={needs.count(cfg)}",
    )
    chk(
        "5d. count() == len(rows) invariant holds with a specialist row present",
        needs.count(cfg) == len(rows),
        f"count={needs.count(cfg)}  len(rows)={len(rows)}",
    )
    chk(
        "5e. summary()['total'] == len(rows) == count() — single number, no inflation",
        s["total"] == len(rows) == needs.count(cfg),
        f"total={s['total']}  len(rows)={len(rows)}  count={needs.count(cfg)}",
    )
    chk(
        "5f. the specialist row carries a non-empty 'why'",
        bool(str(rows[0].get("why", "")).strip()),
        f"why={rows[0].get('why') if rows else None!r}",
    )


# ---------------------------------------------------------------------------
# Test 6 — all four category types contribute to count() / rows individually
# ---------------------------------------------------------------------------

def test_all_four_category_types_in_rows_invariant() -> None:
    """EU-102: seed one item of each category (decision | errored | parked | pr) and
    verify count() == len(rows) in each isolated sub-scenario.

    Guards against a regression where a new category is added to needs.py but summary()
    forgets to append to ``rows`` (making count() silently under-count).
    """
    # ── 6a. decision category ──────────────────────────────────────────────
    tmp_d = Path(tempfile.mkdtemp())
    cfg_d = _make_cfg(tmp_d)
    (tmp_d / "pending_decisions.json").write_text(json.dumps([_decision_item("AUTO-D1")]))
    dashboard.load_tasks = lambda _p: []
    dashboard.load_dismissed = lambda _p: {}
    s_d = needs.summary(cfg_d)
    chk(
        "6a. decision: count() == len(rows)",
        needs.count(cfg_d) == len(s_d["rows"]),
        f"count={needs.count(cfg_d)}  rows={len(s_d['rows'])}",
    )
    chk(
        "6a. decision: row has 'category' == 'decision'",
        any(r.get("category") == "decision" for r in s_d["rows"]),
        f"rows={s_d['rows']}",
    )
    chk(
        "6a. decision: row has 'why' key",
        all("why" in r for r in s_d["rows"]),
        f"rows={s_d['rows']}",
    )

    # ── 6b. errored category ──────────────────────────────────────────────
    tmp_e = Path(tempfile.mkdtemp())
    cfg_e = _make_cfg(tmp_e)
    dashboard.load_tasks = lambda _p: [_run_item("AUTO-E1", outcome="errored")]
    dashboard.load_dismissed = lambda _p: {}
    s_e = needs.summary(cfg_e)
    chk(
        "6b. errored: count() == len(rows)",
        needs.count(cfg_e) == len(s_e["rows"]),
        f"count={needs.count(cfg_e)}  rows={len(s_e['rows'])}",
    )
    chk(
        "6b. errored: row has 'category' == 'errored'",
        any(r.get("category") == "errored" for r in s_e["rows"]),
        f"rows={s_e['rows']}",
    )

    # ── 6c. parked category ───────────────────────────────────────────────
    tmp_p = Path(tempfile.mkdtemp())
    cfg_p = _make_cfg(tmp_p)
    (tmp_p / "blocked_tickets.json").write_text(json.dumps({"AUTO-P1": "stuck"}), encoding="utf-8")
    dashboard.load_tasks = lambda _p: [_run_item("AUTO-P1", outcome="errored")]
    dashboard.load_dismissed = lambda _p: {}
    s_p = needs.summary(cfg_p)
    chk(
        "6c. parked: count() == len(rows)",
        needs.count(cfg_p) == len(s_p["rows"]),
        f"count={needs.count(cfg_p)}  rows={len(s_p['rows'])}",
    )
    chk(
        "6c. parked: row has 'category' == 'parked'",
        any(r.get("category") == "parked" for r in s_p["rows"]),
        f"rows={s_p['rows']}",
    )

    # ── 6d. pr category ───────────────────────────────────────────────────
    tmp_pr = Path(tempfile.mkdtemp())
    cfg_pr = _make_cfg(tmp_pr)
    dashboard.load_tasks = lambda _p: [_run_item("AUTO-PR1", outcome="PR / needs you")]
    dashboard.load_dismissed = lambda _p: {}
    s_pr = needs.summary(cfg_pr)
    chk(
        "6d. pr: count() == len(rows)",
        needs.count(cfg_pr) == len(s_pr["rows"]),
        f"count={needs.count(cfg_pr)}  rows={len(s_pr['rows'])}",
    )
    chk(
        "6d. pr: row has 'category' == 'pr'",
        any(r.get("category") == "pr" for r in s_pr["rows"]),
        f"rows={s_pr['rows']}",
    )


# ---------------------------------------------------------------------------
# Test 7 — dedup: a blocked ticket whose latest run errored yields ONE 'parked' row
# ---------------------------------------------------------------------------

def test_blocked_errored_dedup_single_parked_row() -> None:
    """EU-102 iter-3 regression (the reason iter-2 was rejected): a ticket present in
    blocked_tickets.json whose latest audit run outcome == 'errored' (a REALISTIC fixture, not
    outcome=None) must produce EXACTLY ONE row — category 'parked' — and NO 'errored' row.

    Before the fix the same ticket was appended twice: once by the latest_needs_you() errored loop
    and once by the latest_parked() loop, so count() over-counted and the inbox listed it twice.
    """
    tmp = Path(tempfile.mkdtemp())
    cfg = _make_cfg(tmp)

    # AUTO-50 is BOTH blocked AND its most recent run errored — the exact double-count trigger.
    (tmp / "blocked_tickets.json").write_text(
        json.dumps({"AUTO-50": "dependency not merged"}), encoding="utf-8")
    dashboard.load_tasks = lambda _p: [_run_item("AUTO-50", outcome="errored")]
    dashboard.load_dismissed = lambda _p: {}

    s = needs.summary(cfg)
    rows = s.get("rows", [])
    auto50_rows = [r for r in rows if str(r.get("ticket_id")) == "AUTO-50"]
    cats = sorted(r.get("category") for r in auto50_rows)

    chk(
        "7a. AUTO-50 produces exactly ONE row (no errored+parked duplicate)",
        len(auto50_rows) == 1,
        f"rows for AUTO-50={auto50_rows}",
    )
    chk(
        "7b. that single row's category is 'parked' (autopilot is skipping it)",
        cats == ["parked"],
        f"categories={cats}",
    )
    chk(
        "7c. there is NO 'errored' row for the blocked ticket",
        not any(r.get("category") == "errored" for r in auto50_rows),
        f"rows for AUTO-50={auto50_rows}",
    )
    chk(
        "7d. count() == len(rows) holds for the blocked+errored fixture",
        needs.count(cfg) == len(rows) == 1,
        f"count={needs.count(cfg)}  len(rows)={len(rows)}",
    )
    chk(
        "7e. total == count == len(rows) (single source) for this fixture",
        s["total"] == needs.count(cfg) == len(rows),
        f"total={s['total']}  count={needs.count(cfg)}  len(rows)={len(rows)}",
    )


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

test_count_equals_summary_len_invariant()
test_dismissed_run_excluded_from_count_and_tasks()
test_resolved_decision_excluded_from_count_and_decisions()
test_phantom_dismissed_item_not_counted()
test_specialist_approvals_folded_into_rows_count()
test_all_four_category_types_in_rows_invariant()
test_blocked_errored_dedup_single_parked_row()

print("\n======== NEEDS COUNT INVARIANT QA ========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results) - passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
