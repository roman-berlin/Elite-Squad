"""EU-71 — unit tests for scripts/swebench_report.py.

Covers:
  • weekly_seed() returns a stable string for the current ISO week
  • weekly_seed() format matches "elite-unit-swebench-weekly-YYYYWww"
  • print_summary() outputs headline, per-task table, and totals
  • print_summary() shows abort notice when aborted=True
  • print_summary() handles an empty results list gracefully
  • append_run() writes valid JSON to a temp JSONL file
  • append_run() record shape: ts, event, sample_size, score, passed, total,
                                cost_usd, aborted, per_task
  • append_run() per_task list strips verbose fields (test_output / error)
  • append_run() is idempotent — two calls produce two lines
  • report() calls both print_summary and append_run
  • --weekly CLI flag wires weekly_seed into run_benchmark (integration smoke)
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

# ── path bootstrap — mirror the pattern in swebench_builder_test.py ──────────
sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import swebench_report as sr  # noqa: E402


# ── minimal check helper (same pattern as the other EU-71 test files) ─────────
results: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# ── fixture: a minimal summary dict ──────────────────────────────────────────
def _make_summary(
    passed=7,
    total=20,
    cost_usd=4.21,
    aborted=False,
    results_override=None,
) -> dict:
    if results_override is None:
        results_override = [
            {"task_id": "astropy__astropy-12907", "passed": True,
             "test_output": "PASSED", "error": ""},
            {"task_id": "django__django-11001", "passed": False,
             "test_output": "FAILED", "error": ""},
        ]
    pass_rate = round(passed / total, 4) if total else 0.0
    return {
        "passed": passed,
        "failed": total - passed,
        "total": total,
        "pass_rate": pass_rate,
        "cost_usd": cost_usd,
        "aborted": aborted,
        "results": results_override,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1.  weekly_seed() — format and stability
# ─────────────────────────────────────────────────────────────────────────────

seed1 = sr.weekly_seed()
check("weekly_seed returns a non-empty string", isinstance(seed1, str) and seed1)
check("weekly_seed starts with expected prefix",
      seed1.startswith("elite-unit-swebench-weekly-"))
# Format: "elite-unit-swebench-weekly-2026W26"
parts = seed1.replace("elite-unit-swebench-weekly-", "")
check("weekly_seed contains 'W' separator", "W" in parts, parts)
year_part, week_part = parts.split("W")
check("weekly_seed year is a 4-digit number", year_part.isdigit() and len(year_part) == 4, year_part)
check("weekly_seed week is a zero-padded 2-digit number",
      week_part.isdigit() and len(week_part) == 2 and 1 <= int(week_part) <= 53, week_part)

# Calling twice in the same process should return the identical string.
seed2 = sr.weekly_seed()
check("weekly_seed is stable within the same run", seed1 == seed2, f"{seed1!r} vs {seed2!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 2.  print_summary() — stdout content
# ─────────────────────────────────────────────────────────────────────────────

buf = io.StringIO()
sr.print_summary(_make_summary(), file=buf)
output = buf.getvalue()

check("print_summary headline contains 'Builder:'", "Builder:" in output, repr(output[:100]))
check("print_summary headline shows passed count", "7/20" in output, repr(output[:100]))
check("print_summary headline shows percentage", "35.0%" in output, repr(output[:100]))
check("print_summary shows PASS for passing task",
      "PASS" in output and "astropy__astropy-12907" in output)
check("print_summary shows FAIL for failing task",
      "FAIL" in output and "django__django-11001" in output)
check("print_summary shows cost line", "cost $4.2100" in output, repr(output))
check("print_summary no abort notice when aborted=False",
      "ABORTED" not in output, repr(output))

# Aborted run shows the notice
buf2 = io.StringIO()
sr.print_summary(_make_summary(aborted=True), file=buf2)
output2 = buf2.getvalue()
check("print_summary shows abort notice when aborted=True",
      "ABORTED" in output2, repr(output2))

# Empty results — should not crash
buf3 = io.StringIO()
sr.print_summary(_make_summary(passed=0, total=0, results_override=[]), file=buf3)
output3 = buf3.getvalue()
check("print_summary handles empty results without error",
      "Builder:" in output3, repr(output3))


# ─────────────────────────────────────────────────────────────────────────────
# 3.  append_run() — JSONL output shape
# ─────────────────────────────────────────────────────────────────────────────

with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
    tmp_path = Path(tf.name)

try:
    summary = _make_summary()
    sr.append_run(summary, jsonl_path=tmp_path)
    lines = tmp_path.read_text(encoding="utf-8").strip().splitlines()
    check("append_run produces exactly one line", len(lines) == 1, str(lines))

    row = json.loads(lines[0])
    check("append_run record has 'ts' field", "ts" in row, str(row.keys()))
    check("append_run record event='swebench_run'", row.get("event") == "swebench_run", str(row))
    check("append_run record has sample_size", row.get("sample_size") == 20, str(row))
    check("append_run record has passed", row.get("passed") == 7, str(row))
    check("append_run record has total", row.get("total") == 20, str(row))
    check("append_run record score is a float", isinstance(row.get("score"), float), str(row))
    check("append_run record score matches pass_rate",
          abs(row["score"] - 0.35) < 0.001, str(row))
    check("append_run record cost_usd present", "cost_usd" in row, str(row))
    check("append_run record aborted=False", row.get("aborted") is False, str(row))
    check("append_run record has per_task list", isinstance(row.get("per_task"), list), str(row))
    check("append_run per_task has 2 entries", len(row["per_task"]) == 2, str(row["per_task"]))

    # per_task entries should NOT contain verbose test_output / error fields
    pt0 = row["per_task"][0]
    check("append_run per_task entry has task_id", "task_id" in pt0, str(pt0))
    check("append_run per_task entry has passed", "passed" in pt0, str(pt0))
    check("append_run per_task strips test_output", "test_output" not in pt0, str(pt0))
    check("append_run per_task strips error field", "error" not in pt0, str(pt0))

    # Idempotency: second append produces a second line
    sr.append_run(summary, jsonl_path=tmp_path)
    lines2 = tmp_path.read_text(encoding="utf-8").strip().splitlines()
    check("append_run second call produces second line", len(lines2) == 2, str(lines2))

    # Aborted run records aborted=True
    summary_aborted = _make_summary(aborted=True)
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf2:
        tmp2 = Path(tf2.name)
    sr.append_run(summary_aborted, jsonl_path=tmp2)
    row2 = json.loads(tmp2.read_text(encoding="utf-8").strip())
    check("append_run aborted=True recorded correctly", row2.get("aborted") is True, str(row2))
    tmp2.unlink()

finally:
    tmp_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# 4.  report() — calls both print_summary and append_run
# ─────────────────────────────────────────────────────────────────────────────

with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf3:
    tmp3 = Path(tf3.name)

try:
    out_buf = io.StringIO()
    sr.report(_make_summary(), jsonl_path=tmp3, file=out_buf)
    report_text = out_buf.getvalue()
    check("report() produces stdout output", "Builder:" in report_text, repr(report_text[:80]))

    report_lines = tmp3.read_text(encoding="utf-8").strip().splitlines()
    check("report() appends to JSONL", len(report_lines) == 1, str(report_lines))
    report_row = json.loads(report_lines[0])
    check("report() JSONL row is valid", report_row.get("event") == "swebench_run", str(report_row))
finally:
    tmp3.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# 5.  --weekly smoke: weekly_seed is used when --weekly is set
# ─────────────────────────────────────────────────────────────────────────────
# We test that swebench_builder.main() passes the weekly seed to run_benchmark()
# when --weekly is given, without running the actual benchmark.

import types

# Stub out heavy imports before importing swebench_builder
_sdk = types.ModuleType("claude_agent_sdk")

class _SdkStub:
    def __init__(self, *a, **k):
        pass
    def __call__(self, *a, **k):
        return self

_sdk.__getattr__ = lambda name: _SdkStub  # type: ignore[attr-defined]
_sdk.ClaudeAgentOptions = _SdkStub  # type: ignore[attr-defined]
sys.modules.setdefault("claude_agent_sdk", _sdk)

import swebench_builder as sb  # noqa: E402  (after stub setup)

# Test via run_benchmark signature: random_seed + jsonl_path params accepted.
import inspect
sig = inspect.signature(sb.run_benchmark)
check("run_benchmark accepts random_seed param", "random_seed" in sig.parameters)
check("run_benchmark random_seed default is None",
      sig.parameters["random_seed"].default is None)
# EU-71 iter-2: the injectable audit path that keeps test runs out of the real trend.
check("run_benchmark accepts jsonl_path param", "jsonl_path" in sig.parameters)
check("run_benchmark jsonl_path default is None",
      sig.parameters["jsonl_path"].default is None)


# ─────────────────────────────────────────────────────────────────────────────
# 6.  print_trend() — dated table of last N runs
# ─────────────────────────────────────────────────────────────────────────────

def _write_run_records(path: Path, records: list[dict]) -> None:
    """Write a sequence of swebench_run dicts as JSONL to *path*."""
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _make_run_record(ts: str, sample: int, score: float, cost: float, aborted: bool) -> dict:
    return {
        "ts": ts,
        "event": "swebench_run",
        "sample_size": sample,
        "score": score,
        "passed": int(sample * score),
        "total": sample,
        "cost_usd": cost,
        "aborted": aborted,
        "per_task": [],
    }


# --- 6a. empty JSONL (file does not exist) → graceful notice ---
with tempfile.TemporaryDirectory() as td:
    missing = Path(td) / "nope.jsonl"
    buf_empty = io.StringIO()
    sr.print_trend(jsonl_path=missing, n=5, file=buf_empty)
    out_empty = buf_empty.getvalue()
check("print_trend: missing file prints a notice", "no swebench runs" in out_empty.lower(),
      repr(out_empty))

# --- 6b. empty JSONL (file exists but is empty) → graceful notice ---
with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as tf_empty:
    tf_empty_path = Path(tf_empty.name)
try:
    buf_empty2 = io.StringIO()
    sr.print_trend(jsonl_path=tf_empty_path, n=5, file=buf_empty2)
    out_empty2 = buf_empty2.getvalue()
    check("print_trend: empty file prints a notice", "no swebench runs" in out_empty2.lower(),
          repr(out_empty2))
finally:
    tf_empty_path.unlink(missing_ok=True)

# --- 6c. single entry, N=10 — shows 1 row without crashing ---
with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as tf_single:
    tf_single_path = Path(tf_single.name)
try:
    _write_run_records(tf_single_path, [
        _make_run_record("2026-06-27T10:30:00+0000", 20, 0.35, 4.21, False),
    ])
    buf_single = io.StringIO()
    sr.print_trend(jsonl_path=tf_single_path, n=10, file=buf_single)
    out_single = buf_single.getvalue()
    check("print_trend single entry: header present", "Date/Time" in out_single,
          repr(out_single[:200]))
    check("print_trend single entry: ts shown", "2026-06-27T10:30:00" in out_single,
          repr(out_single))
    check("print_trend single entry: pass% shown", "35.0%" in out_single,
          repr(out_single))
    check("print_trend single entry: cost shown", "4.2100" in out_single,
          repr(out_single))
    check("print_trend single entry: aborted=no shown", "no" in out_single.lower(),
          repr(out_single))
    # N=10 > 1 available — no clip notice should appear
    check("print_trend single entry: no clip notice when N > rows",
          "showing" not in out_single, repr(out_single))
finally:
    tf_single_path.unlink(missing_ok=True)

# --- 6d. N > available rows — shows all rows, no IndexError ---
with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as tf_over:
    tf_over_path = Path(tf_over.name)
try:
    _write_run_records(tf_over_path, [
        _make_run_record("2026-06-25T08:00:00+0000", 20, 0.30, 3.10, False),
        _make_run_record("2026-06-26T09:00:00+0000", 30, 0.40, 5.50, False),
    ])
    buf_over = io.StringIO()
    sr.print_trend(jsonl_path=tf_over_path, n=100, file=buf_over)
    out_over = buf_over.getvalue()
    lines_over = [l for l in out_over.splitlines() if l.strip()
                  and not l.startswith("─") and "Date/Time" not in l]
    check("print_trend N>rows: shows all available rows (2)", len(lines_over) == 2,
          repr(lines_over))
    check("print_trend N>rows: no clip notice", "showing" not in out_over, repr(out_over))
finally:
    tf_over_path.unlink(missing_ok=True)

# --- 6e. N < available rows — shows only last N (most recent), clip notice ---
with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as tf_clip:
    tf_clip_path = Path(tf_clip.name)
try:
    _write_run_records(tf_clip_path, [
        _make_run_record("2026-06-24T07:00:00+0000", 10, 0.20, 1.00, False),
        _make_run_record("2026-06-25T08:00:00+0000", 20, 0.30, 2.00, False),
        _make_run_record("2026-06-26T09:00:00+0000", 30, 0.40, 3.00, False),
        _make_run_record("2026-06-27T10:00:00+0000", 40, 0.50, 4.00, True),
    ])
    buf_clip = io.StringIO()
    sr.print_trend(jsonl_path=tf_clip_path, n=2, file=buf_clip)
    out_clip = buf_clip.getvalue()
    # Should NOT include the oldest entry
    check("print_trend N=2: excludes oldest entry",
          "2026-06-24T07:00:00" not in out_clip, repr(out_clip))
    # Should include the two most recent entries
    check("print_trend N=2: includes second-to-last entry",
          "2026-06-26T09:00:00" in out_clip, repr(out_clip))
    check("print_trend N=2: includes last entry (aborted)",
          "2026-06-27T10:00:00" in out_clip, repr(out_clip))
    check("print_trend N=2: aborted=yes shown for aborted row",
          "yes" in out_clip, repr(out_clip))
    check("print_trend N=2: clip notice appears when rows trimmed",
          "showing" in out_clip, repr(out_clip))
    check("print_trend N=2: clip notice shows correct shown/total counts",
          "2 of 4" in out_clip, repr(out_clip))
finally:
    tf_clip_path.unlink(missing_ok=True)

# --- 6f. non-swebench_run lines are ignored ---
with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as tf_mix:
    tf_mix_path = Path(tf_mix.name)
try:
    with tf_mix_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "other_event", "ts": "2026-01-01T00:00:00"}) + "\n")
        fh.write(json.dumps(_make_run_record("2026-06-28T12:00:00+0000", 25, 0.60, 6.00, False)) + "\n")
        fh.write("not-json-at-all\n")
    buf_mix = io.StringIO()
    sr.print_trend(jsonl_path=tf_mix_path, n=10, file=buf_mix)
    out_mix = buf_mix.getvalue()
    lines_mix = [l for l in out_mix.splitlines() if l.strip()
                 and not l.startswith("─") and "Date/Time" not in l]
    check("print_trend: non-swebench_run lines skipped", len(lines_mix) == 1,
          repr(lines_mix))
    check("print_trend: malformed JSON lines skipped without crash",
          "2026-06-28T12:00:00" in out_mix, repr(out_mix))
finally:
    tf_mix_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# 7.  main() — CLI argument parsing
# ─────────────────────────────────────────────────────────────────────────────

# --- 7a. --trend flag with explicit N prints a trend table ---
with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as tf_cli:
    tf_cli_path = Path(tf_cli.name)
try:
    _write_run_records(tf_cli_path, [
        _make_run_record("2026-06-27T10:30:00+0000", 20, 0.35, 4.21, False),
        _make_run_record("2026-06-28T08:00:00+0000", 50, 0.42, 9.87, True),
    ])
    # Capture stdout by temporarily redirecting sys.stdout.
    import contextlib
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        exit_code = sr.main(["--trend", "5", "--file", str(tf_cli_path)])
    cli_out = captured.getvalue()
    check("main --trend 5: exits with 0", exit_code == 0, str(exit_code))
    check("main --trend 5: prints header", "Date/Time" in cli_out, repr(cli_out[:200]))
    check("main --trend 5: shows both records", "2026-06-27" in cli_out and "2026-06-28" in cli_out,
          repr(cli_out))
finally:
    tf_cli_path.unlink(missing_ok=True)

# --- 7b. --trend without N uses default of 10 ---
with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as tf_noN:
    tf_noN_path = Path(tf_noN.name)
try:
    _write_run_records(tf_noN_path, [
        _make_run_record("2026-06-28T09:00:00+0000", 10, 0.50, 2.00, False),
    ])
    captured2 = io.StringIO()
    with contextlib.redirect_stdout(captured2):
        exit_code2 = sr.main(["--trend", "--file", str(tf_noN_path)])
    cli_out2 = captured2.getvalue()
    check("main --trend (no N): exits with 0", exit_code2 == 0, str(exit_code2))
    check("main --trend (no N): shows entry", "2026-06-28T09:00:00" in cli_out2, repr(cli_out2))
finally:
    tf_noN_path.unlink(missing_ok=True)

# --- 7c. no flags → prints help (exit 0, no crash) ---
captured3 = io.StringIO()
with contextlib.redirect_stdout(captured3):
    exit_code3 = sr.main([])
cli_out3 = captured3.getvalue()
check("main no flags: exits with 0", exit_code3 == 0, str(exit_code3))
check("main no flags: prints something useful", len(cli_out3) > 0, repr(cli_out3[:80]))

# --- 7d. --trend 0 is rejected with a non-zero exit (validation guard) ---
import subprocess
_trend0 = subprocess.run(
    [sys.executable, "scripts/swebench_report.py", "--trend", "0"],
    capture_output=True,
    text=True,
)
check("main --trend 0: exits non-zero (invalid N rejected)",
      _trend0.returncode != 0,
      f"returncode={_trend0.returncode!r} stderr={_trend0.stderr!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 8.  append_run() — zero-division guard when total=0
# ─────────────────────────────────────────────────────────────────────────────
# Regression: summary dict from an empty/aborted run can arrive with total=0.
# append_run() must not divide-by-zero when computing score; it must record
# score=0.0 and produce a valid JSONL line.

with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf_zero:
    tmp_zero = Path(tf_zero.name)

try:
    _zero_summary = {
        "passed": 0, "failed": 0, "total": 0,
        "pass_rate": 0.0, "cost_usd": 0.0,
        "aborted": True, "results": [],
    }
    # Must not raise ZeroDivisionError.
    sr.append_run(_zero_summary, jsonl_path=tmp_zero)
    _zero_lines = tmp_zero.read_text(encoding="utf-8").strip().splitlines()
    check("append_run total=0: produces exactly one line without crashing",
          len(_zero_lines) == 1,
          str(_zero_lines))
    _zero_row = json.loads(_zero_lines[0])
    check("append_run total=0: score is 0.0",
          _zero_row.get("score") == 0.0,
          str(_zero_row))
    check("append_run total=0: sample_size is 0",
          _zero_row.get("sample_size") == 0,
          str(_zero_row))
    check("append_run total=0: event is swebench_run",
          _zero_row.get("event") == "swebench_run",
          str(_zero_row))
finally:
    tmp_zero.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

passed_count = sum(1 for _, ok, _ in results if ok)
failed_tests = [(n, d) for n, ok, d in results if not ok]
print(f"\nswebench_report_test: {passed_count}/{len(results)} passed")
for name, detail in failed_tests:
    print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))

if failed_tests:
    sys.exit(1)
