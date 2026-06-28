"""EU-95 — CLI tests for the 'benchmark' subcommand wired into orchestrator/main.py.

Covers:
  • The 'benchmark' subparser is registered in build_parser().
  • --sample and --budget flags are accepted and propagate to run_benchmark().
  • --seed flag is accepted and propagates to run_benchmark().
  • When --seed is omitted, the weekly_seed() value is used.
  • The REAL report path (run_benchmark → swebench_report.report) appends EXACTLY
    ONE swebench_run record per invocation and prints the summary table ONCE —
    a non-green-blind guard against the iter-1 double-print / double-append bug.
  • --trend N triggers a call to swebench_report.print_trend() with N.
  • The handler returns 0 on a clean run and 1 on an aborted run.

Design notes:
  We test via the public build_parser() / _main() entrypoints to stay close to
  real operator usage.  All external I/O (run_benchmark, report, print_trend) is
  mocked so the test is hermetic and fast.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

# ── bootstrap: make the repo root and scripts/ importable ─────────────────────
sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

# Stub claude_agent_sdk before any swebench module is loaded, mirroring the
# pattern used in eu71_swebench_cli_test.py.
_sdk = types.ModuleType("claude_agent_sdk")
_sdk.__getattr__ = lambda name: MagicMock()  # type: ignore[attr-defined]
_sdk.ClaudeAgentOptions = MagicMock()        # type: ignore[attr-defined]
sys.modules.setdefault("claude_agent_sdk", _sdk)

# Stub heavy orchestrator deps that main.py and swebench_builder pull in at
# import time so the modules load cleanly without real config / credentials.
for _mod in (
    "orchestrator.intake",
    "orchestrator.audit",
    "orchestrator.config",
    "orchestrator.contracts",
    "orchestrator.agent",
):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

# Symbols accessed at module level by main.py.
sys.modules["orchestrator.config"].Config = MagicMock()           # type: ignore[attr-defined]
sys.modules["orchestrator.config"].normalize_effort = MagicMock() # type: ignore[attr-defined]
sys.modules["orchestrator.audit"].AuditLog = MagicMock()          # type: ignore[attr-defined]
# orchestrator.contracts needs Outcome (for main.py) + BuildRequest/Ticket
# (for swebench_builder's module-level import).
sys.modules["orchestrator.contracts"].Outcome = MagicMock()       # type: ignore[attr-defined]
sys.modules["orchestrator.contracts"].BuildRequest = MagicMock()  # type: ignore[attr-defined]
sys.modules["orchestrator.contracts"].Ticket = MagicMock()        # type: ignore[attr-defined]
# orchestrator.agent needs run_agent (swebench_builder module-level import).
sys.modules["orchestrator.agent"].run_agent = AsyncMock()         # type: ignore[attr-defined]

from orchestrator.main import build_parser, _main  # noqa: E402  (after stubs)
import swebench_report as sr                       # noqa: E402
import swebench_eval as ev                         # noqa: E402  (pure-stdlib; real report path)

# ── check helper (same convention as the other EU-* test files) ───────────────
results: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = "") -> None:
    """Record a named test result."""
    results.append((name, bool(cond), str(detail)))


# ── shared fixtures ───────────────────────────────────────────────────────────
_OK_SUMMARY: dict = {
    "total": 5, "passed": 3, "failed": 2,
    "pass_rate": 0.6, "cost_usd": 1.23,
    "aborted": False, "results": [],
}
_ABORTED_SUMMARY: dict = {**_OK_SUMMARY, "aborted": True}


# ─────────────────────────────────────────────────────────────────────────────
# 1. 'benchmark' subparser is registered
# ─────────────────────────────────────────────────────────────────────────────
# build_parser() must expose a 'benchmark' subcommand so the operator can
# discover it in --help and so the orchestrator can invoke it via cron.

p = build_parser()
# Parse a minimal benchmark invocation to confirm the subparser exists.
_parsed = None
_subparser_err: str = ""
try:
    _parsed = p.parse_args(["benchmark"])
except SystemExit as exc:
    _subparser_err = f"parse failed with exit {exc.code}"

check("'benchmark' subparser is registered in build_parser()",
      _parsed is not None and getattr(_parsed, "command", None) == "benchmark",
      _subparser_err or f"command={getattr(_parsed, 'command', None)!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 2. --sample flag is accepted and has correct default
# ─────────────────────────────────────────────────────────────────────────────
_args_default = p.parse_args(["benchmark"])
check("--sample default is 20",
      getattr(_args_default, "sample", None) == 20,
      f"sample={getattr(_args_default, 'sample', None)!r}")

_args_sample = p.parse_args(["benchmark", "--sample", "7"])
check("--sample 7 is parsed correctly",
      getattr(_args_sample, "sample", None) == 7,
      f"sample={getattr(_args_sample, 'sample', None)!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 3. --budget flag is accepted and has correct default
# ─────────────────────────────────────────────────────────────────────────────
check("--budget default is 5.0",
      getattr(_args_default, "budget", None) == 5.0,
      f"budget={getattr(_args_default, 'budget', None)!r}")

_args_budget = p.parse_args(["benchmark", "--budget", "2.5"])
check("--budget 2.5 is parsed correctly",
      getattr(_args_budget, "budget", None) == 2.5,
      f"budget={getattr(_args_budget, 'budget', None)!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 4. --seed flag is accepted (default None → weekly_seed() used at runtime)
# ─────────────────────────────────────────────────────────────────────────────
check("--seed default is None (weekly_seed() computed at runtime)",
      getattr(_args_default, "seed", "MISSING") is None,
      f"seed={getattr(_args_default, 'seed', 'MISSING')!r}")

_args_seed = p.parse_args(["benchmark", "--seed", "my-custom-seed"])
check("--seed my-custom-seed is parsed correctly",
      getattr(_args_seed, "seed", None) == "my-custom-seed",
      f"seed={getattr(_args_seed, 'seed', None)!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 5. --sample and --budget propagate to run_benchmark()
# ─────────────────────────────────────────────────────────────────────────────
# _main(["benchmark", "--sample", "7", "--budget", "3.0"]) must call
# run_benchmark(sample=7, cost_ceiling_usd=3.0, ...).

_captured_rb_kwargs: list[dict] = []


async def _fake_run_benchmark(**kw):
    """Stand-in for swebench_builder.run_benchmark; records kwargs."""
    _captured_rb_kwargs.append(kw)
    return _OK_SUMMARY


try:
    with (
        patch("swebench_builder.run_benchmark", _fake_run_benchmark),
        patch("swebench_report.report"),
        patch("swebench_report.print_trend"),
    ):
        _rc = asyncio.run(_main(["benchmark", "--sample", "7", "--budget", "3.0"]))
except Exception as exc:  # noqa: BLE001
    _captured_rb_kwargs.append({"_error": str(exc)})
    _rc = -1

check("run_benchmark called with sample=7",
      _captured_rb_kwargs and _captured_rb_kwargs[0].get("sample") == 7,
      str(_captured_rb_kwargs))
check("run_benchmark called with cost_ceiling_usd=3.0",
      _captured_rb_kwargs and _captured_rb_kwargs[0].get("cost_ceiling_usd") == 3.0,
      str(_captured_rb_kwargs))


# ─────────────────────────────────────────────────────────────────────────────
# 6. --seed propagates to run_benchmark() as random_seed
# ─────────────────────────────────────────────────────────────────────────────
_captured_seed_kwargs: list[dict] = []


async def _fake_rb_seed(**kw):
    _captured_seed_kwargs.append(kw)
    return _OK_SUMMARY


try:
    with (
        patch("swebench_builder.run_benchmark", _fake_rb_seed),
        patch("swebench_report.report"),
        patch("swebench_report.print_trend"),
    ):
        asyncio.run(_main(["benchmark", "--seed", "test-seed-42"]))
except Exception:  # noqa: BLE001
    pass

check("explicit --seed propagates as random_seed to run_benchmark",
      _captured_seed_kwargs and _captured_seed_kwargs[0].get("random_seed") == "test-seed-42",
      str(_captured_seed_kwargs))


# ─────────────────────────────────────────────────────────────────────────────
# 7. When --seed omitted, the weekly_seed() value is forwarded to run_benchmark
# ─────────────────────────────────────────────────────────────────────────────
_captured_weekly_kwargs: list[dict] = []
_expected_weekly_seed = sr.weekly_seed()


async def _fake_rb_weekly(**kw):
    _captured_weekly_kwargs.append(kw)
    return _OK_SUMMARY


try:
    with (
        patch("swebench_builder.run_benchmark", _fake_rb_weekly),
        patch("swebench_report.report"),
        patch("swebench_report.print_trend"),
    ):
        asyncio.run(_main(["benchmark"]))
except Exception:  # noqa: BLE001
    pass

check("omitting --seed forwards weekly_seed() to run_benchmark as random_seed",
      _captured_weekly_kwargs
      and _captured_weekly_kwargs[0].get("random_seed") == _expected_weekly_seed,
      f"got={(_captured_weekly_kwargs[0].get('random_seed') if _captured_weekly_kwargs else None)!r}, "
      f"expected={_expected_weekly_seed!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 8. The REAL report path appends EXACTLY ONE record and prints the table ONCE
# ─────────────────────────────────────────────────────────────────────────────
# EU-95 iter-2 — NOT green-blind.  The previous version mocked BOTH run_benchmark
# AND report, then merely asserted report() was called.  That could not see the
# bug it was supposed to guard against: _benchmark() called report() a SECOND
# time after run_benchmark() had already printed + appended, double-printing the
# summary and appending a duplicate JSONL trend row.
#
# Here we drive the REAL run_benchmark — only its heavy I/O (task loading, repo
# clone, builder, scoring) is stubbed — so the genuine swebench_report.report()
# (print_summary + append_run) actually runs.  We point swebench_report.AUDIT_JSONL
# at a temp file so BOTH the path run_benchmark threads through
# (jsonl_path=AUDIT_JSONL) AND any accidental default-path write land in the SAME
# file.  A reintroduced redundant report() call would therefore yield 2 records
# and 2 "Builder:" headers and fail the assertions below.

# Helper reused by sections 9–11 (which keep run_benchmark mocked).
async def _noop_rb(**kw):
    return _OK_SUMMARY


# Canned task + harness so the real run_benchmark reaches the real report() fast.
_BENCH_TASK = ev.SWETask(
    instance_id="fake__fake-1", repo="fake/fake",
    base_commit="0" * 40, gold_patch="",
)


def _fake_load_tasks(_n):
    return [_BENCH_TASK]


def _fake_clone_repo(_repo, _commit, _dest):
    return None  # no-op: run_benchmark already created the destination dir


async def _fake_build_patch(_task, _repo_dir, **_kw):
    return ("--- a/x\n+++ b/x\n", 0.01)


def _fake_run_tasks(_patches, *, tasks=None, **_kw):
    return [{"task_id": t.instance_id, "passed": True, "test_output": "", "error": ""}
            for t in (tasks or [])]


_run_records: list[dict] = []
_bench_stdout = ""
try:
    with tempfile.TemporaryDirectory() as _bench_td:
        _tmp_jsonl = Path(_bench_td) / "swebench_runs.jsonl"
        _stdout_buf = io.StringIO()
        with (
            patch("swebench_eval.load_tasks", _fake_load_tasks),
            patch("swebench_eval._clone_repo", _fake_clone_repo),
            patch("swebench_builder.build_patch_for_task", _fake_build_patch),
            patch("swebench_eval.run_tasks", _fake_run_tasks),
            patch.object(sr, "AUDIT_JSONL", _tmp_jsonl),
            contextlib.redirect_stdout(_stdout_buf),
        ):
            _rc_report = asyncio.run(_main(["benchmark", "--sample", "1"]))
        _bench_stdout = _stdout_buf.getvalue()
        _raw_lines = (
            [ln for ln in _tmp_jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if _tmp_jsonl.exists() else []
        )
        _run_records = [
            rec for rec in (json.loads(ln) for ln in _raw_lines)
            if rec.get("event") == "swebench_run"
        ]
except Exception as exc:  # noqa: BLE001
    _bench_stdout = f"__error__: {exc}"

check("real run_benchmark appends EXACTLY ONE swebench_run record (no double-append)",
      len(_run_records) == 1,
      f"record count={len(_run_records)} (stdout={_bench_stdout!r})")

# The single record must reflect THIS run — guards against a stale/empty pass.
check("the appended record reflects the run (event/passed/total)",
      len(_run_records) == 1
      and _run_records[0].get("passed") == 1
      and _run_records[0].get("total") == 1,
      str(_run_records))

check("benchmark prints the summary table EXACTLY ONCE (no double-print)",
      _bench_stdout.count("Builder:") == 1,
      f"'Builder:' count={_bench_stdout.count('Builder:')} (stdout={_bench_stdout!r})")


# ─────────────────────────────────────────────────────────────────────────────
# 9. --trend N triggers swebench_report.print_trend()
# ─────────────────────────────────────────────────────────────────────────────
_trend_calls: list[tuple] = []


def _spy_trend(*a, **kw):
    _trend_calls.append((a, kw))


try:
    with (
        patch("swebench_builder.run_benchmark", _noop_rb),
        patch("swebench_report.report"),
        patch("swebench_report.print_trend", _spy_trend),
    ):
        asyncio.run(_main(["benchmark", "--trend", "8"]))
except Exception:  # noqa: BLE001
    pass

check("--trend 8 triggers swebench_report.print_trend()",
      len(_trend_calls) >= 1,
      f"print_trend call count={len(_trend_calls)}")

# print_trend receives N=8 somewhere in the call (either positional or keyword).
_trend_n = None
if _trend_calls:
    _a, _kw = _trend_calls[0]
    # signature: print_trend(jsonl_path, n) — n is the second positional arg
    if len(_a) >= 2:
        _trend_n = _a[1]
    else:
        _trend_n = _kw.get("n")
check("print_trend receives N=8",
      _trend_n == 8,
      f"n={_trend_n!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 10. When --trend is omitted, print_trend() is NOT called
# ─────────────────────────────────────────────────────────────────────────────
_no_trend_calls: list = []


def _spy_trend_no(*a, **kw):
    _no_trend_calls.append((a, kw))


try:
    with (
        patch("swebench_builder.run_benchmark", _noop_rb),
        patch("swebench_report.report"),
        patch("swebench_report.print_trend", _spy_trend_no),
    ):
        asyncio.run(_main(["benchmark"]))
except Exception:  # noqa: BLE001
    pass

check("print_trend is NOT called when --trend is omitted",
      len(_no_trend_calls) == 0,
      f"call count={len(_no_trend_calls)}")


# ─────────────────────────────────────────────────────────────────────────────
# 11. Handler returns 0 on a clean run
# ─────────────────────────────────────────────────────────────────────────────
_rc_ok = -1
try:
    with (
        patch("swebench_builder.run_benchmark", _noop_rb),
        patch("swebench_report.report"),
        patch("swebench_report.print_trend"),
    ):
        _rc_ok = asyncio.run(_main(["benchmark"]))
except Exception:  # noqa: BLE001
    pass

check("handler returns 0 when run is not aborted",
      _rc_ok == 0,
      f"rc={_rc_ok!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 12. Handler returns 1 when run_benchmark reports aborted=True
# ─────────────────────────────────────────────────────────────────────────────
async def _aborted_rb(**kw):
    return _ABORTED_SUMMARY


_rc_aborted = -1
try:
    with (
        patch("swebench_builder.run_benchmark", _aborted_rb),
        patch("swebench_report.report"),
        patch("swebench_report.print_trend"),
    ):
        _rc_aborted = asyncio.run(_main(["benchmark"]))
except Exception:  # noqa: BLE001
    pass

check("handler returns 1 when run_benchmark reports aborted=True",
      _rc_aborted == 1,
      f"rc={_rc_aborted!r}")


# ─────────────────────────────────────────────────────────────────────────────
# result summary
# ─────────────────────────────────────────────────────────────────────────────
passed_n = sum(1 for _, ok, _ in results if ok)
print(f"\n========= EU-95 benchmark subcommand CLI tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("----------------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
import sys as _sys
_sys.exit(0 if passed_n == len(results) else 1)
