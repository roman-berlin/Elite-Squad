"""Data contracts shared across the pipeline.

These dataclasses are the *only* sanctioned way information moves between the
backlog, the builder, and the reviewer. Keeping the hand-offs structured (not
free text) is what stops the build/review loop from drifting off the ticket.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------- #
# Backlog ticket (source of truth)
# --------------------------------------------------------------------------- #
@dataclass
class Ticket:
    """A unit of work. Either pulled from a backlog (Jira) or created ad-hoc from
    a free-text description (ephemeral=True -> no backlog status writes)."""
    id: str                       # e.g. "PROJ-123" or "adhoc-scrollbar"
    key: str                      # backend-native id used for status writes
    summary: str
    description: str
    acceptance_criteria: list[str] = field(default_factory=list)
    url: Optional[str] = None
    app: Optional[str] = None     # which configured app this belongs to
    ephemeral: bool = False       # ad-hoc (not in a backlog) -> skip status writes
    labels: list[str] = field(default_factory=list)   # Jira labels (complexity + effort override)
    issue_type: Optional[str] = None                  # "Bug" | "Story" | "Epic" | ...

    def slug(self) -> str:
        import re
        s = re.sub(r"[^a-zA-Z0-9]+", "-", self.summary.lower()).strip("-")
        return s[:40] or "task"

    def branch_name(self, prefix: str = "autodev") -> str:
        # ephemeral ids already include the slug (e.g. "adhoc-leads-scroll") -> don't repeat it
        name = self.id if self.ephemeral else f"{self.id}-{self.slug()}"
        return f"{prefix}/{name}"


# --------------------------------------------------------------------------- #
# Builder <-> Orchestrator
# --------------------------------------------------------------------------- #
@dataclass
class BuildRequest:
    ticket: Ticket
    branch: str
    prior_issues: list[str] = field(default_factory=list)   # reviewer feedback on retry
    iteration: int = 1


@dataclass
class BuildResult:
    ok: bool                      # did the builder process complete without error
    summary: str                  # builder's own description of what it did
    cost_usd: float = 0.0
    num_turns: int = 0
    raw: str = ""                 # final assistant text, for the audit log
    tools: list[str] = field(default_factory=list)   # tool calls made (for the transcript)


# --------------------------------------------------------------------------- #
# Verification gate (tests / lint / typecheck)
# --------------------------------------------------------------------------- #
@dataclass
class GateResult:
    passed: bool
    report: str                   # concatenated stdout/stderr of failed commands


# --------------------------------------------------------------------------- #
# Reviewer <-> Orchestrator
# --------------------------------------------------------------------------- #
class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


@dataclass
class QualityIssue:
    severity: str                 # "blocker" | "major" | "minor"
    area: str
    detail: str


@dataclass
class ReviewResult:
    verdict: Verdict
    spec_met: bool
    spec_gaps: list[str] = field(default_factory=list)
    quality_issues: list[QualityIssue] = field(default_factory=list)
    required_changes: list[str] = field(default_factory=list)   # next prompt for builder
    summary: str = ""
    needs_human: bool = False     # a product/scope decision only the Commander can make
    question: str = ""            # the decision being asked, if needs_human
    cost_usd: float = 0.0
    raw: str = ""

    @property
    def blocking_issues(self) -> list[QualityIssue]:
        return [q for q in self.quality_issues if q.severity in ("blocker", "major")]

    def is_ship_ready(self) -> bool:
        return self.verdict == Verdict.PASS and self.spec_met and not self.blocking_issues


# --------------------------------------------------------------------------- #
# Final per-ticket outcome
# --------------------------------------------------------------------------- #
class Outcome(str, Enum):
    MERGED = "merged_to_dev"      # passed review, merged into dev, dev stayed green
    PR_OPENED = "pr_into_dev"     # passed review but couldn't safely merge -> PR for human
    ESCALATED = "escalated"       # hit max_iterations / budget -> Needs Human
    ERRORED = "errored"           # builder/infra failure
    SKIPPED = "skipped"           # dry-run (no side effects)
    REQUEUED = "requeued"         # PM triage sent it back for ONE corrective pass (not parked)


@dataclass
class TicketReport:
    ticket_id: str
    outcome: Outcome
    iterations: int
    cost_usd: float
    app: Optional[str] = None
    branch: Optional[str] = None
    pr_url: Optional[str] = None
    notes: str = ""
