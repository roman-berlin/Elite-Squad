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
0. CONTEXT: read the WHOLE ticket — description, acceptance criteria, AND the Commander's
   comments (on a re-opened ticket they carry the QA feedback on exactly what to fix). If the
   ticket lists image paths (mockups/screenshots), Read each image to SEE the intended design or
   the bug before you start — never guess at visuals.
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

Git: the orchestrator owns git and has ALREADY placed you on the correct branch in an isolated
worktree (your cwd is the repo root). Do NOT run git at all — no fetch, status, rev-parse,
worktree, log, diff — and never commit, push, switch branches, or touch history. Spend your
turns on the code, not on inspecting the repo.

Finish with a short plain-text summary: what you changed, which file(s), and which
acceptance criterion each change satisfies. End with a line EXACTLY in this form:
  TEST: <the single page/route to verify this on DEV, e.g. /leads — or a full URL>
so the Commander knows exactly where to check. If the change has no UI (pure backend/config),
write 'TEST: (no UI — <how to verify, e.g. an endpoint/command>)'.
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

    # 2) map score -> effort. Auto-sizing tops out at "high": max/xhigh burn a lot of thinking
    # and over-explore, so they're reserved for an explicit `effort-max`/`effort-ultra` pin (or
    # the retry-escalation ladder after a real rejection). Heaviest auto bucket = "high".
    if score <= -2:
        eff, size = "low", "XS"
    elif score <= 0:
        eff, size = "medium", "S/M"
    elif score <= 2:
        eff, size = "high", "L"
    else:
        eff, size = "high", "XL"
    return size, eff, "; ".join(reasons) or "no strong signals"


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


# Heavier effort = bigger task = more turns before it's "stuck". Scales the base budget so a deep
# ticket doesn't error out at the turn cap mid-implementation (e.g. base 60 -> high 96 -> max 144).
_TURN_SCALE = {"low": 1.0, "medium": 1.0, "high": 1.6, "xhigh": 2.4, "max": 2.4}


def turns_for(cfg: Config, effort: str) -> int:
    """Max build turns for this effort: the configured base, scaled up for high/max."""
    base = int(getattr(cfg, "builder_max_turns", 60) or 60)
    return max(base, int(base * _TURN_SCALE.get(effort, 1.0)))


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


async def build(req: BuildRequest, app: AppConfig, cfg: Config, audit=None) -> BuildResult:
    """Implement the ticket. For a sized-big ticket on its first pass (and only when delegation is
    armed), the Field Engineer splits it across sized soldiers; otherwise a single focused builder
    pass. Delegation is fail-safe — a thin plan or any hiccup falls back to the solo build."""
    from . import squad
    if squad.should_delegate(cfg, req):
        try:
            result, n = await squad.build_delegated(req, app, cfg, audit=audit)
            if result is not None and n >= 2:
                return result
        except Exception as exc:  # noqa: BLE001 - delegation must never break a run
            print(f"  · delegation off ({str(exc).splitlines()[0][:80]}); building solo", flush=True)
    return await _solo_build(req, app, cfg)


async def _solo_build(req: BuildRequest, app: AppConfig, cfg: Config) -> BuildResult:
    workdir = app.workdir or app.repo_path
    # Fully unattended: load NO filesystem settings (setting_sources=[]) so the repo's
    # `ask: [Edit/Write]` permission rules — at the root OR nested under a subdir like backend/ —
    # never gate the builder mid-run; bypassPermissions then governs and writes go through. Safe
    # by construction: the builder works ONLY inside an isolated git worktree, the read-only
    # Reviewer + the gate validate before any merge, and MAIN is never touched. Repo conventions
    # still apply — BUILDER_SYSTEM tells it to read CLAUDE.md + .claude/rules and follow them.
    eff = effort_for(cfg, req.iteration, req.ticket)
    from . import models, guard
    model, mreason = models.for_builder(cfg, req.ticket, eff)
    if getattr(cfg, "auto_model", False):
        print(f"  · builder model: {mreason}", flush=True)
    options = ClaudeAgentOptions(
        model=model,                   # the configured ceiling, or auto-chosen <= ceiling
        system_prompt=memory.preamble() + BUILDER_SYSTEM,
        cwd=workdir,                   # the isolated worktree when enabled
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        setting_sources=[],            # no settings files -> no ask/deny gate at any level
        hooks=guard.hooks_config(),    # hard denylist: blocks secrets/.env/CI writes + destructive shell
        max_turns=turns_for(cfg, eff),
        effort=eff,
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
