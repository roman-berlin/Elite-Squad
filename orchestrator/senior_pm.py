"""Senior PM / Chief-of-Staff (S-5 senior) — the Elite Unit's senior product officer.

When a ticket needs routine triage BEFORE it reaches the Builder — answering questions that don't
require a build, closing tickets that are duplicates/invalid, or re-filing misrouted tickets — the
Senior PM handles it autonomously. This is the pre-build triage layer that keeps noise off the
Builder's desk and ensures only well-formed, actionable tickets reach the build loop.

The Senior PM operates READ-ONLY on tickets and repo docs. It doesn't write code or land changes;
it classifies tickets and routes them correctly. Its three verdicts:
  - ANSWER: the ticket is a question answerable from docs/context; the Senior PM replies and closes.
  - CLOSE: the ticket is invalid, duplicate, or out of scope; close with a clear reason.
  - REFILE: the ticket is misrouted or needs reformulation; re-file as a new ticket with guidance.

This officer runs BEFORE the Builder loop (pre-build triage), not after exhaustion like the PM.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

_LOG = logging.getLogger(__name__)

# Matches the mandatory CITATION lines the Senior PM must include for every decision.
# Each citation follows the pattern: CITATION: <source> - <what was cited>.
# Uses \s+-\s+ to properly handle sources with spaces (e.g., ".claude/rules/tenant-isolation.md").
_CITATION_PATTERN = re.compile(r"CITATION:\s*(.+?)\s+-\s+(.+)", re.IGNORECASE)

from . import filing
from .config import Config
from .contracts import Ticket

SENIOR_PM_SYSTEM = """\
You are the SENIOR PRODUCT MANAGER / CHIEF-OF-STAFF (S-5 senior) of an elite autonomous software unit,
reporting to THE CTO. Your job: triage tickets BEFORE they reach the Builder, so only well-formed,
actionable work enters the build loop. You are READ-ONLY: you read tickets, docs, and repo context to
classify and route — you never edit code or land changes.

THREE VEREDICTS — you MUST choose exactly one for every ticket:
  1. ANSWER — the ticket is a QUESTION answerable from existing docs, code, or context.
     - Provide a clear, direct answer grounded in specific sources.
     - Include at least one CITATION line showing where your answer comes from.
     - The ticket is resolved and closed without a build.

  2. CLOSE — the ticket is INVALID, DUPLICATE, or PERMANENTLY OUT OF SCOPE.
     - Explain WHY in one sentence (e.g., "duplicate of AUTO-123", "feature deprecated", "out of scope").
     - Include at least one CITATION if the claim is grounded in docs/history.
     - The ticket is closed with no action taken.

  3. REFILE — the ticket is MISROUTED, INCOMPLETE, or needs REFORMULATION.
     - Write a NEW, improved ticket title + body as a JSON block (see format below).
     - Explain WHY the original needs re-filing (one sentence).
     - The new ticket is created; the original is closed with a reference.

DECISION RULES — bias to ANSWER when you can, CLOSE only when certain, REFILE when the ask is
legitimate but ill-formed:
  - ANSWER: questions about design, API behavior, existing features, "why does X work this way?",
    "where is the config for Y?", "what's the status of Z?". Ground in docs/code; cite your source.
  - CLOSE: exact duplicates (existing ticket ID), permanently removed features, requests that
    violate architectural principles, or asks the unit cannot/will not ever fulfill.
  - REFILE: vague requirements ("improve performance"), missing context ("something is wrong"),
    wrong project/track, or tickets that need a split into multiple focused pieces.

CITATION FORMAT — every verdict except CLOSE must include at least one citation line:
  CITATION: <source> - <what was cited>

Examples:
  CITATION: ARCHITECTURE.md §2.3 - tenant isolation is enforced at the query level
  CITATION: AUTO-42 comment - the Commander reserved pricing decisions for himself
  CITATION: config.yaml defaults - DEV environment is intentionally live (no separate prod)

Citations prove your decision is grounded, not invented. A CLOSE verdict needs citations only when
claiming something is documented as deprecated/duplicate (don't cite "this is stupid").

Keep it short and concrete. No hedging, no walls of text.

End your reply with EXACTLY one line, nothing after it:
  SENIOR_PM VERDICT: ANSWER
  SENIOR_PM VERDICT: CLOSE
  SENIOR_PM VERDICT: REFILE

Above that line:
- ANSWER → lead with ≤3 tight bullets:
    • Answer: <the direct answer>
    • Rationale: <one-line why>
    • Source: <where this comes from>
  One sentence of prose may follow if essential.
- CLOSE → one sentence explaining why, with citations if applicable.
- REFILE → a brief explanation (≤2 lines) of what's wrong, followed by the new ticket JSON block.

REFILE JSON BLOCK format (required when verdict is REFILE):
```json
[
  {
    "title": "<imperative, specific summary>",
    "type": "Bug|Story|Task",
    "body": "<clear description with acceptance criteria>",
    "project": "<JIRA project key, e.g., AUTO>",
    "reason": "<why this refile is needed>"
  }
]
```
If the ticket should split into multiple, include multiple objects in the array."""

SENIOR_PM_AUTOMODE = """

AUTOMODE IS ON. The Commander is not available for approval. You MUST make the call yourself —
ANSWER, CLOSE, or REFILE — based on the ticket, docs, and existing conventions. Everything lands
on DEV (never production), and the Commander reviews your decisions afterward. Bias toward the
safest reversible option and document why. Always end with a SENIOR_PM VERDICT line."""


def _prompt(ticket: Ticket, repo_context: str = "") -> str:
    """Build the Senior PM triage prompt from a Ticket and optional repo context."""
    parts = [
        f"Ticket: {ticket.id} — {ticket.summary}",
        "",
        "DESCRIPTION:",
        (ticket.description or "(none)").strip()[:6000],
    ]
    if ticket.acceptance_criteria:
        ac = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(ticket.acceptance_criteria))
        parts += ["", "ACCEPTANCE CRITERIA:", ac]
    if ticket.labels:
        parts += ["", "LABELS:", ", ".join(ticket.labels)]
    if ticket.status:
        parts += ["", "CURRENT STATUS:", ticket.status]
    if ticket.issue_type:
        parts += ["", "ISSUE TYPE:", ticket.issue_type]
    if ticket.url:
        parts += ["", "URL:", ticket.url]
    if repo_context.strip():
        parts += ["", "REPO CONTEXT (relevant docs/config excerpts):", repo_context.strip()[:3000]]
    parts += [
        "",
        "Read the ticket and any relevant docs (CLAUDE.md, ARCHITECTURE.md, ORG.md, etc.), "
        "then decide: ANSWER, CLOSE, or REFILE. Include citations for ANSWER and REFIE. "
        "End with the SENIOR_PM VERDICT line.",
    ]
    return "\n".join(parts)


def parse_verdict(text: str | None, auto_mode: bool = False) -> dict[str, str | list]:
    """Pure parse of the Senior PM's reply into {verdict, body, raw, citations[, tickets]}.

    Unclear reply → REFILE (fail-safe: re-route for human review rather than auto-close).

    CITATION extraction (EU-107): every ANSWER and REFILE must carry at least one 'CITATION:'
    line showing the source grounding the decision. When present, citations are preserved in the
    returned dict under 'citations' (list of {source, claim}). A missing citation on ANSWER/REFILE
    is logged but does NOT flip the verdict — the presumption is the model grounded itself but
    formatted the citation poorly; CLOSE verdicts don't require citations unless making a
    documented claim (e.g., "duplicate of AUTO-123").

    REFILE JSON block: when verdict is REFILE, the function extracts the JSON array of new ticket
    proposals and returns it under 'tickets' (list of dicts). Missing/malformed JSON → empty list
    (logged), but the verdict remains REFILE so the caller knows to refile.

    AUTOMODE: the Senior PM never waits on the Commander — all verdicts stand as-is (no coercion
    needed since the officer is already autonomous in this pre-build triage context).

    Unit-testable without an agent.
    """
    raw = (text or "").strip()
    up = raw.upper()

    # Parse verdict
    if "SENIOR_PM VERDICT: REFILE" in up:
        verdict = "REFILE"
    elif "SENIOR_PM VERDICT: CLOSE" in up:
        verdict = "CLOSE"
    elif "SENIOR_PM VERDICT: ANSWER" in up:
        verdict = "ANSWER"
    else:
        verdict = "REFILE"  # unclear reply → fail-safe: refile for human review

    # Extract citations (format: CITATION: <source> - <claim>)
    citations = []
    for m in _CITATION_PATTERN.finditer(raw):
        citations.append({"source": m.group(1).strip(), "claim": m.group(2).strip()})

    # Log missing citations on ANSWER/REFILE (but don't flip verdict)
    if verdict in ("ANSWER", "REFILE") and not citations:
        _LOG.warning(
            f"parse_verdict: SENIOR_PM VERDICT: {verdict} is missing mandatory "
            "'CITATION:' lines — proceeding anyway (possible formatting issue)."
        )

    # Extract REFILE JSON block if present
    tickets: list[dict] = []
    if verdict == "REFILE":
        fences = re.findall(r"```json\s*(\[.*?\])\s*```", raw, re.DOTALL)
        for cand in reversed(fences):  # Take the last well-formed JSON block
            try:
                data = json.loads(cand)
                if isinstance(data, list):
                    tickets = [d for d in data if isinstance(d, dict)]
                    break
            except json.JSONDecodeError:
                continue
        if not tickets:
            _LOG.warning("parse_verdict: REFILE verdict missing or malformed JSON block — no tickets extracted.")

    body = raw.rsplit("SENIOR_PM VERDICT:", 1)[0].strip() if "SENIOR_PM VERDICT:" in up else raw
    result: dict[str, str | list] = {
        "verdict": verdict,
        "body": body or "(the Senior PM gave no detail)",
        "raw": raw,
        "citations": citations,
        "tickets": tickets,
    }
    return result


# --------------------------------------------------------------------------- #
# Audit helpers — record every action with citations
# --------------------------------------------------------------------------- #

@dataclass
class SeniorPMAudit:
    """Audit record for a Senior PM decision, with full citation trail.

    verdict        — ANSWER|CLOSE|REFILE (the decision made).
    ticket_id      — the ticket that was triaged.
    citations      — list of {source, claim} showing grounding.
    answer         — the answer text (if verdict=ANSWER).
    close_reason   — why closed (if verdict=CLOSE).
    refile_targets — list of new ticket proposals (if verdict=REFILE).
    raw            — full raw reply for the audit log.
    """
    verdict: str
    ticket_id: str
    citations: list[dict] = field(default_factory=list)
    answer: str = ""
    close_reason: str = ""
    refile_targets: list[dict] = field(default_factory=list)
    raw: str = ""

    def to_dict(self) -> dict:
        """Serialize to a dict for audit logging."""
        return {
            "verdict": self.verdict,
            "ticket_id": self.ticket_id,
            "citations": self.citations,
            "answer": self.answer,
            "close_reason": self.close_reason,
            "refile_targets": self.refile_targets,
            "raw": self.raw,
        }


def audit_from_parse(parsed: dict, ticket_id: str) -> SeniorPMAudit:
    """Build a SeniorPMAudit from a parse_verdict result.

    Extracts the relevant fields based on verdict so the audit log has a clean,
    structured record of what the Senior PM decided and why.
    """
    verdict = str(parsed.get("verdict", "REFILE"))
    citations = list(parsed.get("citations") or [])
    raw = str(parsed.get("raw", ""))
    body = str(parsed.get("body", ""))

    audit = SeniorPMAudit(
        verdict=verdict,
        ticket_id=ticket_id,
        citations=citations,
        raw=raw,
    )

    if verdict == "ANSWER":
        audit.answer = body
    elif verdict == "CLOSE":
        audit.close_reason = body
    elif verdict == "REFILE":
        audit.refile_targets = list(parsed.get("tickets") or [])

    return audit


# --------------------------------------------------------------------------- #
# Main triage function
# --------------------------------------------------------------------------- #

async def triage_async(cfg: Config, ticket: Ticket, repo_context: str = "",
                       auto_mode: bool = False) -> SeniorPMAudit:
    """Run the Senior PM officer on one ticket. Returns a SeniorPMAudit with the decision.

    This is the pre-build triage entry point — called BEFORE the ticket reaches the Builder loop.
    The Senior PM reads the ticket and relevant docs, then decides ANSWER/CLOSE/REFILE. The audit
    record captures the full decision chain with citations so every action is reconstructable.

    Args:
        cfg: Pipeline Config (used for model selection and app lookup).
        ticket: The Ticket to triage (provides summary, description, labels, etc.).
        repo_context: Optional repo-wide context (CLAUDE.md excerpts, config defaults, etc.).
        auto_mode: If True, the Senior PM operates autonomously (no Commander approval).

    Returns:
        SeniorPMAudit with the verdict, citations, and any answer/close reason/refile targets.
    """
    app = cfg.app(ticket.app or cfg.apps[0].name)
    from . import recon, models

    # Model selection: high effort for triage (needs strong reasoning)
    model, mreason = models.for_officer(cfg, effort="high")
    if getattr(cfg, "auto_model", False):
        print(f"  · senior_pm model: {mreason}", flush=True)

    # Build the prompt and run the officer
    task = _prompt(ticket, repo_context)
    system = SENIOR_PM_SYSTEM + (SENIOR_PM_AUTOMODE if auto_mode else "")

    report = await recon.run_officer(
        officer="senior_pm",
        label="Senior PM",
        system=system,
        task=task,
        cfg=cfg,
        cwd=app.repo_path,
        model=model,
        soldier_tools=["Read", "Grep", "Glob"],
        max_turns=14,
        effort="high",
        empty="SENIOR_PM VERDICT: REFILE"
    )

    # Parse and build audit record
    parsed = parse_verdict(report, auto_mode=auto_mode)
    audit = audit_from_parse(parsed, ticket.id)

    _LOG.info(
        f"Senior PM triaged {ticket.id}: {audit.verdict} "
        f"({len(audit.citations)} citations, {len(audit.refile_targets)} refiles)"
    )

    return audit


# --------------------------------------------------------------------------- #
# Convenience wrappers for specific verdict types
# --------------------------------------------------------------------------- #

async def answer_ticket(cfg: Config, ticket: Ticket, question: str,
                        repo_context: str = "") -> str:
    """Convenience wrapper: run Senior PM expecting an ANSWER verdict.

    Returns the answer text, or empty string if the verdict wasn't ANSWER.
    """
    # Override the ticket description with the question for Q&A cases
    qa_ticket = Ticket(
        id=ticket.id,
        key=ticket.key,
        summary=ticket.summary,
        description=question,
        acceptance_criteria=ticket.acceptance_criteria,
        url=ticket.url,
        app=ticket.app,
        ephemeral=ticket.ephemeral,
        labels=ticket.labels,
        issue_type=ticket.issue_type,
        status=ticket.status,
    )
    audit = await triage_async(cfg, qa_ticket, repo_context=repo_context)
    return audit.answer if audit.verdict == "ANSWER" else ""


async def close_ticket(cfg: Config, ticket: Ticket, reason: str = "",
                       repo_context: str = "") -> tuple[bool, str]:
    """Convenience wrapper: run Senior PM expecting a CLOSE verdict.

    Returns (success, close_reason) — success is True iff verdict was CLOSE.
    """
    audit = await triage_async(cfg, ticket, repo_context=repo_context)
    if audit.verdict == "CLOSE":
        return True, audit.close_reason
    return False, f"Senior PM returned {audit.verdict}, not CLOSE"


async def refile_ticket(cfg: Config, ticket: Ticket, repo_context: str = ""
                        ) -> tuple[bool, list[dict]]:
    """Convenience wrapper: run Senior PM expecting a REFILE verdict.

    Returns (success, new_tickets) — success is True iff verdict was REFILE.
    new_tickets is a list of {title, type, body, project, reason} dicts.
    """
    audit = await triage_async(cfg, ticket, repo_context=repo_context)
    if audit.verdict == "REFILE":
        return True, audit.refile_targets
    return False, []
