"""SWE-bench builder integration — EU-71.

Wires orchestrator/agent.run_agent (the same choke-point used by builder.py)
into the swebench_eval harness from subtask 1.  For each sampled task:

  (a) Builds a BuildRequest + Ticket from the SWE-bench issue text + repo context.
  (b) Calls run_agent with an ACI-style system prompt (view_file / edit_file /
      run_tests) so the agent follows the OpenHands/SWE-agent interaction pattern.
  (c) Extracts the resulting patch via ``git diff HEAD``.
  (d) Passes patches to swebench_eval.run_tasks() for scoring (the "gate").

Hard budget guard: aborts the run (raises BudgetExceededError) if cumulative
cost across all tasks reaches or exceeds a configurable ceiling (default $5.00).
The ceiling is checked BEFORE each task so we never overspend on the last task.

Sample size is capped at MAX_SAMPLE (50) regardless of CLI --sample input, in
line with the 'never full 500-task run' constraint.

Callable as a library (run_benchmark()) or as a CLI:

    python3 scripts/swebench_builder.py [--sample N] [--cost-ceiling F] [--out FILE]

CLI exit codes: 0 = all tasks ran, 1 = aborted by budget, 2 = missing deps, 3 = other.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

# ── make the orchestrator importable from scripts/ ────────────────────────────
# When this module is run as ``python3 scripts/swebench_builder.py`` the repo
# root is NOT on sys.path.  Insert it so ``from orchestrator.agent import …``
# resolves correctly regardless of the working directory.
sys.path.insert(0, str(Path(__file__).parent.parent))
# Also add scripts/ itself so ``import swebench_eval`` resolves.
sys.path.insert(0, str(Path(__file__).parent))

_log = logging.getLogger(__name__)

# ── orchestrator imports ──────────────────────────────────────────────────────
# Imported at module level so tests can patch swebench_builder.run_agent.
# The sys.path insertion above makes `orchestrator` findable regardless of cwd.
from orchestrator.agent import run_agent  # noqa: E402  (after sys.path setup)
from orchestrator.contracts import BuildRequest, Ticket  # noqa: E402
from swebench_report import report as _report, weekly_seed  # noqa: E402

# ── constants ─────────────────────────────────────────────────────────────────
MAX_SAMPLE = 50           # hard cap — never run the full 500-task dataset
DEFAULT_SAMPLE = 20       # default when --sample is omitted
DEFAULT_COST_CEILING = 5.0   # USD — abort if cumulative benchmark cost hits this

# ── ACI-style system prompt ───────────────────────────────────────────────────
# Modelled on the OpenHands / SWE-agent agent-computer interface (ACI) pattern.
# The agent is given explicit tool-call guidance and a required workflow so it
# explores → localises → patches → verifies rather than guessing and overwriting.
SWE_ACI_SYSTEM = """\
You are a software engineering agent solving a GitHub issue.  You are working
inside a cloned repository; your current working directory is the repo root.

WORKFLOW — follow these steps in order:
1. READ the issue / problem statement carefully to understand the bug or feature.
2. EXPLORE: use Grep and Glob to locate relevant files; avoid reading the whole repo.
3. VIEW (view_file): read relevant source files with Read to understand the code.
4. PLAN the minimal change — the smallest diff that fixes the reported behaviour.
5. EDIT (edit_file): apply the fix with Edit or Write.  Do NOT change unrelated code.
6. TEST (run_tests): verify with Bash:
       python -m pytest <test_ids> -x --tb=short -q
   If tests fail, revisit and fix before finishing.
7. Finish with a one-sentence summary: what you changed and why.

RULES:
- Fix only what the issue describes — no extra refactoring or scope creep.
- Keep the patch minimal; the reviewer checks every line.
- Do NOT run git (no commit / push / add / branch switch).
- Do NOT start a dev server or watch mode.
"""


# ── exceptions ────────────────────────────────────────────────────────────────

class BudgetExceededError(RuntimeError):
    """Raised when the cumulative benchmark cost reaches the configured ceiling.

    This is a hard abort — the caller should save partial results and stop.

    Attributes:
        cost_usd:    Cumulative cost (USD) at the point of the abort.
        ceiling_usd: The ceiling that was reached or exceeded.
    """

    def __init__(self, cost_usd: float, ceiling_usd: float) -> None:
        self.cost_usd = cost_usd
        self.ceiling_usd = ceiling_usd
        super().__init__(
            f"benchmark aborted: cumulative cost ${cost_usd:.4f} has reached "
            f"the ${ceiling_usd:.2f} ceiling"
        )


# ── prompt construction ────────────────────────────────────────────────────────

def _build_prompt(task) -> str:
    """Build the per-task agent prompt from a SWETask.

    The prompt contains:
    - Repository and commit context (so the agent knows where it is).
    - The GitHub issue / problem statement (the primary description of the bug).
    - The FAIL_TO_PASS test identifiers (the acceptance gate the fix must satisfy).

    Args:
        task: A SWETask from swebench_eval.

    Returns:
        A string prompt suitable for run_agent().
    """
    problem = getattr(task, "problem_statement", "") or "(no problem statement available)"
    test_lines = "\n".join(f"  - {t}" for t in (task.fail_to_pass or []))
    test_block = test_lines or "  (no FAIL_TO_PASS tests specified)"
    return (
        f"Repository: {task.repo}  (base commit: {task.base_commit})\n"
        "\n"
        "PROBLEM STATEMENT (GitHub issue):\n"
        f"{problem}\n"
        "\n"
        "TESTS THAT MUST PASS AFTER YOUR FIX (FAIL_TO_PASS):\n"
        f"{test_block}\n"
        "\n"
        "Implement the fix now.  Follow the workflow in your system prompt."
    )


# ── patch extraction ───────────────────────────────────────────────────────────

def _extract_patch(repo_dir: Path) -> str:
    """Extract the agent's changes from *repo_dir* as a unified diff string.

    We FIRST stage every change with ``git add -A`` and THEN read the staged diff
    with ``git diff --cached``.  This is the critical step for correctness: a
    plain ``git diff HEAD`` does NOT include *untracked* files, so any brand-new
    module or test the builder created would be silently dropped from the scored
    patch (SWE-bench solutions routinely add new files).  Staging first pulls
    those untracked files into the diff alongside ordinary edits and deletions.

    Falls back to ``git diff HEAD`` if the staged diff comes back empty (e.g. an
    environment where ``git add`` was a no-op) so previously-working behaviour is
    preserved.

    Args:
        repo_dir: Root of the cloned (and agent-modified) repository.

    Returns:
        Unified diff string, or empty string when no changes were made.
    """
    def _run(args: list[str]) -> str:
        try:
            proc = subprocess.run(
                ["git"] + args, cwd=repo_dir,
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                _log.debug("git %s failed (rc=%d): %s",
                           " ".join(args), proc.returncode, proc.stderr.strip()[:120])
                return ""
            # Preserve the patch verbatim; treat pure-whitespace responses as empty
            # so callers can reliably use ``if patch:`` to detect no-change runs.
            return "" if not proc.stdout.strip() else proc.stdout
        except (subprocess.TimeoutExpired, OSError) as exc:
            _log.warning("could not run git %s: %s", " ".join(args), exc)
            return ""

    # Stage everything — tracked edits, deletions, AND new/untracked files — so
    # the diff is complete.  Best-effort: a staging failure must not crash the run.
    _run(["add", "-A"])

    # Primary: the staged diff (now includes new files).  Fallback: diff HEAD.
    patch = _run(["diff", "--cached"])
    if not patch:
        patch = _run(["diff", "HEAD"])
    return patch


# ── per-task builder ──────────────────────────────────────────────────────────

async def build_patch_for_task(
    task,
    repo_dir: Path,
    cost_ceiling_usd: float = DEFAULT_COST_CEILING,
    cumulative_cost: float = 0.0,
) -> tuple[str, float]:
    """Run the ACI-style builder on a pre-cloned repo and return the resulting patch.

    Integration point: wires orchestrator/agent.run_agent into the SWE-bench
    harness.  We:
      1. Construct a Ticket + BuildRequest from the SWETask (the contracts shared
         across the pipeline, same as a normal Jira-backed run).
      2. Build ClaudeAgentOptions with SWE_ACI_SYSTEM and the cloned repo as cwd.
      3. Call run_agent() — the same choke-point used by builder._solo_build() —
         so token usage is recorded in the usage ledger automatically.
      4. Extract the patch with git diff.

    Args:
        task:             SWETask from swebench_eval.
        repo_dir:         Path to the pre-cloned repository (base commit detached).
        cost_ceiling_usd: Per-benchmark dollar ceiling.
        cumulative_cost:  Cost already spent across previous tasks (USD).

    Returns:
        (patch_str, task_cost_usd) — patch is "" when the agent made no changes.

    Raises:
        BudgetExceededError: If cumulative_cost has already reached the ceiling
                             BEFORE this task runs.  Never raised mid-task; the
                             guard is at entry so we always get a complete result.
        ImportError:         If claude_agent_sdk is not installed.
    """
    # ── pre-task budget guard ─────────────────────────────────────────────────
    # Check BEFORE spending so we never overshoot.  The ceiling is >= (not >)
    # so a run that hits exactly the ceiling aborts cleanly on the next task.
    if cumulative_cost >= cost_ceiling_usd:
        raise BudgetExceededError(cumulative_cost, cost_ceiling_usd)

    from claude_agent_sdk import ClaudeAgentOptions

    # Construct a synthetic Ticket from the SWE-bench task so the BuildRequest
    # contract is honoured and the ledger can attribute tokens to this instance.
    # ephemeral=True suppresses any Jira/backlog status writes.
    ticket = Ticket(
        id=task.instance_id,
        key=task.instance_id,
        summary=f"SWE-bench fix: {task.instance_id}",
        description=_build_prompt(task),
        acceptance_criteria=list(task.fail_to_pass or []),
        ephemeral=True,
    )
    req = BuildRequest(ticket=ticket, branch="swebench-eval", iteration=1)

    options = ClaudeAgentOptions(
        system_prompt=SWE_ACI_SYSTEM,
        cwd=str(repo_dir),
        permission_mode="bypassPermissions",
        # ACI tools: Read=view_file, Edit/Write=edit_file, Bash=run_tests/shell.
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        # Deny the Task/Agent sub-agent tool (allowed_tools does not gate it under bypassPermissions):
        # this benchmark builder is the same profile as builder.py with the highest turn budget (60),
        # so an un-denied sub-agent could fan out beyond max_turns (AUTO-93 class fix).
        disallowed_tools=["Task", "Agent"],
        setting_sources=[],   # no .claude/ settings in the target repo
        max_turns=60,
        effort="high",
    )

    _log.info("running builder on %s (cwd=%s)", task.instance_id, repo_dir)
    run = await run_agent(
        req.ticket.description,
        options,
        tag="swebench-builder",
        ticket_id=req.ticket.id,
        pass_number=req.iteration,
    )

    if run.is_error:
        _log.warning("builder reported an error for %s after %d turn(s)",
                     task.instance_id, run.num_turns)

    patch = _extract_patch(repo_dir)
    _log.debug("extracted patch: %d chars for %s", len(patch), task.instance_id)
    return patch, run.cost_usd


# ── benchmark orchestration ───────────────────────────────────────────────────

async def run_benchmark(
    sample: int = DEFAULT_SAMPLE,
    cost_ceiling_usd: float = DEFAULT_COST_CEILING,
    out_file: Optional[str] = None,
    verbose: bool = False,
    random_seed: Optional[str] = None,
    jsonl_path: Optional[Path] = None,
) -> dict:
    """Run the full SWE-bench builder benchmark and return the summary.

    For each task the lifecycle is:
      clone repo → run builder (ACI loop) → extract patch → score with pytest

    The run is aborted (with partial results) if cumulative cost reaches
    *cost_ceiling_usd*.  Sample size is clamped to [1, MAX_SAMPLE] regardless
    of the argument so we never accidentally run all 500 tasks.

    Args:
        sample:           Tasks to evaluate (clamped to [1, MAX_SAMPLE]).
        cost_ceiling_usd: Hard cost ceiling in USD; aborts when reached.
        out_file:         Optional path to write the JSON summary.
        verbose:          Enable DEBUG logging.
        random_seed:      When set, load up to MAX_SAMPLE tasks then draw
                          *sample* of them with ``random.Random(random_seed)``.
                          This makes the weekly run deterministic: same seed →
                          same task subset every time.  When None, the first
                          *sample* tasks from the dataset are used (original
                          behaviour).
        jsonl_path:       Override for the JSONL audit-log path passed straight
                          through to ``swebench_report.report()``.  Production
                          callers leave this None (the default
                          ``audit/swebench_runs.jsonl`` is used); tests inject a
                          temp path so a benchmark run never pollutes the real
                          audit trail / trend.

    Returns:
        Summary dict:
          { total, passed, failed, pass_rate, cost_usd, aborted, results }
        ``aborted`` is True when the budget ceiling cut the run short.
    """
    import swebench_eval as ev

    sample = max(1, min(sample, MAX_SAMPLE))
    _log.info("loading %d task(s) from SWE-bench Verified (ceiling=$%.2f)",
              sample, cost_ceiling_usd)

    try:
        if random_seed is not None:
            # Load the full capped pool so the random draw has variety, then
            # subsample deterministically.  The pool is always MAX_SAMPLE tasks
            # (the hard cap) to bound download time while still giving the RNG
            # something to choose from.
            pool = ev.load_tasks(MAX_SAMPLE)
            rng = random.Random(random_seed)
            tasks = rng.sample(pool, min(sample, len(pool)))
            _log.info(
                "seeded sample: drew %d from %d-task pool (seed=%r)",
                len(tasks), len(pool), random_seed,
            )
        else:
            tasks = ev.load_tasks(sample)
    except ImportError as exc:
        _log.error("cannot load tasks: %s", exc)
        raise

    if not tasks:
        _log.error("no tasks loaded")
        return {"total": 0, "passed": 0, "failed": 0, "pass_rate": 0.0,
                "cost_usd": 0.0, "aborted": False, "results": []}

    patches: dict[str, str] = {}
    cumulative_cost = 0.0
    aborted = False

    for idx, task in enumerate(tasks, 1):
        # ── pre-task budget guard ─────────────────────────────────────────────
        if cumulative_cost >= cost_ceiling_usd:
            _log.error(
                "budget ceiling $%.2f reached ($%.4f spent) — aborting after %d/%d tasks",
                cost_ceiling_usd, cumulative_cost, idx - 1, len(tasks),
            )
            aborted = True
            break

        _log.info("[%d/%d] processing %s", idx, len(tasks), task.instance_id)
        print(f"  [{idx}/{len(tasks)}] {task.instance_id} …", flush=True)

        # ── clone repo into a temp dir ────────────────────────────────────────
        # We use TemporaryDirectory so the clone is always cleaned up even when
        # the builder or extraction raises an unexpected exception.
        with tempfile.TemporaryDirectory(prefix=f"swebench_{task.instance_id}_") as tmp:
            repo_dir = Path(tmp) / "repo"
            repo_dir.mkdir()
            try:
                ev._clone_repo(task.repo, task.base_commit, repo_dir)
            except ev.WorktreeError as exc:
                _log.error("clone failed for %s: %s", task.instance_id, exc)
                patches[task.instance_id] = ""
                continue

            # ── run builder ───────────────────────────────────────────────────
            try:
                patch, task_cost = await build_patch_for_task(
                    task,
                    repo_dir,
                    cost_ceiling_usd=cost_ceiling_usd,
                    cumulative_cost=cumulative_cost,
                )
            except BudgetExceededError as exc:
                # Budget hit between the outer guard and the inner guard — save
                # partial progress and stop cleanly.
                _log.error("budget guard triggered inside task: %s", exc)
                aborted = True
                break
            except ImportError:
                raise   # propagate missing-SDK errors to the CLI
            except Exception as exc:  # noqa: BLE001
                _log.error("builder failed for %s: %s", task.instance_id, exc)
                patches[task.instance_id] = ""
                continue

            cumulative_cost += task_cost
            patches[task.instance_id] = patch
            _log.info(
                "task %s done: patch=%d chars, cost=$%.4f (cumulative=$%.4f)",
                task.instance_id, len(patch), task_cost, cumulative_cost,
            )
            print(
                f"    → patch={len(patch):,} chars  cost=${task_cost:.4f}  "
                f"total=${cumulative_cost:.4f}",
                flush=True,
            )

    # ── score the patches (the harness gate) ─────────────────────────────────
    _log.info("scoring %d collected patch(es) against the test suite", len(patches))
    results = ev.run_tasks(patches, tasks=tasks)

    passed = sum(1 for r in results if r.get("passed"))
    total = len(results)
    summary = {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "cost_usd": round(cumulative_cost, 4),
        "aborted": aborted,
        "results": results,
    }

    if out_file:
        Path(out_file).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nResults written to {out_file}")

    # ── human-readable summary + JSONL audit record ───────────────────────────
    # _report() prints the pass/fail table to stdout and appends one line to the
    # JSONL audit log (default audit/swebench_runs.jsonl) so the drill/council can
    # cite trending numbers.  jsonl_path lets tests redirect that write to a temp
    # file so a run never pollutes the real trend.
    print()  # blank line before the summary block
    _report(summary, jsonl_path=jsonl_path)

    return summary


# ── CLI entry-point ───────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """CLI entry-point for the SWE-bench builder benchmark.

    Exit codes:
      0 — all tasks completed normally.
      1 — run aborted because the cost ceiling was reached.
      2 — missing dependency (datasets / swebench / claude_agent_sdk).
      3 — unexpected error.
    """
    parser = argparse.ArgumentParser(
        description=(
            "SWE-bench Verified builder benchmark (EU-71). "
            "Runs the orchestrator builder on each task and scores the patches."
        )
    )
    parser.add_argument(
        "--sample", type=int, default=DEFAULT_SAMPLE,
        help=f"tasks to evaluate (1–{MAX_SAMPLE}, default {DEFAULT_SAMPLE})",
    )
    parser.add_argument(
        "--cost-ceiling", type=float, default=DEFAULT_COST_CEILING,
        help=f"abort if cumulative cost reaches this USD amount (default {DEFAULT_COST_CEILING})",
    )
    parser.add_argument(
        "--out", default=None,
        help="write JSON results to this file (default: stdout)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "--weekly", action="store_true",
        help=(
            "use a deterministic random seed derived from the current ISO week "
            "(YYYY-W##) so the weekly sample is always reproducible.  "
            "Implies loading up to MAX_SAMPLE tasks from the dataset and "
            "subsampling --sample of them with the weekly seed."
        ),
    )
    parser.add_argument(
        "--audit-jsonl", default=None,
        help=(
            "override the JSONL audit-log path (default: audit/swebench_runs.jsonl). "
            "Use a throwaway path to run the benchmark without touching the trend."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Enforce the sample cap at the CLI layer too — belt-and-suspenders.
    sample = max(1, min(args.sample, MAX_SAMPLE))
    if args.sample > MAX_SAMPLE:
        _log.warning("--sample %d clamped to MAX_SAMPLE=%d", args.sample, MAX_SAMPLE)

    # Resolve the random seed: --weekly derives it from the ISO year+week so
    # the same week always draws the same task subset.
    seed: Optional[str] = weekly_seed() if args.weekly else None
    if seed:
        _log.info("weekly mode: using deterministic seed %r", seed)

    try:
        summary = asyncio.run(run_benchmark(
            sample=sample,
            cost_ceiling_usd=args.cost_ceiling,
            out_file=args.out,
            verbose=args.verbose,
            random_seed=seed,
            jsonl_path=Path(args.audit_jsonl) if args.audit_jsonl else None,
        ))
        return 1 if summary.get("aborted") else 0
    except ImportError as exc:
        print(f"ERROR (missing dependency): {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        _log.exception("unexpected error in benchmark run")
        return 3


if __name__ == "__main__":
    sys.exit(main())
