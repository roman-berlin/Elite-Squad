"""EU-71 — CLI entry-point tests for the SWE-bench scripts.

The three swebench_*.py scripts each expose a main() that handles argument
parsing and exit codes.  These are contract surfaces (used by the weekly cron
and by the drill/council) and regression-pins that the primary unit tests don't
yet cover.

Tests:
  swebench_eval.main()
    • ImportError (no dataset library) → exits 2
    • no tasks fetched          → exits 2 with message
    • happy path (gold patches) → calls run_tasks and exits 0

  swebench_builder.main()
    • ImportError (missing dep) → exits 2
    • aborted run               → exits 1
    • happy full run            → exits 0
    • --weekly flag             → passes a non-None seed to run_benchmark
    • --audit-jsonl flag        → overrides the jsonl_path in run_benchmark
    • --sample clamping warning → logs when --sample > MAX_SAMPLE

  weekly_seed()
    • called from main() --weekly derives the seed via swebench_report.weekly_seed
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

# ── bootstrap: stub claude_agent_sdk so swebench_builder imports cleanly ──────
_sdk = types.ModuleType("claude_agent_sdk")


class _SdkStub:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


_sdk.__getattr__ = lambda name: _SdkStub  # type: ignore[attr-defined]
_sdk.ClaudeAgentOptions = _SdkStub  # type: ignore[attr-defined]
sys.modules.setdefault("claude_agent_sdk", _sdk)

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import swebench_eval as ev  # noqa: E402
import swebench_builder as sb  # noqa: E402
import swebench_report as sr  # noqa: E402

# ── check helper ──────────────────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# ── shared temp audit path (keep benchmark runs out of the production trail) ──
import shutil as _shutil
import tempfile as _tempfile

_AUDIT_DIR = Path(_tempfile.mkdtemp(prefix="swebench_cli_test_"))
_AUDIT_TMP = _AUDIT_DIR / "runs.jsonl"


# ─────────────────────────────────────────────────────────────────────────────
# 1.  swebench_eval.main() — ImportError exits 2
# ─────────────────────────────────────────────────────────────────────────────
# When neither 'datasets' nor 'swebench' is installed, main() must print an
# error to stderr and exit with code 2 (dependency error, not an assertion
# failure or unhandled exception).

def _raise_import(*a, **k):
    raise ImportError("no datasets library installed")


exit_code_import_err = ev.main.__module__  # smoke-check import

rc = None
try:
    with patch("swebench_eval.load_tasks", side_effect=_raise_import):
        rc = ev.main(["--sample", "1"])
except SystemExit as exc:
    rc = exc.code

check("swebench_eval.main ImportError → returns/exits 2", rc == 2, f"rc={rc}")


# ─────────────────────────────────────────────────────────────────────────────
# 2.  swebench_eval.main() — empty task list exits 2
# ─────────────────────────────────────────────────────────────────────────────
rc_empty = None
try:
    with patch("swebench_eval.load_tasks", return_value=[]):
        rc_empty = ev.main(["--sample", "1"])
except SystemExit as exc:
    rc_empty = exc.code

check("swebench_eval.main empty tasks → returns/exits 2", rc_empty == 2, f"rc={rc_empty}")


# ─────────────────────────────────────────────────────────────────────────────
# 3.  swebench_eval.main() — happy path (gold patches) exits 0
# ─────────────────────────────────────────────────────────────────────────────
# Use a single task with a gold patch; stub run_tasks to return a "passed"
# result so main() receives a 1/1 summary and should exit 0 (all tasks passed).

_single_task = ev.SWETask(
    instance_id="happy__test-1",
    repo="owner/repo",
    base_commit="abc123",
    gold_patch="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n",
    fail_to_pass=["tests/test_x.py::test_a"],
)

rc_happy = None
try:
    with (
        patch("swebench_eval.load_tasks", return_value=[_single_task]),
        patch("swebench_eval.run_tasks", return_value=[
            {"task_id": "happy__test-1", "passed": True, "test_output": "ok", "error": ""}
        ]),
    ):
        rc_happy = ev.main(["--sample", "1"])
except SystemExit as exc:
    rc_happy = exc.code

check("swebench_eval.main happy path → exits 0", rc_happy == 0, f"rc={rc_happy}")


# ─────────────────────────────────────────────────────────────────────────────
# 4.  swebench_eval.main() — non-zero exit when some tasks fail
# ─────────────────────────────────────────────────────────────────────────────
# main() returns 1 when passed < total (some tasks failed or no gold patch).
rc_fail = None
_task_no_patch = ev.SWETask(
    instance_id="fail__test-1", repo="owner/repo", base_commit="abc",
    gold_patch="",  # empty → excluded from gold_patches → run_tasks sees no patch
    fail_to_pass=["tests/test.py::t"],
)

try:
    with (
        patch("swebench_eval.load_tasks", return_value=[_task_no_patch]),
        patch("swebench_eval.run_tasks", return_value=[
            {"task_id": "fail__test-1", "passed": False, "test_output": "", "error": "no patch"}
        ]),
    ):
        rc_fail = ev.main(["--sample", "1"])
except SystemExit as exc:
    rc_fail = exc.code

check("swebench_eval.main partial failure → exits 1 (not 0)", rc_fail == 1, f"rc={rc_fail}")


# ─────────────────────────────────────────────────────────────────────────────
# 5.  swebench_builder.main() — ImportError exits 2
# ─────────────────────────────────────────────────────────────────────────────
# run_benchmark propagates ImportError (missing datasets/swebench); main() must
# catch it and exit 2.

rc_sb_import = None
try:
    with patch(
        "swebench_builder.run_benchmark",
        side_effect=ImportError("datasets not installed"),
    ):
        # asyncio.run is called by main() — patch run_benchmark, not asyncio.run
        rc_sb_import = sb.main(["--sample", "1", "--audit-jsonl", str(_AUDIT_TMP)])
except SystemExit as exc:
    rc_sb_import = exc.code

check("swebench_builder.main ImportError → exits 2", rc_sb_import == 2, f"rc={rc_sb_import}")


# ─────────────────────────────────────────────────────────────────────────────
# 6.  swebench_builder.main() — aborted run exits 1
# ─────────────────────────────────────────────────────────────────────────────
rc_sb_aborted = None
_aborted_summary = {
    "total": 3, "passed": 1, "failed": 2, "pass_rate": 0.333,
    "cost_usd": 4.99, "aborted": True, "results": [],
}

async def _aborted_benchmark(*a, **k):
    return _aborted_summary


try:
    with patch("swebench_builder.run_benchmark", _aborted_benchmark):
        rc_sb_aborted = sb.main(["--sample", "3", "--audit-jsonl", str(_AUDIT_TMP)])
except SystemExit as exc:
    rc_sb_aborted = exc.code

check("swebench_builder.main aborted run → exits 1", rc_sb_aborted == 1, f"rc={rc_sb_aborted}")


# ─────────────────────────────────────────────────────────────────────────────
# 7.  swebench_builder.main() — happy run exits 0
# ─────────────────────────────────────────────────────────────────────────────
rc_sb_ok = None
_ok_summary = {
    "total": 2, "passed": 2, "failed": 0, "pass_rate": 1.0,
    "cost_usd": 0.20, "aborted": False, "results": [],
}

async def _ok_benchmark(*a, **k):
    return _ok_summary


try:
    with patch("swebench_builder.run_benchmark", _ok_benchmark):
        rc_sb_ok = sb.main(["--sample", "2", "--audit-jsonl", str(_AUDIT_TMP)])
except SystemExit as exc:
    rc_sb_ok = exc.code

check("swebench_builder.main happy run → exits 0", rc_sb_ok == 0, f"rc={rc_sb_ok}")


# ─────────────────────────────────────────────────────────────────────────────
# 8.  swebench_builder.main() — --weekly flag passes a non-None seed
# ─────────────────────────────────────────────────────────────────────────────
# The --weekly flag must derive a seed from weekly_seed() and pass it as
# random_seed to run_benchmark.  We capture the actual kwargs passed.

captured_seed: list = []

async def _capture_seed(*a, **k):
    captured_seed.append(k.get("random_seed"))
    return _ok_summary


try:
    with patch("swebench_builder.run_benchmark", _capture_seed):
        sb.main(["--weekly", "--sample", "5", "--audit-jsonl", str(_AUDIT_TMP)])
except SystemExit:
    pass

check("swebench_builder.main --weekly passes a non-None seed to run_benchmark",
      len(captured_seed) == 1 and captured_seed[0] is not None,
      str(captured_seed))
check("swebench_builder.main --weekly seed matches weekly_seed() format",
      isinstance(captured_seed[0], str) and "swebench-weekly" in captured_seed[0],
      str(captured_seed[0] if captured_seed else "no seed"))


# ─────────────────────────────────────────────────────────────────────────────
# 9.  swebench_builder.main() — --audit-jsonl overrides the JSONL path
# ─────────────────────────────────────────────────────────────────────────────
captured_jsonl: list = []

async def _capture_jsonl(*a, **k):
    captured_jsonl.append(k.get("jsonl_path"))
    return _ok_summary


import tempfile as _tf

with _tf.NamedTemporaryFile(suffix=".jsonl", delete=False) as _tf2:
    _custom_jsonl = Path(_tf2.name)

try:
    with patch("swebench_builder.run_benchmark", _capture_jsonl):
        sb.main(["--sample", "1", "--audit-jsonl", str(_custom_jsonl)])
except SystemExit:
    pass
finally:
    _custom_jsonl.unlink(missing_ok=True)

check("swebench_builder.main --audit-jsonl passes custom path to run_benchmark",
      len(captured_jsonl) == 1 and captured_jsonl[0] == _custom_jsonl,
      str(captured_jsonl[0] if captured_jsonl else "no path"))


# ─────────────────────────────────────────────────────────────────────────────
# 10.  swebench_builder.main() — unexpected Exception exits 3
# ─────────────────────────────────────────────────────────────────────────────
rc_sb_err = None

async def _raise_generic(*a, **k):
    raise RuntimeError("disk full")


try:
    with patch("swebench_builder.run_benchmark", _raise_generic):
        rc_sb_err = sb.main(["--sample", "1", "--audit-jsonl", str(_AUDIT_TMP)])
except SystemExit as exc:
    rc_sb_err = exc.code

check("swebench_builder.main unexpected error → exits 3", rc_sb_err == 3, f"rc={rc_sb_err}")


# ─────────────────────────────────────────────────────────────────────────────
# 11.  swebench_builder.main() — --sample > MAX_SAMPLE clamped (no crash)
# ─────────────────────────────────────────────────────────────────────────────
# Confirm that passing a huge --sample value doesn't crash main() (it's clamped
# silently before being forwarded to run_benchmark).

captured_sample: list = []

async def _capture_sample(*a, **k):
    captured_sample.append(k.get("sample"))
    return _ok_summary


try:
    with patch("swebench_builder.run_benchmark", _capture_sample):
        sb.main(["--sample", "9999", "--audit-jsonl", str(_AUDIT_TMP)])
except SystemExit:
    pass

check("swebench_builder.main: --sample 9999 clamped to <= MAX_SAMPLE",
      len(captured_sample) == 1 and captured_sample[0] <= sb.MAX_SAMPLE,
      str(captured_sample[0] if captured_sample else "not called"))


# ─────────────────────────────────────────────────────────────────────────────
# 12.  swebench_eval.main() — --out writes results to a file
# ─────────────────────────────────────────────────────────────────────────────
import json as _json

with _tf.NamedTemporaryFile(suffix=".json", delete=False) as _out_f:
    _out_path = Path(_out_f.name)

rc_out = None
try:
    with (
        patch("swebench_eval.load_tasks", return_value=[_single_task]),
        patch("swebench_eval.run_tasks", return_value=[
            {"task_id": "happy__test-1", "passed": True, "test_output": "ok", "error": ""}
        ]),
    ):
        rc_out = ev.main(["--sample", "1", "--out", str(_out_path)])
except SystemExit as exc:
    rc_out = exc.code
finally:
    pass

check("swebench_eval.main --out writes a JSON file", _out_path.exists(), str(_out_path))
try:
    _out_data = _json.loads(_out_path.read_text(encoding="utf-8"))
    check("swebench_eval.main --out JSON has 'total' key", "total" in _out_data, str(_out_data.keys()))
    check("swebench_eval.main --out JSON has 'pass_rate' key", "pass_rate" in _out_data, str(_out_data.keys()))
    check("swebench_eval.main --out JSON has 'results' list", isinstance(_out_data.get("results"), list))
except Exception as exc:  # noqa: BLE001
    check("swebench_eval.main --out JSON is valid", False, repr(exc))
finally:
    _out_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# 13.  swebench_report.weekly_seed() — --weekly derives seed from the same source
# ─────────────────────────────────────────────────────────────────────────────
# Regression: the seed derived by main() must equal weekly_seed() directly,
# so re-running --weekly in the same week always samples the same tasks.

captured_seed2: list = []

async def _capture_seed2(*a, **k):
    captured_seed2.append(k.get("random_seed"))
    return _ok_summary


expected_seed = sr.weekly_seed()
try:
    with patch("swebench_builder.run_benchmark", _capture_seed2):
        sb.main(["--weekly", "--sample", "5", "--audit-jsonl", str(_AUDIT_TMP)])
except SystemExit:
    pass

check("swebench_builder.main --weekly seed matches sr.weekly_seed() verbatim",
      len(captured_seed2) == 1 and captured_seed2[0] == expected_seed,
      f"got={captured_seed2[0] if captured_seed2 else 'none'!r}, expected={expected_seed!r}")


# ── cleanup ───────────────────────────────────────────────────────────────────
_shutil.rmtree(_AUDIT_DIR, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# result summary
# ─────────────────────────────────────────────────────────────────────────────
passed_n = sum(1 for _, ok, _ in results if ok)
print(f"\n========= EU-71 SWE-bench CLI entry-point tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("----------------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
import sys as _sys
_sys.exit(0 if passed_n == len(results) else 1)
