"""Builder driver — Claude Code (Agent SDK) with full tools, writing on a branch."""
from __future__ import annotations

import re

from claude_agent_sdk import ClaudeAgentOptions

from . import backends, memory
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
5. TEST — FAIL-FIRST (mandatory): for EACH testable acceptance criterion, write the test FIRST,
   run it against the UNCHANGED code, and confirm it FAILS for the right reason — the behaviour is
   genuinely missing, not an import error or a typo in the test. THEN implement until it passes. A
   test you never watched fail can be green for the wrong reason (vacuous), so it has NO TEETH: if a
   test cannot be made to fail without your change, fix the test until it can. When a test is
   genuinely test-after — you changed the code before writing it, or you are adjusting an existing
   test — MUTATION-CHECK it instead: revert your change (or move the asserted line), confirm the
   test goes RED, then restore. Add or adjust ONLY the tests for what you changed. A new test
   FILE must exit non-zero on any failed check — verify by forcing one check red and running the
   file standalone (a harness that prints FAIL but exits 0 hides every regression it ever finds).

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
  NO failing tests. Read the coverage TOTALS/summary line only (never the per-file table) and make
  sure the code you added/changed is exercised; add the missing test(s) if it is not. Every test you
  add must have been RED before your change
  (fail-first — step 5): a test that stays green on the unchanged code is not covering your change.
  (Bun's test runner is light — unlike Vitest below it does not need worker bounding — but still
  scope it to the package you touched, not the whole monorepo.)
  CI PARITY: `bun test` and `vitest` are DIFFERENT runners with different discovery/execution — a
  `bun test` pass does NOT verify a `vitest` job. Before claiming any CI/test-running outcome
  ("tests pass", "CI will go green"), check the app's package.json `test` script and CI workflow
  (.github/workflows/*.yml) for the command CI actually runs, and verify with THAT command. Never
  state a CI outcome you have not actually observed with CI's own runner.
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
- TOKEN HYGIENE — use quiet reporters; every verbose test/coverage dump compounds into every later
  turn's input across the pass. Run `vitest run <path> --reporter=dot` (not the default verbose
  reporter); run Python tests with `pytest -q --no-header`; read coverage as the text-summary TOTALS
  line only — never the per-file table; scope eslint/tsc to the changed files only, not the whole
  package. On a RED run, re-run only the failing test file(s) — not the whole suite — to isolate and
  confirm the fix.

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

MANUAL TEST contract (2026-07-20 Commander order): if ANY acceptance criterion could NOT be
verified by you — missing env/credentials, a device/browser matrix you cannot run, a visual
check needing human eyes — your summary MUST include a section starting exactly 'MANUAL TEST:'
with NUMBERED, exact steps for each unverified item: where to go (page/route), what to do
(clicks/input), and precisely what the Commander must see to pass it. The pipeline lands such a
ticket into the Blocked column with your steps as the hand-off comment, so vague steps = a stuck
ticket. Omit the section entirely when you verified everything yourself.
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


# 2026-07-19 (Commander order — squad modes): the ELITE working method, appended to the system
# prompt only when squad_pref resolves this ticket to the elite squad. It changes HOW the one
# careful Builder works (iterative, verified, reported), never WHAT verifies the result — the
# deterministic gate and the independent review run unchanged after it. English rendition of the
# Commander's iterative-methodology directive.
ELITE_METHOD = """

ELITE SQUAD METHOD — you are the single careful Builder on a small elite squad. Work in
small, verified iterations; the goal is a correct, stable, clear result — not a fast one.

For every work cycle:
1. ANALYZE before touching anything: restate the end goal, the key requirements, the risks and
   anything unclear. Never make an important assumption silently — write it down.
2. Work from the ordered step plan (the design brief's steps if present, else derive one).
   Execute ONLY the next step — never several steps at once.
3. Before changing any file, READ it and understand the current structure. Prefer a targeted,
   controlled edit over rewriting a file.
4. After EVERY step: run the repo's own test/verification command for the touched area. Check —
   does it meet the requirement? errors? anything missing? duplication? is there a simpler way?
5. Report each iteration in your running output, exactly this shape:
     DID: <what was done>   CHECKED: <what was verified and how>   FOUND: <problems, or 'clean'>
     FIXING: <the correction now, or '—'>   NEXT: <the next step>
6. Fix problems BEFORE advancing. A significant problem blocks the next step; small non-blocking
   ones go on an improvements list you carry to the end.
7. Repeat until the acceptance criteria hold and the suite for the touched area is green.

Hard rules: don't settle for the first solution; don't rush to finish; never claim confidence in
anything unverified; never invent missing information; never skip the per-step check. Missing
CRITICAL information is a halt (state exactly what is missing — the pipeline parks it as a
decision); a non-critical gap is a stated assumption you flag for later. If you find a genuinely
better approach than the plan, note it in the handoff with trade-offs — finish the current step
first, don't silently change direction. Your final handoff lists: done / checked / still open /
recommended next steps, plus the improvements list.
"""


# 2026-07-19 (senior turn-limit loop): per-ticket retry markers for a build that blew the turn
# ceiling AND couldn't be split smaller (scrum depth cap). loop._exception_report re-queues such a
# ticket exactly once; the marker is what (a) makes that "once" survive the process (the requeue is
# picked up by a LATER drain cycle) and (b) tells effort_plan to bump one effort level so the retry
# actually gets more turns (turns_for scales with effort). Persisted next to the audit log, atomic
# via locking.locked_rmw like every other state file.
def _turn_retry_file(cfg):
    from pathlib import Path
    return Path(getattr(cfg, "audit_path", "./state/audit.jsonl")).with_name("turn_retries.json")


def turn_retry_count(cfg, ticket_id: str) -> int:
    """How many turn-limit requeues this ticket has already used (0 = none yet)."""
    import json
    try:
        data = json.loads(_turn_retry_file(cfg).read_text(encoding="utf-8"))
        return int(data.get(str(ticket_id), 0)) if isinstance(data, dict) else 0
    except (OSError, ValueError, TypeError):
        return 0


def mark_turn_retry(cfg, ticket_id: str) -> bool:
    """Persist one turn-limit requeue for ``ticket_id``. Returns False when the write failed —
    the caller must then NOT retry (fail-closed), or an unwritable state dir would requeue the
    same blow-out forever."""
    from . import locking

    def _mut(d):
        d = d if isinstance(d, dict) else {}
        d[str(ticket_id)] = int(d.get(str(ticket_id), 0)) + 1
        return d
    try:
        locking.locked_rmw(_turn_retry_file(cfg), _mut, default={}, corrupt_to_default=True)
        return True
    except OSError:
        return False


def _elite_squad(cfg, ticket) -> bool:
    """True when squad_pref routes THIS ticket to the elite squad. Fail-safe to False — a broken
    pref store must never change how a build runs."""
    if ticket is None:
        return False
    try:
        from . import squad_pref
        return squad_pref.resolve_for_ticket(cfg, ticket)[0] == "elite"
    except Exception:  # noqa: BLE001
        return False


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
    # Elite squad floor: one careful pass replaces retry churn, so the Builder gets the xhigh
    # turn/task budget up front (2.4x turns) regardless of how small the sizer called the ticket.
    # Compared by TURN SCALE, not ladder index — xhigh shares a ladder rung with high (it model-
    # falls-back to high off-Opus) but carries the bigger turn budget, which is what elite needs.
    if _elite_squad(cfg, ticket) and _TURN_SCALE.get(base, 1.0) < _TURN_SCALE.get("xhigh", 2.4):
        base = "xhigh"
        reason += "; elite squad floor → xhigh"
    # Turn-limit requeue boost: a ticket re-queued after blowing the turn ceiling unsplittably
    # (see loop._exception_report) runs one effort level higher so turns_for grants real headroom.
    if ticket is not None and turn_retry_count(cfg, getattr(ticket, "id", "")) > 0:
        boosted = EFFORT_LADDER[min(effort_step_index(base) + 1, len(EFFORT_LADDER) - 1)]
        if boosted != base:
            reason += f"; boosted to {boosted} (turn-limit retry)"
            base = boosted
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

# EU-393: a weak (GLM) backend burns roughly ONE tool call per turn (vs. several for Opus), so a
# turn/task-budget ceiling sized for Opus starves it before it even clears exploration — splitting
# the ticket doesn't help, every fragment just inherits the same too-small-for-GLM ceiling. 2.0 is
# inside the ticket's 2-2.5x band.
_BACKEND_TURN_SCALE = 2.0


def _backend_turn_scale(cfg: Config | None = None) -> float:
    """The turn/task-budget multiplier for THIS build pass's EFFECTIVE backend.

    Per-run and hybrid-aware: :func:`backends.current_for_tag` routes a ``'builder'`` call to the
    pinned hybrid secondary when hybrid mode is on, falling back to the run-pinned
    :func:`backends.current` otherwise — so the scale tracks the backend the Builder is ACTUALLY
    routed to, not the fleet default. Only when no hybrid secondary is pinned at all does it also
    consult ``cfg.model_backend`` (mirroring :func:`backends.is_glm`'s dual check), so a direct
    call with a cfg but no run context (tests, ``run_agent_with_fallback`` paths) still classifies
    correctly — a resolved run pin (even to NATIVE) always wins over the fleet-default cfg value.

    'Weak' uses the SAME signal ``loop.py`` uses for ``HARD_MAX_PASSES_WEAK``:
    ``backends.normalize(effective) != backends.NATIVE`` — unknown/registry ids normalize safely
    to NATIVE, never silently to GLM."""
    effective = backends.current_for_tag("builder")
    if (backends.normalize(effective) == backends.NATIVE
            and backends.hybrid_secondary() is None and cfg is not None):
        effective = getattr(cfg, "model_backend", backends.NATIVE)
    return _BACKEND_TURN_SCALE if backends.normalize(effective) != backends.NATIVE else 1.0


def turns_for(cfg: Config, effort: str) -> int:
    """Max build turns for this effort: the configured base, scaled up for high/max, and again
    (EU-393) for a weak/GLM backend — it burns ~1 tool call/turn, so the same ceiling starves it
    before exploration finishes. NATIVE resolves to a 1.0 backend scale, so Anthropic-tier values
    are unchanged."""
    base = int(getattr(cfg, "builder_max_turns", 60) or 60)
    return max(base, int(base * _TURN_SCALE.get(effort, 1.0) * _backend_turn_scale(cfg)))


def budget_for(cfg: Config, effort: str) -> int:
    """EU-377: the pass's task_budget (tokens of NEW content — model output + tool results read),
    scaled by effort like turns_for, and (EU-393) by the same backend factor — a weak/GLM pass
    still needs the classification seam and only glm/zai/z.ai-aliased backends receive the
    ×_BACKEND_TURN_SCALE budget (registry/unknown ids normalize to NATIVE and get the default
    budget, scale 1.0 — see :func:`_backend_turn_scale`), even though GLM itself currently skips
    task_budget (see the call site, builder.py ~659: the backend won't honour the beta header).
    0 disables (no budget sent).

    Why this and not max_turns alone: the builder's cost is quadratic in turns (fitted over 319
    passes: in_tok ≈ 728·N² + 26,491·N, a 36x replay multiple), and 70% of the 1,455-tok/turn
    context growth is TOOL RESULTS — full suite stdout read in, then re-sent every remaining
    turn. A turn cap can't see that; a task budget counts exactly it. The server shows the model
    a countdown, so it paces itself and lands gracefully instead of grinding to the turn ceiling
    and dying (ceiling runs: 10.4% of passes, 27.7% of builder spend, 24.4% outright failures)."""
    base = int(getattr(cfg, "builder_task_budget", 0) or 0)
    if base <= 0:
        return 0
    # The SDK floor is 20,000; anything lower would be rejected.
    return max(20_000, int(base * _TURN_SCALE.get(effort, 1.0) * _backend_turn_scale(cfg)))


# EU-38: prior_issues/feedback is the single biggest input-token contributor on retries — it grows
# every iteration and is fed back verbatim. Cap it (configurable; keep the NEWEST, which is the most
# relevant review feedback) so a deep ticket on pass 4 doesn't blow past the input-token budget.
_FEEDBACK_MAX_ITEMS = 12       # keep at most this many prior-issue lines (newest)
_FEEDBACK_MAX_CHARS = 6000     # ...and at most this many total chars of feedback
_PREAMBLE_MAX_CHARS = 4000     # trim the unit-memory preamble into the builder prompt if oversized


def _cap_feedback(issues, cfg=None, *, max_items: int | None = None, max_chars: int | None = None) -> list[str]:
    """Cap the prior_issues fed back on retry, keeping the NEWEST items (the latest review's points
    are the ones to fix). Bounds by item count first, then by total chars — both configurable via
    `builder_feedback_max_items` / `builder_feedback_max_chars`, or overridden directly via the
    keyword args (EU-395: callers with their own discipline-but-different ceilings, e.g. the
    cross-run prior-attempts digest, reuse this exact trimming logic without adopting the
    review-feedback config keys). Returns the trimmed list, with a leading marker line when
    anything was dropped so the Builder knows older points were elided."""
    items = [str(i) for i in (issues or [])]
    if not items:
        return []
    max_items = int(max_items if max_items is not None
                     else (getattr(cfg, "builder_feedback_max_items", _FEEDBACK_MAX_ITEMS) or _FEEDBACK_MAX_ITEMS))
    max_chars = int(max_chars if max_chars is not None
                     else (getattr(cfg, "builder_feedback_max_chars", _FEEDBACK_MAX_CHARS) or _FEEDBACK_MAX_CHARS))
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


# EU-267: phrase markers that flag a sentence/line as a caveat/limitation admission — e.g. AUTO-109's
# "the e2e tests have broader environmental issues ... but the implementation is correct and ready
# for use", which sat ~1.8k chars into the summary and was truncated out of diff_digest (capped at 500
# chars) before it ever reached the Reviewer. Matched case-insensitively over the FULL summary text
# (never truncated) so a caveat buried anywhere in a long summary still survives into the artifact.
#
# Two tiers (iteration 2): STRONG markers are unambiguous limitation vocabulary and fire on their own.
# WEAK markers ("however", "not fully", "known issue") are ambiguous in isolation — "However, I also
# updated the README" is not a caveat — so they fire ONLY when the same chunk also carries genuine
# limitation-shaped context (a test/verification/coverage/incompleteness word). This mirrors reviewer
# .py's _RESOLVED_CTX_RE guard: precision over a blunt keyword match, so benign prose isn't flagged.
_CAVEAT_STRONG = re.compile(
    r"\b("
    r"caveat|limitation|unverified|not tested|untested|"
    r"environmental issue|broader\s+\S+\s+issues?|"
    r"ready for use but|edge case not|"
    r"does not cover|out of scope for this|manual verification"
    r")\b",
    re.IGNORECASE,
)
_CAVEAT_WEAK = re.compile(
    r"\b(however|not fully|known issue)\b",
    re.IGNORECASE,
)
# Limitation-shaped context that promotes a WEAK marker to a caveat. Deliberately excludes the bare
# word "issue" (it is part of "known issue" and would self-satisfy) — it looks for something being
# incomplete/unverified/untested/broken, not just any mention.
_CAVEAT_CONTEXT = re.compile(
    r"\b("
    r"tests?|verif\w*|cover\w*|fail\w*|skip\w*|broken|"
    r"limitation|caveat|unresolved|incomplete|todo|missing|"
    r"environment\w*|not\s+\w+"
    r")\b",
    re.IGNORECASE,
)

# Bound the extracted caveats consistently with builder.py's _FEEDBACK_MAX_ITEMS / _FEEDBACK_MAX_CHARS
# pattern, so a pathologically verbose summary can't balloon the reviewer prompt. Keep the FIRST items
# (a summary leads with its most salient caveats) and clip any single over-long entry.
_CAVEAT_MAX_ITEMS = 12
_CAVEAT_MAX_CHARS = 6000
_CAVEAT_ITEM_MAX_CHARS = 1000


def _bound_caveats(items: list[str]) -> list[str]:
    """Cap the extracted caveats by item count, per-item length, and total chars — mirroring the
    _cap_feedback bounding so a verbose summary can't grow the reviewer prompt without limit. Keeps the
    FIRST items (leading caveats are the salient ones) and appends a marker line noting any elision."""
    kept: list[str] = []
    total = 0
    dropped = 0
    for item in items:
        if len(kept) >= _CAVEAT_MAX_ITEMS:
            dropped = len(items) - len(kept)
            break
        clipped = item if len(item) <= _CAVEAT_ITEM_MAX_CHARS else item[: _CAVEAT_ITEM_MAX_CHARS - 1].rstrip() + "…"
        if total + len(clipped) > _CAVEAT_MAX_CHARS and kept:
            dropped = len(items) - len(kept)
            break
        kept.append(clipped)
        total += len(clipped)
    if dropped:
        kept.append(f"(+{dropped} further caveat(s) elided to bound context)")
    return kept


def _caveats(summary: str) -> list[str]:
    """Scan the FULL builder summary (never truncated to 500 chars, unlike diff_digest) for
    caveat/limitation language, returning the matching sentences/lines verbatim. Combines a
    phrase-marker scan (catches free-form admissions anywhere in the text, however deep) with the
    existing ``Caveats:``/``Limitations:`` bullet sections (structured callouts). De-duplicated,
    order-preserving, and bounded (item count + chars). Returns [] when the builder flagged nothing —
    a STRONG marker fires alone, a WEAK marker only alongside limitation context (no false positives)."""
    text = summary or ""
    found: list[str] = []

    # Structured sections first (e.g. "Limitations:\n  - ...").
    for heading in ("caveat", "limitation"):
        found.extend(_section_bullets(text, heading))

    # Free-form phrase scan over the WHOLE text — split into sentences/lines so each hit is reported
    # with enough surrounding context to be useful, not just the bare marker word.
    chunks = re.split(r"(?<=[.!?])\s+|\n+", text)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if _CAVEAT_STRONG.search(chunk):
            found.append(chunk)
        elif _CAVEAT_WEAK.search(chunk) and _CAVEAT_CONTEXT.search(chunk):
            found.append(chunk)

    # De-dupe while preserving first-seen order (a phrase-scan hit may overlap a section bullet).
    seen: set[str] = set()
    deduped: list[str] = []
    for item in found:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return _bound_caveats(deduped)


def _build_artifact(result: BuildResult) -> BuildArtifact:
    """Distil a BuildResult into the typed BuildArtifact handoff (EU-72). ``files_changed`` is left
    empty for the loop to stamp (it owns git); the digest and any Decisions/Open-questions sections
    come from the builder's own summary. ``caveats`` (EU-267) scans the FULL summary — independent of
    the 500-char diff_digest cap — so a caveat buried deep in a long summary still reaches the
    Reviewer."""
    summary = result.summary or result.raw or ""
    return BuildArtifact(
        files_changed=[],
        diff_digest=_digest(summary),
        decisions=_section_bullets(summary, "decision"),
        open_questions=_section_bullets(summary, "open question"),
        caveats=_caveats(summary),
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
    # The up-front design brief (Phase-2 §2 Planner, or the Architect's ADR when the Planner is
    # off). Both flow through req.adr — approach + testable AC + in-scope files / touch-points.
    if req.adr:
        parts += [
            "",
            "DESIGN BRIEF (produced up front — follow this approach):",
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
    # EU-251: a CI-relevant ticket gets an explicit reminder to verify with CI's OWN runner (the
    # package.json `test` script, e.g. `vitest run`) rather than `bun test` — AUTO-112 landed on a
    # verbatim "CI will go green" / "verified with bun test" claim while CI (vitest) was red.
    # Best-effort: should_run() failing (or cfg being unavailable in a caller that doesn't pass one)
    # must never block a build.
    try:
        if cfg is not None:
            from . import ci_conclusion
            if ci_conclusion.should_run(cfg, req.ticket):
                parts += [
                    "",
                    "CI VERIFICATION (this ticket is CI-relevant):",
                    "  Verify with the SAME runner CI uses for this app — read the app's "
                    "package.json `test` script (and .github/workflows/*.yml) and run THAT "
                    "command, not `bun test`. `bun test` and `vitest` are different runners; a "
                    "`bun test` pass does not verify a vitest-run CI job. Do not claim \"CI will "
                    "go green\" or any CI outcome you have not actually observed with CI's own "
                    "command.",
                ]
    except Exception:  # noqa: BLE001 — this reminder is best-effort, never blocks a build
        pass
    parts += ["", "Implement the ticket now."]
    return "\n".join(parts)


async def build(req: BuildRequest, app: AppConfig, cfg: Config, audit=None,
                *, store: PerTicketArtifactStore | None = None,
                spec: SpecArtifact | None = None) -> BuildResult:
    """Implement the ticket with a single focused builder pass.

    Phase-2 §2 (2026-07-06): the Dev Team Lead's build-delegation path (squad-lead planner +
    soldiers + ephemeral-specialist synthesis) was removed; the Builder always builds solo.

    EU-72: ``spec`` is the upstream SpecArtifact (primary context — the builder reads its acceptance
    criteria / non-goals, falling back to the raw ticket when None). After the build, the typed
    BuildArtifact is published into ``store`` so the Reviewer reads a tight handoff instead of
    re-deriving from the full diff. Both default to None so direct/CLI callers are unaffected.
    """
    result = await _solo_build(req, app, cfg, spec=spec)
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
        system_prompt=_trim_preamble(memory.preamble(), cfg) + BUILDER_SYSTEM
                      + (ELITE_METHOD if _elite_squad(cfg, req.ticket) else ""),
        cwd=workdir,                   # the isolated worktree when enabled
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        # Deny the Task/Agent sub-agent tool: allowed_tools does NOT gate it under bypassPermissions,
        # so an un-denied sub-agent could fan out with its own turns outside max_turns — worst here of
        # all officers (Opus ceiling + the highest turn budget). The Builder builds solo (delegation
        # was removed); it never needs to spawn sub-agents. (AUTO-93 reviewer-runaway class fix.)
        disallowed_tools=["Task", "Agent"],
        setting_sources=[],            # no settings files -> no ask/deny gate at any level
        hooks=guard.hooks_config(workdir),  # denylist + EU-188 worktree confinement (no writes outside workdir)
        max_turns=turns_for(cfg, eff),
        effort=eff,
    )
    # EU-377: pace the pass with an API-side task budget (tokens of NEW content — output + tool
    # results read). The model sees a countdown and wraps up gracefully instead of grinding to the
    # turn ceiling and dying. Anthropic-only: GLM/z.ai won't honour the beta header the SDK sends
    # with output_config.task_budget, so a routed-GLM pass keeps today's turn-cap-only behaviour.
    _budget = budget_for(cfg, eff)
    if _budget:
        from . import backends as _backends
        # EU-417: gate on the builder's per-tag/hybrid-RESOLVED backend, not just is_glm(cfg). In
        # hybrid mode (main=opus, secondary=glm) is_glm(cfg) is False but the builder is routed to
        # GLM via current_for_tag('builder'); a tag-blind gate would attach the beta option there.
        if not _backends.is_glm_for_tag("builder", cfg):
            options.task_budget = {"total": _budget}
    # EU-38: tag this build pass in the usage ledger (ticket id + iteration) so per-pass input
    # tokens are sliceable by the ledger-analysis tooling. cfg also bounds the feedback/preamble.
    # Sonnet-cap → one-shot Opus retry for this pass (per-call, no weekly pin — see run_agent_with_fallback)
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
        is_turn_limit=getattr(run, "is_turn_limit", False),  # EU-408: max-turns OR glm_token_ceiling
    )
