"""EU-71 — unit tests for scripts/swebench_builder.py.

Tests the builder integration WITHOUT any network calls, real git clones,
or real Agent SDK calls.  All external I/O (subprocess, run_agent, swebench_eval
helpers) is monkey-patched with lightweight stubs so the suite stays fast and
runs completely offline.

Coverage:
  • BudgetExceededError attributes
  • _build_prompt() content and edge cases (missing problem_statement)
  • _extract_patch() happy path, git failure, staged-only fallback
  • build_patch_for_task() budget guard fires at entry (before run_agent)
  • build_patch_for_task() happy path: run_agent called, patch extracted, cost returned
  • build_patch_for_task() run_agent error: patch still extracted, cost still returned
  • run_benchmark() sample clamping to MAX_SAMPLE
  • run_benchmark() budget abort path: stops early, aborted=True in summary
  • run_benchmark() happy path: patches collected, run_tasks called, summary correct
  • swebench_eval.SWETask now exposes problem_statement field
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ── bootstrap: make the Agent SDK importable without the real package ─────────
_sdk = types.ModuleType("claude_agent_sdk")


class _SdkStub:
    def __init__(self, *a, **k):
        self.__dict__.update(k)

    def __call__(self, *a, **k):
        return self


_sdk.__getattr__ = lambda name: _SdkStub  # type: ignore[attr-defined]
sys.modules.setdefault("claude_agent_sdk", _sdk)
# ClaudeAgentOptions must be importable by name from the SDK module.
_sdk.ClaudeAgentOptions = _SdkStub  # type: ignore[attr-defined]

# ── add repo root + scripts/ to path ─────────────────────────────────────────
sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import swebench_builder as sb  # noqa: E402  (import after path setup)
import swebench_eval as ev     # noqa: E402


# ── minimal check helper (mirrors the project's existing test style) ──────────
results: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# ── shared temp audit path — keep benchmark runs OUT of the real trend ────────
# run_benchmark() appends one JSONL line per run; with no injected path it writes
# to the production audit/swebench_runs.jsonl.  Every run_benchmark() call below
# passes jsonl_path=_AUDIT_TMP so the suite never pollutes that committed trend.
import shutil as _shutil
import tempfile as _tempfile

_AUDIT_DIR = Path(_tempfile.mkdtemp(prefix="swebench_audit_"))
_AUDIT_TMP = _AUDIT_DIR / "runs.jsonl"


# ─────────────────────────────────────────────────────────────────────────────
# 1.  BudgetExceededError — attributes and message
# ─────────────────────────────────────────────────────────────────────────────
err = sb.BudgetExceededError(cost_usd=4.99, ceiling_usd=5.00)
check("BudgetExceededError.cost_usd stored", err.cost_usd == 4.99)
check("BudgetExceededError.ceiling_usd stored", err.ceiling_usd == 5.00)
check("BudgetExceededError is a RuntimeError", isinstance(err, RuntimeError))
check("BudgetExceededError message mentions ceiling", "5.00" in str(err))
check("BudgetExceededError message mentions cost", "4.99" in str(err))


# ─────────────────────────────────────────────────────────────────────────────
# 2.  _build_prompt() — content for a fully-populated task
# ─────────────────────────────────────────────────────────────────────────────
_task = ev.SWETask(
    instance_id="astropy__astropy-12907",
    repo="astropy/astropy",
    base_commit="deadbeef",
    gold_patch="--- a/x.py\n+++ b/x.py\n",
    fail_to_pass=["tests/test_foo.py::test_bar", "tests/test_foo.py::test_baz"],
    problem_statement="Calling foo() with None crashes with AttributeError.",
)
prompt = sb._build_prompt(_task)
check("_build_prompt contains repo name", "astropy/astropy" in prompt)
check("_build_prompt contains base commit", "deadbeef" in prompt)
check("_build_prompt contains problem statement", "AttributeError" in prompt)
check("_build_prompt contains fail_to_pass test id", "test_bar" in prompt)
check("_build_prompt contains second fail_to_pass test id", "test_baz" in prompt)
check("_build_prompt non-empty", len(prompt) > 50)

# Edge case: no problem_statement
_task_no_ps = ev.SWETask(
    instance_id="x__x-1", repo="x/x", base_commit="abc",
    gold_patch="", fail_to_pass=["t.py::f"],
)
prompt_no_ps = sb._build_prompt(_task_no_ps)
check("_build_prompt handles missing problem_statement gracefully",
      "no problem statement" in prompt_no_ps.lower() or "x/x" in prompt_no_ps)

# Edge case: no fail_to_pass
_task_no_tests = ev.SWETask(
    instance_id="y__y-2", repo="y/y", base_commit="def",
    gold_patch="", problem_statement="Something broke.",
)
prompt_no_tests = sb._build_prompt(_task_no_tests)
check("_build_prompt handles missing fail_to_pass gracefully",
      "no FAIL_TO_PASS" in prompt_no_tests or "y/y" in prompt_no_tests)


# ─────────────────────────────────────────────────────────────────────────────
# 3.  _extract_patch() — stages with `git add -A`, captures new files, fallbacks
# ─────────────────────────────────────────────────────────────────────────────
_GOOD_DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"

# Happy path: every git call succeeds → the staged diff is returned.
_ok = subprocess.CompletedProcess(["git"], 0, stdout=_GOOD_DIFF, stderr="")
with patch("swebench_builder.subprocess.run", return_value=_ok) as mock_run:
    extracted = sb._extract_patch(Path("/tmp/fake"))
    check("_extract_patch returns diff on success", extracted == _GOOD_DIFF)
    # It MUST stage everything first so untracked/new files land in the diff.
    _cmds = [c.args[0] for c in mock_run.call_args_list]
    check("_extract_patch runs `git add -A` before diffing",
          any("add" in cmd and "-A" in cmd for cmd in _cmds), str(_cmds))

# New/untracked file capture (the EU-71 iter-2 fix): `git add -A` stages a
# builder-created file, `git diff --cached` then emits it.  A plain `git diff
# HEAD` would have dropped it silently.
_NEW_FILE_DIFF = (
    "diff --git a/newmod.py b/newmod.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n+++ b/newmod.py\n@@ -0,0 +1 @@\n+print('hi')\n"
)
_add_seen = {"n": 0}


def _add_then_cached(cmd, *a, **k):
    """Empty for `add` (stages silently); the new-file diff for `diff --cached`."""
    if "add" in cmd:
        _add_seen["n"] += 1
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    if "--cached" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout=_NEW_FILE_DIFF, stderr="")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


with patch("swebench_builder.subprocess.run", side_effect=_add_then_cached):
    extracted_new = sb._extract_patch(Path("/tmp/fake"))
check("_extract_patch stages untracked files (git add -A called)", _add_seen["n"] >= 1)
check("_extract_patch captures builder-created new files",
      "new file mode" in extracted_new and "newmod.py" in extracted_new, repr(extracted_new))

# `git diff --cached` empty → fall back to `git diff HEAD`.
_head_ok = subprocess.CompletedProcess(["git"], 0, stdout=_GOOD_DIFF, stderr="")


def _cached_empty_head_ok(cmd, *a, **k):
    if "--cached" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")   # staged diff empty
    if "diff" in cmd:                                                       # plain `diff HEAD`
        return _head_ok
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")        # `add -A`


with patch("swebench_builder.subprocess.run", side_effect=_cached_empty_head_ok):
    extracted_fallback = sb._extract_patch(Path("/tmp/fake"))
    check("_extract_patch falls back to `git diff HEAD` when staged diff empty",
          extracted_fallback == _GOOD_DIFF)

# No changes at all (every git call returns empty)
_empty = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
with patch("swebench_builder.subprocess.run", return_value=_empty):
    extracted_empty = sb._extract_patch(Path("/tmp/fake"))
    check("_extract_patch returns '' when no changes", extracted_empty == "")

# Timeout → graceful empty string (no crash)
with patch("swebench_builder.subprocess.run",
           side_effect=subprocess.TimeoutExpired(["git"], 30)):
    extracted_timeout = sb._extract_patch(Path("/tmp/fake"))
    check("_extract_patch returns '' on timeout", extracted_timeout == "")


# ─────────────────────────────────────────────────────────────────────────────
# 4.  build_patch_for_task() — budget guard fires BEFORE run_agent
# ─────────────────────────────────────────────────────────────────────────────
async def _test_budget_guard():
    run_agent_called = {"n": 0}

    async def _fake_run_agent(*a, **k):
        run_agent_called["n"] += 1
        raise AssertionError("run_agent should NOT have been called")

    with patch("swebench_builder.run_agent", _fake_run_agent):
        try:
            await sb.build_patch_for_task(
                _task, Path("/tmp/repo"),
                cost_ceiling_usd=5.0,
                cumulative_cost=5.0,   # exactly at ceiling → abort
            )
            return False, False
        except sb.BudgetExceededError:
            return True, run_agent_called["n"] == 0


guard_raised, guard_before_call = asyncio.run(_test_budget_guard())
check("build_patch_for_task: budget guard raises BudgetExceededError", guard_raised)
check("build_patch_for_task: budget guard fires before run_agent", guard_before_call)


# ─────────────────────────────────────────────────────────────────────────────
# 5.  build_patch_for_task() — happy path: run_agent called, patch returned
# ─────────────────────────────────────────────────────────────────────────────
class _FakeRun:
    """Stand-in for AgentRun."""
    def __init__(self):
        self.is_error = False
        self.num_turns = 5
        self.cost_usd = 0.12


async def _test_happy():
    async def _fake_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None):
        return _FakeRun()

    with (
        patch("swebench_builder.run_agent", _fake_run_agent),
        patch("swebench_builder._extract_patch", return_value=_GOOD_DIFF),
    ):
        patch_str, cost = await sb.build_patch_for_task(
            _task, Path("/tmp/repo"),
            cost_ceiling_usd=5.0,
            cumulative_cost=0.0,
        )
        return patch_str, cost


patch_result, cost_result = asyncio.run(_test_happy())
check("build_patch_for_task happy path: returns patch", patch_result == _GOOD_DIFF)
check("build_patch_for_task happy path: returns cost", abs(cost_result - 0.12) < 1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# 6.  build_patch_for_task() — agent error: still returns patch + cost
# ─────────────────────────────────────────────────────────────────────────────
class _FakeErrorRun:
    is_error = True
    num_turns = 3
    cost_usd = 0.07


async def _test_agent_error():
    async def _fake_run_agent(prompt, options, **k):
        return _FakeErrorRun()

    with (
        patch("swebench_builder.run_agent", _fake_run_agent),
        patch("swebench_builder._extract_patch", return_value=""),
    ):
        patch_str, cost = await sb.build_patch_for_task(
            _task, Path("/tmp/repo"), cost_ceiling_usd=5.0, cumulative_cost=0.0,
        )
        return patch_str, cost


p_err, c_err = asyncio.run(_test_agent_error())
check("build_patch_for_task: agent error → empty patch (no crash)", p_err == "")
check("build_patch_for_task: agent error → cost still reported", abs(c_err - 0.07) < 1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# 7.  swebench_eval.SWETask — problem_statement field added by this subtask
# ─────────────────────────────────────────────────────────────────────────────
t_with_ps = ev.SWETask(
    instance_id="z__z-99",
    repo="z/z",
    base_commit="fff",
    gold_patch="",
    problem_statement="This is the issue description.",
)
check("SWETask has problem_statement field", hasattr(t_with_ps, "problem_statement"))
check("SWETask.problem_statement stored correctly",
      t_with_ps.problem_statement == "This is the issue description.")

# Default is empty string (backward-compatible)
t_no_ps = ev.SWETask(instance_id="z__z-0", repo="z/z", base_commit="0", gold_patch="")
check("SWETask.problem_statement defaults to ''", t_no_ps.problem_statement == "")


# ─────────────────────────────────────────────────────────────────────────────
# 8.  run_benchmark() — sample clamped to MAX_SAMPLE
# ─────────────────────────────────────────────────────────────────────────────
_dummy_tasks = [
    ev.SWETask(f"t-{i}", "o/r", "sha", "", [f"t/test.py::t_{i}"])
    for i in range(5)
]


async def _test_sample_clamp():
    """Confirm run_benchmark clamps sample to MAX_SAMPLE."""
    load_calls = {"n": 0, "arg": 0}

    def _fake_load_tasks(n):
        load_calls["n"] += 1
        load_calls["arg"] = n
        # return fewer tasks than requested to simulate clamping
        return _dummy_tasks[:min(n, len(_dummy_tasks))]

    async def _fake_build(*a, **k):
        return "", 0.0

    with (
        patch("swebench_eval.load_tasks", _fake_load_tasks),
        patch("swebench_builder.build_patch_for_task", _fake_build),
        patch("swebench_eval.run_tasks", return_value=[]),
    ):
        await sb.run_benchmark(sample=9999, cost_ceiling_usd=1000.0, jsonl_path=_AUDIT_TMP)

    # The clamped value passed to load_tasks must be <= MAX_SAMPLE
    return load_calls["arg"]


loaded_n = asyncio.run(_test_sample_clamp())
check("run_benchmark clamps sample to MAX_SAMPLE",
      loaded_n <= sb.MAX_SAMPLE, str(loaded_n))


# ─────────────────────────────────────────────────────────────────────────────
# 9.  run_benchmark() — budget abort: stops early, aborted=True
# ─────────────────────────────────────────────────────────────────────────────
async def _test_budget_abort():
    """First task costs $3, second should be aborted by the $4 ceiling."""
    call_count = {"n": 0}

    async def _expensive_build(task, repo_dir, cost_ceiling_usd, cumulative_cost):
        if cumulative_cost >= cost_ceiling_usd:
            raise sb.BudgetExceededError(cumulative_cost, cost_ceiling_usd)
        call_count["n"] += 1
        return _GOOD_DIFF, 3.0   # $3 per task

    with (
        patch("swebench_eval.load_tasks", return_value=_dummy_tasks[:3]),
        patch("swebench_eval._clone_repo"),   # no real git
        patch("swebench_builder.build_patch_for_task", _expensive_build),
        patch("swebench_eval.run_tasks", return_value=[
            {"task_id": f"t-{i}", "passed": False, "test_output": "", "error": ""}
            for i in range(3)
        ]),
    ):
        summary = await sb.run_benchmark(sample=3, cost_ceiling_usd=4.0, jsonl_path=_AUDIT_TMP)
    return summary, call_count["n"]


abort_summary, n_called = asyncio.run(_test_budget_abort())
check("run_benchmark aborted=True when budget exceeded", abort_summary["aborted"] is True)
# Only 1 task should have run ($3 spend) before the $4 ceiling cut in.
check("run_benchmark stops after budget hit (≤ 2 builder calls)", n_called <= 2, str(n_called))


# ─────────────────────────────────────────────────────────────────────────────
# 10.  run_benchmark() — happy path: patches collected, summary correct
# ─────────────────────────────────────────────────────────────────────────────
async def _test_happy_benchmark():
    """All tasks produce a patch at $0.10 each; run_tasks says all passed."""
    async def _cheap_build(task, repo_dir, cost_ceiling_usd, cumulative_cost):
        if cumulative_cost >= cost_ceiling_usd:
            raise sb.BudgetExceededError(cumulative_cost, cost_ceiling_usd)
        return _GOOD_DIFF, 0.10

    run_tasks_patches = {}

    def _fake_run_tasks(patches, tasks=None, **k):
        run_tasks_patches.update(patches)
        return [
            {"task_id": t.instance_id, "passed": True, "test_output": "1 passed", "error": ""}
            for t in (tasks or [])
        ]

    with (
        patch("swebench_eval.load_tasks", return_value=_dummy_tasks[:3]),
        patch("swebench_eval._clone_repo"),
        patch("swebench_builder.build_patch_for_task", _cheap_build),
        patch("swebench_eval.run_tasks", side_effect=_fake_run_tasks),
    ):
        summary = await sb.run_benchmark(sample=3, cost_ceiling_usd=10.0, jsonl_path=_AUDIT_TMP)
    return summary, run_tasks_patches


happy_summary, collected_patches = asyncio.run(_test_happy_benchmark())
check("run_benchmark happy: aborted=False", happy_summary["aborted"] is False)
check("run_benchmark happy: total == tasks loaded", happy_summary["total"] == 3)
check("run_benchmark happy: all passed", happy_summary["passed"] == 3)
check("run_benchmark happy: pass_rate == 1.0", happy_summary["pass_rate"] == 1.0)
check("run_benchmark happy: cost_usd accumulated",
      abs(happy_summary["cost_usd"] - 0.30) < 1e-6, str(happy_summary["cost_usd"]))
check("run_benchmark happy: patches passed to run_tasks",
      len(collected_patches) == 3, str(len(collected_patches)))
check("run_benchmark happy: patches contain expected content",
      all(v == _GOOD_DIFF for v in collected_patches.values()))


# ─────────────────────────────────────────────────────────────────────────────
# 11.  MAX_SAMPLE and DEFAULT constants are sane
# ─────────────────────────────────────────────────────────────────────────────
check("MAX_SAMPLE == 50 (hard cap)", sb.MAX_SAMPLE == 50)
check("DEFAULT_SAMPLE <= MAX_SAMPLE", sb.DEFAULT_SAMPLE <= sb.MAX_SAMPLE)
check("DEFAULT_COST_CEILING > 0", sb.DEFAULT_COST_CEILING > 0)
check("DEFAULT_COST_CEILING <= 10 (sane default)", sb.DEFAULT_COST_CEILING <= 10.0)


# ─────────────────────────────────────────────────────────────────────────────
# 12.  audit isolation — run_benchmark wrote to the INJECTED path, not the trend
# ─────────────────────────────────────────────────────────────────────────────
# The three run_benchmark() calls above all passed jsonl_path=_AUDIT_TMP.  If the
# injection works they appended their JSONL records there and never touched the
# production audit/swebench_runs.jsonl.
check("run_benchmark wrote audit lines to the injected jsonl_path",
      _AUDIT_TMP.exists() and bool(_AUDIT_TMP.read_text(encoding="utf-8").strip()),
      str(_AUDIT_TMP))
_shutil.rmtree(_AUDIT_DIR, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# result summary
# ─────────────────────────────────────────────────────────────────────────────
passed_n = sum(1 for _, ok, _ in results if ok)
print("\n========= EU-71 SWE-bench builder integration QA =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("-----------------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
