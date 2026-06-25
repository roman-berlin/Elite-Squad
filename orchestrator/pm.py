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
You are the PRODUCT MANAGER (S-5) of an elite autonomous software unit, reporting to THE CTO and
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

Above that line:
- DECIDE → one short paragraph: the decision + a one-line rationale the Builder can act on.
- ESCALATE → a BRIEF for the Commander, at most 6 lines, in EXACTLY this shape — no preamble, no
  re-derivation, no quoting the whole ticket:
    BLOCKER: <one sentence — what is blocked and why it's his call>
    DECISION: <the single question he must answer>
    OPTIONS: <A vs B in a few words, or "—" if not a choice>
    RECOMMENDATION: <your suggested call + the one key trade-off>
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
    """Pure parse of the PM's reply into {verdict, body}. Unclear -> ESCALATE (fail-safe: ask the
    Commander rather than auto-decide). In AUTOMODE the PM never waits on the Commander: an ESCALATE (or
    an unclear reply) is coerced to DECIDE so the unit keeps moving — the recommendation becomes the
    decision, logged for the Commander to review/reverse. Unit-testable without an agent."""
    raw = (text or "").strip()
    up = raw.upper()
    if "PM VERDICT: ESCALATE" in up:
        verdict = "ESCALATE"
    elif "PM VERDICT: DECIDE" in up:
        verdict = "DECIDE"
    else:
        verdict = "ESCALATE"
    if auto_mode and verdict == "ESCALATE":
        verdict = "DECIDE"   # automode: decide autonomously, never stop for the approve button
    body = raw.rsplit("PM VERDICT:", 1)[0].strip() if "PM VERDICT:" in up else raw
    return {"verdict": verdict, "body": body or "(the PM gave no detail)", "raw": raw}


PM_TRIAGE_SYSTEM = """You are the Product Manager, called when a ticket has EXHAUSTED its build passes —
the Builder built it several times and the Reviewer kept rejecting it. Before the Commander is bothered,
you triage the run. You are given the latest Builder summary and the Reviewer's outstanding required
changes; you may Read the repo to ground your call.

Judge honestly:
- If the ticket's CORE deliverable is essentially DONE and the remaining rejections are fixable
  scope-creep / hygiene the unit can finish ITSELF — an unrelated dependency or lockfile change to back
  out, a missing config/CSP line, a layout fix, a stray file to delete, an out-of-scope app touched —
  then TRIAGE: RESOLVE. Give ONE precise, surgical instruction for a single final pass that lands the
  in-scope work and drops the out-of-scope churn. Name the files and the exact action. Ask the Commander
  NOTHING.
- If a GENUINE Commander decision remains — a value only he has (a real phone number, a credential), a
  product/scope call he reserved for himself, or an irreducibly ambiguous requirement — then
  TRIAGE: ESCALATE, as a tight 1–3 line brief (BLOCKER / DECISION / RECOMMENDATION). No essay.

- If the ticket is simply TOO BIG to land within the pass budget — it spans many files or areas, each
  independently substantial, and no single corrective pass could finish it (a repo-wide rename, a
  multi-screen feature, a cross-cutting refactor) — then TRIAGE: SPLIT. In 1–2 lines say why it's too
  heavy; the Scrum Master will break it into small sub-tickets that each land on their own.

Bias to RESOLVE when the work is substantively done and only discipline is missing. Use SPLIT only when
the ticket is genuinely oversized for one build. Reserve ESCALATE for what only the Commander can settle.

End with EXACTLY one line, nothing after it:  TRIAGE: RESOLVE   or   TRIAGE: ESCALATE   or   TRIAGE: SPLIT"""


def parse_triage(text: str | None) -> dict[str, str]:
    """Parse the PM's triage reply into {action: RESOLVE|ESCALATE|SPLIT, text}. Unclear -> ESCALATE."""
    raw = (text or "").strip()
    up = raw.upper()
    if "TRIAGE: SPLIT" in up:
        action = "SPLIT"
    elif "TRIAGE: RESOLVE" in up:
        action = "RESOLVE"
    elif "TRIAGE: ESCALATE" in up:
        action = "ESCALATE"
    else:
        action = "ESCALATE"
    body = raw.rsplit("TRIAGE:", 1)[0].strip() if "TRIAGE:" in up else raw
    return {"action": action, "text": body or "(the PM gave no detail)"}


async def triage(cfg: Config, app_name: str, ticket_id: str, last_build: str = "",
                 rejections: list[str] | None = None) -> dict[str, str]:
    """Consulted when a ticket exhausts its passes. Returns {action: RESOLVE|ESCALATE, text}. RESOLVE ->
    the loop re-queues the ticket with `text` as a corrective instruction; ESCALATE -> park with `text`
    as the Commander brief."""
    app = cfg.app(app_name)
    from . import recon
    task = "\n".join([
        f"Ticket {ticket_id} exhausted its build passes ({cfg.max_iterations}). Decide RESOLVE vs ESCALATE.",
        "", "Latest Builder summary:", (last_build or "(none)")[:1500],
        "", "Reviewer's outstanding required changes:",
        ("\n".join(f"- {c}" for c in (rejections or [])[:12]) or "(none recorded)"),
    ])
    report = await recon.run_officer(
        officer="pm", label="Product Manager", system=PM_TRIAGE_SYSTEM,
        task=task, cfg=cfg, cwd=app.repo_path, model=cfg.reviewer_model,
        soldier_tools=["Read", "Grep", "Glob"], max_turns=14, effort="high",
        empty="TRIAGE: ESCALATE")
    return parse_triage(report)


async def review(cfg: Config, app_name: str, ticket_id: str, question: str, context: str = "") -> dict[str, str]:
    """Run the PM officer on one product question. Returns {verdict: DECIDE|ESCALATE, body, raw}.
    Pure decision — the caller (CLI or the loop) acts on the verdict (resume the build, or file a
    Commander decision). In automode the PM is told to decide and the verdict is coerced to DECIDE."""
    app = cfg.app(app_name)
    auto = bool(getattr(cfg, "auto_mode", False))
    from . import recon
    # Read-only product review. With delegation armed, the PM decides for itself whether to recruit
    # soldiers (a slice of the console each) and synthesize the call, else a single solo pass. Either
    # way the reply ends with the PM VERDICT line that parse_verdict reads.
    report = await recon.run_officer(
        officer="pm", label="Product Manager",
        system=PM_SYSTEM + (PM_AUTOMODE if auto else ""),
        task=_prompt(app_name, ticket_id, question, context),
        cfg=cfg, cwd=app.repo_path, model=cfg.reviewer_model,
        soldier_tools=["Read", "Grep", "Glob"], max_turns=18, effort="high",
        empty="(the PM gave no answer)")
    return parse_verdict(report, auto_mode=auto)
