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
        max_turns=30,
        effort=normalize_effort(cfg.reviewer_effort),
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
