"""SWE-bench Verified evaluation harness — EU-71.

Responsibilities (this module only — no Builder logic):
  1. Fetch a configurable sample (default 20, max 50) from the SWE-bench Verified
     dataset (princeton-nlp/SWE-bench_Verified) via the HuggingFace ``datasets``
     library, falling back to the ``swebench`` package if available.
  2. For each task: clone the target repo at ``base_commit`` into an isolated temp
     directory (mirrors the worktree-lifecycle pattern in orchestrator/git_ops.py).
  3. Accept a per-task patch string from the caller (the Builder supplies it);
     apply it with ``git apply`` inside the cloned repo.
  4. Create a fresh virtualenv, install the repo package, then run pytest against
     the task's FAIL_TO_PASS *and* PASS_TO_PASS test identifiers, capturing
     stdout/stderr.  A task is scored as solved only when every FAIL_TO_PASS test
     passes and every PASS_TO_PASS test still passes (the regression guard) —
     faithful to the official SWE-bench resolution criterion.
  5. Return a list of per-task dicts: ``{task_id, passed, test_output, error}``.

Callable as a library or as a CLI:

    python3 scripts/swebench_eval.py [--sample N] [--out results.json]

CLI mode applies the task's gold patch (``patch`` field) so you can smoke-test the
harness end-to-end without wiring the Builder.  In library mode the caller supplies
patches via ``patches: dict[task_id, patch_str]`` in ``run_tasks()``.

Why not Docker?  The real SWE-bench harness isolates tasks in per-image containers.
We deliberately avoid that dependency here: the goal is a lightweight harness the
unit can run on its own VPS (no Docker daemon required).  Caveat: env reproducibility
is weaker — tests that depend on unusual system packages may fail for reasons unrelated
to the patch.  This is acceptable for a benchmark run that prioritises signal over
perfect isolation.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

_log = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────
DATASET_ID = "princeton-nlp/SWE-bench_Verified"
DEFAULT_SAMPLE = 20
MAX_SAMPLE = 50

# Timeout (seconds) for individual subprocess calls so a runaway test suite
# does not block the whole evaluation indefinitely.
CLONE_TIMEOUT = 300     # git clone of a potentially large repo
INSTALL_TIMEOUT = 300   # pip install -e .
TEST_TIMEOUT = 120      # pytest run per task


# ── data structures ──────────────────────────────────────────────────────────

@dataclass
class SWETask:
    """Minimal projection of one SWE-bench Verified dataset row."""
    instance_id: str        # e.g. "astropy__astropy-12907"
    repo: str               # e.g. "astropy/astropy"
    base_commit: str        # SHA to checkout before applying the patch
    gold_patch: str         # reference patch (used only in CLI/smoke mode)
    fail_to_pass: list[str] = field(default_factory=list)   # pytest node-ids
    pass_to_pass: list[str] = field(default_factory=list)
    problem_statement: str = ""   # GitHub issue text — the builder's primary input


@dataclass
class TaskResult:
    """Per-task harness output — the unit of data returned to the Builder."""
    task_id: str
    passed: bool        # True iff ALL fail_to_pass AND ALL pass_to_pass tests pass (faithful SWE-bench)
    test_output: str    # raw combined stdout+stderr from pytest (FAIL_TO_PASS then PASS_TO_PASS)
    error: str = ""     # non-empty only when the harness itself failed (not the tests)

    def to_dict(self) -> dict:
        """Convert to a plain dict (JSON-serialisable)."""
        return asdict(self)


# ── dataset loading ──────────────────────────────────────────────────────────

def _parse_test_list(raw) -> list[str]:
    """Normalise a test-list field that may arrive as a JSON string, a list, or None."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw]
    # Some versions of the dataset encode this as a JSON string.
    try:
        parsed = json.loads(raw)
        return [str(t) for t in parsed] if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def load_tasks(sample: int = DEFAULT_SAMPLE) -> list[SWETask]:
    """Fetch up to *sample* tasks from SWE-bench Verified.

    Tries the HuggingFace ``datasets`` library first (most portable); falls back
    to the ``swebench`` package if datasets is unavailable.  Raises ImportError
    with a human-readable hint if neither is installed.

    Args:
        sample: Number of tasks to return.  Clamped to [1, MAX_SAMPLE].

    Returns:
        List of SWETask objects (length <= sample).

    Raises:
        ImportError: Neither ``datasets`` nor ``swebench`` is importable.
        RuntimeError: The dataset could not be fetched.
    """
    sample = max(1, min(sample, MAX_SAMPLE))
    return list(_iter_tasks(sample))


def _iter_tasks(n: int) -> Iterator[SWETask]:
    """Internal generator that yields up to *n* SWETask objects."""
    rows = _fetch_rows(n)
    for row in rows:
        instance_id = str(row.get("instance_id", ""))
        if not instance_id:
            _log.warning("skipping row with missing instance_id")
            continue
        yield SWETask(
            instance_id=instance_id,
            repo=str(row.get("repo", "")),
            base_commit=str(row.get("base_commit", "")),
            gold_patch=str(row.get("patch", "")),
            fail_to_pass=_parse_test_list(row.get("FAIL_TO_PASS")),
            pass_to_pass=_parse_test_list(row.get("PASS_TO_PASS")),
            problem_statement=str(row.get("problem_statement", "")),
        )


def _fetch_rows(n: int) -> list[dict]:
    """Attempt to fetch *n* rows via datasets, then swebench, then raise."""
    # ── attempt 1: HuggingFace datasets library ───────────────────────────
    try:
        from datasets import load_dataset  # type: ignore[import]
        _log.info("fetching %d tasks from %s via datasets library", n, DATASET_ID)
        ds = load_dataset(DATASET_ID, split="test", streaming=True)
        rows: list[dict] = []
        for row in ds:
            rows.append(dict(row))
            if len(rows) >= n:
                break
        _log.info("fetched %d task(s)", len(rows))
        return rows
    except ImportError:
        _log.debug("datasets library not available, trying swebench package")
    except Exception as exc:  # noqa: BLE001
        _log.warning("datasets load failed (%s), trying swebench package", exc)

    # ── attempt 2: swebench package ───────────────────────────────────────
    try:
        from swebench.harness.utils import load_swebench_dataset  # type: ignore[import]
        _log.info("fetching tasks via swebench package (dataset=%s)", DATASET_ID)
        all_tasks = load_swebench_dataset(DATASET_ID, split="test")
        rows = [dict(t) for t in all_tasks[:n]]
        _log.info("fetched %d task(s) via swebench", len(rows))
        return rows
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        _log.warning("swebench package load failed: %s", exc)

    raise ImportError(
        "Neither 'datasets' nor 'swebench' is installed. "
        "Install one with: pip install datasets   (or: pip install swebench)"
    )


# ── repo lifecycle ───────────────────────────────────────────────────────────

class WorktreeError(RuntimeError):
    """Raised when the harness cannot set up or tear down a task's worktree."""


def _git(cwd: Path, *args: str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command inside *cwd*, capturing output.

    Args:
        cwd:     Directory in which to run git.
        *args:   git sub-command and its arguments.
        timeout: Maximum wall-clock seconds before the call is killed.
        check:   If True, raise WorktreeError on non-zero exit.

    Returns:
        CompletedProcess with stdout/stderr as strings.

    Raises:
        WorktreeError: When *check* is True and git exits non-zero.
    """
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)!r} in {cwd} failed (rc={proc.returncode}):\n"
            f"{proc.stderr.strip()}"
        )
    return proc


def _clone_repo(repo: str, commit: str, dest: Path) -> None:
    """Clone *repo* (GitHub shorthand, e.g. 'astropy/astropy') at *commit* into *dest*.

    We use a shallow clone when possible to save bandwidth, then fetch and
    checkout the exact commit so even older commits are reachable.

    Args:
        repo:   GitHub repository in ``owner/name`` format.
        commit: The exact commit SHA the task requires.
        dest:   Empty (or non-existent) directory that will become the clone root.

    Raises:
        WorktreeError: On any git failure.
    """
    url = f"https://github.com/{repo}.git"
    _log.info("cloning %s → %s (commit=%s)", url, dest, commit[:8])

    # Shallow clone of the default branch first (fast).
    _git(dest.parent, "clone", "--depth=1", "--no-single-branch", url, str(dest),
         timeout=CLONE_TIMEOUT, check=True)

    # Ensure the target commit is available (it may not be in the shallow pack).
    _git(dest, "fetch", "--depth=1", "origin", commit,
         timeout=CLONE_TIMEOUT, check=False)   # best-effort; commit might already be present

    # Detach HEAD at the exact base commit.
    _git(dest, "checkout", "--detach", commit, timeout=60, check=True)


def _apply_patch(repo_dir: Path, patch: str) -> None:
    """Apply *patch* (unified diff string) to the working tree inside *repo_dir*.

    Uses ``git apply`` which handles multi-file patches robustly and honours
    the repo's .gitattributes / CRLF settings.

    Args:
        repo_dir: Root of the cloned repository.
        patch:    Unified diff string (the Builder's output).

    Raises:
        WorktreeError: If the patch cannot be applied cleanly.
    """
    if not patch or not patch.strip():
        raise WorktreeError("patch is empty — nothing to apply")

    proc = subprocess.run(
        ["git", "apply", "--whitespace=fix", "-"],
        input=patch, cwd=repo_dir, capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        raise WorktreeError(
            f"git apply failed (rc={proc.returncode}):\n{proc.stderr.strip()}"
        )
    _log.debug("patch applied cleanly in %s", repo_dir)


def _create_venv(venv_dir: Path, repo_dir: Path) -> Path:
    """Create a fresh venv at *venv_dir* and install the repo package into it.

    The repo is installed in editable mode (``pip install -e .``) so that the
    patched source files are immediately importable without a re-install step.

    Args:
        venv_dir: Where to create the venv (inside the temp work area).
        repo_dir: Root of the cloned (and patched) repository.

    Returns:
        Path to the python interpreter inside the venv.

    Raises:
        WorktreeError: If venv creation or package installation fails.
    """
    _log.info("creating venv at %s", venv_dir)
    proc = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        raise WorktreeError(f"venv creation failed:\n{proc.stderr.strip()}")

    python = venv_dir / "bin" / "python"
    if not python.exists():
        python = venv_dir / "Scripts" / "python.exe"  # Windows fallback
    if not python.exists():
        raise WorktreeError(f"cannot find python interpreter in venv at {venv_dir}")

    # Upgrade pip silently first (avoids noisy warnings on older system Pythons).
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        capture_output=True, timeout=INSTALL_TIMEOUT
    )

    # Install pytest so we can always run tests, even in repos that don't declare it.
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "pytest"],
        capture_output=True, timeout=INSTALL_TIMEOUT
    )

    # Install the repo itself in editable mode.  Many repos also have a
    # [test] or [dev] extra — try with it first, fall back without.
    for extra in ([".[test]"], [".[dev]"], [".[testing]"], ["."]):
        _log.debug("pip install -e %s", extra[0])
        res = subprocess.run(
            [str(python), "-m", "pip", "install", "--quiet", "-e", *extra],
            cwd=repo_dir, capture_output=True, text=True, timeout=INSTALL_TIMEOUT
        )
        if res.returncode == 0:
            _log.debug("installed repo with extra %r", extra[0])
            break
        if extra[-1] == ".":
            # Last attempt failed — surface the error.
            raise WorktreeError(
                f"pip install -e . failed:\n{res.stderr.strip()}"
            )

    return python


def _run_tests(python: Path, repo_dir: Path, test_ids: list[str]) -> tuple[bool, str]:
    """Run the given pytest *test_ids* inside *repo_dir* using *python*.

    Args:
        python:   Path to the venv's python interpreter.
        repo_dir: Repository root (pytest's rootdir).
        test_ids: Pytest node-ids from FAIL_TO_PASS (e.g. ``tests/test_foo.py::test_bar``).

    Returns:
        ``(passed, output)`` where *passed* is True iff all tests pass (rc==0)
        and *output* is the combined stdout+stderr from the pytest run.

    Note:
        If *test_ids* is empty we run the full test suite (``pytest``).  This is
        deliberately permissive so the harness still produces signal for tasks
        that have no explicit test list in the dataset row.
    """
    cmd: list[str] = [str(python), "-m", "pytest", "-x", "--tb=short", "-q"]
    if test_ids:
        cmd.extend(test_ids)
    else:
        _log.warning("no FAIL_TO_PASS test ids — running full test suite (slow/noisy)")

    _log.info("running: %s", " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    proc = subprocess.run(
        cmd, cwd=repo_dir, capture_output=True, text=True, timeout=TEST_TIMEOUT
    )
    output = (proc.stdout + proc.stderr).strip()
    passed = proc.returncode == 0
    _log.info("pytest rc=%d for %d test(s)", proc.returncode, len(test_ids))
    return passed, output


# ── per-task orchestration ───────────────────────────────────────────────────

def run_single_task(task: SWETask, patch: str, *, work_root: Path | None = None) -> TaskResult:
    """Run the full task lifecycle for one SWE-bench task.

    Steps:
      1. Create an isolated temp directory (or a subdir of *work_root*).
      2. Clone the target repo at ``task.base_commit``.
      3. Apply *patch* with ``git apply``.
      4. Build a fresh venv and install the repo.
      5. Run pytest on ``task.fail_to_pass`` AND ``task.pass_to_pass``; the task
         is solved only when BOTH sets pass (faithful SWE-bench scoring — the
         pass_to_pass set is the regression guard).
      6. Return a TaskResult and clean up (unless *work_root* was supplied —
         the caller then owns cleanup, useful for debugging).

    Args:
        task:      The SWE-bench task descriptor.
        patch:     Unified diff string from the Builder.
        work_root: If given, the temp directory is created INSIDE this path
                   (and is NOT deleted on exit).  Useful for post-run inspection.

    Returns:
        TaskResult with task_id, passed, test_output, and error (if any).
    """
    managed = work_root is None
    tmp_parent = work_root if work_root else Path(tempfile.mkdtemp(prefix="swebench_"))
    work_dir = tmp_parent / task.instance_id if work_root else tmp_parent
    work_dir.mkdir(parents=True, exist_ok=True)

    repo_dir = work_dir / "repo"
    venv_dir = work_dir / "venv"

    try:
        if not task.repo or not task.base_commit:
            raise WorktreeError(
                f"task {task.instance_id!r} is missing repo ({task.repo!r}) "
                f"or base_commit ({task.base_commit!r})"
            )

        _clone_repo(task.repo, task.base_commit, repo_dir)
        _apply_patch(repo_dir, patch)
        python = _create_venv(venv_dir, repo_dir)

        # ── faithful SWE-bench scoring ────────────────────────────────────────
        # A task is "solved" iff ALL FAIL_TO_PASS tests pass after the patch AND
        # ALL PASS_TO_PASS tests STILL pass (no regressions).  We run them as two
        # pytest invocations and AND the results — scoring only on FAIL_TO_PASS
        # would let a patch that breaks unrelated tests count as a solve.
        f2p_passed, test_output = _run_tests(python, repo_dir, task.fail_to_pass)
        passed = f2p_passed

        if task.pass_to_pass:
            if f2p_passed:
                # Only spend the (potentially long) regression run when the fix
                # already passed its target tests.  When FAIL_TO_PASS failed the
                # task is unsolved regardless, so skipping the work never changes
                # the verdict — `passed` is already False.
                p2p_passed, p2p_output = _run_tests(python, repo_dir, task.pass_to_pass)
                passed = f2p_passed and p2p_passed
                test_output = (
                    f"{test_output}\n\n"
                    f"--- PASS_TO_PASS ({len(task.pass_to_pass)} test(s)) ---\n"
                    f"{p2p_output}"
                )
            else:
                test_output = (
                    f"{test_output}\n\n"
                    "--- PASS_TO_PASS skipped (FAIL_TO_PASS already failing) ---"
                )

        return TaskResult(
            task_id=task.instance_id,
            passed=passed,
            test_output=test_output,
        )

    except WorktreeError as exc:
        _log.error("harness error for %s: %s", task.instance_id, exc)
        return TaskResult(
            task_id=task.instance_id,
            passed=False,
            test_output="",
            error=str(exc),
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"timeout: {exc}"
        _log.error("timeout for %s: %s", task.instance_id, msg)
        return TaskResult(
            task_id=task.instance_id,
            passed=False,
            test_output="",
            error=msg,
        )
    finally:
        # Only remove the temp tree when we own it (managed=True) and no
        # caller-supplied work_root is expected to survive.
        if managed and work_dir.exists():
            _log.debug("cleaning up %s", work_dir)
            shutil.rmtree(work_dir, ignore_errors=True)


def run_tasks(
    patches: dict[str, str],
    *,
    sample: int = DEFAULT_SAMPLE,
    tasks: list[SWETask] | None = None,
    work_root: Path | None = None,
) -> list[dict]:
    """Run the evaluation harness over a set of SWE-bench tasks.

    This is the main entry-point for the Builder to call.  It loads tasks from
    the dataset (or uses the supplied *tasks* list), looks up each task's patch
    from *patches*, runs the lifecycle, and returns the results.

    Args:
        patches:   Mapping of ``instance_id → unified-diff patch string``.
                   Tasks whose id is absent from this dict are skipped with an
                   error result (so the Builder can detect coverage gaps).
        sample:    How many tasks to draw from the dataset (ignored when *tasks*
                   is supplied explicitly).  Clamped to [1, MAX_SAMPLE].
        tasks:     Pre-loaded list of SWETask objects.  Pass this to avoid a
                   second network round-trip when the caller already fetched tasks.
        work_root: Optional persistent work directory.  When omitted each task
                   gets its own temp dir that is deleted after the run.

    Returns:
        List of per-task result dicts, each with keys:
          ``task_id`` (str), ``passed`` (bool), ``test_output`` (str), ``error`` (str).
    """
    if tasks is None:
        tasks = load_tasks(sample)

    results: list[dict] = []
    for task in tasks:
        patch = patches.get(task.instance_id)
        if patch is None:
            _log.warning("no patch supplied for %s — skipping", task.instance_id)
            results.append(TaskResult(
                task_id=task.instance_id,
                passed=False,
                test_output="",
                error="no patch supplied by the Builder",
            ).to_dict())
            continue

        _log.info("=== running task %s ===", task.instance_id)
        result = run_single_task(task, patch, work_root=work_root)
        results.append(result.to_dict())
        _log.info("task %s → passed=%s", task.instance_id, result.passed)

    return results


# ── CLI (smoke-test with gold patches) ───────────────────────────────────────

def _build_gold_patches(tasks: list[SWETask]) -> dict[str, str]:
    """Build a patches dict from the gold patch in each task (CLI/smoke mode only).

    The gold patch is the reference solution from the dataset — using it lets us
    verify the harness infrastructure works end-to-end without involving the Builder.
    """
    return {t.instance_id: t.gold_patch for t in tasks if t.gold_patch}


def main(argv: list[str] | None = None) -> int:
    """CLI entry-point.  Runs the harness with the dataset's own gold patches."""
    parser = argparse.ArgumentParser(
        description="SWE-bench Verified evaluation harness (smoke-test with gold patches)"
    )
    parser.add_argument(
        "--sample", type=int, default=DEFAULT_SAMPLE,
        help=f"number of tasks to evaluate (1-{MAX_SAMPLE}, default {DEFAULT_SAMPLE})"
    )
    parser.add_argument(
        "--out", default=None,
        help="write JSON results to this file (default: print to stdout)"
    )
    parser.add_argument(
        "--work-dir", default=None,
        help="persistent work directory for clones/venvs (default: a temp dir per task)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    sample = max(1, min(args.sample, MAX_SAMPLE))
    _log.info("loading %d task(s) from %s", sample, DATASET_ID)
    try:
        tasks = load_tasks(sample)
    except ImportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR loading dataset: {exc}", file=sys.stderr)
        return 2

    if not tasks:
        print("No tasks fetched — check network access or dataset name.", file=sys.stderr)
        return 2

    work_root = Path(args.work_dir) if args.work_dir else None
    if work_root:
        work_root.mkdir(parents=True, exist_ok=True)

    patches = _build_gold_patches(tasks)
    _log.info("evaluating %d task(s) with gold patches", len(tasks))
    results = run_tasks(patches, tasks=tasks, work_root=work_root)

    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    summary = {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "results": results,
    }

    output = json.dumps(summary, indent=2)
    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        print(f"Results written to {args.out}")
        print(f"Pass rate: {passed}/{total} ({summary['pass_rate'] * 100:.1f}%)")
    else:
        print(output)

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
