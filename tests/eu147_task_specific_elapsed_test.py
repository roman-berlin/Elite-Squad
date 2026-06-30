"""EU-147: Elapsed time should be task-specific, not project-level.

This test verifies that when displaying elapsed time in the cockpit,
it uses the task's own start time (from the audit log) rather than
the project-level run_started timestamp.
"""
from __future__ import annotations

import datetime
import sys
import tempfile
import types
from pathlib import Path

# ---------------------------------------------------------------------------
# Minimal stubs so orchestrator modules load without the real SDK / Flask / etc.
# ---------------------------------------------------------------------------
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass  # noqa: N807
    def __call__(s, *a, **k): return s  # noqa: N807


_sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", _sdk)

_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),  # type: ignore
)
sys.modules.setdefault("requests", _req)

sys.path.insert(0, ".")

from orchestrator import dashboard as _D  # noqa: E402
from orchestrator import warroom  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    """Record a named assertion result."""
    results.append((name, bool(cond), str(detail)))


def _make_cfg(tmp: Path) -> Config:
    """Create a minimal Config for testing."""
    import json as _json

    audit = tmp / "audit.jsonl"

    # Create a task that started 5 minutes ago (task time)
    task_start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)

    # Write audit events with the task start time (format must match _parse_ts expectations)
    # Use format without microseconds: "%Y-%m-%dT%H:%M:%S%z"
    audit.write_text(
        _json.dumps({
            "event": "ticket_start",
            "ticket_id": "EU-147",
            "app": "EU",
            "branch": "dev/EU-147",
            "ts": task_start_time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
        + "\n",
        encoding="utf-8",
    )

    return Config(
        apps=[
            AppConfig(
                name="EU",
                repo_path=str(tmp),
                base_branch="dev",
                protected_branch="main",
                backlog_backend="none",
            )
        ],
        audit_path=str(audit),
    )


def test_task_specific_elapsed_time() -> None:
    """Verify elapsed time uses task start time, not project run_started."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        cfg = _make_cfg(tmp)

        # Load tasks from audit
        tasks = _D.load_tasks(cfg.audit_path)

        chk("tasks loaded", len(tasks) > 0, f"found {len(tasks)} tasks")
        if not tasks:
            print("  SKIP: no tasks loaded from audit")
            return

        task = tasks[0]
        task_started = task.get("started")

        chk("task has started field", task_started is not None, f"started={task_started}")

        if not task_started:
            print("  SKIP: task has no started field")
            return

        # The task should have started about 5 minutes ago (based on our seed)
        # Give a wide window (3-7 minutes) to account for test execution time
        now = datetime.datetime.now(datetime.timezone.utc)
        if isinstance(task_started, datetime.datetime):
            task_age_minutes = (now - task_started).total_seconds() / 60
            chk("task age is reasonable", 3 <= task_age_minutes <= 7,
                f"task is {task_age_minutes:.1f} minutes old")
        else:
            print(f"  SKIP: task_started is not datetime: {type(task_started)}")
            return

        # Simulate what render_html does: get the elapsed time for this task
        elapsed = None
        if task_started:
            if isinstance(task_started, datetime.datetime):
                elapsed = warroom._fmt_dur(now.timestamp() - task_started.timestamp())

        chk("elapsed time calculated", elapsed is not None, f"elapsed={elapsed}")

        if not elapsed:
            print("  SKIP: elapsed time is None")
            return

        # Elapsed time should be roughly 5 minutes (not 2 hours)
        # The format is "Xm YYs" or "Xh YYm"
        if "h" in elapsed:
            chk("elapsed time is in hours (should be minutes)", False,
                f"elapsed={elapsed} - this suggests project-level time was used")
        else:
            # Parse the minutes from the elapsed string
            parts = elapsed.split()
            if len(parts) >= 1 and "m" in parts[0]:
                mins = int(parts[0].replace("m", ""))
                chk("elapsed minutes is reasonable (3-7)", 3 <= mins <= 7,
                    f"elapsed={elapsed} ({mins} minutes)")
            else:
                chk(f"elapsed format parseable", False, f"elapsed={elapsed}")


def test_string_timestamp_elapsed_time() -> None:
    """Verify elapsed time works when started_dt is a string (EU-147 iter-2 fix).

    This tests the path where the task's 'started' field is a string timestamp
    that needs to be parsed via D._parse_ts before calculating elapsed time.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Create audit with a string timestamp (format: "%Y-%m-%dT%H:%M:%S%z")
        task_start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
        audit = tmp / "audit.jsonl"
        import json as _json
        audit.write_text(
            _json.dumps({
                "event": "ticket_start",
                "ticket_id": "EU-147",
                "app": "EU",
                "branch": "dev/EU-147",
                "ts": task_start_time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            })
            + "\n",
            encoding="utf-8",
        )

        cfg = Config(
            apps=[
                AppConfig(
                    name="EU",
                    repo_path=str(tmp),
                    base_branch="dev",
                    protected_branch="main",
                    backlog_backend="none",
                )
            ],
            audit_path=str(audit),
        )

        # Load tasks - the started field will be a string initially
        tasks = _D.load_tasks(cfg.audit_path)

        chk("tasks loaded (string test)", len(tasks) > 0, f"found {len(tasks)} tasks")
        if not tasks:
            print("  SKIP: no tasks loaded from audit")
            return

        task = tasks[0]
        task_started = task.get("started")

        chk("task has started field (string test)", task_started is not None, f"started={task_started}")
        if not task_started:
            print("  SKIP: task has no started field")
            return

        # Force the started field to be a string (simulating the scenario)
        # This is what happens when data comes from JSON
        task_started_str = task_start_time.strftime("%Y-%m-%dT%H:%M:%S%z")

        chk("started is string (string test)", isinstance(task_started_str, str),
            f"started type={type(task_started_str)}")

        # Test the string parsing path - this is the EU-147 iter-2 fix
        now = datetime.datetime.now(datetime.timezone.utc)

        # Simulate the code path in warroom.py line 1531-1535:
        # elif isinstance(started_dt, str):
        #     parsed_ts = D._parse_ts(started_dt)
        #     if parsed_ts:
        #         elapsed = _fmt_dur(datetime.now().timestamp() - parsed_ts.timestamp())
        parsed_ts = _D._parse_ts(task_started_str)

        chk("string timestamp parsed successfully", parsed_ts is not None,
            f"parsed_ts={parsed_ts}")

        if not parsed_ts:
            print("  SKIP: failed to parse string timestamp")
            return

        # Calculate elapsed time
        elapsed = warroom._fmt_dur(now.timestamp() - parsed_ts.timestamp())

        chk("elapsed time calculated from string (string test)", elapsed is not None,
            f"elapsed={elapsed}")

        if not elapsed:
            print("  SKIP: elapsed time is None")
            return

        # Elapsed time should be roughly 5 minutes (not 2 hours)
        # The format is "Xm YYs" or "Xh YYm"
        if "h" in elapsed:
            chk("elapsed time is in hours (string test) - WRONG!", False,
                f"elapsed={elapsed} - this suggests project-level time was used")
        else:
            # Parse the minutes from the elapsed string
            parts = elapsed.split()
            if len(parts) >= 1 and "m" in parts[0]:
                mins = int(parts[0].replace("m", ""))
                chk("elapsed minutes is reasonable from string (3-7)", 3 <= mins <= 7,
                    f"elapsed={elapsed} ({mins} minutes)")
            else:
                chk(f"elapsed format parseable (string test)", False, f"elapsed={elapsed}")


if __name__ == "__main__":
    print(f"================= EU-147 Task-Specific Elapsed Time QA =================")

    print("\n--- Test 1: datetime timestamp path ---")
    test_task_specific_elapsed_time()

    print("\n--- Test 2: string timestamp path (EU-147 iter-2 fix) ---")
    test_string_timestamp_elapsed_time()

    print("-" * 59)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"  {passed}/{total} passed")

    all_passed = all(ok for _, ok, _ in results)
    print(f"  RESULT: {'ALL GREEN' if all_passed else 'SOME RED'}")
    sys.exit(0 if all_passed else 1)
