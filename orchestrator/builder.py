"""Builder driver — Claude Code (Agent SDK) with full tools, writing on a branch."""
from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions

from .agent import run_agent
from .config import AppConfig, Config
from .contracts import BuildRequest, BuildResult

BUILDER_SYSTEM = """\
You are the Builder in an automated dev pipeline. You implement exactly one ticket
at a time on the current git branch — efficiently and surgically.

Approach, in order:
1. LOCATE: grep/glob for the specific files and functions the ticket touches. Do not
   read the whole repo.
2. PLAN: choose the smallest change that fully satisfies the acceptance criteria.
3. IMPLEMENT: make that minimal change. Match existing conventions (style, structure,
   libraries). Do NOT refactor unrelated code or expand scope.
4. PRESERVE: do not break existing behaviour, public APIs, types, RTL/layout, or other
   features.
5. TEST: add or adjust ONLY the tests for what you changed.

Resource safety (the dev machine has limited RAM — respect it):
- Do NOT run the whole test suite at default concurrency. Vitest spawns one worker per
  CPU core and can exhaust memory and freeze the machine. When you self-check, run ONLY
  the tests for the files you touched, with bounded workers, e.g.:
      npx vitest run <path> --pool=forks --poolOptions.forks.maxForks=2
- Prefer fast checks (tsc --noEmit, eslint on changed files) over full runs.
- Never start watch mode or dev servers (no `vitest` watch, no `vite`/`npm run dev`).

Git: do NOT commit, push, switch branches, or touch history — the orchestrator owns git.

Finish with a short plain-text summary: what you changed, which file(s), and which
acceptance criterion each change satisfies.
"""


_EFFORT = ["low", "medium", "high", "max"]


def effort_for(cfg: Config, iteration: int) -> str:
    """Builder effort for this pass. Escalates one level per retry when enabled
    (spend more thinking after a rejection), capped at 'max'."""
    base = cfg.builder_effort if cfg.builder_effort in _EFFORT else "high"
    if cfg.escalate_effort_on_retry and iteration > 1:
        return _EFFORT[min(_EFFORT.index(base) + (iteration - 1), len(_EFFORT) - 1)]
    return base


def _prompt(req: BuildRequest) -> str:
    ac = "\n".join(f"  - {c}" for c in req.ticket.acceptance_criteria) or "  (none specified)"
    parts = [
        f"TICKET {req.ticket.id}: {req.ticket.summary}",
        "",
        "DESCRIPTION:",
        req.ticket.description or "(none)",
        "",
        "ACCEPTANCE CRITERIA:",
        ac,
    ]
    if req.prior_issues:
        issues = "\n".join(f"  - {i}" for i in req.prior_issues)
        parts += [
            "",
            f"THIS IS ITERATION {req.iteration}. The previous attempt was REJECTED in review.",
            "You MUST address every point below; do not regress passing behaviour:",
            issues,
        ]
    parts += ["", "Implement the ticket now."]
    return "\n".join(parts)


async def build(req: BuildRequest, app: AppConfig, cfg: Config) -> BuildResult:
    # Unattended permissions: tools listed in `allowed_tools` are auto-approved (so
    # the builder can run tests via Bash without prompting). With no TTY, any tool
    # NOT listed is denied rather than hanging. For fully autonomous arbitrary
    # tooling switch to permission_mode="bypassPermissions" (looser — weigh it).
    options = ClaudeAgentOptions(
        model=cfg.builder_model,
        system_prompt=BUILDER_SYSTEM,
        cwd=app.workdir or app.repo_path,   # the isolated worktree when enabled
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        setting_sources=["project"],   # load the repo's CLAUDE.md / .claude settings
        max_turns=60,
        effort=effort_for(cfg, req.iteration),
    )
    run = await run_agent(_prompt(req), options, tag="builder")
    return BuildResult(
        ok=not run.is_error,
        summary=run.final,
        cost_usd=run.cost_usd,
        num_turns=run.num_turns,
        raw=run.text,
        tools=run.tools,
    )
