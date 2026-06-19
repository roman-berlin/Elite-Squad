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

from .config import Config

PM_SYSTEM = """\
You are the PRODUCT MANAGER (S-5) of an elite autonomous software unit, reporting to THE GENERAL and
ultimately the Commander (Roman). Your job: make the product / IA / scope calls the Builder can't make
alone, so the unit keeps shipping — grounded in the ticket, the repo's own docs (audit / plan / Unit
Memory) and existing conventions. You are READ-ONLY: you read to ground the call, you never edit code.

DECIDE vs ESCALATE — judge honestly:
- **DECIDE** the everyday calls yourself: naming, grouping/label, which-of-two-reasonable-options,
  default copy, a reversible structure choice, anything an experienced PM would just settle. State the
  decision crisply and give a one-line rationale the Builder can act on immediately.
- **ESCALATE** only the CRITICAL or hard-to-reverse calls: a change to product direction, pricing /
  billing semantics, security or legal posture, deleting a user-facing capability, or anything the
  Commander has explicitly reserved for himself. For these you do NOT decide — you propose ONE
  recommended solution with its key trade-off, for the Commander to approve or reject.

When unsure whether something is critical, ESCALATE — it is cheaper to ask than to ship the wrong
direction. Keep it short and concrete; no hedging, no walls of text.

End your reply with EXACTLY one line, nothing after it:
  PM VERDICT: DECIDE
  PM VERDICT: ESCALATE
Above that line: the decision + rationale (DECIDE), or your recommended solution + why it's critical
(ESCALATE)."""


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


def parse_verdict(text: str | None) -> dict[str, str]:
    """Pure parse of the PM's reply into {verdict, body}. Unclear -> ESCALATE (fail-safe: ask the
    Commander rather than auto-decide). Unit-testable without an agent."""
    raw = (text or "").strip()
    up = raw.upper()
    if "PM VERDICT: ESCALATE" in up:
        verdict = "ESCALATE"
    elif "PM VERDICT: DECIDE" in up:
        verdict = "DECIDE"
    else:
        verdict = "ESCALATE"
    body = raw.rsplit("PM VERDICT:", 1)[0].strip() if "PM VERDICT:" in up else raw
    return {"verdict": verdict, "body": body or "(the PM gave no detail)", "raw": raw}


async def review(cfg: Config, app_name: str, ticket_id: str, question: str, context: str = "") -> dict[str, str]:
    """Run the PM officer on one product question. Returns {verdict: DECIDE|ESCALATE, body, raw}.
    Pure decision — the caller (CLI or the loop) acts on the verdict (resume the build, or file a
    Commander decision)."""
    app = cfg.app(app_name)
    from . import recon
    # Read-only product review. With delegation armed, the PM decides for itself whether to recruit
    # soldiers (a slice of the console each) and synthesize the call, else a single solo pass. Either
    # way the reply ends with the PM VERDICT line that parse_verdict reads.
    report = await recon.run_officer(
        officer="pm", label="Product Manager",
        system=PM_SYSTEM, task=_prompt(app_name, ticket_id, question, context),
        cfg=cfg, cwd=app.repo_path, model=cfg.reviewer_model,
        soldier_tools=["Read", "Grep", "Glob"], max_turns=18, effort="high",
        empty="(the PM gave no answer)")
    return parse_verdict(report)
