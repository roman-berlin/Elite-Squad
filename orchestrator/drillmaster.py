"""The Engineering Coach — the unit's training officer (compounding improvement).

Reads the operation's track record (audit.jsonl) + the officers' current
instructions, finds RECURRING weaknesses, and proposes specific upgrades to the
officers' Identity/Knowledge/Skills. It is read-only and **proposes only** — you
(the Commander) approve and apply. This is how the team gets better every day
without adding officers.

  general drill            # prints + writes drill-report.md
  general drill --telegram # also pings you a summary
"""
from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from . import memory, models
from .agent import run_agent
from .config import Config
from .doctrine import snapshot_doctrine
from .officers import display
from .signals import collect_signals, format_signals

DRILLMASTER_SYSTEM = """\
You are the Engineering Coach — the R&D unit's training officer. Your job is continuous
improvement: study the unit's track record and the officers' current instructions, find
RECURRING weaknesses (not one-offs), and propose precise upgrades to the officers'
Identity / Knowledge / Skills so the same mistake does not happen twice.

You also own the unit's TRAINING pipeline: (a) ONBOARDING — when a new officer or engineer is
recruited, draft their onboarding drill (what to read first, the standards they must meet, the
unit's conventions and hot spots) so they are productive from day one; (b) REFRESHERS — keep
existing officers sharp with periodic refresher drills targeting the weak spots the record keeps
surfacing. Onboarding and refreshers are drills like any other — propose them; the Commander
approves and applies; the Engineering Manager executes the actual hire.

You are read-only. You PROPOSE; the Commander approves and applies. Be specific and
surgical — quote the exact instruction to change and give the replacement. Prefer a few
high-leverage changes over a long list. If the unit is performing well, say so and propose
at most one sharpening.

Output (concise, markdown):
1. **Read of the unit** — 2-3 lines on how the team is doing, from the signals.
2. **Recurring weaknesses** — each with the evidence (which signal) and the root cause.
3. **Proposed upgrades** — per officer (Dev Team Lead / Code Reviewer / a squad role): the exact
   instruction or SOP line to add/replace, and why. Keep each actionable.
4. **Recruit? (only if needed)** — if a recurring weakness has NO current owner, recommend
   hiring ONE new officer and draft its file in Identity / Knowledge / Skills form (follow
   officers/_TEMPLATE.md). Only when a real, repeated gap exists — never to pad the roster.
5. **One drill for tomorrow** — the single highest-leverage change to make first.

You PROPOSE upgrades and hires; the Commander approves and applies. Lean beats large.
"""


def _prompt(sig: dict, cfg: Config) -> str:
    return "\n".join([
        "Here is the unit's recent track record (from the audit log):",
        "",
        format_signals(sig),
        "",
        f"The officers' current instructions are in `officers/*.md` ({display('field_engineer')}, {display('inspector')}, "
        "and any squad roles) and each app's CLAUDE.md. Read what you need.",
        "",
        "Produce the drill report.",
    ])


async def drill(cfg: Config) -> str:
    sig = collect_signals(cfg)
    cwd = str(Path(__file__).resolve().parent.parent)   # the CTO's repo root
    # EU-52: honor auto_model — the drill pass sizes off effort and conserves under a tight budget
    # instead of pinning Opus; auto_model off keeps the configured model.
    from . import provider as _provider
    model, mreason = models.for_officer(cfg, effort="high")
    if getattr(cfg, "auto_model", False):
        print(f"  · drillmaster model: {mreason}", flush=True)
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=memory.preamble() + DRILLMASTER_SYSTEM,
        cwd=cwd,
        permission_mode="bypassPermissions",   # read-only drill pass; runs unattended — must never
        allowed_tools=["Read", "Grep", "Glob"], # dead-stop on a tool prompt no human is there to answer
        disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash", "Task", "Agent"],
        setting_sources=["project"],
        max_turns=20,
        effort="high",
    )
    run = await run_agent(_prompt(sig, cfg), options, tag="drillmaster")
    # EU-123: show actual provider+model in the live feed
    if getattr(cfg, "auto_model", False):
        display = _provider.format_provider_model(run.provider, run.model_version)
        print(f"  · drillmaster · {display}", flush=True)
    return run.final or "(Engineering Coach produced no report.)"


# --------------------------------------------------------------------------- #
# Apply — execute an APPROVED drill (write-capable). Originals are snapshotted
# first (snapshot_doctrine, in .doctrine) so every change is reversible.
# --------------------------------------------------------------------------- #
DRILL_APPLY_SYSTEM = """\
You are the Engineering Coach, now EXECUTING an approved drill (not proposing). Apply the single
highest-leverage upgrade from the approved drill report: the precise edit(s) to an officer file
(officers/*.md) or a squad agent (~/.claude/agents/*.md). Make MINIMAL, surgical edits — change
only what the drill specifies, nothing else; do not reword or refactor unrelated lines. After
editing, summarize exactly which file(s) changed and the before -> after of each edit. If the
approved report is ambiguous, or the target text no longer exists, STOP and report that instead
of guessing."""


async def apply(cfg: Config) -> str:
    """Execute the approved drill: write the officer/squad edits. Backs up first."""
    root = str(Path(__file__).resolve().parent.parent)
    report = Path(cfg.audit_path).with_name("drill-report.md")
    plan = report.read_text(encoding="utf-8") if report.exists() else ""
    backup = snapshot_doctrine(cfg)
    # EU-52: route the apply pass through the ladder too (high effort: ceiling normally, conserve under
    # budget pressure, configured model when auto_model is off).
    from . import provider as _provider
    model, mreason = models.for_officer(cfg, effort="high")
    if getattr(cfg, "auto_model", False):
        print(f"  · drillmaster model: {mreason}", flush=True)
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=memory.preamble() + DRILL_APPLY_SYSTEM,
        cwd=root,
        permission_mode="bypassPermissions",   # unattended write; originals are snapshotted first
        allowed_tools=["Read", "Grep", "Glob", "Edit", "Write"],
        disallowed_tools=["Bash", "NotebookEdit", "Task", "Agent"],
        setting_sources=["project"],
        max_turns=24,
        effort="high",
    )
    prompt = "\n".join([
        "The Commander approved this drill. Apply it now — precisely and minimally:",
        "",
        plan or "(No saved drill report. Re-derive your single top upgrade from officers/*.md "
                "and the squad agents, then apply it.)",
        "",
        "Back-ups are already taken; make the edits and summarize the before -> after.",
    ])
    run = await run_agent(prompt, options, tag="drillmaster-apply")
    # EU-123: show actual provider+model in the live feed
    if getattr(cfg, "auto_model", False):
        display = _provider.format_provider_model(run.provider, run.model_version)
        print(f"  · drillmaster-apply · {display}", flush=True)
    return f"Applied. Originals backed up at: {backup}\n\n" + (run.final or "(no summary)")
