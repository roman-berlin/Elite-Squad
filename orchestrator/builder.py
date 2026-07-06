"""Builder driver — Claude Code (Agent SDK) with full tools, writing on a branch."""
from __future__ import annotations

import re

from claude_agent_sdk import ClaudeAgentOptions

from . import memory
from .agent import run_agent, run_agent_with_fallback
from .config import (AppConfig, Config, EFFORT_LADDER, effort_step_index,
                     normalize_effort)
from .contracts import (BuildArtifact, BuildRequest, BuildResult,
                       PerTicketArtifactStore, SpecArtifact)

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
   read the whole repo, and do NOT open large or generated files — lockfiles
   (bun.lock/package-lock), build artifacts under dist/build/coverage, minified bundles,
   or big fixtures/snapshots. They cost a lot of input tokens and rarely help; read only
   the source the ticket actually needs.
2. PLAN: choose the smallest change that fully satisfies the acceptance criteria.
3. IMPLEMENT: make that minimal change. Match existing conventions (style, structure,
   libraries). Do NOT refactor unrelated code or expand scope.
4. PRESERVE: do not break existing behaviour, public APIs, types, RTL/layout, or other
   features.
5. TEST: add or adjust ONLY the tests for what you changed.

PRE-SUBMIT GATES (mandatory — run these BEFORE you write your summary / hand off to Reviewer).
These checks are the unit's biggest Reviewer friction sources; the Reviewer will bounce the diff if
they are not green, so catch them here first. All are HARD gates: you do not finish until they pass.
If any fails, treat the failure as YOUR remediation work — fix the code (or tests), and re-run the
gate, before tagging Reviewer. Do NOT hand a diff to Reviewer with a known gate failure.
- A11Y / axe-core (zero violations): for any ticket that touches the UI (a page, route, component,
  or markup), run axe-core against the affected app/route and require ZERO violations. Use the
  repo's existing a11y harness if one exists (e.g. `bun run test:a11y`, a jest-axe/vitest-axe test,
  or `npx @axe-core/cli <url>`); otherwise add a jest-axe/vitest-axe assertion on the component you
  changed. Fix every reported violation before finishing. (Pure backend/config tickets with no UI
  surface have nothing to scan — say so in your summary instead of running it.)
- TESTS + COVERAGE: run `bun test --coverage` for the package you changed and require it to pass with
  NO failing tests. Read the coverage output and make sure the code you added/changed is exercised;
  add the missing test(s) if it is not. (Bun's test runner is light — unlike Vitest below it does not
  need worker bounding — but still scope it to the package you touched, not the whole monorepo.)
- SECURITY: never commit a secret (API key, token, password, private key, connection string).
  Keep queries parameterised and new routes behind their auth guard. (Phase-2 §2: the §1/§2/§3
  countersignature block was retired with the LLM security gate — a deterministic secret/dep scan
  runs on your diff now, so just don't introduce the problem.)
Report the outcome of all gates in your final summary (passed, or what you had to fix to make them
pass) so it is auditable that they ran before Reviewer saw the diff.

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

Finish with a plain-text summary that MUST open with ≤5 tight bullets in this order:
  • (a) what was done — which file(s) changed and which acceptance criterion each satisfies
  • (b) any gap or known limitation (omit the bullet if none)
  • (c) what changed vs the previous attempt — decisions made, approach shifted (omit on first pass)
Any brief prose detail may follow the bullets. End with a line EXACTLY in this form (keep it last):
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
    configured default. Retry keeps the pass-1 effort (and with it the turn budget) unless
    `escalate_effort_on_retry` is explicitly enabled — then a rejected pass escalates one
    level per retry (capped). Default is OFF: the 2026-07-05 audit found 135/135 round-≥2
    reviewer objections were new, so effort escalation bought bloat, not convergence."""
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


# EU-38: prior_issues/feedback is the single biggest input-token contributor on retries — it grows
# every iteration and is fed back verbatim. Cap it (configurable; keep the NEWEST, which is the most
# relevant review feedback) so a deep ticket on pass 4 doesn't blow past the input-token budget.
_FEEDBACK_MAX_ITEMS = 12       # keep at most this many prior-issue lines (newest)
_FEEDBACK_MAX_CHARS = 6000     # ...and at most this many total chars of feedback
_PREAMBLE_MAX_CHARS = 4000     # trim the unit-memory preamble into the builder prompt if oversized


def _cap_feedback(issues, cfg=None) -> list[str]:
    """Cap the prior_issues fed back on retry, keeping the NEWEST items (the latest review's points
    are the ones to fix). Bounds by item count first, then by total chars — both configurable via
    `builder_feedback_max_items` / `builder_feedback_max_chars`. Returns the trimmed list, with a
    leading marker line when anything was dropped so the Builder knows older points were elided."""
    items = [str(i) for i in (issues or [])]
    if not items:
        return []
    max_items = int(getattr(cfg, "builder_feedback_max_items", _FEEDBACK_MAX_ITEMS) or _FEEDBACK_MAX_ITEMS)
    max_chars = int(getattr(cfg, "builder_feedback_max_chars", _FEEDBACK_MAX_CHARS) or _FEEDBACK_MAX_CHARS)
    dropped = max(0, len(items) - max_items)
    kept = items[-max_items:] if max_items > 0 else []
    # Char budget: drop from the OLDEST end (front of `kept`) until under budget.
    while kept and sum(len(x) for x in kept) > max_chars:
        kept.pop(0)
        dropped += 1
    if dropped:
        kept.insert(0, f"(+{dropped} older point(s) elided to bound context — newest kept below)")
    return kept


def _trim_preamble(text: str, cfg=None) -> str:
    """Bound the unit-memory preamble concatenated into the system prompt. The Standing Orders at the
    top matter most, so keep the head and truncate the (oldest) tail when oversized — configurable via
    `builder_preamble_max_chars`. A no-op when the preamble already fits."""
    limit = int(getattr(cfg, "builder_preamble_max_chars", _PREAMBLE_MAX_CHARS) or _PREAMBLE_MAX_CHARS)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n… (unit memory truncated to bound builder context)\n\n"


def _digest(text: str, limit: int = 500) -> str:
    """Collapse the builder's summary into a ≤ ``limit``-char one-liner for BuildArtifact.diff_digest:
    whitespace-collapsed, then truncated with a single-char ellipsis so the result is ALWAYS ≤ limit
    (BuildArtifact.__post_init__ enforces the ceiling)."""
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _section_bullets(text: str, heading: str) -> list[str]:
    """Best-effort: the bullet/numbered items under a ``<heading>…:`` line in the builder's summary
    (e.g. ``Decisions:`` / ``Open questions:``), stopping at the next blank line or non-bullet. Returns
    [] when the builder emitted no such section — the full summary stays on the BuildResult for digging,
    so the artifact is a digest, never the only copy."""
    items: list[str] = []
    capturing = False
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not capturing:
            if re.match(rf"^\**\s*{re.escape(heading)}s?\b[^:]*:", s, re.IGNORECASE):
                capturing = True
            continue
        if not s:
            break
        m = re.match(r"^[-*•]\s+(.*)$", s) or re.match(r"^\d+[.)]\s+(.*)$", s)
        if not m:
            break
        items.append(m.group(1).strip())
    return items


def _build_artifact(result: BuildResult) -> BuildArtifact:
    """Distil a BuildResult into the typed BuildArtifact handoff (EU-72). ``files_changed`` is left
    empty for the loop to stamp (it owns git); the digest and any Decisions/Open-questions sections
    come from the builder's own summary."""
    summary = result.summary or result.raw or ""
    return BuildArtifact(
        files_changed=[],
        diff_digest=_digest(summary),
        decisions=_section_bullets(summary, "decision"),
        open_questions=_section_bullets(summary, "open question"),
    )


def _prompt(req: BuildRequest, cfg=None, spec: SpecArtifact | None = None) -> str:
    # EU-72: when the loop hands us a SpecArtifact, read the acceptance criteria (and non-goals) from
    # that structured handoff as the primary spec; otherwise fall back to the raw ticket. The full
    # description is always included either way — artifact-first, full-context-on-demand.
    criteria = spec.acceptance if spec is not None else req.ticket.acceptance_criteria
    ac = "\n".join(f"  - {c}" for c in criteria) or "  (none specified)"
    parts = [
        f"TICKET {req.ticket.id}: {req.ticket.summary}",
        "",
        "DESCRIPTION:",
        req.ticket.description or "(none)",
        "",
        "ACCEPTANCE CRITERIA:",
        ac,
    ]
    # EU-109: include the Architect's ADR if produced (provides approach, risk, touch-points, DoD)
    if req.adr:
        parts += [
            "",
            "ARCHITECT'S ADR (design upfront — follow this approach):",
            req.adr,
        ]
    if spec is not None and spec.non_goals:
        parts += ["", "NON-GOALS (explicitly out of scope — do NOT touch):",
                  "\n".join(f"  - {g}" for g in spec.non_goals)]
    capped = _cap_feedback(req.prior_issues, cfg)
    if capped:
        issues = "\n".join(f"  - {i}" for i in capped)
        parts += [
            "",
            f"THIS IS ITERATION {req.iteration}. The previous attempt was REJECTED in review.",
            "You MUST address every point below; do not regress passing behaviour:",
            issues,
        ]
    parts += ["", "Implement the ticket now."]
    return "\n".join(parts)


async def build(req: BuildRequest, app: AppConfig, cfg: Config, audit=None,
                *, store: PerTicketArtifactStore | None = None,
                spec: SpecArtifact | None = None) -> BuildResult:
    """Implement the ticket. For a sized-big ticket on its first pass (and only when delegation is
    armed), the Dev Team Lead splits it across sized soldiers; otherwise a single focused builder
    pass. Delegation is fail-safe — a thin plan or any hiccup falls back to the solo build.

    EU-72: ``spec`` is the upstream SpecArtifact (primary context — the builder reads its acceptance
    criteria / non-goals, falling back to the raw ticket when None). After the build, the typed
    BuildArtifact is published into ``store`` so the Test Engineer + Reviewer read a tight handoff
    instead of re-deriving from the full diff. Both default to None so direct/CLI callers are
    unaffected.

    ``n`` from ``build_delegated`` encodes the flow used:
      0   → thin plan or synthesis stub not ready → fall through to solo.
      1   → synthesis flow produced a result (EU-69 domain gap, single specialist).
      ≥2  → normal squad delegation with n soldiers.
    All three cases keep the fail-safe: any exception → solo build.
    """
    from . import squad
    result: BuildResult | None = None
    # 2026-07-05 telemetry audit: when delegation falls back to solo, the gap-detect + planner
    # calls already burned real tokens with no BuildResult to carry them (the EU-139 run dropped
    # $0.386 this way). build_delegated reports that burn here; the solo result absorbs it below.
    sunk: dict = {}
    if squad.should_delegate(cfg, req):
        try:
            delegated, n = await squad.build_delegated(req, app, cfg, audit=audit, sunk=sunk)
            if delegated is not None and n >= 1:   # n=1: synthesis; n>=2: squad split
                result = delegated
        except Exception as exc:  # noqa: BLE001 - delegation must never break a run
            print(f"  · delegation off ({str(exc).splitlines()[0][:80]}); building solo", flush=True)
    if result is None:
        result = await _solo_build(req, app, cfg, spec=spec)
        if sunk:
            result.cost_usd += float(sunk.get("cost_usd", 0.0) or 0.0)
            result.num_turns += int(sunk.get("num_turns", 0) or 0)
            result.input_tokens += int(sunk.get("input_tokens", 0) or 0)
            result.output_tokens += int(sunk.get("output_tokens", 0) or 0)
    # EU-72: publish the typed BuildArtifact into the shared per-ticket pool. The loop stamps the
    # authoritative files_changed (it owns git); the full summary/raw stays on the result for digging.
    if store is not None:
        store.put(_build_artifact(result))
    return result


async def _solo_build(req: BuildRequest, app: AppConfig, cfg: Config,
                      *, spec: SpecArtifact | None = None) -> BuildResult:
    workdir = app.workdir or app.repo_path
    # Fully unattended: load NO filesystem settings (setting_sources=[]) so the repo's
    # `ask: [Edit/Write]` permission rules — at the root OR nested under a subdir like backend/ —
    # never gate the builder mid-run; bypassPermissions then governs and writes go through. Safe
    # by construction: the builder works ONLY inside an isolated git worktree, the read-only
    # Reviewer + the gate validate before any merge, and MAIN is never touched. Repo conventions
    # still apply — BUILDER_SYSTEM tells it to read CLAUDE.md + .claude/rules and follow them.
    eff = effort_for(cfg, req.iteration, req.ticket)
    from . import models, guard, provider as _provider
    guard.warn_if_absent("builder")   # EU-2 F7: loud one-liner if bypassPermissions runs with no guard
    model, mreason = models.for_builder(cfg, req.ticket, eff, req.iteration)
    if getattr(cfg, "auto_model", False):
        print(f"  · builder model: {mreason}", flush=True)
    options = ClaudeAgentOptions(
        model=model,                   # the configured ceiling, or auto-chosen <= ceiling
        system_prompt=_trim_preamble(memory.preamble(), cfg) + BUILDER_SYSTEM,
        cwd=workdir,                   # the isolated worktree when enabled
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        setting_sources=[],            # no settings files -> no ask/deny gate at any level
        hooks=guard.hooks_config(workdir),  # denylist + EU-188 worktree confinement (no writes outside workdir)
        max_turns=turns_for(cfg, eff),
        effort=eff,
    )
    # EU-38: tag this build pass in the usage ledger (ticket id + iteration) so per-pass input
    # tokens are sliceable by the ledger-analysis tooling. cfg also bounds the feedback/preamble.
    # EU-108: use run_agent_with_fallback to handle Sonnet-cap → Opus fallback
    # EU-174: determine routing tier based on task characteristics
    routing_tier = None
    try:
        from . import routing as _routing
        if _routing.is_routing_enabled():
            ticket_desc = req.ticket.description or ""
            ticket_size = req.ticket.size or ""
            tier = _routing.classify_task(
                ticket_description=ticket_desc,
                task_type="build",
                effort=eff,
                size=ticket_size,
            )
            routing_tier = tier.value
            if routing_tier == "local":
                print(f"      · routing → Tier 1 (Local Ollama)", flush=True)
            else:
                print(f"      · routing → Tier 2 (Cloud)", flush=True)
    except Exception:  # noqa: BLE001 — routing failure must not break the build
        routing_tier = None

    run = await run_agent_with_fallback(_prompt(req, cfg, spec), options, tag="builder",
                                        ticket_id=req.ticket.id, pass_number=req.iteration, cfg=cfg,
                                        routing_tier=routing_tier)
    # EU-123: show actual provider+model in the live feed
    if getattr(cfg, "auto_model", False):
        display = _provider.format_provider_model(run.provider, run.model_version)
        print(f"  · builder · {display}", flush=True)
    return BuildResult(
        ok=not run.is_error,
        summary=run.final,
        cost_usd=run.cost_usd,
        num_turns=run.num_turns,
        raw=run.text,
        tools=run.tools,
        input_tokens=getattr(run, "input_tokens", 0),   # EU-96: expose for per-officer burn tracking
        output_tokens=getattr(run, "output_tokens", 0),  # getattr-guarded: stubs may omit these
        provider=getattr(run, "provider", ""),         # EU-123: which provider served this run
        model_version=getattr(run, "model_version", ""), # EU-123: clean model identifier
    )
