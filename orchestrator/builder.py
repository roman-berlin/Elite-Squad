"""Builder driver — Claude Code (Agent SDK) with full tools, writing on a branch."""
from __future__ import annotations

import re

from claude_agent_sdk import ClaudeAgentOptions

from . import memory
from .agent import run_agent
from .config import (AppConfig, Config, EFFORT_LADDER, effort_step_index,
                     normalize_effort)
from .contracts import BuildRequest, BuildResult

BUILDER_SYSTEM = """\
You are the Builder in an automated dev pipeline. You implement exactly one ticket
at a time on the current git branch — efficiently and surgically.

HOUSE RULES: before you edit, read this repo's conventions and follow them strictly — CLAUDE.md
at the repo root and the relevant files under .claude/rules/ (e.g. Bun-only — never npm;
tenant-isolation / zero-trust; TypeScript conventions). They override generic habits.

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


# Heavier work => more thinking. Lighter work => spend less. Deterministic keyword signals.
_HEAVY_KW = re.compile(
    r"\b(refactor\w*|migrat\w+|re-?architect\w*|redesign|rewrite|concurren\w+|race[ -]condition|"
    r"deadlock|performance|optimi[sz]\w+|securit\w+|auth\w*|permission\w*|rbac|tenant|isolation|"
    r"schema|database|migration|index|encrypt\w+|payment\w*|billing|webhook|"
    r"end-to-end|backward[ -]compat\w*|breaking[ -]change|epic|spike|integration)\b", re.I)
_LIGHT_KW = re.compile(
    r"\b(typo|copy[- ]?(write|edit)?|wording|label|rename|bump|version|comment|docs?|readme|"
    r"lint|format\w*|whitespace|tooltip|placeholder|alt[ -]text|colou?r|css|padding|margin|icon)\b", re.I)
# Commander override — a label like `effort-max` / `effort-ultra`, or an `[effort:ultra]`
# marker in the text. Captures any spelling; normalize_effort() maps it to a valid level.
_EFF_TOKENS = (r"low|medium|med|high|hi|xhigh|xh|ultra|ultracode|ultrathink|extended|"
               r"vhigh|veryhigh|max|maximum|top")
_LABEL_EFFORT = re.compile(rf"^effort[:\-/]?({_EFF_TOKENS})$", re.I)        # a Jira label
_MARKER_EFFORT = re.compile(rf"\[effort[:\-/ ]?({_EFF_TOKENS})\]", re.I)    # inline marker in text

_SIZE_NAME = {"low": "XS", "medium": "S/M", "high": "L", "max": "XL"}


def size_ticket(ticket) -> tuple[str, str, str]:
    """Estimate task complexity from the ticket and map it to a base effort.

    Pure heuristic — instant, free, deterministic. Returns (size, effort, reason).
    A Commander override wins outright: an `effort-max` label (or an `[effort:max]`
    marker in the text) pins the effort directly."""
    text = " ".join([ticket.summary or "", ticket.description or "",
                     " ".join(ticket.acceptance_criteria or [])])
    labels = [str(l).lower() for l in (getattr(ticket, "labels", None) or [])]
    itype = (getattr(ticket, "issue_type", None) or "").lower()

    # 0) explicit override (normalized: 'ultra'->'xhigh', 'maximum'->'max', …)
    for lab in labels:
        m = _LABEL_EFFORT.match(lab)
        if m:
            eff = normalize_effort(m.group(1))
            return f"pinned:{eff}", eff, f"pinned by label '{lab}'"
    m = _MARKER_EFFORT.search(text)
    if m:
        eff = normalize_effort(m.group(1))
        return f"pinned:{eff}", eff, f"pinned by [effort:{m.group(1).lower()}] marker"

    # 1) score the signals
    score, reasons = 0, []
    ac = len(ticket.acceptance_criteria or [])
    if ac >= 5:
        score += 2; reasons.append(f"{ac} acceptance criteria")
    elif ac >= 2:
        score += 1; reasons.append(f"{ac} acceptance criteria")

    dlen = len(ticket.description or "")
    if dlen > 1200:
        score += 2; reasons.append("detailed spec")
    elif dlen > 400:
        score += 1
    elif dlen < 80:
        score -= 1; reasons.append("very short description")

    heavy = {mm.group(0).lower() for mm in _HEAVY_KW.finditer(text)}
    light = {mm.group(0).lower() for mm in _LIGHT_KW.finditer(text)}
    if heavy:
        score += min(len(heavy), 3); reasons.append("complexity: " + ", ".join(sorted(heavy))[:60])
    if light and not heavy:
        score -= 1; reasons.append("trivial-change signal")

    if "bug" in labels or itype == "bug":
        score -= 1; reasons.append("bug")
    if "epic" in labels or itype == "epic":
        score += 2; reasons.append("epic")

    # 2) map score -> effort
    if score <= -2:
        eff = "low"
    elif score <= 0:
        eff = "medium"
    elif score <= 2:
        eff = "high"
    else:
        eff = "max"
    return _SIZE_NAME[eff], eff, "; ".join(reasons) or "no strong signals"


def effort_plan(cfg: Config, iteration: int, ticket=None) -> tuple[str, str]:
    """(effort, human-readable reason) for this build pass.

    Base effort is sized from the ticket when adaptive_effort is on; otherwise the
    configured default. A rejected pass then escalates one level per retry (capped)."""
    if ticket is not None and getattr(cfg, "adaptive_effort", True):
        size, base, why = size_ticket(ticket)
        reason = f"sized {size} → {base} ({why})"
    else:
        base = normalize_effort(getattr(cfg, "builder_effort", "high"))
        reason = f"default → {base}"
    if cfg.escalate_effort_on_retry and iteration > 1:
        bumped = EFFORT_LADDER[min(effort_step_index(base) + (iteration - 1), len(EFFORT_LADDER) - 1)]
        if bumped != base:
            reason += f"; escalated to {bumped} (retry {iteration})"
        return bumped, reason
    return base, reason


def effort_for(cfg: Config, iteration: int, ticket=None) -> str:
    """Builder effort for this pass (see effort_plan for the reasoning)."""
    return effort_plan(cfg, iteration, ticket)[0]


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
    workdir = app.workdir or app.repo_path
    # Fully unattended: load NO filesystem settings (setting_sources=[]) so the repo's
    # `ask: [Edit/Write]` permission rules — at the root OR nested under a subdir like backend/ —
    # never gate the builder mid-run; bypassPermissions then governs and writes go through. Safe
    # by construction: the builder works ONLY inside an isolated git worktree, the read-only
    # Reviewer + the gate validate before any merge, and MAIN is never touched. Repo conventions
    # still apply — BUILDER_SYSTEM tells it to read CLAUDE.md + .claude/rules and follow them.
    options = ClaudeAgentOptions(
        model=cfg.builder_model,
        system_prompt=memory.preamble() + BUILDER_SYSTEM,
        cwd=workdir,                   # the isolated worktree when enabled
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        setting_sources=[],            # no settings files -> no ask/deny gate at any level
        max_turns=60,
        effort=effort_for(cfg, req.iteration, req.ticket),
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
