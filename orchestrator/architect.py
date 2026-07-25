"""Architect officer (S-7) — produces lightweight ADRs and definition-of-done for feature/large tickets.

When a feature or large ticket reaches the build loop, the Architect runs BEFORE the Builder to:
1. Decide whether the ticket needs an ADR (feature/large) or can skip (bug/small)
2. Produce a lightweight ADR: chosen approach, main risk + alternative, touch-points, and explicit
   definition-of-done (which tests, a11y checks, security touch-points)

The Architect produces ONLY the ADR. The calling code (loop.py) then detects oversized designs
and triggers the Scrum Master to split if needed. This separation keeps the Architect focused on
design while allowing the loop to control the split timing and flow.

This is a design-upfront officer that prevents test/a11y leakage to review and makes splitting
design-driven rather than guessed. Uses Opus for design reasoning with high effort and 18 turns.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TypedDict

_LOG = logging.getLogger(__name__)

from . import memory
from .builder import size_ticket
from .config import Config
from .contracts import Ticket

# Threshold for when a design is "too big" and should trigger a Scrum Master split
# (5 touch-points or 3 distinct modules). Fixed here — Config declares no override knob.
DEFAULT_SPLIT_THRESHOLD = {
    "touch_points": 5,      # Max files/areas before suggesting split
    "modules": 3,           # Max distinct modules/components before suggesting split
}

ARCHITECT_SYSTEM = """\
You are the ARCHITECT (S-7) of an elite autonomous software unit. Your job: produce a lightweight
Architecture Decision Record (ADR) for feature or large tickets BEFORE they reach the Builder.

Your ADR answers FOUR questions:
1. APPROACH — what will we build? (the chosen design/strategy)
2. RISK + ALTERNATIVE — what's the main risk and what alternative did we consider?
3. TOUCH-POINTS — which modules/files will this change? (be specific)
4. DEFINITION-OF-DONE — what tests, a11y checks, and security touch-points must the Builder satisfy?

Keep it LIGHT — this is not a 10-page document. Each section should be 2–5 tight lines. The goal is
to stop test/a11y/security leakage to the review stage by making the DoD explicit upfront.

OUTPUT FORMAT — exactly these four headers with content under each (no prose outside):

## APPROACH
<2–5 lines: the chosen design/strategy. Be concrete.>

## RISK + ALTERNATIVE
<2–3 lines: the main risk (e.g., performance, security, complexity) and one realistic alternative
we considered and rejected, with why.>

## TOUCH-POINTS
<2–5 lines: specific files, modules, or areas this ticket touches. List them concretely:
  • src/auth/middleware.py
  • frontend/components/LeadForm.tsx
  • README.md (docs update)>

## DEFINITION-OF-DONE
<3–6 lines covering:
  • Tests: which test files/cases must pass (happy path + regression)
  • A11y: which pages/routes need axe-core scanning (zero violations)
  • Security: which routes need authz checks, what inputs need validation, any parameterized queries>

End with one line:
ADR_COMPLETE

If the ticket is a small bug or trivial change, say so and end with SKIP_ADR instead."""

ARCHITECT_GATED_SYSTEM = """\
You are the ARCHITECT (S-7) with GATED ACTIVATION. You decide whether to produce an ADR or skip.

SKIP RULES — produce an ADR ONLY for feature/large work; skip for bugs/small changes:
  SKIP (no ADR needed) if ANY apply:
    • Issue type is "Bug"
    • Labels include "bug" or "trivial"
    • Size is XS or S/M (≤2 acceptance criteria, short description, no complexity keywords)
    • Change is isolated to one file/area with no design decisions (e.g., typo fix, simple refactor)

  PRODUCE ADR if ANY apply:
    • Issue type is "Story", "Epic", or "Feature"
    • Labels include "feature" or "epic"
    • Size is L or XL (≥5 acceptance criteria, detailed spec, complexity keywords like refactor,
      migration, performance, security, auth, payment, webhook, breaking change)
    • Cross-cutting concern (touches multiple modules, requires coordination)

If skipping, reply with EXACTLY:
SKIP_ADR

If producing an ADR, follow the full ADR format above and end with ADR_COMPLETE.

Your decision is final — no Commander approval needed. Everything lands on DEV; the Commander
reviews your ADRs after the fact."""


def _prompt(ticket: Ticket, repo_context: str = "") -> str:
    """Build the Architect's prompt from a Ticket and optional repo context."""
    parts = [
        f"Ticket: {ticket.id} — {ticket.summary}",
        "",
        "DESCRIPTION:",
        (ticket.description or "(none)").strip()[:8000],
    ]
    if ticket.acceptance_criteria:
        ac = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(ticket.acceptance_criteria))
        parts += ["", "ACCEPTANCE CRITERIA:", ac]
    if ticket.labels:
        parts += ["", "LABELS:", ", ".join(ticket.labels)]
    if ticket.issue_type:
        parts += ["", "ISSUE TYPE:", ticket.issue_type]
    if ticket.url:
        parts += ["", "URL:", ticket.url]
    if repo_context.strip():
        parts += ["", "REPO CONTEXT (relevant docs/architecture):", repo_context.strip()[:4000]]

    # Add sizing context to help with the skip decision
    size, effort, reason = size_ticket(ticket)
    parts += [
        "",
        f"TICKET SIZE: {size} (effort: {effort}) — {reason}",
        "",
        "Decide: SKIP_ADR (bug/small) or produce a full ADR (feature/large). "
        "If producing an ADR, follow the four-section format exactly.",
    ]
    return "\n".join(parts)


class ADRDict(TypedDict):
    """The serialized ADRExtraction shape (to_dict) for audit logging and handoff."""
    approach: str
    risk_alt: str
    touch_points: list[str]
    definition_of_done: str
    raw: str
    skipped: bool


@dataclass
class ADRExtraction:
    """Structured extraction of an ADR's sections.

    approach         — the chosen approach/design.
    risk_alt        — the main risk and alternative considered.
    touch_points     — list of specific files/modules/areas touched.
    definition_of_done — the explicit DoD (tests, a11y, security).
    raw              — full raw ADR text for audit.
    skipped          — True if the Architect decided to skip (bug/small).
    """
    approach: str = ""
    risk_alt: str = ""
    touch_points: list[str] = field(default_factory=list)
    definition_of_done: str = ""
    raw: str = ""
    skipped: bool = False

    def to_dict(self) -> ADRDict:
        """Serialize to a dict for audit logging and handoff."""
        return {
            "approach": self.approach,
            "risk_alt": self.risk_alt,
            "touch_points": self.touch_points,
            "definition_of_done": self.definition_of_done,
            "raw": self.raw,
            "skipped": self.skipped,
        }


def parse_adr(text: str | None) -> ADRExtraction:
    """Parse the Architect's reply into an ADRExtraction.

    Handles both SKIP_ADR (no ADR needed) and full ADR with four sections.
    Unit-testable without an agent. Returns a struct with each section extracted."""
    raw = (text or "").strip()
    up = raw.upper()

    # Check for skip
    if "SKIP_ADR" in up or not raw:
        result = ADRExtraction(skipped=True, raw=raw)
        return result

    # Extract the four sections
    sections = {
        "APPROACH": "",
        "RISK + ALTERNATIVE": "",
        "RISK": "",  # Allow without "+ ALTERNATIVE" too
        "TOUCH-POINTS": "",
        "DEFINITION-OF-DONE": "",
    }

    # Split by section headers
    lines = raw.splitlines()
    current_section = None
    current_content: list[str] = []

    for line in lines:
        stripped = line.strip()
        # Check if this is a section header
        for sec in sections.keys():
            if stripped.startswith(f"## {sec}") or stripped.startswith(f"## {sec.replace(' +', '').replace(' ', '')}"):
                # Save previous section if any
                if current_section and current_content:
                    sections[current_section] = "\n".join(current_content).strip()
                current_section = sec
                current_content = []
                break
        else:
            # Not a header, accumulate content
            if current_section:
                current_content.append(line)

    # Save last section
    if current_section and current_content:
        sections[current_section] = "\n".join(current_content).strip()

    # Normalize RISK + ALTERNATIVE (try both variants)
    risk_alt = sections.get("RISK + ALTERNATIVE") or sections.get("RISK") or ""

    # Parse touch-points into a list (bullet lines or just split)
    touch_raw = sections.get("TOUCH-POINTS", "")
    touch_points: list[str] = []
    for line in touch_raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # Remove bullet prefixes if present
        m = re.match(r"^[\s*•\-\d+.)]+\s*(.+)", line)
        if m:
            touch_points.append(m.group(1).strip())
        else:
            touch_points.append(line)

    # If no bullets found, try splitting by commas
    if not touch_points and touch_raw:
        touch_points = [tp.strip() for tp in touch_raw.split(",") if tp.strip()]

    return ADRExtraction(
        approach=sections.get("APPROACH", ""),
        risk_alt=risk_alt,
        touch_points=touch_points,
        definition_of_done=sections.get("DEFINITION-OF-DONE", ""),
        raw=raw,
        skipped=False,
    )


def detect_oversized(adrex: ADRExtraction, threshold: dict | None = None) -> bool:
    """Check if the ADR indicates an oversized design that should trigger a Scrum Master split.

    Rules (configurable via threshold dict, defaults to DEFAULT_SPLIT_THRESHOLD):
      - Too many touch-points (many files/areas to change)
      - Too many distinct modules (cross-cutting concern)

    Returns True if the design exceeds thresholds and should be split."""
    if adrex.skipped or not adrex.touch_points:
        return False

    thresh = threshold or DEFAULT_SPLIT_THRESHOLD

    touch_point_threshold = int(thresh.get("touch_points", DEFAULT_SPLIT_THRESHOLD["touch_points"]))
    module_threshold = int(thresh.get("modules", DEFAULT_SPLIT_THRESHOLD["modules"]))

    # Check touch-point count first
    if len(adrex.touch_points) >= touch_point_threshold:
        return True

    # Check distinct modules (heuristic: count distinct top-level paths)
    modules = set()
    for tp in adrex.touch_points:
        # Extract module from path (e.g., "src/auth/middleware.py" -> "src/auth")
        parts = tp.replace("\\", "/").split("/")
        if len(parts) >= 2:
            modules.add("/".join(parts[:2]))
        elif parts:
            modules.add(parts[0])

    if len(modules) >= module_threshold:
        return True

    return False


async def design(cfg: Config, ticket: Ticket, repo_context: str = "",
                 gated: bool = True, audit=None) -> ADRExtraction:
    """Run the Architect officer on one ticket. Returns an ADRExtraction with the ADR or skip decision.

    This is the design-upfront entry point — called BEFORE the ticket reaches the Builder loop.
    The Architect reads the ticket and decides:
      - SKIP for bugs/small changes (no ADR needed)
      - PRODUCE ADR for features/large work (full ADR with approach, risk, touch-points, DoD)

    The Architect produces ONLY the ADR — it does NOT trigger the Scrum Master split.
    The calling code (loop.py) is responsible for detecting oversized designs and calling
    the Scrum Master to split the ticket. This separation keeps the Architect focused on
    design and allows the loop to control the split timing and flow.

    Args:
        cfg: Pipeline Config (used for model selection and app lookup).
        ticket: The Ticket to design (provides summary, description, labels, etc.).
        repo_context: Optional repo-wide context (ARCHITECTURE.md, tech docs, etc.).
        gated: If True, use ARCHITECT_GATED_SYSTEM (skip logic). If False, always produce ADR.
        audit: Optional audit hook to record Architect decisions.

    Returns:
        ADRExtraction with the ADR sections or skip=True for bugs/small changes.
    """
    app = cfg.app(ticket.app or cfg.apps[0].name)
    from . import recon, models

    # Model selection: Opus for design reasoning, high effort, 18 turns
    model, _aeffort, mreason = models.for_planner(cfg, ticket, effort="high")
    if getattr(cfg, "auto_model", False):
        print(f"  · architect model: {mreason}", flush=True)

    # Build the prompt and run the officer
    task = _prompt(ticket, repo_context)
    system = (ARCHITECT_GATED_SYSTEM if gated else ARCHITECT_SYSTEM) + memory.preamble()

    report = await recon.run_officer(
        officer="architect",
        label="Architect",
        system=system,
        task=task,
        cfg=cfg,
        cwd=app.repo_path,
        model=model,
        soldier_tools=["Read", "Grep", "Glob"],
        max_turns=18,
        effort="high",
        empty="SKIP_ADR"
    )

    # Parse the ADR
    adrex = parse_adr(report)

    # Record audit events for Architect completion
    # Note: loop.py handles oversized detection and Scrum Master triggering
    if audit is not None:
        if adrex.skipped:
            audit.record("architect_skipped", ticket_id=ticket.id, reason="small_bug")
        else:
            # Build a concise ADR summary for audit (first 200 chars of approach + touch-points count)
            adr_summary = f"{len(adrex.touch_points)} touch-points"
            if adrex.approach:
                adr_summary = f"{adrex.approach[:150]}... | {adr_summary}"
            audit.record("architect_run", ticket_id=ticket.id,
                         adr_summary=adr_summary, triggered_split=False)

    _LOG.info(
        f"Architect completed {ticket.id}: skipped={adrex.skipped}, "
        f"touch_points={len(adrex.touch_points)}"
    )

    return adrex


# --------------------------------------------------------------------------- #
# Convenience wrappers for specific workflows
# --------------------------------------------------------------------------- #

async def should_run_architect(cfg: Config, ticket: Ticket) -> bool:
    """Quick check: should the Architect run on this ticket?

    Uses the same sizing logic as the gated system to decide upfront.
    Returns True if the ticket is feature/large, False for bug/small."""
    size, effort, reason = size_ticket(ticket)
    labels = [str(l).lower() for l in (getattr(ticket, "labels", None) or [])]
    itype = (getattr(ticket, "issue_type", None) or "").lower()

    # Explicit skip signals
    if itype == "bug" or "bug" in labels or "trivial" in labels:
        return False
    if size in ("XS", "S/M"):
        return False

    # Explicit run signals
    if itype in ("story", "epic", "feature"):
        return True
    if "feature" in labels or "epic" in labels:
        return True
    if size in ("L", "XL"):
        return True

    # Default: run for anything larger than S/M
    return True
