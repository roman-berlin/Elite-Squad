"""Invariant tests for needs.count() == len(needs.summary() combined items).

Four assertions (EU-67):
  1. count() equals the sum of all item lists in summary() for any state.
  2. A dismissed run item is excluded from both count() and summary() tasks list.
  3. A resolved/answered decision item (absent from pending_decisions.json) is
     excluded from both count() and summary() decisions list.
  4. A stale/phantom dismissed item does not increment the count.

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
    """Re-derive the total from the four item lists — the invariant's ground truth."""
    return (
        len(s.get("decisions", []))
        + len(s.get("approvals", []))
        + len(s.get("proposals", []))
        + len(s.get("tasks", []))
    )


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
# Run all tests
# ---------------------------------------------------------------------------

test_count_equals_summary_len_invariant()
test_dismissed_run_excluded_from_count_and_tasks()
test_resolved_decision_excluded_from_count_and_decisions()
test_phantom_dismissed_item_not_counted()

print("\n======== NEEDS COUNT INVARIANT QA ========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results) - passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
