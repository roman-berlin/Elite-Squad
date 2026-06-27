"""EU-71 — gap-fill tests for swebench_eval.py and swebench_builder.py.

The three primary test files (swebench_eval_test.py, swebench_builder_test.py,
swebench_report_test.py) achieve excellent coverage of the happy paths and the
main error paths.  This file pins the remaining branches that were left
untested:

  eval:
    • _iter_tasks() — row with a missing instance_id is silently skipped
    • run_single_task() — task with missing repo or base_commit raises WorktreeError
    • _run_tests() — empty test_ids triggers the full-suite fallback warning

  builder:
    • run_benchmark() — random_seed parameter causes a seeded sub-sample
    • run_benchmark() — zero tasks loaded returns an empty-summary dict early
    • run_benchmark() — generic Exception from build_patch_for_task is caught;
                        the run continues with an empty patch for that task
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

# ── bootstrap: make the Agent SDK importable (no real SDK in the test env) ──
_sdk = types.ModuleType("claude_agent_sdk")


class _SdkStub:
    def __init__(self, *a, **k):
        self.__dict__.update(k)

    def __call__(self, *a, **k):
        return self


_sdk.__getattr__ = lambda name: _SdkStub  # type: ignore[attr-defined]
_sdk.ClaudeAgentOptions = _SdkStub  # type: ignore[attr-defined]
sys.modules.setdefault("claude_agent_sdk", _sdk)

# ── path setup — repo root + scripts/ ────────────────────────────────────────
sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import swebench_eval as ev  # noqa: E402
import swebench_builder as sb  # noqa: E402

# ── minimal check helper (mirrors project test style) ─────────────────────────
results: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# ── shared temp audit path — keep benchmark runs OUT of the real trend ────────
# Every run_benchmark() call below passes jsonl_path=_AUDIT_TMP so the suite
# never appends to the production audit/swebench_runs.jsonl.
import shutil as _shutil
import tempfile as _tempfile

_AUDIT_DIR = Path(_tempfile.mkdtemp(prefix="swebench_audit_gaps_"))
_AUDIT_TMP = _AUDIT_DIR / "runs.jsonl"


# ─────────────────────────────────────────────────────────────────────────────
# 1.  _iter_tasks() — row with missing instance_id is silently skipped
# ─────────────────────────────────────────────────────────────────────────────
# Regression: a dataset row that has no instance_id (or an empty string) must
# be silently skipped so the harness doesn't produce a SWETask with instance_id=""
# that would collide in the patches dict and produce meaningless results.

_rows_with_missing_id = [
    {   # bad row — empty instance_id
        "instance_id": "",
        "repo": "owner/repo",
        "base_commit": "abc",
        "patch": "diff...",
        "FAIL_TO_PASS": "[]",
        "PASS_TO_PASS": "[]",
    },
    {   # bad row — missing key entirely
        "repo": "owner/repo",
        "base_commit": "def",
        "patch": "diff...",
        "FAIL_TO_PASS": "[]",
    },
    {   # good row
        "instance_id": "real__repo-1",
        "repo": "owner/repo",
        "base_commit": "ghi",
        "patch": "diff...",
        "FAIL_TO_PASS": "[]",
        "PASS_TO_PASS": "[]",
    },
]

import types as _types


def _fake_load_dataset(name, split, streaming):
    return iter(_rows_with_missing_id)


_fake_datasets = _types.ModuleType("datasets")
_fake_datasets.load_dataset = _fake_load_dataset  # type: ignore[attr-defined]

with patch.dict(sys.modules, {"datasets": _fake_datasets}):
    good_tasks = ev.load_tasks(10)

check("_iter_tasks skips rows with empty instance_id",
      len(good_tasks) == 1, f"got {len(good_tasks)} task(s)")
check("_iter_tasks keeps the valid row's instance_id",
      good_tasks[0].instance_id == "real__repo-1",
      good_tasks[0].instance_id)


# ─────────────────────────────────────────────────────────────────────────────
# 2.  run_single_task() — missing repo or base_commit raises WorktreeError
# ─────────────────────────────────────────────────────────────────────────────
# The guard at the top of run_single_task() validates that the task has both
# repo and base_commit before touching the filesystem.

_patch_str = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"

# Task with no repo → WorktreeError (passed=False, error non-empty)
_task_no_repo = ev.SWETask(
    instance_id="t-no-repo",
    repo="",               # ← missing
    base_commit="abc123",
    gold_patch="",
)
with patch("swebench_eval.shutil.rmtree"):   # don't touch real fs
    res_no_repo = ev.run_single_task(_task_no_repo, _patch_str)

check("run_single_task: missing repo → passed=False", res_no_repo.passed is False)
check("run_single_task: missing repo → error non-empty", bool(res_no_repo.error))
check("run_single_task: missing repo → error mentions repo",
      "repo" in res_no_repo.error.lower(), res_no_repo.error[:60])

# Task with no base_commit → WorktreeError
_task_no_commit = ev.SWETask(
    instance_id="t-no-commit",
    repo="owner/repo",
    base_commit="",         # ← missing
    gold_patch="",
)
with patch("swebench_eval.shutil.rmtree"):
    res_no_commit = ev.run_single_task(_task_no_commit, _patch_str)

check("run_single_task: missing base_commit → passed=False", res_no_commit.passed is False)
check("run_single_task: missing base_commit → error non-empty", bool(res_no_commit.error))


# ─────────────────────────────────────────────────────────────────────────────
# 3.  _run_tests() — empty test_ids triggers the full-suite fallback
# ─────────────────────────────────────────────────────────────────────────────
# When no FAIL_TO_PASS ids are supplied the harness logs a warning and runs
# plain pytest (no node-id arguments).  This ensures the command is built
# without extra arguments and a zero-exit pytest call still returns passed=True.

_pytest_ok = subprocess.CompletedProcess(
    ["python", "-m", "pytest"], 0, stdout="3 passed", stderr=""
)

with patch("subprocess.run", return_value=_pytest_ok) as mock_run_tests:
    passed_flag, output = ev._run_tests(Path("/venv/bin/python"), Path("/repo"), [])

check("_run_tests empty test_ids still calls subprocess.run", mock_run_tests.called)
check("_run_tests empty test_ids returns passed=True on rc=0", passed_flag is True)
check("_run_tests empty test_ids returns stdout in output", "passed" in output)

# Verify no test-node-id args sneak into the command (only pytest + flags).
call_args = mock_run_tests.call_args
cmd = call_args[0][0]   # positional first arg = command list
check("_run_tests empty test_ids: cmd does not include extra node-ids",
      not any("::" in part for part in cmd), str(cmd))


# ─────────────────────────────────────────────────────────────────────────────
# 4.  run_benchmark() — random_seed triggers a seeded sub-sample
# ─────────────────────────────────────────────────────────────────────────────
# When random_seed is set, run_benchmark should load MAX_SAMPLE tasks, then
# use random.Random(seed).sample() to pick exactly `sample` of them.  The same
# seed must always produce the same subset — deterministic repeatability.

_large_pool = [
    ev.SWETask(f"pool-{i}", "o/r", "sha", "", [])
    for i in range(50)
]


async def _test_seeded_sample():
    patches_received: list[dict] = []

    async def _fake_build(task, repo_dir, cost_ceiling_usd, cumulative_cost):
        if cumulative_cost >= cost_ceiling_usd:
            raise sb.BudgetExceededError(cumulative_cost, cost_ceiling_usd)
        return "", 0.01

    def _fake_run_tasks(patches, tasks=None, **k):
        patches_received.append({"ids": list(patches.keys()), "tasks": tasks})
        return [
            {"task_id": t.instance_id, "passed": False, "test_output": "", "error": ""}
            for t in (tasks or [])
        ]

    with (
        patch("swebench_eval.load_tasks", return_value=_large_pool),
        patch("swebench_eval._clone_repo"),       # offline — no real git clone
        patch("swebench_builder.build_patch_for_task", _fake_build),
        patch("swebench_eval.run_tasks", side_effect=_fake_run_tasks),
    ):
        # Run twice with the same seed and a 10-task sample
        s1 = await sb.run_benchmark(sample=10, cost_ceiling_usd=999.0,
                                    random_seed="test-seed-123", jsonl_path=_AUDIT_TMP)
        s2 = await sb.run_benchmark(sample=10, cost_ceiling_usd=999.0,
                                    random_seed="test-seed-123", jsonl_path=_AUDIT_TMP)

    # Both runs must evaluate the same 10 tasks
    ids_run1 = patches_received[0]["ids"]
    ids_run2 = patches_received[1]["ids"]
    return ids_run1, ids_run2, s1, s2


ids1, ids2, sum1, sum2 = asyncio.run(_test_seeded_sample())

check("run_benchmark seeded: same seed → same task subset",
      sorted(ids1) == sorted(ids2), f"{ids1} vs {ids2}")
check("run_benchmark seeded: sample size respected",
      len(ids1) == 10, str(len(ids1)))
check("run_benchmark seeded: aborted=False (no budget hit)",
      sum1["aborted"] is False)
check("run_benchmark seeded: total == 10",
      sum1["total"] == 10, str(sum1["total"]))


# ─────────────────────────────────────────────────────────────────────────────
# 5.  run_benchmark() — zero tasks returns empty-summary dict early
# ─────────────────────────────────────────────────────────────────────────────
# If load_tasks returns an empty list the benchmark must return immediately
# without calling build_patch_for_task or run_tasks.

async def _test_zero_tasks():
    build_called = {"n": 0}
    run_tasks_called = {"n": 0}

    async def _fake_build(*a, **k):
        build_called["n"] += 1
        return "", 0.0

    def _fake_run_tasks(*a, **k):
        run_tasks_called["n"] += 1
        return []

    with (
        patch("swebench_eval.load_tasks", return_value=[]),
        patch("swebench_builder.build_patch_for_task", _fake_build),
        patch("swebench_eval.run_tasks", side_effect=_fake_run_tasks),
    ):
        summary = await sb.run_benchmark(sample=5, cost_ceiling_usd=10.0, jsonl_path=_AUDIT_TMP)

    return summary, build_called["n"], run_tasks_called["n"]


zero_summary, build_n, run_n = asyncio.run(_test_zero_tasks())

check("run_benchmark zero-tasks: total == 0", zero_summary["total"] == 0)
check("run_benchmark zero-tasks: aborted == False", zero_summary["aborted"] is False)
check("run_benchmark zero-tasks: pass_rate == 0.0",
      zero_summary["pass_rate"] == 0.0, str(zero_summary["pass_rate"]))
check("run_benchmark zero-tasks: build_patch not called", build_n == 0, str(build_n))
check("run_benchmark zero-tasks: run_tasks not called", run_n == 0, str(run_n))


# ─────────────────────────────────────────────────────────────────────────────
# 6.  run_benchmark() — generic Exception from builder is caught; run continues
# ─────────────────────────────────────────────────────────────────────────────
# When build_patch_for_task raises an unexpected exception (anything other than
# BudgetExceededError or ImportError), the benchmark logs the error, records an
# empty patch for that task, and continues to the next task.

_tasks_3 = [
    ev.SWETask(f"err-{i}", "o/r", "sha", "", [f"t/test.py::t_{i}"])
    for i in range(3)
]


async def _test_generic_exception():
    call_order: list[str] = []

    async def _flaky_build(task, repo_dir, cost_ceiling_usd, cumulative_cost):
        call_order.append(task.instance_id)
        if cumulative_cost >= cost_ceiling_usd:
            raise sb.BudgetExceededError(cumulative_cost, cost_ceiling_usd)
        if task.instance_id == "err-1":
            raise RuntimeError("disk full — simulated crash")
        return "--- a/x.py\n+++ b/x.py\n", 0.01

    collected_patches: dict[str, str] = {}

    def _capture_run_tasks(patches, tasks=None, **k):
        collected_patches.update(patches)
        return [
            {"task_id": t.instance_id, "passed": patches.get(t.instance_id, "") != "",
             "test_output": "", "error": ""}
            for t in (tasks or [])
        ]

    with (
        patch("swebench_eval.load_tasks", return_value=_tasks_3),
        patch("swebench_eval._clone_repo"),     # skip real git
        patch("swebench_builder.build_patch_for_task", _flaky_build),
        patch("swebench_eval.run_tasks", side_effect=_capture_run_tasks),
    ):
        summary = await sb.run_benchmark(sample=3, cost_ceiling_usd=10.0, jsonl_path=_AUDIT_TMP)

    return summary, call_order, collected_patches


exc_summary, order, patches_out = asyncio.run(_test_generic_exception())

check("run_benchmark generic-exc: aborted=False (not a budget abort)",
      exc_summary["aborted"] is False)
check("run_benchmark generic-exc: all 3 tasks attempted",
      len(order) == 3, str(order))
check("run_benchmark generic-exc: err-1 gets empty patch",
      patches_out.get("err-1") == "", repr(patches_out.get("err-1")))
check("run_benchmark generic-exc: err-0 gets a real patch",
      bool(patches_out.get("err-0")), repr(patches_out.get("err-0")))
check("run_benchmark generic-exc: err-2 gets a real patch",
      bool(patches_out.get("err-2")), repr(patches_out.get("err-2")))
check("run_benchmark generic-exc: total == 3",
      exc_summary["total"] == 3, str(exc_summary["total"]))


# ─────────────────────────────────────────────────────────────────────────────
# 7.  audit isolation — every run_benchmark() above used the injected jsonl_path
# ─────────────────────────────────────────────────────────────────────────────
check("run_benchmark wrote audit lines to the injected jsonl_path (not the trend)",
      _AUDIT_TMP.exists() and bool(_AUDIT_TMP.read_text(encoding="utf-8").strip()),
      str(_AUDIT_TMP))
_shutil.rmtree(_AUDIT_DIR, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# result summary
# ─────────────────────────────────────────────────────────────────────────────
passed_n = sum(1 for _, ok, _ in results if ok)
print(f"\n========= EU-71 SWE-bench gap-fill tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("---------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
