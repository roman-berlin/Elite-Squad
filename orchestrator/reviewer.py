"""Reviewer driver — a second, READ-ONLY Claude that judges the diff.

Read-only is enforced at the permission layer (disallowed_tools covers every
mutating tool), not by prompt alone: the reviewer can read the repo for context
but physically cannot edit it. It judges BOTH spec conformance and quality.
"""
from __future__ import annotations

import json
import re

from claude_agent_sdk import ClaudeAgentOptions

from . import filing, memory
from .agent import run_agent, run_agent_with_fallback
from .config import AppConfig, Config, normalize_effort
from .contracts import (BuildArtifact, PerTicketArtifactStore, QualityIssue,
                       ReviewResult, ReviewVerdict, Ticket, Verdict)

REVIEWER_SYSTEM = """\
You are the Reviewer in an automated dev pipeline. You are deliberately adversarial:
your job is to catch problems before they ship, not to be agreeable.

You judge a diff on TWO axes:
1. Spec conformance — does it satisfy every acceptance criterion of the ticket?
2. Quality — architecture, correctness, security, error handling, edge cases,
   tests, and consistency with the codebase, PLUS regressions, scope creep, and
   resource/memory leaks (unclosed DB/network handles, dangling listeners, tests
   that don't clean up in afterEach/afterAll).

Engage your specialist audit lenses on the diffs that warrant them (skip the rest):
- Security (Security Engineer): authz, secret leakage, input validation, injection, unsafe queries → area "security".
- Accessibility: UI changes — keyboard/focus, ARIA, contrast, labels, RTL → area "a11y".
- Performance: N+1 queries, hot loops, render thrash, unbounded memory → area "performance".
- Test-coverage: is the changed behaviour actually tested? → area "tests".
Report each finding as a quality issue with the matching "area".

You may read files in the repo for context, but you cannot modify anything.

You MUST end your response with a single fenced ```json block, and nothing after it,
matching exactly this schema:

```json
{
  "verdict": "PASS" | "FAIL",
  "spec_conformance": { "met": true, "gaps": ["..."] },
  "quality": { "issues": [ { "severity": "blocker|major|minor", "area": "...", "detail": "..." } ] },
  "required_changes": ["concrete instruction for the builder if FAIL"],
  "needs_human": false,
  "question": "",
  "summary": "≤5 tight bullets — lead with each blocking issue and what to fix, then a one-sentence overall rationale. Example: '• Missing tenant filter on /leads query\\n• No test for the error path\\n• Passes otherwise.'"
}
```

Rules for the verdict:
- PASS only if spec_conformance.met is true AND there are no blocker or major issues.
- Otherwise FAIL, and required_changes must be specific enough to act on directly.
- Set "needs_human": true with a clear "question" ONLY when the work is blocked on a
  product/scope DECISION the Commander must make (ambiguous requirement, a trade-off, a
  missing acceptance criterion) — not for ordinary code fixes. When needs_human is true,
  set verdict to FAIL.
"""

REVIEWER_SYSTEM += """
EU-249 hard rule — admitted-red tests are ALWAYS blocking: if the Builder's own handoff (the
diff_digest/decisions/open_questions you were given) admits that a test is failing, was skipped, or
hit a "test infrastructure issue" it didn't resolve, you MUST record that as a blocker/major quality
issue (area "tests") and the verdict CANNOT be PASS. Do not let confident prose about the rest of the
diff talk you into passing a self-reported red or skipped test — a diff whose own author says the
tests didn't run/pass ships nothing verified, regardless of how the rest of the diff reads.
"""

# EU-42: give the Reviewer the out-of-scope findings channel. A real-but-off-spec issue it notices
# while judging the diff (a bug/risk outside THIS ticket's scope) is emitted as the shared
# ===TICKETS=== block, which loop._route_out_of_scope parses off review.raw and routes into the
# backlog (de-duped, labeled out-of-scope) instead of letting it evaporate. Reuses filing.py — the
# same machine block the QA/Security/Release officers already use.
REVIEWER_SYSTEM += filing.TICKET_BLOCK_RULE


def _prompt(diff: str, ticket: Ticket, build_artifact: BuildArtifact | None = None) -> str:
    ac = "\n".join(f"  - {c}" for c in ticket.acceptance_criteria) or "  (none specified)"
    parts = [
        f"TICKET {ticket.id}: {ticket.summary}",
        "",
        "DESCRIPTION:",
        ticket.description or "(none)",
        "",
        "ACCEPTANCE CRITERIA:",
        ac,
    ]
    # EU-72: lead with the Builder's structured handoff (read it first), THEN the full diff below —
    # artifact-first, full-context-on-demand. Falls back to the diff alone when no artifact was passed.
    if build_artifact is not None:
        parts.append("")
        parts.append("BUILDER'S STRUCTURED HANDOFF (read first, then verify against the diff):")
        if build_artifact.files_changed:
            parts.append("  files changed: " + ", ".join(build_artifact.files_changed))
        if build_artifact.diff_digest:
            parts.append("  summary: " + build_artifact.diff_digest)
        if build_artifact.decisions:
            parts.append("  decisions: " + "; ".join(build_artifact.decisions))
        if build_artifact.open_questions:
            parts.append("  open questions: " + "; ".join(build_artifact.open_questions))
    parts += [
        "",
        "DIFF UNDER REVIEW (feature branch vs base):",
        "```diff",
        diff if diff.strip() else "(empty diff — the builder produced no changes)",
        "```",
        "",
        "Review it now. Read any files you need for context, then emit the JSON verdict.",
    ]
    return "\n".join(parts)


# EU-249: a deterministic backstop for the AUTO-97 failure mode — the Builder's OWN handoff admitted
# "test files were created but encountered test infrastructure issues with localStorage mocking" and
# the LLM reviewer still emitted PASS with blocking:0. The system-prompt rule above is the primary
# defence; this pair of regexes is the safety net that doesn't depend on the model noticing, so it
# can't silently regress.
#
# Iteration-2 narrowing: the v1 regex was too broad — it flagged ordinary GREEN summaries like
# "Tests: 23 passed, 0 failed." and explicitly RESOLVED failures like "Fixed the failing test, now
# green", bouncing perfectly good diffs. The detector now fires only on a genuinely UNRESOLVED
# admission and deliberately ignores negation/resolution contexts:
#   • STRONG signals fire unconditionally — an unambiguous unresolved admission (test infra issue,
#     "still failing", "not passing", "couldn't get … to pass", "had to skip", "never ran").
#   • WEAK signals (a bare "failing/broken/skipped … test") fire ONLY when the same field carries NO
#     negation/resolution context ("no", "0", "zero", "none", "fixed", "resolved", "now pass/green",
#     "all tests pass", "nothing broke") — so "0 failed", "No tests failed", "Fixed the failing test,
#     now green" and friends stay PASS.
# A false positive costs one extra builder pass; a false negative ships an unverified diff — but the
# earlier over-broad version was itself parking valid diffs, so precision here IS the fix.
_UNRESOLVED_STRONG_RE = re.compile(
    r"(?i)("
    r"\btest(?:s|ing)?\s+infra(?:structure)?\b"                          # "test infrastructure issues"
    r"|\binfra(?:structure)?\s+issues?\b[^.\n]{0,40}\btests?\b"
    r"|\btests?\b[^.\n]{0,40}\binfra(?:structure)?\s+issues?\b"
    r"|\bstill\s+(?:fail\w*|broken|red|not\s+pass\w*)\b"                 # "still failing"
    r"|\bnot\s+passing\b|\bnot\s+green\b"
    r"|\b(?:won'?t|can'?t|cannot|couldn'?t|could\s+not|unable\s+to)\b[^.\n]{0,40}\bpass\w*\b"
    r"|\bhad\s+to\s+skip\b"
    r"|\btests?\b[^.\n]{0,40}\bnever\s+(?:ran|passed|run)\b"
    r")"
)
# A bare "failing/broken/skipped … test" — ambiguous on its own (could be "Fixed the failing test").
_WEAK_RED_TEST_RE = re.compile(
    r"(?i)("
    r"\b(?:fail(?:ing|ed|s)?|broken)\b[^.\n]{0,30}\btests?\b"
    r"|\btests?\b[^.\n]{0,30}\b(?:fail(?:ing|ed|s)?|broken)\b"
    r"|\bskip(?:ped|ping|s)?\b[^.\n]{0,30}\btests?\b"
    r"|\btests?\b[^.\n]{0,30}\bskip(?:ped|ping|s)?\b"
    r")"
)
# Negation / resolution wording that clears a WEAK signal (the failure was reported as GONE).
_RESOLVED_CTX_RE = re.compile(
    r"(?i)("
    r"\bfixed\b|\bresolved\b|\bcorrected\b"
    r"|\bnow\s+(?:pass\w*|green|work\w*)\b"
    r"|\ball\s+tests?\s+(?:pass\w*|green)\b"
    r"|\bnothing\s+(?:broke\w*|fail\w*)\b"
    r"|\bno\b|\bnone\b|\bzero\b|\b0\b"
    r")"
)


def _admitted_red_test_note(build_artifact: BuildArtifact | None) -> str | None:
    """The offending sentence if the Builder's handoff (diff_digest/decisions/open_questions) admits
    a genuinely UNRESOLVED failing/skipped/broken test, else None. A STRONG signal fires on its own;
    a WEAK ("failing … test") signal fires only absent negation/resolution context in the same field
    — so ordinary green summaries and already-fixed failures are not flagged (EU-249 iteration 2)."""
    if build_artifact is None:
        return None
    fields = ([build_artifact.diff_digest] + list(build_artifact.decisions or [])
             + list(build_artifact.open_questions or []))
    for text in fields:
        if not text:
            continue
        if _UNRESOLVED_STRONG_RE.search(text):
            return text.strip()
        if _WEAK_RED_TEST_RE.search(text) and not _RESOLVED_CTX_RE.search(text):
            return text.strip()
    return None


def _enforce_admitted_red_tests(result: ReviewResult, build_artifact: BuildArtifact | None) -> ReviewResult:
    """EU-249: force FAIL (with a recorded blocker) when the Builder's own handoff admits a failing,
    skipped, or infrastructure-broken test — even if the LLM verdict said PASS with no blocking
    issues. A self-reported red/skipped test is categorically blocking, not prose the reviewer's
    overall impression can wave through (the exact AUTO-97 shape)."""
    note = _admitted_red_test_note(build_artifact)
    if note is None:
        return result
    if result.verdict != Verdict.PASS and result.blocking_issues:
        return result   # already failing on this diff for another reason — nothing to force
    forced = QualityIssue(
        severity="blocker", area="tests",
        detail=("Builder's own handoff admits a failing/skipped/broken test: "
                f"\"{note[:300]}\" — a self-reported red or skipped test is an automatic blocking "
                "finding (EU-249) and cannot ship PASS."),
    )
    result.quality_issues = list(result.quality_issues) + [forced]
    result.required_changes = list(result.required_changes) + [
        "Fix the failing/skipped test admitted in the build handoff (or remove the dead test) and "
        "confirm it actually runs green before resubmitting."
    ]
    result.verdict = Verdict.FAIL
    return result


def _classify_diff(diff: str) -> tuple[str, str]:
    """Classify a diff as 'trivial' or 'production' based on size and content.

    Returns (category, reason) where category is 'trivial' or 'production'.
    Trivial: tests-only changes AND <30 total changed lines.
    Production: any .tsx/.py production file changed, OR ≥30 lines, OR mixed changes.
    """
    if not diff or not diff.strip():
        return ("trivial", "empty diff")

    # Check for production file changes (.tsx or .py files, excluding test files)
    # Production files are those not under /tests/ or with names not ending in _test.py
    has_production_change = False
    production_reason = ""

    for line in diff.split("\n"):
        # Look for file paths in diff headers (e.g., "+++ b/src/components/Button.tsx")
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            file_path = line.split()[1] if len(line.split()) > 1 else ""
            # Check if it's a production file
            is_tsx = file_path.endswith(".tsx")
            is_py = file_path.endswith(".py")
            is_test = "/tests/" in file_path or file_path.endswith("_test.py")

            if (is_tsx or is_py) and not is_test:
                has_production_change = True
                production_reason = f"{'.tsx' if is_tsx else '.py'} production file"
                break

    # Count changed lines (lines starting with + or -)
    changed_lines = sum(1 for line in diff.split("\n") if line.startswith("+") or line.startswith("-"))

    # Classification logic
    if has_production_change:
        return ("production", production_reason)
    if changed_lines >= 30:
        return ("production", "≥30 lines")
    return ("trivial", "tests-only, <30 lines")


def _effort_for_diff(category: str, cfg: Config) -> tuple[str, int]:
    """Return (effort, max_turns) based on diff category.

    Trivial diffs → low effort, max_turns ≤ 10 (fast conformance check).
    Production diffs → cfg.reviewer_effort, max_turns = 30 (full-depth review).
    """
    if category == "trivial":
        return ("low", 10)
    return (normalize_effort(cfg.reviewer_effort), 30)


async def review(diff: str, ticket: Ticket, app: AppConfig, cfg: Config, iteration: int = 1,
                 *, store: PerTicketArtifactStore | None = None,
                 build_artifact: BuildArtifact | None = None) -> ReviewResult:
    from . import models, provider as _provider
    # EU-72: read the Builder's BuildArtifact (passed by the loop, or from the shared pool) as the
    # primary handoff; the full diff is still under review below. After parsing, publish a typed
    # ReviewVerdict into the pool for the next iteration / audit. Both default None so direct callers
    # are unaffected.
    if build_artifact is None and store is not None:
        build_artifact = store.build
    # EU-52: thread the build iteration so the Reviewer escalates one tier per re-review — a small diff
    # is judged on Sonnet on pass 1 and on Opus when a rebuilt diff comes back (for_reviewer climbs a
    # tier per retry). Without this the iteration>1 escalation branch was dead and every review pinned
    # the ceiling. Defaults to 1 so direct/CLI callers are unaffected.
    rmodel, rreason = models.for_reviewer(cfg, diff, iteration)   # ceiling unless auto_model is on
    if getattr(cfg, "auto_model", False):
        print(f"  · reviewer model: {rreason}", flush=True)

    # EU-198: scale effort/turns based on diff size + nature
    diff_category, diff_reason = _classify_diff(diff)
    review_effort, review_turns = _effort_for_diff(diff_category, cfg)
    print(f"  · reviewer: {diff_category} diff → {review_effort} effort, {review_turns} turns ({diff_reason})", flush=True)

    options = ClaudeAgentOptions(
        model=rmodel,
        system_prompt=memory.preamble() + REVIEWER_SYSTEM,
        cwd=app.workdir or app.repo_path,   # the isolated worktree when enabled
        permission_mode="bypassPermissions",   # read-only audit; runs unattended in the build loop —
        allowed_tools=["Read", "Grep", "Glob"], # must never dead-stop on a tool prompt mid-cycle
        # Task/Agent (sub-agent spawn) is denied: allowed_tools does NOT gate it under bypassPermissions,
        # and a sub-agent burns its OWN turns outside this max_turns cap — the 2026-07-08 AUTO-93 review
        # ran ~75 min fanning out on a trivial diff. Denied at every officer site (class fix); a read-only
        # judge reasons over the diff, it never needs to delegate.
        disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash", "Task", "Agent"],
        setting_sources=["project"],
        max_turns=review_turns,
        effort=review_effort,
    )
    # Sonnet-cap → one-shot Opus retry for this pass (per-call, no weekly pin — see run_agent_with_fallback)
    # EU-174: determine routing tier based on task characteristics
    routing_tier = None
    try:
        from . import routing as _routing
        if _routing.is_routing_enabled():
            ticket_desc = ticket.description or ""
            ticket_size = ticket.size or ""
            effort = normalize_effort(cfg.reviewer_effort)
            tier = _routing.classify_task(
                ticket_description=ticket_desc,
                task_type="review",
                effort=effort,
                size=ticket_size,
            )
            routing_tier = tier.value
            if routing_tier == "local":
                print(f"      · routing → Tier 1 (Local Ollama)", flush=True)
            else:
                print(f"      · routing → Tier 2 (Cloud)", flush=True)
    except Exception:  # noqa: BLE001 — routing failure must not break the review
        routing_tier = None

    run = await run_agent_with_fallback(_prompt(diff, ticket, build_artifact), options, tag="reviewer", cfg=cfg, routing_tier=routing_tier)

    result = _parse(run.final or run.text)
    result = _enforce_admitted_red_tests(result, build_artifact)   # EU-249 deterministic backstop
    result.cost_usd = run.cost_usd
    result.raw = run.final
    result.input_tokens = getattr(run, "input_tokens", 0)   # EU-96: expose for per-officer burn tracking
    result.output_tokens = getattr(run, "output_tokens", 0)  # getattr-guarded: stubs may omit these
    result.provider = getattr(run, "provider", "")         # EU-123: which provider served this run
    result.model_version = getattr(run, "model_version", "") # EU-123: clean model identifier
    # EU-123: show actual provider+model in the live feed
    if getattr(cfg, "auto_model", False):
        display = _provider.format_provider_model(run.provider, run.model_version)
        print(f"  · reviewer · {display}", flush=True)
    # EU-72: publish the typed ReviewVerdict into the shared pool — a small, auditable record of the
    # verdict + blockers the next iteration reads (the rich ReviewResult keeps its own enum/parser
    # surface for the loop's decision logic).
    if store is not None:
        store.put(ReviewVerdict(
            verdict=result.verdict,
            blocking=[q.detail for q in result.blocking_issues] + list(result.spec_gaps),
            notes=[q.detail for q in result.quality_issues if q.severity == "minor"],
        ))
    return result


def _parse(text: str) -> ReviewResult:
    """Extract the JSON verdict. Fails safe to FAIL if it can't be parsed."""
    data = _extract_json(text)
    if data is None:
        return ReviewResult(
            verdict=Verdict.FAIL,
            spec_met=False,
            summary="Could not parse reviewer verdict; failing safe.",
            required_changes=["Reviewer output was unparseable; re-run review."],
            raw=text,
            parse_failed=True,
        )
    spec = data.get("spec_conformance", {}) or {}
    quality = (data.get("quality", {}) or {}).get("issues", []) or []
    issues = [
        QualityIssue(
            severity=str(q.get("severity", "minor")).lower(),
            area=str(q.get("area", "")),
            detail=str(q.get("detail", "")),
        )
        for q in quality
    ]
    verdict = Verdict.PASS if str(data.get("verdict", "")).upper() == "PASS" else Verdict.FAIL
    return ReviewResult(
        verdict=verdict,
        spec_met=bool(spec.get("met", False)),
        spec_gaps=list(spec.get("gaps", []) or []),
        quality_issues=issues,
        required_changes=list(data.get("required_changes", []) or []),
        needs_human=bool(data.get("needs_human", False)),
        question=str(data.get("question", "")),
        summary=str(data.get("summary", "")),
    )


def _extract_json(text: str) -> dict | None:
    # Prefer a fenced ```json block; fall back to the last {...} span.
    fences = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = list(fences)
    if not candidates:
        brace = re.findall(r"(\{.*\})", text, re.DOTALL)
        candidates = brace[-1:] if brace else []
    for cand in reversed(candidates):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None
