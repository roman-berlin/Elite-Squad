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

import copy
import types
import json
import re
from dataclasses import dataclass, field
from typing import TypedDict

from claude_agent_sdk import ClaudeAgentOptions

from . import backends, memory, models
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

TWO SIZING RULES THE FILE-COUNT TEST MISSES (2026-07-22 — both cost a full wasted pass):

  · PRIOR WALL-CLOCK TIMEOUT. If the ticket text carries a "Prior attempts" section showing a
    previous run hit a wall-clock/turn limit, that is PROOF this ticket is too big for one pass —
    regardless of how few files it touches. Do not hand back the same scope and hope; SPLIT it, or
    narrow the BUILD to the smallest slice that can finish, and say in "answer" what you deferred.
    EU-438 is the case that taught this: it timed out at 3600s, was re-planned as BUILD unchanged
    because it touches almost no files, and timed out again.

  · OPEN-ENDED INVESTIGATION. "Investigate/diagnose/find out why X" is bounded in FILES but
    unbounded in TIME — the file-count rule can never catch it. Convert it into a bounded question
    with a definite stopping condition ("determine whether A or B causes X, by doing Y"), and if the
    investigation and the resulting fix are both substantial, SPLIT them: diagnose first, fix second.

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

# ALWAYS appended to PLANNER_SYSTEM (2026-07-22). The Analyst's decompose-first duties: an ordered
# step plan the Builder executes one-at-a-time, and every assumption/question surfaced NOW —
# because a mid-build "stop and wait" strands a worktree; questions batch here, at analysis time.
# Previously gated behind the "elite squad" mode; that mode is retired — decomposing before
# building is correct for every ticket, and effort now scales per ticket via size_ticket().
PLAN_ADDENDUM = """

WORKING METHOD — you are the Analyst; the Builder will execute your plan one step at a time,
running the repo's own checks after every step. Two extra duties on a BUILD verdict:
1. In "steps" (or the plan body), give an ORDERED list of small, independently-checkable steps —
   each one small enough to implement and verify in one sitting, sequenced so every step leaves
   the tree green. Name the verification for each step (which test/command proves it).
2. Surface EVERY material assumption and unclear point NOW, in the plan — the Builder will not
   stop mid-build to ask. A genuinely CRITICAL unknown that blocks safe work is a needs_human
   question at this stage, not a guess.
Calibrate SPLIT honestly: the Builder works step-by-step, and its turn budget scales with the
ticket's own size (a large ticket earns a large budget; a small one does not). Prefer
BUILD-with-step-plan for large-but-coherent work; reserve SPLIT for genuinely unrelated asks or
repo-wide sweeps that no single careful pass can land.
"""


class PlannerResultDict(TypedDict):
    """The serialized PlannerResult shape (to_dict) handed to the Builder and the audit."""
    verdict: str
    approach: str
    testable_ac: list[str]
    in_scope_files: list[str]
    answer: str


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
    # EU-833: true when the agent returned nothing useful (zero tokens / unparseable → refusal),
    # even if parse_plan produced a BUILD (its fail-safe default). The loop intercepts this to
    # park rather than build blind. Always False on a legitimate BUILD with real content.
    refused: bool = False

    def to_dict(self) -> PlannerResultDict:
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


def _str_list(v: object) -> list[str]:
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


def _is_refusal(res: PlannerResult, run) -> bool:
    """Return True when the agent call produced nothing useful — a refusal that should NOT build.

    A refusal is one of:
      * The raw reply was stamped by EU-266 as unparseable (the JSON parser failed and the
        code marked it with '(planner error:' so the downstream verifier can spot it); or
      * The agent returned zero output tokens AND the plan has no usable content (an empty
        build with zero tokens is a contradiction — the model didn't answer).
    """
    if res.raw.startswith("(planner error:"):
        return True
    if res.verdict == "BUILD" and run.output_tokens == 0:
        # Zero tokens = model didn't answer. An empty build here is impossible with real content;
        # refuse rather than proceed blind. (If approach exists despite 0 tokens it's a data bug
        # in the caller, not a genuine refusal signal.)
        if not res.approach:
            return True
    return False


def _fanout_addendum(max_agents: int) -> str:
    """Instructions for a Planner that may fan out. Only composed when the gate armed (L/XL ticket,
    native backend, enabled in config) — a Planner that cannot spawn must never be told it can.

    The value of a fan-out is INDEPENDENCE, not volume: several investigators each reading a
    different subsystem surface things one context misses, and a claim that survives a skeptic is
    worth more than one nobody checked. Volume alone just multiplies the same blind spot, which is
    why the cap is small and the last instruction is to reconcile disagreements rather than average
    them."""
    return f"""

---

INVESTIGATE IN PARALLEL (this ticket is large — you have the Task tool)

You may launch up to {max_agents} READ-ONLY sub-agents to investigate before you write the plan.
Use them when the ticket spans several subsystems, or when the right approach genuinely depends on
facts you do not yet have. Do NOT use them for a change whose shape is already obvious — a fan-out
on simple work is pure cost.

How to use them well:
1. SPLIT BY SUBSYSTEM, not by task. Give each sub-agent a DIFFERENT area (e.g. "the auth middleware
   and its tests", "the DB layer and migrations", "every existing caller of this API"). Overlapping
   briefs return overlapping findings and teach you nothing new.
2. ASK FOR EVIDENCE, not opinions. Each brief must demand concrete file:line references and the
   actual current behaviour — never "assess whether this is a good idea".
3. Launch them in ONE message so they run concurrently.
4. RECONCILE, don't average. When two sub-agents disagree, say so explicitly in your plan and state
   which you believe and why — a disagreement is a finding. Never quietly split the difference.
5. Anything a sub-agent could not verify goes in the plan as an open assumption/question, exactly
   like your own unverified assumptions. A sub-agent's guess is still a guess.

Your own output contract is UNCHANGED: the same JSON plan, the same fields. The sub-agents inform
it; they do not replace it, and you remain accountable for every claim in it.
"""


async def plan(cfg: Config, ticket: Ticket, app=None, audit=None) -> PlannerResult:
    """Run the Planner on one ticket → a PlannerResult. Read-only, Opus-tier, high effort.

    EU-833 retry-on-refusal: when the agent returns nothing useful (zero tokens / unparseable),
    retry ONCE on a different model from ``models.LADDER``. If the retry also refuses, set
    ``res.refused = True`` so the loop parks rather than builds blind. Only active when the
    ``planner_refusal_park`` knob is truthy (default True).

    Fail-safe: with the knob OFF, ANY error (SDK, network, parse) returns a BUILD result with
    empty fields, so the ticket always proceeds to the Builder — the Planner can only ADD guidance,
    never block."""
    from . import provider as _provider
    if app is None:
        app = cfg.app(ticket.app or cfg.apps[0].name)

    _can_retry = bool(getattr(cfg, "planner_refusal_park", True))
    _attempt = 0          # how many calls have been made (0-based)
    # EU-833: accumulate burn across retries; assign to ``res`` once at the end.
    _total_cost = 0.0
    _total_turns = 0
    _total_input = 0
    _total_output = 0
    _last_run = types.SimpleNamespace(
        output_tokens=0, cost_usd=0.0, num_turns=0, input_tokens=0,
        provider="", model_version="", final="", text="")
    while True:
        try:
            # 2026-07-22: the Planner's effort now scales with the TICKET instead of being pinned
            # at "high" for everything. size_ticket() already grades every ticket for the Builder;
            # a planner that thinks equally hard about a typo and an architecture migration was
            # both wasteful and under-powered at the top end. Floored at medium — planning is
            # cheap relative to building, and a thin plan costs a whole build pass, so we never go
            # below medium here even when the sizer says low.
            _p_effort = "high"
            _p_size = ""
            try:
                from .builder import size_ticket as _size
                _p_size, _sized_effort, _ = _size(ticket)
                _p_effort = (_sized_effort if _sized_effort in ("high", "xhigh", "max")
                             else "medium")
            except Exception:  # noqa: BLE001 — a sizer hiccup must never block a plan
                _p_effort = "high"
            model, _peffort, mreason = models.for_planner(cfg, ticket, effort=_p_effort)
            if getattr(cfg, "auto_model", False) or "deep" in mreason:
                print(f"  · planner model: {mreason}", flush=True)
            # Fan-out gate (all three must hold, else the Planner stays single-agent as before):
            #   · enabled in config, · the ticket is genuinely large (L/XL), · the effective
            #     backend is NATIVE — Task is a Claude Code capability and the GLM compat
            #     endpoint is not guaranteed to serve it, so arming it there would burn turns
            #     on a tool that never works.
            _fanout = False
            try:
                _fanout = (bool(getattr(cfg, "planner_fanout", True))
                           and _p_size in ("L", "XL")
                           and backends.normalize(
                               backends.current_for_tag("planner")) == backends.NATIVE)
            except Exception:  # noqa: BLE001 — never let the gate itself break planning
                _fanout = False
            _max_agents = max(1, int(getattr(cfg, "planner_fanout_max_agents", 4) or 4))
            if _fanout:
                print(f"  · planner: fan-out armed (≤{_max_agents} read-only sub-agents, "
                      f"≤${float(getattr(cfg, 'planner_fanout_budget_usd', 3.0)):.2f})",
                      flush=True)

            # ---- pick a different model on retry ----
            if _attempt > 0:
                _first_family = models.family_of(model)
                for _t in range(models.tier_of(model) - 1, -1, -1):
                    _alt = models.model_at(_t)
                    if _alt != _first_family:
                        model = _alt
                        print(f"  · planner retry → {model} (original {_first_family} refused)",
                              flush=True)
                        break
                else:
                    # already at lowest tier — stay put and try again
                    print(f"  · planner retry: already lowest tier ({model}); trying anyway",
                          flush=True)

            # ---- compose options once, clone+swap model on retry ----
            if _attempt == 0:
                _saved_options = ClaudeAgentOptions(
                    model=model,
                    system_prompt=memory.preamble() + PLANNER_SYSTEM
                                  + PLAN_ADDENDUM
                                  + (_fanout_addendum(_max_agents) if _fanout else ""),
                    cwd=app.workdir or app.repo_path,
                    permission_mode="bypassPermissions",
                    allowed_tools=(["Read", "Grep", "Glob", "Task"] if _fanout
                                   else ["Read", "Grep", "Glob"]),
                    disallowed_tools=(["Write", "Edit", "Bash", "NotebookEdit"] if _fanout
                                      else ["Write", "Edit", "Bash", "NotebookEdit", "Task", "Agent"]),
                    max_budget_usd=(float(
                        getattr(cfg, "planner_fanout_budget_usd", 3.0)) if _fanout else None),
                    setting_sources=[], max_turns=24, effort=_p_effort,
                )
            _options = copy.copy(_saved_options)
            _options.model = model

            run = await run_agent(_prompt(ticket), _options, tag="planner", ticket_id=ticket.id)
            res = parse_plan(run.final or run.text)
            _last_run = run
            if res.verdict == "BUILD" and not res.testable_ac and not res.in_scope_files \
                    and not res.approach:
                # EU-266: a garbled/JSON-less reply used to fall back to a SILENT briefless BUILD
                # — indistinguishable in audit from a designed one. Stamp the raw with the error
                # marker so the audit event (below) carries WHY the brief is empty; the build still
                # proceeds (fail-safe contract unchanged), it just stops lying about being planned.
                res.raw = (f"(planner error: unparseable plan reply — briefless BUILD; "
                           f"head: {(run.final or run.text or '')[:120]!r})")
            # EU-833: accumulate burn per-call; assign totals to ``res`` only at the very end.
            _total_cost += run.cost_usd
            _total_turns += run.num_turns
            _total_input += getattr(run, "input_tokens", 0)
            _total_output += getattr(run, "output_tokens", 0)
        except Exception as exc:  # noqa: BLE001 — the Planner must never break a run; default to
                                 # BUILD. EU-833: on refusal, retry once on a different model
                                 # before parking.
            res = PlannerResult(verdict="BUILD",
                                raw=f"(planner error: {type(exc).__name__}: {exc})")
            _last_run = types.SimpleNamespace(output_tokens=0, cost_usd=0.0, num_turns=0,
                                              input_tokens=0, provider="", model_version="")
            _total_output = 0   # ensures _is_refusal picks it up
            print(f"  · planner failed ({type(exc).__name__}: {exc}) — ", end="", flush=True)

        _attempt += 1

        # EU-833: detect refusal (stamp OR zero-tokens) and retry once on a different model.
        # Burn already added to _total_* above.
        if _is_refusal(res, _last_run) and _can_retry and _attempt < 2:
            print("refused — retrying on different model …", flush=True)
            continue       # loop body → different model → fresh run_agent call

        # ---- done (final attempt or refused-and-knob-off): settle the result ----
        if _is_refusal(res, _last_run) and not _can_retry:
            res.refused = True
            print(f"  · planner refused (no retry — knob off) — building without a design brief",
                  flush=True)
        elif _is_refusal(res, _last_run):
            res.refused = True
            print(f"  · planner refused (retry exhausted) — building without a design brief",
                  flush=True)

        # Assign accumulated burn (EU-833: totals persist across retries).
        res.cost_usd = _total_cost
        res.num_turns = _total_turns
        res.input_tokens = _total_input
        res.output_tokens = _total_output
        if _last_run:
            res.provider = _last_run.provider
            res.model_version = _last_run.model_version

        break                        # final result — exit loop

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
