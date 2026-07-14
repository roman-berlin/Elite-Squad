"""EU-315 QA: EU-136's reported symptom — a ticket whose events go build -> gate -> build (a
retry after a failed gate) must render as 'Building' with a retry indicator, and must NEVER get
stuck showing a bare 'Gate' stage.

Before this ticket, `dashboard._load_tasks_uncached()` didn't recognise `gate` audit events at
all, so a run's stage was derived purely from `outcome`/`verdict`/`passes` — there was no way to
surface "this build is a RETRY after a failed gate" to the Commander. This harness plants real
audit-log events (mirrors tests/dashboard_stale_block_test.py's `_write_audit_ev` pattern) and
drives them through the real `dashboard.load_tasks()` parser — not a hand-built task dict — so the
event-parsing logic itself is under test, not just `derive_pipeline_stage`'s branching.

Four checks:
  1. build(1) -> gate(1, failed) -> build(2): derive_pipeline_stage contains 'Building' AND a
     retry marker ('retry'), and NEVER the substring 'Gate' — the direct EU-136 regression.
  2. The SAME task rendered through cockpit_views._pipeline_board() also carries 'Building' and
     'retry' in its row, and never 'Gate'.
  3. Mutation guard: a plain build(1) -> build(2) with NO gate failure in between must NOT show a
     retry marker — proves the marker is conditioned on an actual failed gate, not just passes>1.
  4. A gate that PASSES clears the retry condition: build(1) -> gate(1, passed) -> build(2) must
     not be flagged as a retry either (nothing to retry from).
"""
import sys
import json
import types
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

# --- stub the Agent SDK so orchestrator modules import cleanly (mirrors pipeline_board_test.py) ---
_sdk = types.ModuleType("claude_agent_sdk")
class _Stub:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
_sdk.__getattr__ = lambda _n: _Stub
sys.modules["claude_agent_sdk"] = _sdk
_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules.setdefault("requests", _req)

sys.path.insert(0, ".")

from orchestrator import cockpit_views, dashboard

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail) if not cond else ""))


def _write(audit: Path, event: str, ts: str, **extra) -> None:
    with audit.open("a") as f:
        f.write(json.dumps({"event": event, "ts": ts, **extra}) + "\n")


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _load_one(lines_writer) -> dict:
    """Write the given events to a fresh temp audit log and return the single resulting task row."""
    with tempfile.TemporaryDirectory() as d:
        audit = Path(d) / "audit.jsonl"
        audit.touch()
        lines_writer(audit)
        tasks = dashboard.load_tasks(str(audit))
    assert len(tasks) == 1, f"expected exactly one run, got {len(tasks)}"
    return tasks[0]


# ---------------------------------------------------------------------------
# Test 1 — the EU-136 regression itself: build -> gate(fail) -> build
# ---------------------------------------------------------------------------

def test_build_gate_fail_build_shows_building_with_retry() -> None:
    now = datetime.now()

    def _events(audit: Path) -> None:
        _write(audit, "ticket_start", _ts(now - timedelta(minutes=20)), ticket_id="EU-900",
               app="Elite-Unit")
        _write(audit, "build", _ts(now - timedelta(minutes=18)), ticket_id="EU-900", iteration=1)
        _write(audit, "gate", _ts(now - timedelta(minutes=15)), ticket_id="EU-900", iteration=1,
               passed=False)
        _write(audit, "build", _ts(now - timedelta(minutes=10)), ticket_id="EU-900", iteration=2)

    task = _load_one(_events)
    stage = dashboard.derive_pipeline_stage(task)

    chk("1a. stage contains 'Building'", "Building" in stage, stage)
    chk("1b. stage carries a retry marker", "retry" in stage.lower(), stage)
    chk("1c. stage NEVER gets stuck showing 'Gate'", "Gate" not in stage, stage)


# ---------------------------------------------------------------------------
# Test 2 — the same sequence, rendered through the actual board HTML
# ---------------------------------------------------------------------------

def test_board_renders_retry_indicator() -> None:
    now = datetime.now()

    def _events(audit: Path) -> None:
        _write(audit, "ticket_start", _ts(now - timedelta(minutes=20)), ticket_id="EU-901",
               app="Elite-Unit")
        _write(audit, "build", _ts(now - timedelta(minutes=18)), ticket_id="EU-901", iteration=1)
        _write(audit, "gate", _ts(now - timedelta(minutes=15)), ticket_id="EU-901", iteration=1,
               passed=False)
        _write(audit, "build", _ts(now - timedelta(minutes=10)), ticket_id="EU-901", iteration=2)

    task = _load_one(_events)
    board = cockpit_views._pipeline_board(None, [task], "Elite-Unit")

    chk("2a. board shows the ticket", "EU-901" in board, board)
    chk("2b. board row says 'Building'", "Building" in board, board)
    chk("2c. board row carries a retry marker", "retry" in board.lower(), board)
    chk("2d. board row never shows 'Gate'", "Gate" not in board, board)


# ---------------------------------------------------------------------------
# Test 3 — mutation guard: two plain builds, no gate failure -> no retry marker
# ---------------------------------------------------------------------------

def test_plain_rebuild_without_gate_failure_has_no_retry_marker() -> None:
    now = datetime.now()

    def _events(audit: Path) -> None:
        _write(audit, "ticket_start", _ts(now - timedelta(minutes=20)), ticket_id="EU-902",
               app="Elite-Unit")
        _write(audit, "build", _ts(now - timedelta(minutes=18)), ticket_id="EU-902", iteration=1)
        _write(audit, "build", _ts(now - timedelta(minutes=10)), ticket_id="EU-902", iteration=2)

    task = _load_one(_events)
    stage = dashboard.derive_pipeline_stage(task)

    chk("3a. stage still shows 'Building'", "Building" in stage, stage)
    chk("3b. NO retry marker without a preceding failed gate", "retry" not in stage.lower(), stage)


# ---------------------------------------------------------------------------
# Test 4 — a PASSING gate must not be mistaken for a retry condition
# ---------------------------------------------------------------------------

def test_passing_gate_then_build_has_no_retry_marker() -> None:
    now = datetime.now()

    def _events(audit: Path) -> None:
        _write(audit, "ticket_start", _ts(now - timedelta(minutes=20)), ticket_id="EU-903",
               app="Elite-Unit")
        _write(audit, "build", _ts(now - timedelta(minutes=18)), ticket_id="EU-903", iteration=1)
        _write(audit, "gate", _ts(now - timedelta(minutes=15)), ticket_id="EU-903", iteration=1,
               passed=True)
        _write(audit, "build", _ts(now - timedelta(minutes=10)), ticket_id="EU-903", iteration=2)

    task = _load_one(_events)
    stage = dashboard.derive_pipeline_stage(task)

    chk("4a. stage still shows 'Building'", "Building" in stage, stage)
    chk("4b. NO retry marker after a PASSING gate", "retry" not in stage.lower(), stage)
    chk("4c. never 'Gate' either", "Gate" not in stage, stage)


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

test_build_gate_fail_build_shows_building_with_retry()
test_board_renders_retry_indicator()
test_plain_rebuild_without_gate_failure_has_no_retry_marker()
test_passing_gate_then_build_has_no_retry_marker()

print("\n============= EU-315 PIPELINE BOARD — EU-136 RETRY REGRESSION =============")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
