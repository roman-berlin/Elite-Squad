"""SWE-bench run reporting — EU-71.

Consumes the per-task result list produced by swebench_builder.run_benchmark()
and does two things:

  1. Prints a human-readable summary to stdout (or any file object):

       Builder: 7/20 solved (35.0%)
       ────────────────────────────────────────
       PASS  astropy__astropy-12907
       FAIL  django__django-11001
       ...
       [ABORTED by budget after 15/20 tasks]

  2. Appends one JSON line to audit/swebench_runs.jsonl (one line per run),
     mirroring the AuditLog.record() pattern from orchestrator/audit.py for
     thread + process safety:

       {"ts": "...", "event": "swebench_run", "sample_size": N,
        "score": 0.35, "passed": 7, "total": 20, "cost_usd": 4.21,
        "aborted": false, "per_task": [...]}

The module is designed to be importable from swebench_builder.py and also
runnable as a CLI to replay / display a previously-written JSONL file.

Weekly deterministic seed helper:

    weekly_seed() -> str   — returns a seed stable across the whole ISO week,
                             e.g. "elite-unit-swebench-weekly-2026W26".
                             Callers use random.Random(seed).sample(tasks, n)
                             so the same week always draws the same subset.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Optional

try:  # POSIX advisory file locking; absent on Windows
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

# Repo-root-relative default location — mirrors orchestrator/audit.py convention.
# Resolved at import time so callers can rely on the default without knowing cwd.
_REPO_ROOT = Path(__file__).parent.parent
AUDIT_JSONL: Path = _REPO_ROOT / "audit" / "swebench_runs.jsonl"

# Thread-level lock serialises concurrent appends within one process
# (same pattern as orchestrator/audit.py's _WRITE_LOCK).
_WRITE_LOCK = threading.Lock()

# Table column widths for the per-task pass/fail grid.
_PASS_COL = 4   # "PASS" / "FAIL"
_SEP = "─" * 44


# ── public helpers ────────────────────────────────────────────────────────────

def weekly_seed() -> str:
    """Return a deterministic seed string for the current ISO calendar week.

    The seed is stable for the entire week, so running the benchmark multiple
    times in the same week always draws the same random task sample.

    Example: "elite-unit-swebench-weekly-2026W26"

    Returns:
        A string suitable for passing to ``random.Random(seed)``.
    """
    now = datetime.now(tz=timezone.utc)
    iso_year, iso_week, _ = now.isocalendar()
    return f"elite-unit-swebench-weekly-{iso_year}W{iso_week:02d}"


def print_summary(summary: dict, *, file: IO[str] | None = None) -> None:
    """Print a human-readable pass/fail report to *file* (default: stdout).

    Output format::

        Builder: 7/20 solved (35.0%)
        ────────────────────────────────────────────
        PASS  astropy__astropy-12907
        FAIL  django__django-11001
        ...
        [ABORTED by budget — partial results above]

    Args:
        summary: The dict returned by ``swebench_builder.run_benchmark()``.
                 Must contain keys: ``passed``, ``total``, ``pass_rate``,
                 ``results`` (list of ``{task_id, passed}``), ``aborted``.
        file:    Output file object.  Defaults to ``sys.stdout``.
    """
    out = file if file is not None else sys.stdout

    passed: int = summary.get("passed", 0)
    total: int = summary.get("total", 0)
    pass_rate: float = summary.get("pass_rate", 0.0)
    results: list[dict] = summary.get("results", [])
    aborted: bool = bool(summary.get("aborted", False))

    # ── headline ──────────────────────────────────────────────────────────────
    pct = pass_rate * 100
    print(f"Builder: {passed}/{total} solved ({pct:.1f}%)", file=out)

    # ── per-task table ────────────────────────────────────────────────────────
    if results:
        print(_SEP, file=out)
        for row in results:
            task_id: str = row.get("task_id", "(unknown)")
            did_pass: bool = bool(row.get("passed", False))
            label = "PASS" if did_pass else "FAIL"
            print(f"{label:<{_PASS_COL}}  {task_id}", file=out)
        print(_SEP, file=out)

    # ── footer ────────────────────────────────────────────────────────────────
    cost: float = summary.get("cost_usd", 0.0)
    cost_str = f"  |  cost ${cost:.4f}" if cost else ""
    print(f"Total: {passed}/{total} passed{cost_str}", file=out)

    if aborted:
        print("[ABORTED by budget — partial results above]", file=out)


def append_run(
    summary: dict,
    jsonl_path: Path | None = None,
) -> None:
    """Append one JSON line describing this benchmark run to *jsonl_path*.

    Mirrors the AuditLog.record() pattern from orchestrator/audit.py:
    thread-safe via ``_WRITE_LOCK``; process-safe via ``fcntl.flock`` on POSIX.

    Record shape::

        {
          "ts":          "2026-06-27T10:30:00+0000",
          "event":       "swebench_run",
          "sample_size": 20,
          "score":       0.35,
          "passed":      7,
          "total":       20,
          "cost_usd":    4.2100,
          "aborted":     false,
          "per_task":    [{"task_id": "...", "passed": true}, ...]
        }

    Args:
        summary:     The dict returned by ``swebench_builder.run_benchmark()``.
        jsonl_path:  Path to the ``.jsonl`` file.  Defaults to
                     ``audit/swebench_runs.jsonl`` under the repo root.

    Raises:
        OSError: If the file cannot be opened or written.
    """
    path = jsonl_path if jsonl_path is not None else AUDIT_JSONL
    path.parent.mkdir(parents=True, exist_ok=True)

    passed: int = summary.get("passed", 0)
    total: int = summary.get("total", 0)
    score: float = summary.get("pass_rate", round(passed / total, 4) if total else 0.0)

    # Build a compact per_task list (drop verbose test_output / error fields to
    # keep each JSONL line scannable; the full summary JSON can be kept separately).
    per_task: list[dict[str, Any]] = [
        {"task_id": r.get("task_id", ""), "passed": bool(r.get("passed", False))}
        for r in summary.get("results", [])
    ]

    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": "swebench_run",
        "sample_size": total,
        "score": score,
        "passed": passed,
        "total": total,
        "cost_usd": round(float(summary.get("cost_usd", 0.0)), 4),
        "aborted": bool(summary.get("aborted", False)),
        "per_task": per_task,
    }
    line = json.dumps(record, default=str) + "\n"

    with _WRITE_LOCK:
        with path.open("a", encoding="utf-8") as fh:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(line)
                fh.flush()
            finally:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def report(
    summary: dict,
    jsonl_path: Optional[Path] = None,
    *,
    file: IO[str] | None = None,
) -> None:
    """Print the human-readable summary AND append the JSONL audit record.

    This is the single call that swebench_builder.run_benchmark() makes after
    collecting results.  It replaces the inline print in the original builder
    so both outputs are produced atomically from one place.

    Args:
        summary:    Dict returned by ``run_benchmark()``.
        jsonl_path: Optional override for the JSONL output path.
        file:       Optional override for the human-readable output stream
                    (defaults to sys.stdout).
    """
    print_summary(summary, file=file)
    append_run(summary, jsonl_path=jsonl_path)
