"""EU-158 — GET /api/merge-stats: merge statistics aggregated by time period.

Covers the ticket's four testable acceptance criteria:
  1. time_range=all -> 200 with total_merges == count of `merged` audit events, any date
  2. time windows filter correctly: today/week count only recent events, month/all count both
  3. success_rate == merged / (merged + pr_opened) within the same window; 0 attempts -> defined,
     non-crashing value (None)
  4. an invalid or missing time_range -> 400 with an error naming today|week|month|all
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import types
from pathlib import Path

# ── minimal SDK / requests stubs so the orchestrator can import without real deps ──
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import server
from orchestrator.config import AppConfig, Config

# ── shared config / audit path / Flask test client ─────────────────────────────
_TMP = Path(tempfile.mkdtemp())
_AUDIT = _TMP / "audit.jsonl"
_CFG = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_TMP), base_branch="DEV",
                     protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(_AUDIT),
    use_worktree=False,
)
_CFG.detected_auth = lambda: "test"

_APP = server.create_app(_CFG)
_CLIENT = _APP.test_client()

# ── result accumulator ──────────────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


def _reset_audit() -> None:
    _AUDIT.parent.mkdir(parents=True, exist_ok=True)
    _AUDIT.write_text("", encoding="utf-8")


_event_seq = 0


def _write_event(event: str, ts_epoch: float) -> None:
    # audit_lines() collapses EXACT-DUPLICATE lines (its local+synced dedup, see dashboard.py) — real
    # audit rows always carry a ticket_id/branch that make same-second events distinct on disk, so a
    # seq field here keeps seeded test rows from accidentally colliding into one.
    global _event_seq
    _event_seq += 1
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts_epoch)), "event": event,
           "ticket_id": f"TEST-{_event_seq}"}
    with _AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


# =============================================================================
# (1) time_range=all -> 200, total_merges == number of seeded `merged` events
# =============================================================================

def _test_all_counts_every_merge_regardless_of_date() -> None:
    _reset_audit()
    now = time.time()
    _write_event("merged", now)
    _write_event("merged", now - 400 * 86400)   # over a year old — "all" must still count it
    _write_event("merged", now - 10 * 86400)
    resp = _CLIENT.get("/api/merge-stats?time_range=all")
    chk("all: HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
    j = json.loads(resp.data)
    chk("all: total_merges == 3", j.get("total_merges") == 3, str(j))
    chk("all: time_range echoed back", j.get("time_range") == "all", str(j))


_test_all_counts_every_merge_regardless_of_date()

# =============================================================================
# (2) time windows filter correctly
# =============================================================================

def _test_windows_filter_by_recency() -> None:
    _reset_audit()
    now = time.time()
    _write_event("merged", now)                  # ~now
    _write_event("merged", now - 10 * 86400)      # ~10 days ago

    today = server.compute_merge_stats(str(_AUDIT), "today", now=now)
    week = server.compute_merge_stats(str(_AUDIT), "week", now=now)
    month = server.compute_merge_stats(str(_AUDIT), "month", now=now)
    all_time = server.compute_merge_stats(str(_AUDIT), "all", now=now)

    chk("today: counts only the recent merge", today["total_merges"] == 1, str(today))
    chk("week: counts only the recent merge", week["total_merges"] == 1, str(week))
    chk("month: counts both merges", month["total_merges"] == 2, str(month))
    chk("all: counts both merges", all_time["total_merges"] == 2, str(all_time))


_test_windows_filter_by_recency()

# =============================================================================
# (3) success_rate == merged / (merged + pr_opened) in the same window
# =============================================================================

def _test_success_rate_computed_from_window() -> None:
    _reset_audit()
    now = time.time()
    for _ in range(3):
        _write_event("merged", now)
    _write_event("pr_opened", now)
    stats = server.compute_merge_stats(str(_AUDIT), "all", now=now)
    chk("success_rate: 3 merged + 1 pr_opened -> 0.75",
        stats["success_rate"] == 0.75, str(stats))


def _test_success_rate_with_no_attempts_is_defined() -> None:
    _reset_audit()
    stats = server.compute_merge_stats(str(_AUDIT), "all", now=time.time())
    chk("success_rate: zero land attempts -> total_merges 0", stats["total_merges"] == 0, str(stats))
    chk("success_rate: zero land attempts -> defined (None), no crash",
        stats["success_rate"] is None, str(stats))


_test_success_rate_computed_from_window()
_test_success_rate_with_no_attempts_is_defined()

# =============================================================================
# (4) invalid / missing time_range -> 400 naming the valid values
# =============================================================================

def _test_invalid_time_range_rejected() -> None:
    _reset_audit()
    resp = _CLIENT.get("/api/merge-stats?time_range=year")
    chk("invalid time_range: HTTP 400", resp.status_code == 400, f"status={resp.status_code}")
    j = json.loads(resp.data)
    err = str(j.get("error", ""))
    chk("invalid time_range: error present", bool(err), str(j))
    for token in ("today", "week", "month", "all"):
        chk(f"invalid time_range: error names '{token}'", token in err, err)


def _test_missing_time_range_rejected() -> None:
    _reset_audit()
    resp = _CLIENT.get("/api/merge-stats")
    chk("missing time_range: HTTP 400", resp.status_code == 400, f"status={resp.status_code}")
    j = json.loads(resp.data)
    chk("missing time_range: error present", bool(j.get("error")), str(j))


_test_invalid_time_range_rejected()
_test_missing_time_range_rejected()

# =============================================================================
# Summary
# =============================================================================
passed_n = sum(1 for _, ok, _ in results if ok)
print(f"\n========= EU-158 merge-stats API tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("------------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
