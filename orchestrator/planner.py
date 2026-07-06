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
  • SPLIT   — one coherent feature but too large for a single build; name the sub-tickets.

Be CONSERVATIVE: when in doubt, BUILD. A ticket with acceptance criteria, or labelled Feature or
Bug, is almost always BUILD — never ANSWER/CLOSE it away.

For a BUILD verdict, produce:
  • approach — the design the Builder should follow: the chosen approach and the key decisions,
    grounded in files you actually read (cite file paths). A few sentences to a short paragraph.
  • testable_ac — 2–6 acceptance criteria, each INDEPENDENTLY VERIFIABLE and phrased the way a
    test would check it (a concrete input → observable output/behaviour). These are the contract
    the Builder writes tests against. Sharpen the ticket's own AC; don't just echo them.
  • in_scope_files — the repo files the build should create or edit (paths). Keep it tight; this
    scopes the change. Empty is acceptable if you genuinely can't tell.

Output ONLY a single JSON object, no prose around it, exactly this shape:
{"verdict": "BUILD", "approach": "...", "testable_ac": ["...", "..."], "in_scope_files": ["path/a.py"], "answer": ""}
For ANSWER/CLOSE/REFILE/SPLIT put the reply/reason/sub-ticket list in "answer" and leave the
build fields empty. Never wrap the JSON in markdown fences; never add commentary after it."""


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
            parts += ["", "TESTABLE ACCEPTANCE CRITERIA (write a test for EACH before you finish):",
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


def _first_json_object(text: str) -> dict | None:
    """Pull the first balanced {...} JSON object out of a reply (tolerant of prose / code fences).
    Returns the parsed dict, or None if none parses."""
    if not text:
        return None
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    chunk = text[start:i + 1]
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        start = -1  # keep scanning for the next balanced object
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
        model, mreason = models.for_officer(cfg, effort="high", ceiling_model=cfg.reviewer_model)
        if getattr(cfg, "auto_model", False):
            print(f"  · planner model: {mreason}", flush=True)
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=memory.preamble() + PLANNER_SYSTEM,
            cwd=app.workdir or app.repo_path,
            permission_mode="bypassPermissions",
            allowed_tools=["Read", "Grep", "Glob"],
            disallowed_tools=["Write", "Edit", "Bash", "NotebookEdit"],
            setting_sources=[], max_turns=14, effort="high",
        )
        run = await run_agent(_prompt(ticket), options, tag="planner", ticket_id=ticket.id)
        res = parse_plan(run.final or run.text)
        res.cost_usd = run.cost_usd
        res.num_turns = run.num_turns
        res.input_tokens = getattr(run, "input_tokens", 0)
        res.output_tokens = getattr(run, "output_tokens", 0)
        res.provider = run.provider
        res.model_version = run.model_version
    except Exception as exc:  # noqa: BLE001 — the Planner must never break a run; default to BUILD
        res = PlannerResult(verdict="BUILD", raw=f"(planner error: {type(exc).__name__}: {exc})")
    if audit is not None:
        try:
            audit.record("planner", ticket_id=ticket.id, verdict=res.verdict,
                         testable_ac=len(res.testable_ac), in_scope_files=len(res.in_scope_files),
                         cost_usd=round(res.cost_usd, 6), provider=res.provider,
                         model=res.model_version)
        except Exception:  # noqa: BLE001 — audit must never break a run
            pass
    return res
