"""The Planner — the Elite Unit's single up-front design officer (Phase-2 §2).

One Opus call per ticket, BEFORE the build, that replaces a cluster of separate LLM roles
(Architect ADR, squad-lead split-planning, Scrum split decision, Senior-PM prebuild triage).
It reads the ticket + repo and produces a structured plan:

  • verdict — BUILD (implement it) · ANSWER (answerable from docs) · CLOSE (invalid/dup) ·
    REFILE (misrouted) · SPLIT (too big for one build). The loop acts on the verdict.
  • approach — the design the Builder should follow (what the Architect's ADR used to carry).
  • testable_ac — sharp, independently-verifiable acceptance criteria phrased the way a test
    would check them. THIS is what lets the Builder write the tests itself and the deterministic
    gate run them — the replacement for the separate Test Engineer coverage pass (§2).
  • in_scope_files — the files the build should touch (the scoping the squad split used to give).

Read-only (Read/Grep/Glob), Opus-tier, high effort. Fail-safe: any parse/agent error yields a
BUILD verdict with empty fields, so a Planner hiccup never blocks a ticket — the Builder just
proceeds from the raw ticket exactly as it does today when the Planner is disabled.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions

from . import memory, models
from .agent import run_agent
from .config import Config
from .contracts import Ticket

VALID_VERDICTS = ("BUILD", "ANSWER", "CLOSE", "REFILE", "SPLIT")

PLANNER_SYSTEM = """\
You are the Planner — the Elite Unit's single design officer, reporting to THE CTO. You run ONCE
per ticket, before any code is written. You are READ-ONLY: you read the ticket and the repo, then
hand the Builder a crisp plan. You do not write code.

Decide a VERDICT for the ticket:
  • BUILD   — it needs implementation (the common case).
  • ANSWER  — it's a question answerable from the code/docs; give the answer, no build needed.
  • CLOSE   — invalid, duplicate, or already done; give the reason.
  • REFILE  — misrouted / actually several unrelated asks; say how it should be re-filed.
  • SPLIT   — too large to implement AND test in ONE focused build pass; name 2–5 sub-tickets, each a
    coherent, independently-shippable slice.

SIZE THE WORK before choosing BUILD vs SPLIT — this is where the Unit saves the most. Splitting a
too-big ticket UP FRONT costs one Planner call; letting the Builder discover mid-build that it's too big
burns millions of tokens and then splits anyway. From the files you actually read, estimate the change's
span and choose SPLIT when it genuinely cannot land+test in a single pass — e.g. it touches many
INDEPENDENT files across unrelated areas (roughly more than 6–8), spans several distinct
screens/routes/modules that each need their own tests, or is a repo-wide rename/sweep. Choose BUILD when
the change is focused (a handful of related files, one screen/module) even if non-trivial. When you
SPLIT, list the sub-tickets in "answer", each sized to land on its own in one pass.

Be CONSERVATIVE about ANSWER/CLOSE/REFILE: when in doubt whether a ticket is real work, BUILD it — a
ticket with acceptance criteria, or labelled Feature or Bug, is almost always BUILD, never ANSWER/CLOSE
it away. But do NOT force a genuinely oversized ticket through as one BUILD — SPLIT it per the sizing
rule above, up front, before the Builder burns the tokens.

For a BUILD verdict, produce:
  • approach — the design the Builder should follow: the chosen approach and the key decisions,
    grounded in files you actually read (cite file paths). A few sentences to a short paragraph.
  • testable_ac — 2–6 acceptance criteria, each INDEPENDENTLY VERIFIABLE and phrased the way a
    test would check it (a concrete input → observable output/behaviour). These are the contract the
    Builder writes a FAILING test against first (fail-first): phrase each so a test can go RED when
    the behaviour is absent. Sharpen the ticket's own AC; don't just echo them.
  • in_scope_files — the repo files the build should create or edit (paths). Keep it tight; this
    scopes the change. Empty is acceptable if you genuinely can't tell.

Output ONLY a single JSON object, no prose around it, exactly this shape:
{"verdict": "BUILD", "approach": "...", "testable_ac": ["...", "..."], "in_scope_files": ["path/a.py"], "answer": ""}
For ANSWER/CLOSE/REFILE/SPLIT put the reply/reason/sub-ticket list in "answer" and leave the
build fields empty. Never wrap the JSON in markdown fences; never add commentary after it."""

# 2026-07-19 (Commander order — squad modes): appended to PLANNER_SYSTEM only when squad_pref
# routes the ticket to the ELITE squad. The Analyst's decompose-first duties: an ordered step
# plan the Builder executes one-at-a-time, and every assumption/question surfaced NOW — because
# a mid-build "stop and wait" strands a worktree; questions batch here, at analysis time.
ELITE_PLAN_ADDENDUM = """

ELITE SQUAD — you are the Analyst for a small elite squad; the Builder will execute your plan
one step at a time with a check after every step. Two extra duties on a BUILD verdict:
1. In "steps" (or the plan body), give an ORDERED list of small, independently-checkable steps —
   each one small enough to implement and verify in one sitting, sequenced so every step leaves
   the tree green. Name the verification for each step (which test/command proves it).
2. Surface EVERY material assumption and unclear point NOW, in the plan — the Builder will not
   stop mid-build to ask. A genuinely CRITICAL unknown that blocks safe work is a needs_human
   question at this stage, not a guess.
Also recalibrate SPLIT: the elite Builder carries a 2.4x turn budget and works step-by-step, so
prefer BUILD-with-step-plan for large-but-coherent work; reserve SPLIT for genuinely unrelated
asks or repo-wide sweeps that no single careful pass can land.
"""


@dataclass
class PlannerResult:
    """Structured plan for one ticket. Fail-safe default is a BUILD with empty fields (the Builder
    then proceeds from the raw ticket, exactly as with the Planner disabled)."""
    verdict: str = "BUILD"
    approach: str = ""
    testable_ac: list[str] = field(default_factory=list)
    in_scope_files: list[str] = field(default_factory=list)
    answer: str = ""              # reply/reason for ANSWER/CLOSE/REFILE/SPLIT
    raw: str = ""
    # burn accounting (the Planner replaces the Test Engineer's spend — so it must be countable)
    cost_usd: float = 0.0
    num_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    provider: str = ""
    model_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "approach": self.approach,
                "testable_ac": self.testable_ac, "in_scope_files": self.in_scope_files,
                "answer": self.answer}

    def as_builder_brief(self) -> str:
        """Render the plan as the design brief injected into the Builder prompt (the channel the
        Architect's ADR used). Empty string when there's nothing to add, so the Builder prompt is
        unchanged when the Planner produced no usable plan."""
        if self.verdict != "BUILD":
            return ""
        parts: list[str] = []
        if self.approach.strip():
            parts += ["APPROACH (follow this design):", self.approach.strip()]
        if self.testable_ac:
            parts += ["", "TESTABLE ACCEPTANCE CRITERIA (write a FAILING test for EACH first, then implement until it passes):",
                      "\n".join(f"  {i+1}. {c}" for i, c in enumerate(self.testable_ac))]
        if self.in_scope_files:
            parts += ["", "IN-SCOPE FILES (keep the change to these unless you find a real reason):",
                      "\n".join(f"  - {p}" for p in self.in_scope_files)]
        return "\n".join(parts).strip()


def _prompt(ticket: Ticket) -> str:
    parts = [
        f"Ticket: {ticket.id} — {ticket.summary}",
        "",
        "DESCRIPTION:",
        (ticket.description or "(none)").strip()[:8000],
    ]
    if ticket.acceptance_criteria:
        ac = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(ticket.acceptance_criteria))
        parts += ["", "TICKET ACCEPTANCE CRITERIA (sharpen these into testable_ac):", ac]
    if ticket.labels:
        parts += ["", "LABELS:", ", ".join(ticket.labels)]
    if ticket.issue_type:
        parts += ["", "ISSUE TYPE:", ticket.issue_type]
    parts += ["", "Read the relevant files, then output the JSON plan (verdict + build fields)."]
    return "\n".join(parts)


_DECODER = json.JSONDecoder()


def _first_json_object(text: str) -> dict | None:
    """Pull the first JSON object out of a reply (tolerant of prose / code fences around it).

    Uses ``json.JSONDecoder.raw_decode`` from each ``{`` position — the decoder respects string
    literals and escapes, so braces INSIDE a string value (e.g. an ``approach`` that describes code
    like ``"wrap in a try { … }"``) don't break the scan. Naive brace-counting did (2026-07-06
    review, HIGH): such a plan silently fell back to an empty BUILD and the design brief was lost.
    Returns the first parsed dict, or None if none parses."""
    if not text:
        return None
    i = text.find("{")
    while i != -1:
        try:
            obj, _end = _DECODER.raw_decode(text, i)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        i = text.find("{", i + 1)
    return None


def _str_list(v: Any) -> list[str]:
    """Coerce a JSON value into a clean list of non-empty strings (tolerates a comma string)."""
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [p.strip() for p in re.split(r"[,\n]", v) if p.strip()]
    return []


def parse_plan(text: str | None) -> PlannerResult:
    """Parse the Planner's reply into a PlannerResult. Unit-testable without an agent.

    Fail-safe: an empty/garbled reply, or a missing/invalid verdict, yields a BUILD result with
    empty fields — so the Builder proceeds from the raw ticket and a Planner hiccup never blocks."""
    raw = (text or "").strip()
    obj = _first_json_object(raw)
    if not obj:
        return PlannerResult(verdict="BUILD", raw=raw)
    verdict = str(obj.get("verdict", "BUILD")).strip().upper()
    if verdict not in VALID_VERDICTS:
        verdict = "BUILD"
    return PlannerResult(
        verdict=verdict,
        approach=str(obj.get("approach", "") or "").strip(),
        testable_ac=_str_list(obj.get("testable_ac")),
        in_scope_files=_str_list(obj.get("in_scope_files")),
        answer=str(obj.get("answer", "") or "").strip(),
        raw=raw,
    )


async def plan(cfg: Config, ticket: Ticket, app=None, audit=None) -> PlannerResult:
    """Run the Planner on one ticket → a PlannerResult. Read-only, Opus-tier, high effort.

    Fail-safe: ANY error (SDK, network, parse) returns a BUILD result with empty fields, so the
    ticket always proceeds to the Builder — the Planner can only ADD guidance, never block."""
    from . import provider as _provider
    if app is None:
        app = cfg.app(ticket.app or cfg.apps[0].name)
    try:
        model, _peffort, mreason = models.for_planner(cfg, ticket, effort="high")
        if getattr(cfg, "auto_model", False) or "deep" in mreason:
            print(f"  · planner model: {mreason}", flush=True)
        try:
            from . import squad_pref as _squad
            _elite = _squad.resolve_for_ticket(cfg, ticket)[0] == "elite"
        except Exception:  # noqa: BLE001 - a pref hiccup must never change a plan
            _elite = False
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=memory.preamble() + PLANNER_SYSTEM
                          + (ELITE_PLAN_ADDENDUM if _elite else ""),
            cwd=app.workdir or app.repo_path,
            permission_mode="bypassPermissions",
            allowed_tools=["Read", "Grep", "Glob"],
            disallowed_tools=["Write", "Edit", "Bash", "NotebookEdit", "Task", "Agent"],
            # 24 turns, not 14: GLM-routed planners batch ~1 tool call per turn (Anthropic batches
            # many), so on monorepo tickets they hit the old cap mid-exploration and fail-safe into
            # a briefless BUILD — 4 of 6 GLM planner calls on 2026-07-16 (AUTO-155/156/157/137,
            # "Reached maximum number of turns (14)"), one of which (AUTO-156) then burned a full
            # build into a turn-limit park. A read-only planner turn is far cheaper than that.
            setting_sources=[], max_turns=24, effort="high",
        )
        run = await run_agent(_prompt(ticket), options, tag="planner", ticket_id=ticket.id)
        res = parse_plan(run.final or run.text)
        if res.verdict == "BUILD" and not res.testable_ac and not res.in_scope_files \
                and not res.approach:
            # EU-266: a garbled/JSON-less reply used to fall back to a SILENT briefless BUILD —
            # indistinguishable in audit from a designed one. Stamp the raw with the error marker
            # so the audit event (below) carries WHY the brief is empty; the build still proceeds
            # (fail-safe contract unchanged), it just stops lying about being planned.
            res.raw = (f"(planner error: unparseable plan reply — briefless BUILD; "
                       f"head: {(run.final or run.text or '')[:120]!r})")
        res.cost_usd = run.cost_usd
        res.num_turns = run.num_turns
        res.input_tokens = getattr(run, "input_tokens", 0)
        res.output_tokens = getattr(run, "output_tokens", 0)
        res.provider = run.provider
        res.model_version = run.model_version
    except Exception as exc:  # noqa: BLE001 — the Planner must never break a run; default to BUILD
        res = PlannerResult(verdict="BUILD", raw=f"(planner error: {type(exc).__name__}: {exc})")
        # Surface the swallowed failure in the live stream too — a briefless BUILD looks identical
        # to a designed one downstream (2026-07-16: AUTO-155/156/157 fail-safed silently; AUTO-156
        # then built briefless into a turn-limit park, with the burn unmetered).
        print(f"  · planner failed ({type(exc).__name__}: {exc}) — building without a design brief",
              flush=True)
    if audit is not None:
        try:
            extra = {}
            if res.raw.startswith("(planner error:"):
                extra["error"] = res.raw[:300]   # make the fail-safe diagnosable from audit alone
            audit.record("planner", ticket_id=ticket.id, verdict=res.verdict,
                         testable_ac=len(res.testable_ac), in_scope_files=len(res.in_scope_files),
                         cost_usd=round(res.cost_usd, 6), provider=res.provider,
                         model=res.model_version, **extra)
        except Exception:  # noqa: BLE001 — audit must never break a run
            pass
    return res
