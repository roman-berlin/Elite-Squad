"""Squad delegation — the Dev Team Lead's chain of command executes.

For a big ticket, the Dev Team Lead (squad lead) decomposes the work into a few non-overlapping
subtasks, each owned by the right SOLDIER (frontend / backend / DB / DevOps / generalist) and
SIZED to its slice (task-adaptive effort, all the way down to the soldier). The soldiers run
sequentially on the SAME isolated branch; the orchestrator's gate + review + keep-DEV-green merge
still validate the combined result, and MAIN is never touched.

Design notes (Roman's reflexes):
- Fail-safe: any planning hiccup (no/!invalid plan, <2 subtasks, an exception) falls back to the
  normal SOLO build — delegation never breaks a run.
- Off by default (`delegation_enabled`), and only ever engages on iteration 1 of a sized-big
  ticket; a rejected retry goes back to a single focused builder pass.
- Sequential, not parallel: soldiers share one worktree, so concurrent edits would conflict (and
  the dev machine has limited RAM). One soldier at a time.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from claude_agent_sdk import ClaudeAgentOptions

from . import memory
from .agent import run_agent
from .config import AppConfig, Config
from .contracts import BuildRequest, BuildResult

# Squad roles (from officers/engineer.md). key -> (label, focus line for the soldier's system).
SQUAD: dict[str, tuple[str, str]] = {
    "vanguard-fe": ("Frontend Engineer", "Frontend — React/Vite, TypeScript, Tailwind, components, UI state."),
    "ordnance-be": ("Ordnance BE", "Backend — FastAPI/Python: routes, services, repositories, schemas."),
    "logistics-db": ("Logistics DB", "Data — Supabase/Postgres: SQL migrations, RLS policies, types."),
    "devops": ("DevOps", "CI / build / deploy / config — package scripts, env, Docker, Vercel/Turbo."),
    "generalist": ("Software Engineer", "General-purpose — anything outside the specialist lanes, or glue work."),
}

# subtask size -> builder effort (task-adaptive effort, extended to the soldiers; capped at high).
_SIZE_EFFORT = {"XS": "low", "S": "medium", "M": "medium", "L": "high", "XL": "high"}


@dataclass
class Subtask:
    role: str          # a SQUAD key
    title: str
    detail: str
    size: str = "M"    # XS | S | M | L | XL
    def effort(self) -> str:
        return _SIZE_EFFORT.get((self.size or "M").upper(), "medium")


def _extract_json_array(text: str | None):
    """Pull the first JSON array of objects out of an agent reply (tolerant of prose / fences)."""
    if not text:
        return None
    m = re.search(r"\[\s*\{.*}\s*\]", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else None
    except json.JSONDecodeError:
        return None


def parse_subtasks(text: str | None) -> list[Subtask]:
    """Validate the planner's JSON into Subtasks: unknown roles -> generalist, bad size -> M,
    empty detail dropped. Pure + deterministic, so it's unit-testable without an agent."""
    out: list[Subtask] = []
    for item in (_extract_json_array(text) or []):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip().lower()
        if role not in SQUAD:
            role = "generalist"
        detail = str(item.get("detail", "")).strip()
        if not detail:
            continue
        title = str(item.get("title", "")).strip()[:120] or detail[:60]
        size = str(item.get("size", "M")).strip().upper()
        if size not in _SIZE_EFFORT:
            size = "M"
        out.append(Subtask(role=role, title=title, detail=detail, size=size))
    return out


_PLANNER_SYSTEM = """\
You are the Dev Team Lead (squad lead) planning how to split ONE ticket across your squad so
specialists each own their slice. Read what you need (Grep/Glob/Read only — do NOT edit). Decide
the SMALLEST set of subtasks (1-5) that fully covers the acceptance criteria, each owned by the
right engineer. If the ticket is genuinely atomic (one concern, one area), return a SINGLE subtask.

Roles (use the exact key):
- vanguard-fe   : frontend (React/Vite/TS/Tailwind)
- ordnance-be   : backend (FastAPI/Python)
- logistics-db  : database (Supabase/SQL/migrations/RLS)
- devops        : CI/build/deploy/config
- generalist    : anything else / glue

Reply with ONLY a JSON array, no prose, each item exactly:
{"role":"<key>","title":"<short>","detail":"<what to implement + which files/area>","size":"XS|S|M|L"}
Order them so dependencies come first (e.g. a DB migration before the backend that uses it). Keep
slices NON-OVERLAPPING — two engineers must never edit the same lines."""

_SOLDIER_SYSTEM = """\
You are an ENGINEER of the Dev Team Lead's squad — {label}: {focus}
You implement ONE subtask of a larger ticket, on the current git branch, surgically.

HOUSE RULES: before editing, read this repo's conventions and follow them strictly — CLAUDE.md at
the repo root and the relevant .claude/rules/ files (Bun-only, never npm; tenant-isolation /
zero-trust; TypeScript conventions). Stay strictly inside YOUR subtask — do NOT implement other
engineers' slices, and do NOT refactor unrelated code. Match existing conventions. Add or adjust
ONLY the tests for what you changed.

Resource safety (limited RAM): never run the whole suite; run ONLY targeted tests with bounded
workers, e.g. `npx vitest run <path> --pool=forks --poolOptions.forks.maxForks=2`; prefer
`tsc --noEmit` / eslint on changed files. Never start watch mode or dev servers.

Git: the orchestrator owns git and has already placed you on the correct branch (cwd = repo root).
Do NOT run git at all. Spend your turns on the code.

Finish with a short plain-text summary: what you changed and which file(s)."""


def _planner_prompt(req: BuildRequest) -> str:
    ac = "\n".join(f"  - {c}" for c in req.ticket.acceptance_criteria) or "  (none specified)"
    return "\n".join([
        f"TICKET {req.ticket.id}: {req.ticket.summary}", "",
        "DESCRIPTION:", req.ticket.description or "(none)", "",
        "ACCEPTANCE CRITERIA:", ac, "",
        "Plan the squad split now — JSON array only.",
    ])


def _soldier_prompt(st: Subtask, req: BuildRequest, idx: int, total: int) -> str:
    ac = "\n".join(f"  - {c}" for c in req.ticket.acceptance_criteria) or "  (none)"
    label = SQUAD[st.role][0]
    parts = [
        f"Parent ticket {req.ticket.id}: {req.ticket.summary}",
        f"(You are engineer {idx} of {total}. Other engineers handle the rest — stay in your slice.)",
        "", "FULL ACCEPTANCE CRITERIA (context — you own only your slice):", ac, "",
        f"YOUR SUBTASK [{label}] — {st.title}:", st.detail, "",
    ]
    if idx > 1:
        parts.append("Earlier engineers already changed this branch; build on their work, don't redo it.")
    parts.append("Implement your subtask now.")
    return "\n".join(parts)


async def _plan(req: BuildRequest, app: AppConfig, cfg: Config):
    """Read-only planning pass. Returns (subtasks, cost, turns, tools)."""
    cwd = app.workdir or app.repo_path
    options = ClaudeAgentOptions(
        model=cfg.builder_model,
        system_prompt=memory.preamble() + _PLANNER_SYSTEM,
        cwd=cwd, permission_mode="bypassPermissions",   # read-only plan; unattended — never dead-stop
        allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash", "NotebookEdit"],
        setting_sources=[], max_turns=14, effort="medium")
    run = await run_agent(_planner_prompt(req), options, tag="squad-lead")
    return parse_subtasks(run.final or run.text), run.cost_usd, run.num_turns, run.tools


async def _soldier(st: Subtask, req: BuildRequest, app: AppConfig, cfg: Config, idx: int, total: int):
    from .builder import turns_for
    from . import guard
    guard.warn_if_absent(f"soldier·{st.role}")   # EU-2 F7: loud one-liner if guard is absent under bypass
    cwd = app.workdir or app.repo_path
    label, focus = SQUAD[st.role]
    options = ClaudeAgentOptions(
        model=cfg.builder_model,
        system_prompt=memory.preamble() + _SOLDIER_SYSTEM.format(label=label, focus=focus),
        cwd=cwd, permission_mode="bypassPermissions",
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        hooks=guard.hooks_config(),    # same hard denylist as the builder
        setting_sources=[], max_turns=turns_for(cfg, st.effort()), effort=st.effort())
    return await run_agent(_soldier_prompt(st, req, idx, total), options, tag=f"soldier·{st.role}")


def should_delegate(cfg: Config, req: BuildRequest) -> bool:
    """Delegate only on the first pass of a sized-big ticket (L/XL or many acceptance criteria).
    Retries are targeted fixes -> a single focused builder pass, never a re-split."""
    if not getattr(cfg, "delegation_enabled", False):
        return False
    if req.iteration != 1:
        return False
    from .builder import size_ticket
    size, _eff, _why = size_ticket(req.ticket)
    if size in ("L", "XL"):
        return True
    return len(req.ticket.acceptance_criteria or []) >= int(getattr(cfg, "delegation_min_ac", 3))


async def build_delegated(req: BuildRequest, app: AppConfig, cfg: Config, audit=None):
    """Plan -> dispatch soldiers -> aggregate into one BuildResult. Returns (result, n_subtasks).
    Returns (None, 0) when the plan has <2 subtasks, signalling the caller to do a solo build."""
    subtasks, p_cost, p_turns, p_tools = await _plan(req, app, cfg)
    cap = max(1, int(getattr(cfg, "delegation_max_soldiers", 4)))
    subtasks = subtasks[:cap]
    if len(subtasks) < 2:
        return None, 0
    print("  squad · split into " + str(len(subtasks)) + ": "
          + ", ".join(f"{SQUAD[s.role][0]}({s.size})" for s in subtasks), flush=True)
    if audit is not None:
        audit.record("delegation", ticket_id=req.ticket.id, subtasks=len(subtasks),
                     roles=[s.role for s in subtasks])

    cost, turns, tools, summaries, ok = p_cost, p_turns, list(p_tools), [], True
    for i, st in enumerate(subtasks, 1):
        print(f"  engineer {i}/{len(subtasks)} · {SQUAD[st.role][0]} (effort {st.effort()}) — {st.title}",
              flush=True)
        run = await _soldier(st, req, app, cfg, i, len(subtasks))
        cost += run.cost_usd
        turns += run.num_turns
        tools += run.tools
        summaries.append(f"[{SQUAD[st.role][0]} · {st.size}] {st.title}\n{(run.final or '').strip()}")
        if run.is_error:
            ok = False
        if audit is not None:
            audit.record("soldier_build", ticket_id=req.ticket.id, role=st.role, size=st.size,
                         effort=st.effort(), ok=not run.is_error, cost_usd=run.cost_usd,
                         turns=run.num_turns, tools=run.tools)

    summary = (f"Squad delegation — {len(subtasks)} subtasks dispatched:\n\n" + "\n\n".join(summaries))
    return BuildResult(ok=ok, summary=summary, cost_usd=cost, num_turns=turns,
                       raw=summary, tools=tools), len(subtasks)
