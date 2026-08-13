"""Product Manager (S-5) — the Elite Unit's product officer.

When a ticket needs a PRODUCT / IA / scope decision the Builder can't make on its own — an ambiguous
requirement, a navigation/structure call, a "which of two reasonable options" choice — the PM decides,
grounded in the ticket, the Unit Memory, and any audit/plan docs in the repo. It makes the EVERYDAY
calls itself so the unit keeps shipping; for a CRITICAL or irreversible call it does NOT decide alone —
it proposes ONE recommended solution for the Commander to approve or reject.

This is the officer that resolves things like AUTO-14's `COMMANDER DECISION` lines: the routine ones it
answers; the product-direction ones it routes to Roman with a recommendation.

  general pm automatixy AUTO-14 "Billing: one parent route or four siblings?"
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

_LOG = logging.getLogger(__name__)

# Matches the mandatory WHY line the PM must include on every ESCALATE verdict.
# The absence of this line on an ESCALATE triggers a coerce-to-DECIDE/RESOLVE with a warning.
_WHY_PATTERN = re.compile(r"WHY\s+PM\s+CANNOT\s+RESOLVE\s*:(.+)", re.IGNORECASE)

from . import filing
from .config import Config
from .contracts import QualityIssue, ReviewResult, Ticket

PM_SYSTEM = """\
You are the PRODUCT MANAGER (S-5) of an elite autonomous software unit, reporting to THE CTO and
ultimately the Commander (Roman). Your job: make the product / IA / scope calls the Builder can't make
alone, so the unit keeps shipping — grounded in the ticket, the repo's own docs (audit / plan / Unit
Memory) and existing conventions. You are READ-ONLY: you read to ground the call, you never edit code.

DECIDE vs ESCALATE — you have strong authority to DECIDE; ESCALATE is the rare exception.

THREE ROUTINE CLASSES you MUST self-resolve — never escalate these:
  1. OUT-OF-SCOPE FINDINGS: a Reviewer finding that predates this ticket or lives outside the diff
     surface → classify it as out-of-scope, file/skip it, and unblock the build. Do NOT escalate.
  2. ITERATION / RETRY / DEPENDENCY ISSUES: the build has looped, a dependency is ambiguous, or the
     Builder is stuck on a corrective path → decide the next concrete action yourself. Do NOT escalate.
  3. ANY PM-REASONABLE QUESTION: naming, grouping/label, copy, reversible structure choices,
     which-of-two-reasonable-options, default values, anything an experienced PM just settles → decide.
     Do NOT escalate.

ESCALATE ONLY when ALL THREE of the following are true:
  (a) the call is CRITICAL or hard to reverse (product direction, pricing/billing semantics, security
      or legal posture, deleting a user-facing capability), AND
  (b) you genuinely CANNOT decide it from the ticket, docs, and existing conventions alone, AND
  (c) the Commander has the specific authority, credential, or context that is actually missing.
  If you can't articulate all three, DECIDE.

Keep it short and concrete; no hedging, no walls of text.

End your reply with EXACTLY one line, nothing after it:
  PM VERDICT: DECIDE
  PM VERDICT: ESCALATE

Above that line:
- DECIDE → lead with ≤3 tight bullets:
    • Decision: <what was decided>
    • Rationale: <one-line why>
    • Action: <the single thing the Builder must do next>
  One sentence of prose may follow if essential context is needed.
- ESCALATE → a BRIEF for the Commander, at most 7 lines, in EXACTLY this shape — no preamble, no
  re-derivation, no quoting the whole ticket:
    WHY PM CANNOT RESOLVE: <one sentence — the specific authority, credential, or irreducible context only the Commander has>
    BLOCKER: <one sentence — what is blocked and why it's his call>
    DECISION: <the single question he must answer>
    OPTIONS:
    1. <first way forward, one line> (RECOMMENDED) — <the one key trade-off>
    2. <second way forward, one line>
    3. <optional third way, one line>
  Number the options exactly like that ("1." / "2." / "3.") and mark EXACTLY ONE with
  "(RECOMMENDED)" — the cockpit renders them as one-click buttons and the Commander can reply
  with just the number from his phone.
  The WHY PM CANNOT RESOLVE line is MANDATORY — omitting it invalidates the escalation.
  The Commander reads this on his phone — if he can't grasp the decision in five seconds, it's too long."""


PM_AUTOMODE = """

AUTOMODE IS ON. The Commander is not available to approve anything right now and wants the unit to keep
moving without stopping for his approval. You MUST DECIDE — do NOT escalate. Make the best REVERSIBLE
call you can, state your assumptions explicitly, and the unit proceeds. Everything lands on DEV (never
production), and the Commander reviews your decisions afterward and can reverse any of them — so bias to
the safest reversible option and document why. Always end with PM VERDICT: DECIDE."""


def _prompt(app_name: str, ticket_id: str, question: str, context: str) -> str:
    parts = [
        f"App: {app_name}    Ticket: {ticket_id}",
        "",
        "PRODUCT QUESTION the Builder is blocked on:",
        question.strip() or "(none given — infer the open product decision from the ticket/docs)",
    ]
    if context.strip():
        parts += ["", "CONTEXT (ticket text / blocker report / relevant excerpt):", context.strip()[:6000]]
    parts += [
        "",
        "Read the ticket and any audit/plan/memory docs you need, then make the call — decide it, or "
        "if it's critical, recommend a solution for the Commander. End with the PM VERDICT line.",
    ]
    return "\n".join(parts)


def parse_verdict(text: str | None, auto_mode: bool = False) -> dict[str, str]:
    """Pure parse of the PM's reply into {verdict, body, raw[, why]}.

    Unclear reply → ESCALATE (fail-safe: ask the Commander rather than auto-decide).

    WHY line (EU-92): every Commander-facing ESCALATE must carry a 'WHY PM CANNOT RESOLVE:' line so
    noise is self-evident; when present it is preserved in the returned dict under 'why'. An explicit
    ESCALATE that OMITS it is left ESCALATE (a genuine can't-decide must never be silently auto-decided
    away) but a warning is logged flagging it as possible noise. The WHY line is *enforced* in the PM
    prompt, and the routine classes are kept off the Commander's desk upstream (prompt + auto-file
    routing) — parse-time coercion would only bury the critical calls this gate exists to surface.

    AUTOMODE: the PM never waits on the Commander — an ESCALATE (or an unclear reply) is coerced to
    DECIDE so the unit keeps moving; the recommendation becomes the decision, logged for review/reversal.

    Unit-testable without an agent.
    """
    raw = (text or "").strip()
    up = raw.upper()
    # Track whether the PM *explicitly* wrote ESCALATE vs the reply being unclear/unparseable.
    explicit_escalate = "PM VERDICT: ESCALATE" in up
    if explicit_escalate:
        verdict = "ESCALATE"
    elif "PM VERDICT: DECIDE" in up:
        verdict = "DECIDE"
    else:
        verdict = "ESCALATE"  # unclear reply → fail-safe: ask the Commander rather than auto-decide

    # Extract the WHY line from *explicit* ESCALATE verdicts (not the unclear fallback above) so the
    # escalation that reaches the Commander carries its one-line justification. A missing WHY line is
    # logged as possible noise but does NOT flip the verdict: coercing a genuine can't-decide into a
    # DECIDE would bury exactly the critical call this gate exists to surface (EU-92). The routine
    # classes are kept off the Commander's desk upstream (PM prompt + auto-file routing), not here.
    why = ""
    if explicit_escalate:
        m = _WHY_PATTERN.search(raw)
        if m:
            why = m.group(1).strip()
        else:
            _LOG.warning(
                "parse_verdict: PM VERDICT: ESCALATE is missing the mandatory "
                "'WHY PM CANNOT RESOLVE' line — surfacing it anyway (possible noise)."
            )

    if auto_mode and verdict == "ESCALATE":
        verdict = "DECIDE"   # automode: decide autonomously, never stop for the approve button

    body = raw.rsplit("PM VERDICT:", 1)[0].strip() if "PM VERDICT:" in up else raw
    result: dict[str, str] = {"verdict": verdict, "body": body or "(the PM gave no detail)", "raw": raw}
    if why:
        result["why"] = why
    return result


PM_TRIAGE_SYSTEM = """You are the Product Manager, called when a ticket has EXHAUSTED its build passes —
the Builder built it several times and the Reviewer kept rejecting it. Before the Commander is bothered,
you triage the run. You are given the latest Builder summary and the Reviewer's outstanding required
changes; you may Read the repo to ground your call.

THREE ROUTINE CLASSES you MUST self-resolve — never escalate these:
  1. OUT-OF-SCOPE REJECTIONS: the remaining Reviewer objections are scope-creep, hygiene, or pre-existing
     issues not introduced by this ticket (unrelated lockfile change, stray file, out-of-scope app
     touched, missing config/CSP line) → TRIAGE: RESOLVE with a precise surgical instruction. Ask the
     Commander NOTHING.
  2. ITERATION / RETRY EXHAUSTION: the ticket looped because the Builder lacked a clear corrective path,
     not because a Commander decision was missing → decide the corrective action and TRIAGE: RESOLVE.
  3. DEPENDENCY / SCOPE AMBIGUITY that an experienced PM can settle from the ticket + docs → decide and
     TRIAGE: RESOLVE. Do NOT escalate.

Judge honestly:
- TRIAGE: RESOLVE when the ticket's CORE deliverable is essentially DONE and the remaining rejections
  fall into one of the three routine classes above. Give ONE precise, surgical instruction: name the
  files and the exact action. Ask the Commander NOTHING.
- TRIAGE: SPLIT when the ticket is genuinely TOO BIG for one build — it spans many files or areas,
  each independently substantial, and no single corrective pass could finish it (repo-wide rename,
  multi-screen feature, cross-cutting refactor). In 1–2 lines say why it's too heavy; the Scrum Master
  will break it into small sub-tickets.
- TRIAGE: ESCALATE ONLY when ALL THREE of the following are true:
    (a) a GENUINE Commander decision remains — a value only he has (real credential, phone number), a
        product/scope call he explicitly reserved, or an irreducibly ambiguous requirement, AND
    (b) you genuinely CANNOT resolve it from the ticket, docs, and existing conventions alone, AND
    (c) you can state in one sentence the specific authority or information only the Commander has.
    If you can't articulate all three, TRIAGE: RESOLVE.
  When escalating, write a tight brief in EXACTLY this shape:
    WHY PM CANNOT RESOLVE: <one sentence — the specific authority or information only the Commander has>
    BLOCKER: <one sentence — what is blocked>
    DECISION: <the single question he must answer>
    OPTIONS:
    1. <first way forward, one line> (RECOMMENDED)
    2. <second way forward, one line>
  Number the options ("1." / "2." / optionally "3.") and mark EXACTLY ONE "(RECOMMENDED)" —
  they render as one-click buttons in the cockpit and a phone reply can be just the number.
  The WHY PM CANNOT RESOLVE line is MANDATORY — omitting it invalidates the escalation.

COMMENT FORMAT (2026-07-23 Commander order — applies to EVERYTHING you write for the ticket,
RESOLVE and ESCALATE alike): the Commander reads these on a phone. Write ONLY tight '- ' bullets in
this order — PROBLEM: (1-2 bullets, plain language), ACTION: (what you did / what you need from him),
RECOMMENDATION: (one bullet). Max ~120 words. NO markdown headings, NO tables, NO code blocks, NO
inline identifiers unless naming the file is the point. Not a story — a hand-off.

Bias strongly to RESOLVE when the work is substantively done and only discipline is missing. Use SPLIT
only when genuinely oversized. Reserve ESCALATE for what truly only the Commander can settle.

End with EXACTLY one line, nothing after it:  TRIAGE: RESOLVE   or   TRIAGE: ESCALATE   or   TRIAGE: SPLIT"""

# EU-42: PM triage can ALSO spot real-but-off-spec issues while judging an exhausted ticket. Give it the
# same out-of-scope findings channel (shared ===TICKETS=== block) so those land in the backlog via
# loop._route_out_of_scope — which reads the PM's full raw reply (parse_triage(...)['raw']) so the block
# survives regardless of where the model places it relative to the TRIAGE verdict line.
PM_TRIAGE_SYSTEM += filing.ticket_block_rule_for("PM")


def parse_triage(text: str | None) -> dict[str, str]:
    """Parse the PM's triage reply into {action: RESOLVE|ESCALATE|SPLIT, text, raw[, why]}.

    Unclear reply → ESCALATE (fail-safe: ask the Commander rather than silently auto-resolve).

    WHY line (EU-92): every TRIAGE: ESCALATE must carry a 'WHY PM CANNOT RESOLVE:' line so noise is
    self-evident; when present it is preserved in the returned dict under 'why'. An explicit ESCALATE
    that omits it is left ESCALATE (a genuine can't-decide is never silently auto-resolved) but a
    warning flags it as possible noise. The PM prompt enforces the line and self-resolves the routine
    classes upstream, so they don't reach this verdict as escalations at all.

    `text` is the human-facing instruction with the TRIAGE verdict line AND any EU-42 out-of-scope
    ===TICKETS=== block stripped (so a RESOLVE comment never carries the machine block); `raw` keeps the
    full reply so the loop can route that findings block to the backlog separately.
    """
    raw = (text or "").strip()
    up = raw.upper()
    # Track whether the PM *explicitly* wrote ESCALATE vs the reply being unclear/unparseable.
    explicit_escalate = "TRIAGE: ESCALATE" in up
    if "TRIAGE: SPLIT" in up:
        action = "SPLIT"
    elif "TRIAGE: RESOLVE" in up:
        action = "RESOLVE"
    elif explicit_escalate:
        action = "ESCALATE"
    else:
        action = "ESCALATE"  # unclear reply → fail-safe: ask the Commander rather than auto-resolve

    # Extract the WHY line from *explicit* ESCALATE verdicts (not the unclear fallback above) so the
    # brief that reaches the Commander carries its justification. A missing WHY line is logged as
    # possible noise but does NOT flip the action to RESOLVE: silently auto-resolving a genuine
    # can't-decide is the failure mode EU-92 guards against. Routine classes are self-resolved upstream.
    why = ""
    if explicit_escalate:
        m = _WHY_PATTERN.search(raw)
        if m:
            why = m.group(1).strip()
        else:
            _LOG.warning(
                "parse_triage: TRIAGE: ESCALATE is missing the mandatory "
                "'WHY PM CANNOT RESOLVE' line — surfacing it anyway (possible noise)."
            )

    # Drop the out-of-scope findings block (routed separately from `raw`) before isolating the
    # instruction, then trim the trailing TRIAGE verdict line.
    _proposals, clean = filing.parse_tickets(raw)
    body = clean.rsplit("TRIAGE:", 1)[0].strip() if "TRIAGE:" in clean.upper() else clean
    result: dict[str, str] = {"action": action, "text": body or "(the PM gave no detail)", "raw": raw}
    if why:
        result["why"] = why
    return result


async def triage(cfg: Config, app_name: str, ticket_id: str, last_build: str = "",
                 rejections: list[str] | None = None) -> dict[str, str]:
    """Consulted when a ticket exhausts its passes. Returns {action: RESOLVE|ESCALATE, text}. RESOLVE ->
    the loop re-queues the ticket with `text` as a corrective instruction; ESCALATE -> park with `text`
    as the Commander brief."""
    app = cfg.app(app_name)
    from . import recon, models
    task = "\n".join([
        f"Ticket {ticket_id} exhausted its build passes ({cfg.max_iterations}). Decide RESOLVE vs ESCALATE.",
        "", "Latest Builder summary:", (last_build or "(none)")[:1500],
        "", "Reviewer's outstanding required changes:",
        ("\n".join(f"- {c}" for c in (rejections or [])[:12]) or "(none recorded)"),
    ])
    # EU-52: route the PM through the ladder instead of pinning Opus — high effort holds the ceiling,
    # conserves under a tight budget, and falls back to the configured model when auto_model is off.
    model, mreason = models.for_officer(cfg, effort="high")
    if getattr(cfg, "auto_model", False):
        print(f"  · pm model: {mreason}", flush=True)
    report = await recon.run_officer(
        officer="pm", label="Product Manager", system=PM_TRIAGE_SYSTEM,
        task=task, cfg=cfg, cwd=app.repo_path, model=model,
        soldier_tools=["Read", "Grep", "Glob"], max_turns=14, effort="high",
        empty="TRIAGE: ESCALATE")
    return parse_triage(report)


# --------------------------------------------------------------------------- #
# EU-90: PM findings triage — classify reviewer quality_issues by scope
# --------------------------------------------------------------------------- #

@dataclass
class FindingsTriage:
    """PM classification of a Reviewer's quality_issues by scope.

    in_scope   — defects introduced (or worsened) by THIS ticket's change; the Builder must fix
                 these before the ticket can ship.
    out_of_scope — pre-existing / unrelated issues that were not caused by this PR; the unit
                   auto-files them as linked backlog tickets so they are not lost.
    decisions  — genuine product/scope ambiguities that only the Commander can settle; escalated
                 via the normal needs_human path.
    """
    in_scope: list[QualityIssue] = field(default_factory=list)
    out_of_scope: list[dict] = field(default_factory=list)   # dicts: {severity, area, detail}
    decisions: list[QualityIssue] = field(default_factory=list)


PM_FINDINGS_SYSTEM = """\
You are the PRODUCT MANAGER classifying a Reviewer's quality findings after a build review.

Your task: for each finding in the numbered QUALITY ISSUES list, decide the bucket:

  in_scope   — the finding is a defect INTRODUCED or WORSENED by this ticket's change.
               Evidence: the diff touches the file/path where the issue lives.
               The Builder must fix these in the current iteration.

  out_of_scope — the finding is a pre-existing bug, technical debt, or unrelated issue that
                 existed BEFORE this ticket and was NOT caused by it.
                 These are auto-filed as linked backlog tickets; do NOT block the current ticket.

  decision   — a genuine product/scope ambiguity the Commander must settle (a missing acceptance
               criterion, a conflicting requirement, a call only he can make). NOT for ordinary
               code defects — do not abuse this bucket to avoid work.

Classification rules:
1. Only classify as in_scope if the diff evidence shows this ticket CAUSED or WORSENED the issue —
   i.e. the finding lives in a path listed under CHANGED FILES, or the DIFF EXCERPT shows this
   change introduced it.
2. Classify as out_of_scope when the code/pattern predates this PR, or the file the finding is
   about is NOT in CHANGED FILES and the DIFF EXCERPT does not touch it.
3. Classify as decision only for irreducible requirement conflicts or missing AC values.
4. When in doubt, prefer in_scope — the Builder always gets at least one retry.
5. Every finding must appear in exactly one bucket.

You are given:
  TICKET SUMMARY + ACCEPTANCE CRITERIA — what the ticket was supposed to change.
  CHANGED FILES                         — the repo-relative paths THIS ticket's diff touched
                                          (the authoritative in-scope surface).
  DIFF EXCERPT                          — a bounded excerpt of the actual diff under review.
  REVIEWER SUMMARY                      — the Reviewer's prose synopsis, for context only.
  QUALITY ISSUES                        — the Reviewer's findings, numbered 0-based.

Respond with a fenced ```json block and NOTHING after it:
```json
{
  "in_scope":    [<0-based indices>],
  "out_of_scope": [<0-based indices>],
  "decisions":   [<0-based indices>]
}
```
Any index not listed is treated as in_scope by the caller (fail-safe)."""


# EU-90: cap the diff excerpt handed to the classifier so a huge diff can't blow the prompt budget.
# The classifier needs enough of the diff to see WHICH paths changed and roughly what — not the whole
# patch. The authoritative changed-file list is passed separately and is never truncated.
_DIFF_EXCERPT_LIMIT = 4000


def _findings_prompt(review: ReviewResult, ticket: Ticket,
                     diff: str = "", files_changed: list[str] | None = None) -> str:
    """Build the PM findings-triage task prompt from a ReviewResult and Ticket.

    EU-90: the in-scope vs out-of-scope split needs REAL diff evidence, not the reviewer's prose
    synopsis (which carries no path information). The loop owns git, so it passes:
      * ``files_changed`` — the authoritative repo-relative paths this ticket's diff touched, and
      * ``diff``          — the actual diff under review (bounded to ``_DIFF_EXCERPT_LIMIT`` here).
    The reviewer summary is still included, but only as secondary context."""
    ac = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(ticket.acceptance_criteria)) or "  (none)"
    issues_block = "\n".join(
        f"  [{i}] ({q.severity}/{q.area}) {q.detail}"
        for i, q in enumerate(review.quality_issues)
    ) or "  (none)"
    files_block = "\n".join(f"  - {p}" for p in (files_changed or [])) or "  (changed-file list unavailable)"
    diff_excerpt = (diff or "").strip()
    if not diff_excerpt:
        diff_excerpt = "(diff unavailable — fall back to CHANGED FILES and the reviewer summary)"
    elif len(diff_excerpt) > _DIFF_EXCERPT_LIMIT:
        diff_excerpt = diff_excerpt[:_DIFF_EXCERPT_LIMIT] + "\n… (diff truncated — see CHANGED FILES for the full surface)"
    return "\n".join([
        f"TICKET: {ticket.id} — {ticket.summary}",
        "",
        "ACCEPTANCE CRITERIA:",
        ac,
        "",
        "CHANGED FILES (the paths THIS ticket's diff touched — the in-scope surface):",
        files_block,
        "",
        "DIFF EXCERPT (the actual diff under review, bounded):",
        "```diff",
        diff_excerpt,
        "```",
        "",
        "REVIEWER SUMMARY (prose synopsis, context only):",
        f"  {(review.summary or '(none)')[:600]}",
        "",
        "QUALITY ISSUES (classify each by index):",
        issues_block,
        "",
        "Classify every finding now and emit the JSON verdict.",
    ])


def _parse_findings_verdict(text: str, issues: list[QualityIssue]) -> FindingsTriage:
    """Parse the PM's findings-triage JSON from a fenced ```json block.

    On ANY parse failure the function falls back to treating ALL findings as
    in_scope — this guarantees the Builder loop always has at least one retry
    and no finding silently disappears when the model output is malformed.
    """
    fences = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    data: dict | None = None
    for cand in reversed(fences):
        try:
            data = json.loads(cand)
            break
        except json.JSONDecodeError:
            continue

    # Fail-safe: unparseable → all findings in_scope so the Builder gets a retry.
    if data is None or not isinstance(data, dict):
        return FindingsTriage(in_scope=list(issues), out_of_scope=[], decisions=[])

    def _valid_indices(key: str) -> list[int]:
        """Return valid 0-based indices from a JSON list field, silently dropping bad values."""
        raw = data.get(key) or []
        return [i for i in raw if isinstance(i, int) and 0 <= i < len(issues)]

    in_idx = _valid_indices("in_scope")
    out_idx = _valid_indices("out_of_scope")
    dec_idx = _valid_indices("decisions")

    classified = set(in_idx) | set(out_idx) | set(dec_idx)
    # Any finding the model omitted falls back to in_scope (no silent drops).
    unclassified = [i for i in range(len(issues)) if i not in classified]

    return FindingsTriage(
        in_scope=[issues[i] for i in in_idx] + [issues[i] for i in unclassified],
        out_of_scope=[
            {"severity": issues[i].severity, "area": issues[i].area, "detail": issues[i].detail}
            for i in out_idx
        ],
        decisions=[issues[i] for i in dec_idx],
    )


async def triage_findings(review: ReviewResult, ticket: Ticket, cfg: "Config",
                          diff: str = "", files_changed: list[str] | None = None) -> FindingsTriage:
    """Classify a Reviewer's quality_issues into in-scope fixes, out-of-scope backlog items,
    and Commander decisions.

    Called by the auto-route layer (EU-90) after a FAIL review to separate:
    - findings the Builder must fix NOW (in_scope),
    - pre-existing issues to auto-file as linked tickets (out_of_scope),
    - genuine product ambiguities to escalate to the Commander (decisions).

    On LLM parse failure all findings are treated as in_scope so the Builder loop
    always receives at least one actionable retry rather than silently stalling.

    Args:
        review:        The Reviewer's ReviewResult for the current iteration.
        ticket:        The Ticket being built (provides acceptance criteria + scope context).
        cfg:           Pipeline Config (used for model selection and app lookup).
        diff:          The actual diff under review — the real evidence for the in-scope vs
                       out-of-scope split (EU-90). Bounded inside ``_findings_prompt``.
        files_changed: The authoritative repo-relative paths this ticket's diff touched
                       (the loop owns git and passes ``store.build.files_changed``).

    Returns:
        FindingsTriage with findings distributed across the three buckets.
    """
    from . import recon, models

    if not review.quality_issues:
        return FindingsTriage(in_scope=[], out_of_scope=[], decisions=[])

    app = cfg.app(ticket.app or cfg.apps[0].name)
    model, mreason = models.for_officer(cfg, effort="medium")
    if getattr(cfg, "auto_model", False):
        print(f"  · pm findings-triage model: {mreason}", flush=True)

    task = _findings_prompt(review, ticket, diff, files_changed)
    raw = await recon.run_officer(
        officer="pm", label="PM Findings Triage",
        system=PM_FINDINGS_SYSTEM,
        task=task,
        cfg=cfg, cwd=app.repo_path, model=model,
        soldier_tools=["Read", "Grep", "Glob"],
        max_turns=10, effort="medium",
        empty="",
    )
    return _parse_findings_verdict(raw or "", review.quality_issues)


async def review(cfg: Config, app_name: str, ticket_id: str, question: str, context: str = "") -> dict[str, str]:
    """Run the PM officer on one product question. Returns {verdict: DECIDE|ESCALATE, body, raw}.
    Pure decision — the caller (CLI or the loop) acts on the verdict (resume the build, or file a
    Commander decision). In automode the PM is told to decide and the verdict is coerced to DECIDE."""
    app = cfg.app(app_name)
    auto = bool(getattr(cfg, "auto_mode", False))
    from . import recon, models
    # Read-only product review. With delegation armed, the PM decides for itself whether to recruit
    # soldiers (a slice of the console each) and synthesize the call, else a single solo pass. Either
    # way the reply ends with the PM VERDICT line that parse_verdict reads.
    # EU-52: honor auto_model — sized off effort, conserved under budget, ceiling when the ladder is off.
    model, mreason = models.for_officer(cfg, effort="high")
    if getattr(cfg, "auto_model", False):
        print(f"  · pm model: {mreason}", flush=True)
    report = await recon.run_officer(
        officer="pm", label="Product Manager",
        system=PM_SYSTEM + (PM_AUTOMODE if auto else ""),
        task=_prompt(app_name, ticket_id, question, context),
        cfg=cfg, cwd=app.repo_path, model=model,
        soldier_tools=["Read", "Grep", "Glob"], max_turns=18, effort="high",
        empty="(the PM gave no answer)")
    return parse_verdict(report, auto_mode=auto)
