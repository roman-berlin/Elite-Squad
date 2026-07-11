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
    status: Optional[str] = None                      # Jira status name at fetch time (e.g. "In Progress", "To Do")

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
    adr: Optional[str] = None                                 # Architect's ADR (if produced)


@dataclass
class BuildResult:
    ok: bool                      # did the builder process complete without error
    summary: str                  # builder's own description of what it did
    cost_usd: float = 0.0
    num_turns: int = 0
    raw: str = ""                 # final assistant text, for the audit log
    tools: list[str] = field(default_factory=list)   # tool calls made (for the transcript)
    input_tokens: int = 0         # prompt + cache tokens this run (EU-96 per-officer burn tracking)
    output_tokens: int = 0        # completion tokens this run
    provider: str = ""            # EU-123: which provider served this run ("Anthropic" or "GLM")
    model_version: str = ""       # EU-123: clean model identifier (e.g., "claude-opus-4-8", "glm-4")


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
    parse_failed: bool = False    # reviewer output was unparseable (fail-safe FAIL) -> re-review, don't rebuild
    input_tokens: int = 0         # prompt + cache tokens this run (EU-96 per-officer burn tracking)
    output_tokens: int = 0        # completion tokens this run
    provider: str = ""            # EU-123: which provider served this run ("Anthropic" or "GLM")
    model_version: str = ""       # EU-123: clean model identifier (e.g., "claude-opus-4-8", "glm-4")

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

    @property
    def audit_event(self) -> str:
        """The canonical ``audit.jsonl`` ``event`` string this outcome is recorded under.

        The Outcome enum is the SINGLE source of truth for run outcomes (EU-51). The land path
        records terminal events via ``Outcome.<X>.audit_event`` instead of hand-typed literals, and
        the dashboard/forensics reconstruct the outcome via ``AUDIT_EVENT_OUTCOME`` — so the two
        vocabularies can never silently drift apart and mis-classify a run."""
        return _OUTCOME_AUDIT_EVENT[self]


# Canonical Outcome -> audit-event name. One event per outcome; this is the name the loop records.
_OUTCOME_AUDIT_EVENT: dict[Outcome, str] = {
    Outcome.MERGED: "merged",
    Outcome.PR_OPENED: "pr_opened",
    Outcome.ESCALATED: "needs_human",
    Outcome.ERRORED: "ticket_exception",
    Outcome.SKIPPED: "dryrun_land",
    Outcome.REQUEUED: "pm_triage",
}

# Reverse map: audit-event string -> Outcome, used to reconstruct a run's outcome from the log. It
# includes the canonical events above PLUS sub-cause ALIASES — finer-grained events the loop records
# for a specific reason that still reconstruct to the same coarse Outcome (e.g. an infra exception
# is ERRORED, while a no-diff build that was already satisfied is ESCALATED). Keep new audit events
# that represent a run outcome registered here.
AUDIT_EVENT_OUTCOME: dict[str, Outcome] = {
    **{event: outcome for outcome, event in _OUTCOME_AUDIT_EVENT.items()},
    "no_changes": Outcome.ESCALATED,   # EU-116: builder found no changes (fix already present) -> ESCALATED
    "escalated": Outcome.ESCALATED,    # explicit/legacy escalation event
    "scrum_split": Outcome.REQUEUED,   # too big → split into sub-tickets, parent closed (a terminal REQUEUE,
                                       # so the run reconstructs as done, not perpetually "running")
}


# Outcomes that PARK a ticket IMMEDIATELY — a human decision (ESCALATED) or an open PR (PR_OPENED) is
# waiting on the Commander, so there is no point auto-retrying. The SINGLE source of truth for the
# "should this outcome park now?" check (EU-56): autopilot imports this instead of re-declaring its own
# copy, which had drifted out of sync with a now-deleted dead duplicate in events.py. ERRORED is
# deliberately NOT here — a transient blip is retried a few times before parking (see
# autopilot._MAX_TICKET_ERRORS); do not add it, or errored tickets would never get their retry budget.
PARKED: tuple[Outcome, ...] = (Outcome.ESCALATED, Outcome.PR_OPENED)


# --------------------------------------------------------------------------- #
# Structured handoff artifacts — EU-72 (MetaGPT pattern)
#
# Three dataclasses carry typed information between officers so the next officer
# reads a tight structured input instead of re-deriving from the full diff. The
# loop instantiates ONE PerTicketArtifactStore per ticket in _attempt() and
# threads it through the officers: the builder publishes a BuildArtifact and the
# reviewer a ReviewVerdict, while the SpecArtifact is derived from the ticket.
# Artifact-first, full-context-on-demand — the full summary/diff is always still
# available, so a digest never has to carry everything.
# --------------------------------------------------------------------------- #

@dataclass
class SpecArtifact:
    """Written by the Spec officer; consumed by the Builder and Reviewer.

    Carries the structured version of the ticket's acceptance criteria so that
    downstream officers never have to re-parse the raw Jira description.
    """
    acceptance: list[str]   # parsed acceptance criteria, one item per criterion
    scope: str              # one-sentence description of what IS in scope
    non_goals: list[str]    # explicit out-of-scope items; prevents scope creep


@dataclass
class BuildArtifact:
    """Written by the Builder; consumed by the Test Engineer and Reviewer.

    Gives the Test Engineer and Reviewer a machine-readable summary of what
    changed so they can focus their checks instead of re-reading the diff.
    """
    files_changed: list[str]    # repo-relative paths that were modified
    diff_digest: str            # short (≤ 500 char) human-readable summary of the diff
    decisions: list[str]        # key design decisions made during the build
    open_questions: list[str]   # concerns the Builder couldn't resolve unilaterally
    # EU-267: caveats/limitations the builder flagged (e.g. "tests are unverified in this
    # environment but the implementation itself is correct"). Deliberately NOT subject to the
    # diff_digest 500-char ceiling below — a caveat buried past char 500 of the summary must
    # still reach the Reviewer, so this field is uncapped by design.
    caveats: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Enforce the digest ceiling the docstring promises: the artifact is a SUMMARY, not a second
        # copy of the diff. An over-long digest means the builder inlined the diff — reject it so the
        # handoff stays cheap (the full summary/raw lives on BuildResult for on-demand digging).
        if len(self.diff_digest) > 500:
            raise ValueError(
                f"BuildArtifact.diff_digest must be ≤ 500 chars, got {len(self.diff_digest)}; "
                "summarise the diff instead of inlining it."
            )


@dataclass
class ReviewVerdict:
    """Written by the Reviewer; consumed by the orchestrator loop.

    A typed counterpart to ReviewResult for the structured-artifact pipeline —
    ReviewResult keeps its existing parser/enum surface; ReviewVerdict is the
    lightweight version emitted into the PerTicketArtifactStore.
    """
    verdict: Verdict        # Verdict.PASS | Verdict.FAIL (the same enum ReviewResult uses)
    blocking: list[str]     # blocking issues that must be resolved before merge
    notes: list[str]        # non-blocking observations for the next iteration


# --------------------------------------------------------------------------- #
# Security Engineer artifact — EU-105
# --------------------------------------------------------------------------- #
import re as _re

# Regex that matches any angle-bracket template placeholder such as
# "<describe secrets here>" or "<§1 fill-in text>".  A field whose entire
# content (after stripping whitespace) matches this pattern is still a
# template — the Security Engineer hasn't actually filled it in.
_PLACEHOLDER_RE = _re.compile(r"^\s*<[^>]+>\s*$")


# Phase-2 §2 (2026-07-06): SecurityArtifact (the §1/§2/§3 countersignature the deleted provost
# security gate produced) was removed with that gate. The deterministic secret/dep scan in
# gate.py plus a Reviewer checklist section replace it.


@dataclass
class PerTicketArtifactStore:
    """The shared per-ticket pool. Instantiated once per ticket in _attempt() and threaded through
    the officers; each officer publishes its artifact with put() and reads the upstream one (passed
    by the loop as a named argument) as its primary input.

    Each slot is Optional so a downstream officer can tell whether a given artifact has been produced
    yet (None → that stage didn't run / didn't publish) and fall back to the full diff/description.
    The store is cheap: five small dataclasses + a token-burn dict, no I/O.

    ``token_burn`` holds per-officer usage totals keyed by officer tag (e.g. ``"builder"``,
    ``"test-engineer"``, ``"reviewer"``) so the measurement layer can track Opus burn per stage
    without parsing raw ledger files.

    ``stage_costs`` is the USD mirror of ``token_burn`` for stages whose (ok, report)-style
    return can't carry a cost. The 2026-07-05 EU-139-run telemetry audit found such a stage's
    spend never reached the ticket report, so run_end's total_cost_usd under-reported the
    ledger-true run cost; the loop reads the delta from here.
    """
    spec: Optional[SpecArtifact] = None
    build: Optional[BuildArtifact] = None
    review: Optional[ReviewVerdict] = None
    token_burn: dict[str, int] = field(default_factory=dict)
    stage_costs: dict[str, float] = field(default_factory=dict)

    def put(self, artifact: SpecArtifact | BuildArtifact | ReviewVerdict) -> None:
        """Store *artifact* in the correct slot (determined by type).

        Raises TypeError for unknown artifact types so callers discover
        misuse immediately rather than silently dropping data.
        """
        if isinstance(artifact, SpecArtifact):
            self.spec = artifact
        elif isinstance(artifact, BuildArtifact):
            self.build = artifact
        elif isinstance(artifact, ReviewVerdict):
            self.review = artifact
        else:
            raise TypeError(f"Unknown artifact type: {type(artifact)!r}")


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
    token_burn: dict[str, int] = field(default_factory=dict)
    """Per-officer token totals (input + output) for this ticket, keyed by officer tag.

    Populated by the loop from ``PerTicketArtifactStore.token_burn`` after the ticket
    resolves (EU-96).  Officers: ``"builder"``, ``"test-engineer"``, ``"reviewer"``,
    ``"provost"`` (when the security gate ran).  Empty dict when no tracking data is
    available (e.g. tickets resolved before EU-96).
    """
