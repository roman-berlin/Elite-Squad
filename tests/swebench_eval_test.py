"""EU-71 — unit tests for scripts/swebench_eval.py.

Tests the harness's task-environment lifecycle WITHOUT any network calls,
real git clones, or Builder logic.  Every external call (subprocess, datasets
library, git) is monkey-patched with lightweight stubs so the suite stays fast
and offline.

Coverage:
  • SWETask construction and _parse_test_list normalisation
  • load_tasks() routing (datasets → swebench → ImportError)
  • _apply_patch() happy path and failure (empty patch, non-zero git exit)
  • run_single_task() end-to-end with all subprocesses stubbed
  • run_tasks() — patch lookup, missing-patch handling, result aggregation
  • MAX_SAMPLE clamping in load_tasks()
  • TaskResult.to_dict() contract
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── bootstrap: make the Agent SDK importable (no real SDK in the test env) ──
_sdk = types.ModuleType("claude_agent_sdk")


class _Stub:
    def __init__(self, *a, **k):
        self.__dict__.update(k)

    def __call__(self, *a, **k):
        return self


_sdk.__getattr__ = lambda n: _Stub  # type: ignore[attr-defined]
sys.modules.setdefault("claude_agent_sdk", _sdk)

# ── add repo root + scripts/ to path so the module is importable ────────────
sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import swebench_eval as ev  # noqa: E402  (import after path setup)

# ── minimal check helper (mirrors the project's existing test style) ─────────
results: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# ─────────────────────────────────────────────────────────────────────────────
# 1.  _parse_test_list — normalise dataset field variants
# ─────────────────────────────────────────────────────────────────────────────
check("parse list as-is", ev._parse_test_list(["a::b", "c::d"]) == ["a::b", "c::d"])
check("parse None → []", ev._parse_test_list(None) == [])
check("parse JSON string", ev._parse_test_list('["x::y"]') == ["x::y"])
check("parse malformed string → []", ev._parse_test_list("not json") == [])
check("parse empty list", ev._parse_test_list([]) == [])


# ─────────────────────────────────────────────────────────────────────────────
# 2.  SWETask construction
# ─────────────────────────────────────────────────────────────────────────────
t = ev.SWETask(
    instance_id="astropy__astropy-12907",
    repo="astropy/astropy",
    base_commit="abc123",
    gold_patch="--- a/foo.py\n+++ b/foo.py\n",
    fail_to_pass=["tests/test_foo.py::test_bar"],
)
check("SWETask.instance_id", t.instance_id == "astropy__astropy-12907")
check("SWETask.fail_to_pass list", t.fail_to_pass == ["tests/test_foo.py::test_bar"])
check("SWETask.pass_to_pass defaults to []", t.pass_to_pass == [])


# ─────────────────────────────────────────────────────────────────────────────
# 3.  TaskResult.to_dict()
# ─────────────────────────────────────────────────────────────────────────────
r = ev.TaskResult(task_id="t1", passed=True, test_output="1 passed")
d = r.to_dict()
check("to_dict has task_id", d["task_id"] == "t1")
check("to_dict has passed", d["passed"] is True)
check("to_dict has test_output", d["test_output"] == "1 passed")
check("to_dict has error key", "error" in d)
check("to_dict error defaults to ''", d["error"] == "")


# ─────────────────────────────────────────────────────────────────────────────
# 4.  load_tasks() MAX_SAMPLE clamping
# ─────────────────────────────────────────────────────────────────────────────
_dummy_rows = [
    {
        "instance_id": f"repo__repo-{i}",
        "repo": "owner/repo",
        "base_commit": "deadbeef",
        "patch": "diff ...",
        "FAIL_TO_PASS": '["tests/test_x.py::test_y"]',
        "PASS_TO_PASS": "[]",
    }
    for i in range(60)
]


def _fake_load_dataset(name, split, streaming):
    return iter(_dummy_rows)


def _fake_datasets_module():
    m = types.ModuleType("datasets")
    m.load_dataset = _fake_load_dataset  # type: ignore[attr-defined]
    return m


with patch.dict(sys.modules, {"datasets": _fake_datasets_module()}):
    tasks_20 = ev.load_tasks(20)
    tasks_over = ev.load_tasks(999)   # should be clamped to MAX_SAMPLE

check("load_tasks returns 20 by default", len(tasks_20) == 20, len(tasks_20))
check("load_tasks clamps to MAX_SAMPLE", len(tasks_over) == ev.MAX_SAMPLE, len(tasks_over))
check("load_tasks returns SWETask objects", isinstance(tasks_20[0], ev.SWETask))
check("load_tasks parses fail_to_pass JSON string",
      tasks_20[0].fail_to_pass == ["tests/test_x.py::test_y"])


# ─────────────────────────────────────────────────────────────────────────────
# 5.  load_tasks() falls back to swebench when datasets is absent
# ─────────────────────────────────────────────────────────────────────────────
_swebench_mod = types.ModuleType("swebench")
_swebench_harness = types.ModuleType("swebench.harness")
_swebench_utils = types.ModuleType("swebench.harness.utils")
_swebench_utils.load_swebench_dataset = lambda name, split: _dummy_rows[:5]  # type: ignore[attr-defined]
sys.modules["swebench"] = _swebench_mod
sys.modules["swebench.harness"] = _swebench_harness
sys.modules["swebench.harness.utils"] = _swebench_utils

# Simulate datasets being absent
_no_datasets = types.ModuleType("datasets")


def _raise(*a, **k):
    raise ImportError("no datasets")


_no_datasets.load_dataset = _raise  # type: ignore[attr-defined]

with patch.dict(sys.modules, {"datasets": _no_datasets}):
    tasks_sw = ev.load_tasks(3)

check("swebench fallback returns tasks", len(tasks_sw) == 3, len(tasks_sw))
check("swebench fallback returns SWETask", isinstance(tasks_sw[0], ev.SWETask))

# Cleanup: remove stub swebench modules so later tests don't see them
for key in ["swebench", "swebench.harness", "swebench.harness.utils"]:
    sys.modules.pop(key, None)


# ─────────────────────────────────────────────────────────────────────────────
# 6.  load_tasks() raises ImportError when neither library is available
# ─────────────────────────────────────────────────────────────────────────────
with patch.dict(sys.modules, {"datasets": _no_datasets}):
    try:
        ev.load_tasks(1)
        check("ImportError raised when both absent", False, "no exception")
    except ImportError as exc:
        check("ImportError raised when both absent", True, str(exc)[:60])
    except Exception as exc:  # noqa: BLE001
        check("ImportError raised when both absent", False, repr(exc))


# ─────────────────────────────────────────────────────────────────────────────
# 7.  _apply_patch() — happy path and failure modes
# ─────────────────────────────────────────────────────────────────────────────
import subprocess  # noqa: E402

_GOOD_PATCH = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
_EMPTY_PATCH = "   \n"

# Happy path: git apply exits 0
_ok = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
with patch("subprocess.run", return_value=_ok) as mock_run:
    try:
        ev._apply_patch(Path("/tmp/fake_repo"), _GOOD_PATCH)
        check("_apply_patch happy path does not raise", True)
        check("_apply_patch calls git apply", mock_run.called)
        # Verify the patch string was piped via stdin
        _, kwargs = mock_run.call_args
        check("_apply_patch pipes patch via stdin", kwargs.get("input") == _GOOD_PATCH)
    except Exception as exc:  # noqa: BLE001
        check("_apply_patch happy path does not raise", False, repr(exc))

# Empty patch → WorktreeError before git is called
try:
    ev._apply_patch(Path("/tmp/fake_repo"), _EMPTY_PATCH)
    check("_apply_patch rejects empty patch", False, "no exception raised")
except ev.WorktreeError:
    check("_apply_patch rejects empty patch", True)

# git apply exits non-zero → WorktreeError
_fail = subprocess.CompletedProcess(["git"], 1, stdout="", stderr="patch does not apply")
with patch("subprocess.run", return_value=_fail):
    try:
        ev._apply_patch(Path("/tmp/fake_repo"), _GOOD_PATCH)
        check("_apply_patch raises WorktreeError on git failure", False, "no exception")
    except ev.WorktreeError as exc:
        check("_apply_patch raises WorktreeError on git failure", True, str(exc)[:40])


# ─────────────────────────────────────────────────────────────────────────────
# 8.  run_single_task() — full lifecycle with all subprocesses stubbed
# ─────────────────────────────────────────────────────────────────────────────
_task = ev.SWETask(
    instance_id="lib__lib-42",
    repo="owner/lib",
    base_commit="cafebabe",
    gold_patch=_GOOD_PATCH,
    fail_to_pass=["tests/test_lib.py::test_answer"],
)


def _make_subprocess_stub(returncode=0, stdout="", stderr=""):
    """Return a side-effect function that always yields a fixed CompletedProcess."""
    cp = subprocess.CompletedProcess(["cmd"], returncode, stdout=stdout, stderr=stderr)

    def _stub(*args, **kwargs):
        return cp

    return _stub


# Stub every subprocess.run call in the module so no real git/python runs.
_stub_ok = _make_subprocess_stub(returncode=0, stdout="1 passed", stderr="")

with (
    patch("swebench_eval.shutil.rmtree"),           # don't touch real filesystem
    patch("swebench_eval._clone_repo"),             # skip real git clone
    patch("swebench_eval._apply_patch"),            # skip real git apply
    patch("swebench_eval._create_venv", return_value=Path("/venv/bin/python")),
    patch("swebench_eval._run_tests", return_value=(True, "1 passed")),
):
    res = ev.run_single_task(_task, _GOOD_PATCH)

check("run_single_task returns TaskResult", isinstance(res, ev.TaskResult))
check("run_single_task passed=True on success", res.passed is True)
check("run_single_task carries test_output", "passed" in res.test_output)
check("run_single_task task_id matches", res.task_id == "lib__lib-42")
check("run_single_task error is empty on success", res.error == "")

# Simulate a WorktreeError mid-lifecycle (e.g. patch apply fails)
with (
    patch("swebench_eval.shutil.rmtree"),
    patch("swebench_eval._clone_repo"),
    patch("swebench_eval._apply_patch", side_effect=ev.WorktreeError("patch failed")),
):
    res_err = ev.run_single_task(_task, _GOOD_PATCH)

check("run_single_task catches WorktreeError", res_err.passed is False)
check("run_single_task error field set on WorktreeError", "patch failed" in res_err.error)

# Simulate a timeout
with (
    patch("swebench_eval.shutil.rmtree"),
    patch("swebench_eval._clone_repo",
          side_effect=subprocess.TimeoutExpired(["git"], 300)),
):
    res_to = ev.run_single_task(_task, _GOOD_PATCH)

check("run_single_task catches TimeoutExpired", res_to.passed is False)
check("run_single_task error field set on timeout", "timeout" in res_to.error.lower())


# ─────────────────────────────────────────────────────────────────────────────
# 8b.  run_single_task() — faithful SWE-bench scoring (FAIL_TO_PASS + PASS_TO_PASS)
# ─────────────────────────────────────────────────────────────────────────────
# A task counts as solved ONLY when every FAIL_TO_PASS test passes AND every
# PASS_TO_PASS test STILL passes (the regression guard).  Scoring on FAIL_TO_PASS
# alone would let a patch that breaks unrelated tests be counted as a solve.
_task_p2p = ev.SWETask(
    instance_id="lib__lib-43",
    repo="owner/lib",
    base_commit="cafebabe",
    gold_patch=_GOOD_PATCH,
    fail_to_pass=["tests/test_lib.py::test_answer"],
    pass_to_pass=["tests/test_lib.py::test_existing"],
)

# (A) both sets pass → solved, and BOTH sets are actually executed.
_runs_a: list[list[str]] = []


def _both_pass(python, repo_dir, test_ids):
    _runs_a.append(list(test_ids))
    return (True, "1 passed")


with (
    patch("swebench_eval.shutil.rmtree"),
    patch("swebench_eval._clone_repo"),
    patch("swebench_eval._apply_patch"),
    patch("swebench_eval._create_venv", return_value=Path("/venv/bin/python")),
    patch("swebench_eval._run_tests", side_effect=_both_pass),
):
    res_both = ev.run_single_task(_task_p2p, _GOOD_PATCH)

check("run_single_task: solved when FAIL_TO_PASS and PASS_TO_PASS both pass",
      res_both.passed is True)
check("run_single_task: runs BOTH fail_to_pass and pass_to_pass", len(_runs_a) == 2, str(_runs_a))
check("run_single_task: pass_to_pass ids are executed",
      _task_p2p.pass_to_pass in _runs_a, str(_runs_a))


# (B) FAIL_TO_PASS passes but PASS_TO_PASS fails → task is NOT solved (the core
#     EU-71 iter-2 fix: a regression must never be counted as a solve).
def _f2p_pass_p2p_fail(python, repo_dir, test_ids):
    if test_ids == _task_p2p.fail_to_pass:
        return (True, "1 passed")
    return (False, "1 failed")   # pass_to_pass regressed


with (
    patch("swebench_eval.shutil.rmtree"),
    patch("swebench_eval._clone_repo"),
    patch("swebench_eval._apply_patch"),
    patch("swebench_eval._create_venv", return_value=Path("/venv/bin/python")),
    patch("swebench_eval._run_tests", side_effect=_f2p_pass_p2p_fail),
):
    res_regress = ev.run_single_task(_task_p2p, _GOOD_PATCH)

check("run_single_task: PASS_TO_PASS regression marks task UNSOLVED (no fake solve)",
      res_regress.passed is False)
check("run_single_task: regression output records the PASS_TO_PASS section",
      "PASS_TO_PASS" in res_regress.test_output, repr(res_regress.test_output))


# (C) FAIL_TO_PASS fails → task unsolved and the PASS_TO_PASS run is SKIPPED
#     (pure optimization: the verdict is already False, so we save a pytest run).
_runs_c: list[list[str]] = []


def _f2p_fail(python, repo_dir, test_ids):
    _runs_c.append(list(test_ids))
    return (False, "1 failed")


with (
    patch("swebench_eval.shutil.rmtree"),
    patch("swebench_eval._clone_repo"),
    patch("swebench_eval._apply_patch"),
    patch("swebench_eval._create_venv", return_value=Path("/venv/bin/python")),
    patch("swebench_eval._run_tests", side_effect=_f2p_fail),
):
    res_f2p_fail = ev.run_single_task(_task_p2p, _GOOD_PATCH)

check("run_single_task: FAIL_TO_PASS failure → unsolved", res_f2p_fail.passed is False)
check("run_single_task: skips PASS_TO_PASS run when FAIL_TO_PASS already failed",
      _runs_c == [_task_p2p.fail_to_pass], str(_runs_c))


# (D) no PASS_TO_PASS → solved on FAIL_TO_PASS alone (backward compatible).
_task_no_p2p = ev.SWETask(
    instance_id="lib__lib-44", repo="owner/lib", base_commit="cafebabe",
    gold_patch=_GOOD_PATCH, fail_to_pass=["tests/test_lib.py::test_answer"],
)
_runs_d: list[list[str]] = []


def _only_f2p(python, repo_dir, test_ids):
    _runs_d.append(list(test_ids))
    return (True, "ok")


with (
    patch("swebench_eval.shutil.rmtree"),
    patch("swebench_eval._clone_repo"),
    patch("swebench_eval._apply_patch"),
    patch("swebench_eval._create_venv", return_value=Path("/venv/bin/python")),
    patch("swebench_eval._run_tests", side_effect=_only_f2p),
):
    res_no_p2p = ev.run_single_task(_task_no_p2p, _GOOD_PATCH)

check("run_single_task: no PASS_TO_PASS → solved on FAIL_TO_PASS alone",
      res_no_p2p.passed is True)
check("run_single_task: no PASS_TO_PASS → only one pytest run", len(_runs_d) == 1, str(_runs_d))


# ─────────────────────────────────────────────────────────────────────────────
# 9.  run_tasks() — aggregation and missing-patch handling
# ─────────────────────────────────────────────────────────────────────────────
_tasks = [
    ev.SWETask("id-1", "owner/r", "sha1", _GOOD_PATCH, ["t/test.py::a"]),
    ev.SWETask("id-2", "owner/r", "sha2", _GOOD_PATCH, ["t/test.py::b"]),
    ev.SWETask("id-3", "owner/r", "sha3", _GOOD_PATCH, ["t/test.py::c"]),
]
_patches = {"id-1": _GOOD_PATCH, "id-2": _GOOD_PATCH}   # id-3 deliberately absent


def _fake_run_single(task, patch_, *, work_root=None):
    return ev.TaskResult(task_id=task.instance_id, passed=True, test_output="ok")


with patch("swebench_eval.run_single_task", side_effect=_fake_run_single):
    run_results = ev.run_tasks(_patches, tasks=_tasks)

check("run_tasks returns one result per task", len(run_results) == 3, len(run_results))
check("run_tasks all results are dicts", all(isinstance(r, dict) for r in run_results))
check("run_tasks id-1 passed", run_results[0]["passed"] is True)
check("run_tasks id-2 passed", run_results[1]["passed"] is True)
check("run_tasks id-3 skipped (no patch) → passed=False", run_results[2]["passed"] is False)
check("run_tasks id-3 error mentions missing patch", "patch" in run_results[2]["error"].lower())

# ─────────────────────────────────────────────────────────────────────────────
# 10.  _build_gold_patches() helper (CLI smoke-test utility)
# ─────────────────────────────────────────────────────────────────────────────
_gp = ev._build_gold_patches([
    ev.SWETask("x-1", "o/r", "s", "patch1", []),
    ev.SWETask("x-2", "o/r", "s", "",       []),   # empty gold patch — should be excluded
])
check("_build_gold_patches includes tasks with patches", "x-1" in _gp)
check("_build_gold_patches excludes empty gold patches", "x-2" not in _gp)
check("_build_gold_patches maps id → patch string", _gp.get("x-1") == "patch1")


# ─────────────────────────────────────────────────────────────────────────────
# result summary
# ─────────────────────────────────────────────────────────────────────────────
passed_n = sum(1 for _, ok, _ in results if ok)
print("\n========= EU-71 SWE-bench eval harness QA =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("----------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
