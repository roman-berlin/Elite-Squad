"""Unified-inbox integration test (EU-102).

Seeds one item of each category (decision | errored | parked | pr), calls
needs.summary(), and asserts:
  • count() == len(rows)                          — the headline invariant
  • every row carries both 'category' and 'why'  — structural contract
  • each category is represented exactly once     — coverage check

All disk I/O is replaced by fixture data; no real files are written except the
temp dirs created by tempfile.mkdtemp().
"""
import json
import sys
import tempfile
import types
from pathlib import Path

# ── stub out the Agent SDK so orchestrator modules import cleanly ──────────
_sdk = types.ModuleType("claude_agent_sdk")


class _Stub:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self


_sdk.__getattr__ = lambda _n: _Stub
sys.modules["claude_agent_sdk"] = _sdk

sys.path.insert(0, ".")

from orchestrator import needs, dashboard
from orchestrator.config import Config, AppConfig

# ── test-result accumulator ───────────────────────────────────────────────

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    """Soft-check: accumulates without raising so all assertions run."""
    results.append((name, bool(cond), str(detail) if not cond else ""))


# ── helpers ───────────────────────────────────────────────────────────────

def _make_cfg(tmp: Path) -> Config:
    """Minimal Config backed by a fresh empty temp dir."""
    (tmp / "audit.jsonl").write_text("")
    app = AppConfig(
        name="automatixy",
        repo_path=str(tmp),
        base_branch="DEV",
        protected_branch="MAIN",
        backlog_backend="none",
    )
    return Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)


# ── main test: one item per category ─────────────────────────────────────

def test_unified_inbox_all_four_categories() -> None:
    """Seed one item of each category type and assert the structural invariants.

    decision — pending_decisions.json entry
    errored  — task with outcome 'errored'
    parked   — task that also appears in blocked_tickets.json
    pr       — task with outcome 'PR / needs you'
    """
    tmp = Path(tempfile.mkdtemp())
    cfg = _make_cfg(tmp)

    # ── 1. decision ───────────────────────────────────────────────────────
    (tmp / "pending_decisions.json").write_text(json.dumps([
        {"id": "AUTO-D1", "app": "automatixy", "question": "Use tabs or spaces?",
         "summary": "style guide"},
    ]), encoding="utf-8")

    # ── 2. errored + 3. pr (two task-stream items) ───────────────────────
    errored_run = {
        "ticket_id": "AUTO-E1",
        "outcome": "errored",
        "app": "automatixy",
        "note": "build blew up",
        "started": "2026-06-28T08:00:00",
    }
    pr_run = {
        "ticket_id": "AUTO-PR1",
        "outcome": "PR / needs you",
        "app": "automatixy",
        "note": "PR opened — please review",
        "started": "2026-06-28T09:00:00",
    }
    # 4. parked — must also appear in blocked_tickets.json.
    # REALISTIC fixture (EU-102 iter-3): outcome='errored' — the same ticket is BOTH blocked AND its
    # latest run errored. needs.summary() must dedup this to a single 'parked' row (parked wins
    # because autopilot is skipping it); it must NOT double-count as errored + parked.
    parked_run = {
        "ticket_id": "AUTO-P1",
        "outcome": "errored",
        "app": "automatixy",
        "note": "stuck — skip for now",
        "started": "2026-06-28T07:00:00",
    }

    dashboard.load_tasks = lambda _p: [errored_run, pr_run, parked_run]
    dashboard.load_dismissed = lambda _p: {}

    # blocked_tickets.json marks AUTO-P1 as parked.
    (tmp / "blocked_tickets.json").write_text(
        json.dumps({"AUTO-P1": "dependency not merged"}), encoding="utf-8"
    )

    s = needs.summary(cfg)
    rows = s.get("rows", [])

    # ── invariant: count() == len(rows) ───────────────────────────────────
    chk(
        "count() == len(rows)",
        needs.count(cfg) == len(rows),
        f"count={needs.count(cfg)}  len(rows)={len(rows)}",
    )

    # ── structural: every row has 'category' and 'why' ───────────────────
    missing_category = [r for r in rows if "category" not in r]
    missing_why = [r for r in rows if "why" not in r]
    chk(
        "every row has 'category' key",
        not missing_category,
        f"rows missing 'category': {missing_category}",
    )
    chk(
        "every row has 'why' key",
        not missing_why,
        f"rows missing 'why': {missing_why}",
    )

    # ── coverage: each category appears exactly once ──────────────────────
    category_counts: dict[str, int] = {}
    for r in rows:
        cat = r.get("category", "")
        category_counts[cat] = category_counts.get(cat, 0) + 1

    for cat in ("decision", "errored", "parked", "pr"):
        chk(
            f"category '{cat}' appears exactly once in rows",
            category_counts.get(cat, 0) == 1,
            f"category_counts={category_counts}",
        )

    # ── total row count == 4 (one per category) ───────────────────────────
    chk(
        "rows has exactly 4 items (one per category)",
        len(rows) == 4,
        f"len(rows)={len(rows)}  rows={[r.get('category') for r in rows]}",
    )

    # ── dedup: the blocked+errored ticket yields ONLY a 'parked' row ──────
    auto_p1 = [r for r in rows if str(r.get("ticket_id")) == "AUTO-P1"]
    chk(
        "blocked+errored AUTO-P1 produces exactly one row",
        len(auto_p1) == 1,
        f"AUTO-P1 rows={auto_p1}",
    )
    chk(
        "blocked+errored AUTO-P1 row is category 'parked', not 'errored'",
        len(auto_p1) == 1 and auto_p1[0].get("category") == "parked",
        f"AUTO-P1 rows={auto_p1}",
    )

    # ── 'why' strings are non-empty for each row ─────────────────────────
    empty_why = [r for r in rows if not str(r.get("why", "")).strip()]
    chk(
        "no row has an empty 'why' string",
        not empty_why,
        f"rows with empty 'why': {empty_why}",
    )


# ── run ───────────────────────────────────────────────────────────────────

test_unified_inbox_all_four_categories()

print("\n======== NEEDS UNIFIED INBOX QA ========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("-----------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results) - passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
