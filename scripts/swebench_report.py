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

CLI usage:

    python3 scripts/swebench_report.py [--trend N] [--file PATH]

    --trend N    Print a dated table of the last N runs from the JSONL log.
                 N defaults to 10 when the flag is given without a value.
    --file PATH  Path to the JSONL file (defaults to audit/swebench_runs.jsonl).

Weekly deterministic seed helper:

    weekly_seed() -> str   — returns a seed stable across the whole ISO week,
                             e.g. "elite-unit-swebench-weekly-2026W26".
                             Callers use random.Random(seed).sample(tasks, n)
                             so the same week always draws the same subset.
"""
from __future__ import annotations

import argparse
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


# ── trend viewer ──────────────────────────────────────────────────────────────

# Column widths for the trend table.
_TS_COL = 20       # timestamp (trimmed to 19 chars "YYYY-MM-DDTHH:MM:SS")
_SAMPLE_COL = 6    # "Sample"
_RATE_COL = 7      # "Pass%"
_COST_COL = 9      # "Cost"
_ABORT_COL = 7     # "Aborted"
_TREND_SEP = "─" * (_TS_COL + 1 + _SAMPLE_COL + 1 + _RATE_COL + 1 + _COST_COL + 1 + _ABORT_COL)


def print_trend(
    jsonl_path: Path | None = None,
    n: int = 10,
    file: IO[str] | None = None,
) -> None:
    """Print a dated table of the last *n* benchmark runs from *jsonl_path*.

    Reads ``audit/swebench_runs.jsonl`` (or the given path) and renders the
    last *n* ``swebench_run`` entries as a fixed-width table so regressions and
    improvements are visible at a glance::

        Date/Time             Sample  Pass%    Cost       Aborted
        ──────────────────────────────────────────────────────────
        2026-06-27T10:30:00   20      35.0%    $4.2100    no
        2026-06-28T08:00:00   50      42.0%    $9.8700    yes

    If the file does not exist or contains no matching records, a short notice
    is printed instead of the table.

    Args:
        jsonl_path: Path to the JSONL audit file.  Defaults to
                    ``audit/swebench_runs.jsonl`` under the repo root.
        n:          Maximum number of runs to show (most recent first, then
                    printed oldest→newest so the trend reads left-to-right in
                    time).  If *n* exceeds the number of available rows, all
                    available rows are shown without error.
        file:       Output stream.  Defaults to ``sys.stdout``.
    """
    out = file if file is not None else sys.stdout
    path = jsonl_path if jsonl_path is not None else AUDIT_JSONL

    # ── read + filter ─────────────────────────────────────────────────────────
    rows: list[dict[str, Any]] = []
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue  # skip malformed lines — fail-safe, not fail-silent
            if record.get("event") == "swebench_run":
                rows.append(record)

    if not rows:
        print("(no swebench runs recorded yet)", file=out)
        return

    # Take the last N rows; display oldest→newest so the trend is readable.
    tail = rows[-n:] if n < len(rows) else rows

    # ── header ────────────────────────────────────────────────────────────────
    header = (
        f"{'Date/Time':<{_TS_COL}}  "
        f"{'Sample':>{_SAMPLE_COL}}  "
        f"{'Pass%':>{_RATE_COL}}  "
        f"{'Cost':>{_COST_COL}}  "
        f"{'Aborted':>{_ABORT_COL}}"
    )
    print(header, file=out)
    print(_TREND_SEP, file=out)

    # ── rows ──────────────────────────────────────────────────────────────────
    for row in tail:
        ts_raw: str = str(row.get("ts", ""))
        ts_display = ts_raw[:19]  # keep only "YYYY-MM-DDTHH:MM:SS"

        sample: int = int(row.get("sample_size", row.get("total", 0)))
        score: float = float(row.get("score", 0.0))
        pass_pct = f"{score * 100:.1f}%"

        cost: float = float(row.get("cost_usd", 0.0))
        cost_str = f"${cost:.4f}"

        aborted: bool = bool(row.get("aborted", False))
        abort_str = "yes" if aborted else "no"

        print(
            f"{ts_display:<{_TS_COL}}  "
            f"{sample:>{_SAMPLE_COL}}  "
            f"{pass_pct:>{_RATE_COL}}  "
            f"{cost_str:>{_COST_COL}}  "
            f"{abort_str:>{_ABORT_COL}}",
            file=out,
        )

    # ── footer note when the log was clipped ─────────────────────────────────
    total_runs = len(rows)
    shown = len(tail)
    if total_runs > shown:
        print(
            f"(showing {shown} of {total_runs} runs — pass --trend {total_runs} to see all)",
            file=out,
        )


# ── CLI entry-point ───────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """CLI for replaying / displaying ``audit/swebench_runs.jsonl``.

    Usage::

        python3 scripts/swebench_report.py [--trend N] [--file PATH]

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]`` when *None*).

    Returns:
        Exit code: 0 on success, non-zero on error.
    """
    parser = argparse.ArgumentParser(
        prog="swebench_report",
        description="Display or summarise SWE-bench benchmark runs from the audit JSONL log.",
    )
    parser.add_argument(
        "--trend",
        metavar="N",
        type=int,
        nargs="?",          # allows bare "--trend" without a value
        const=10,           # default when flag is present but N omitted
        default=None,
        help="Print a table of the last N runs (default 10 when flag is given).",
    )
    parser.add_argument(
        "--file",
        metavar="PATH",
        type=Path,
        default=None,
        help=(
            "Path to the JSONL audit file "
            f"(default: {AUDIT_JSONL.relative_to(_REPO_ROOT)})."
        ),
    )

    args = parser.parse_args(argv)
    jsonl_path: Path | None = args.file

    if args.trend is not None:
        if args.trend < 1:
            parser.error("--trend N must be a positive integer")
        print_trend(jsonl_path=jsonl_path, n=args.trend)
        return 0

    # No flag given — print a short usage hint rather than doing nothing silently.
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
