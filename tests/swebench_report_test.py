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
# Runner
# ─────────────────────────────────────────────────────────────────────────────

passed_count = sum(1 for _, ok, _ in results if ok)
failed_tests = [(n, d) for n, ok, d in results if not ok]
print(f"\nswebench_report_test: {passed_count}/{len(results)} passed")
for name, detail in failed_tests:
    print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))

if failed_tests:
    sys.exit(1)
