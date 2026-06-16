"""The Drillmaster — the unit's training officer (compounding improvement).

Reads the operation's track record (audit.jsonl) + the officers' current
instructions, finds RECURRING weaknesses, and proposes specific upgrades to the
officers' Identity/Knowledge/Skills. It is read-only and **proposes only** — you
(the Commander) approve and apply. This is how the team gets better every day
without adding officers.

  general drill            # prints + writes drill-report.md
  general drill --telegram # also pings you a summary
"""
from __future__ import annotations

import collections
import json
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from .agent import run_agent
from .config import Config
from .dashboard import load_tasks

DRILLMASTER_SYSTEM = """\
You are the Drillmaster — the R&D unit's training officer. Your job is continuous
improvement: study the unit's track record and the officers' current instructions, find
RECURRING weaknesses (not one-offs), and propose precise upgrades to the officers'
Identity / Knowledge / Skills so the same mistake does not happen twice.

You are read-only. You PROPOSE; the Commander approves and applies. Be specific and
surgical — quote the exact instruction to change and give the replacement. Prefer a few
high-leverage changes over a long list. If the unit is performing well, say so and propose
at most one sharpening.

Output (concise, markdown):
1. **Read of the unit** — 2-3 lines on how the team is doing, from the signals.
2. **Recurring weaknesses** — each with the evidence (which signal) and the root cause.
3. **Proposed upgrades** — per officer (Engineer / Inspector / a squad role): the exact
   instruction or SOP line to add/replace, and why. Keep each actionable.
4. **Recruit? (only if needed)** — if a recurring weakness has NO current owner, recommend
   hiring ONE new officer and draft its file in Identity / Knowledge / Skills form (follow
   officers/_TEMPLATE.md). Only when a real, repeated gap exists — never to pad the roster.
5. **One drill for tomorrow** — the single highest-leverage change to make first.

You PROPOSE upgrades and hires; the Commander approves and applies. Lean beats large.
"""


def collect_signals(cfg: Config) -> dict:
    """Aggregate the track record from the audit log (pure, no LLM)."""
    tasks = load_tasks(cfg.audit_path)
    sig = {
        "tasks": len(tasks),
        "outcomes": collections.Counter(),
        "retried_tasks": 0,
        "max_effort_hits": 0,
        "issue_areas": collections.Counter(),
        "gate_fails": 0,
        "needs_human": 0,
        "avg_passes": 0.0,
    }
    total_passes = 0
    for t in tasks:
        sig["outcomes"][t["outcome"] or "running"] += 1
        total_passes += t["passes"]
        if t["passes"] > 1:
            sig["retried_tasks"] += 1
        for d in t["passes_list"]:
            if d.get("effort") == "max":
                sig["max_effort_hits"] += 1
            for iss in d.get("issues", []) or []:
                sig["issue_areas"][iss.get("area", "?")] += 1
    sig["avg_passes"] = round(total_passes / len(tasks), 2) if tasks else 0.0

    path = Path(cfg.audit_path)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") == "gate" and ev.get("passed") is False:
                sig["gate_fails"] += 1
            elif ev.get("event") == "needs_human":
                sig["needs_human"] += 1
    return sig


def format_signals(sig: dict) -> str:
    outcomes = ", ".join(f"{k}: {v}" for k, v in sig["outcomes"].most_common()) or "none"
    areas = ", ".join(f"{k} ×{v}" for k, v in sig["issue_areas"].most_common(8)) or "none"
    return (
        f"Tasks run: {sig['tasks']}\n"
        f"Outcomes: {outcomes}\n"
        f"Avg passes/ticket: {sig['avg_passes']} | retried: {sig['retried_tasks']} | "
        f"hit max effort: {sig['max_effort_hits']}\n"
        f"Gate failures: {sig['gate_fails']} | decisions needed: {sig['needs_human']}\n"
        f"Recurring Inspector issue areas: {areas}"
    )


def _prompt(sig: dict, cfg: Config) -> str:
    return "\n".join([
        "Here is the unit's recent track record (from the audit log):",
        "",
        format_signals(sig),
        "",
        "The officers' current instructions are in `officers/*.md` (Engineer, Inspector, "
        "and any squad roles) and each app's CLAUDE.md. Read what you need.",
        "",
        "Produce the drill report.",
    ])


async def drill(cfg: Config) -> str:
    sig = collect_signals(cfg)
    cwd = str(Path(cfg.audit_path).resolve().parent)
    options = ClaudeAgentOptions(
        model=cfg.reviewer_model,
        system_prompt=DRILLMASTER_SYSTEM,
        cwd=cwd,
        permission_mode="default",
        allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash", "MultiEdit"],
        setting_sources=["project"],
        max_turns=20,
        effort="high",
    )
    run = await run_agent(_prompt(sig, cfg), options, tag="drillmaster")
    return run.final or "(Drillmaster produced no report.)"
