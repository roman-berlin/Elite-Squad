"""Test Engineer driver — the coverage gate between the Builder and the Reviewer.

The Builder implements the ticket; the Test Engineer then ensures the change is actually
PROVEN: a happy-path test that exercises the intended behaviour, plus a regression test that
pins the exact bug/edge case. It runs the REPO'S OWN test+coverage command — the one that repo's
CLAUDE.md declares (e.g. `python3 tests/run_all.py` for the Python EU repo, `bun test --coverage`
for a Bun/TS repo like automatixy) — on the touched scope and emits the coverage artifact that
lands in the PR description, making the gate real, not a checkbox. It runs AFTER the Builder and
BEFORE the Reviewer.

It operates with full tools inside the SAME isolated worktree the Builder used, so it can add or
adjust ONLY the tests for the change under review (never product code). Its doctrine is loaded
from officers/test-engineer.md (slice 1's prompt file); a compact fallback keeps the gate
functional if that file is ever absent.
"""
from __future__ import annotations

import re
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from . import memory, models
from .agent import run_agent
from .config import AppConfig, Config, normalize_effort
from .contracts import TestEngineerResult, Ticket

_OFFICER_FILE = "test-engineer.md"

# Fallback doctrine if officers/test-engineer.md is missing (slice 1 not yet landed). Keeps the
# gate functional instead of dead-stopping the pipeline.
_FALLBACK_DOCTRINE = (
    "You are the Test Engineer: you own the unit's test suite and its coverage gate. For every "
    "change that lands you ensure a happy-path test (real assertions on observable output) and a "
    "regression test (fails on the old code, passes on the new) exist. You guard against tests that "
    "pass for the wrong reason (no assertions, mocked-away logic, snapshot churn). The coverage "
    "delta you report must come from a real run, not an estimate."
)

# The driver instructions wrapped around the officer doctrine: how the Test Engineer behaves as a
# pipeline stage (cwd, scope, resource limits, and the exact artifact line we parse out).
_DRIVER = """\
You are running as the Test Engineer stage of an automated dev pipeline. The Builder has just
implemented this ticket on the current git branch (your cwd is the repo root — an isolated
worktree). You run AFTER the build and BEFORE the read-only Reviewer.

HOUSE RULES: read THIS repo's conventions first and follow them strictly — its CLAUDE.md at the
repo root and the relevant files under .claude/rules/ (its test runner, language, isolation /
zero-trust and style invariants). They override generic habits and they DECIDE which test tool you
use — do not assume a stack (the Python EU orchestrator repo and a Bun/TS product repo are both in
scope, with different runners).

Do this, surgically:
1. LOCATE the change: inspect what the Builder touched and the tests near it. Map each acceptance
   criterion and each changed behaviour to the cases that must be covered.
2. ADD the happy-path test — real assertions on observable output, never an assertion-free test.
3. ADD the regression test for any bug fix — it must fail on the old behaviour and pass on the new,
   pinning the exact defect. Cover the edges (empty/null, boundaries, error paths, and for any data
   access a cross-tenant case proving isolation holds).
4. STAY IN SCOPE: add or adjust ONLY the tests for THIS change. Do NOT modify product code or other
   officers' work, and do NOT refactor unrelated tests.
5. MEASURE for real, with THIS repo's OWN test command — the one its CLAUDE.md declares (e.g.
   `python3 tests/run_all.py` for the Python EU repo, `bun test --coverage` for a Bun/TS repo like
   automatixy). Scope it to the touched tests; for runners that spawn one worker per core
   (vitest/jest) bound the workers. Never start watch mode or a dev server (the box has limited RAM).

Git: the orchestrator owns git and has ALREADY placed you on the correct branch. Do NOT run git —
no commit, push, branch switch, or history edits. Spend your turns on the tests.

Finish with a short plain-text summary of the tests you added, then end with ONE line EXACTLY in
this form (the coverage artifact for the PR description) — in WHATEVER shape this repo's tooling
produces:
  COVERAGE: <before→after coverage numbers if the runner reports them (e.g. lines/functions/
  branches); else the test pass/fail counts plus 'n/a (<runner> reports no line coverage)'>
"""

# Pull the COVERAGE: artifact line out of the final message. Tolerant of bold/spacing; the body is
# whatever the officer reported (numbers, or an honest 'n/a (...)').
_COVERAGE_RE = re.compile(r"^\s*\**\s*COVERAGE\s*:\s*(.+?)\s*\**\s*$", re.IGNORECASE | re.MULTILINE)


def _general_root() -> Path:
    """The General's repo root (where officers/ live) — independent of the cwd, so the doctrine
    always grounds on the right file."""
    return Path(__file__).resolve().parent.parent


def doctrine() -> str:
    """The Test Engineer's doctrine: officers/test-engineer.md, or a compact fallback if absent."""
    try:
        text = (_general_root() / "officers" / _OFFICER_FILE).read_text(encoding="utf-8").strip()
        return text or _FALLBACK_DOCTRINE
    except OSError:
        return _FALLBACK_DOCTRINE


def system_prompt() -> str:
    """Unit memory + the loaded officer doctrine + the pipeline driver instructions."""
    return memory.preamble() + doctrine() + "\n\n---\n\n" + _DRIVER


def extract_coverage(text: str) -> str:
    """The coverage artifact for the PR description, parsed from the officer's final message.
    Empty string if it produced no COVERAGE: line."""
    m = None
    for m in _COVERAGE_RE.finditer(text or ""):
        pass  # keep the LAST match — the officer's final, summary line
    return m.group(1).strip() if m else ""


def _prompt(ticket: Ticket) -> str:
    ac = "\n".join(f"  - {c}" for c in ticket.acceptance_criteria) or "  (none specified)"
    return "\n".join([
        f"TICKET {ticket.id}: {ticket.summary}",
        "",
        "DESCRIPTION:",
        ticket.description or "(none)",
        "",
        "ACCEPTANCE CRITERIA:",
        ac,
        "",
        "The Builder has implemented this on the current branch. Ensure it is proven by tests and "
        "report the coverage artifact now.",
    ])


async def ensure_coverage(ticket: Ticket, app: AppConfig, cfg: Config) -> TestEngineerResult:
    """Run the Test Engineer coverage pass on the just-built branch. Returns the coverage artifact
    for the PR description. Fail-safe: any process error returns ok=False with an empty artifact —
    the caller keeps moving to review rather than dead-stopping the pipeline."""
    from . import guard
    workdir = app.workdir or app.repo_path
    eff = normalize_effort(getattr(cfg, "test_engineer_effort", "medium"))
    # EU-52: the Test Engineer writes test code under the Builder's ceiling — route it through the ladder
    # sized off its effort (floor stays Sonnet for code), conserving under budget; ceiling when auto off.
    model, mreason = models.for_officer(cfg, effort=eff, ceiling_model=cfg.builder_model)
    if getattr(cfg, "auto_model", False):
        print(f"  · test-engineer model: {mreason}", flush=True)
    options = ClaudeAgentOptions(
        model=model,                        # ladder-chosen under the Builder ceiling — it writes test code
        system_prompt=system_prompt(),
        cwd=workdir,                        # the isolated worktree, where the Builder's change lives
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        setting_sources=[],                 # no settings files -> no ask/deny gate at any level
        hooks=guard.hooks_config(),         # hard denylist: blocks secrets/.env/CI writes + destructive shell
        max_turns=int(getattr(cfg, "builder_max_turns", 60) or 60),
        effort=eff,
    )
    run = await run_agent(_prompt(ticket), options, tag="test-engineer")
    return TestEngineerResult(
        ok=not run.is_error,
        coverage=extract_coverage(run.final or run.text),
        summary=run.final,
        cost_usd=run.cost_usd,
        num_turns=run.num_turns,
        raw=run.text,
        tools=run.tools,
    )
