"""The Adjutant (S-1) — the Elite Unit's personnel officer (HR).

Keeps the right officers in post: recommends recruiting a new officer when a real, repeated
capability gap has no owner, and retiring/retraining one that is idle or chronically weak.
It **proposes**; the Commander approves. `hire`/`retire` are the apply-actions (writing or
shelving an officer file) used only once a proposal is approved. Doctrine: never pad the
roster — every officer must earn its post.

  general adjutant             # personnel review (propose-only) -> adjutant-report.md
  general adjutant --telegram  # also brief the Commander
"""
from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from .agent import run_agent
from .config import Config
from .drillmaster import collect_signals, format_signals

ADJUTANT_SYSTEM = """\
You are the Adjutant (S-1) — the Elite Unit's personnel officer, reporting to THE GENERAL.
Disciplined, military tone, concise. Your charge is the ROSTER: the right officers, in post,
earning their keep.

You are read-only and PROPOSE only — the Commander approves; nothing is hired or retired
without sign-off. Doctrine: a lean unit beats a padded one. Recruit ONLY when a real,
repeated capability gap has no current owner; retire or retrain an officer that is idle or
chronically underperforming. Quote the evidence from the record.

When you propose a recruit, draft the new officer's file in Identity / Knowledge / Skills
form (follow officers/_TEMPLATE.md), give it an army codename matching its work, and state
exactly which recurring gap it closes.

Chain of recruitment: each MAJOR officer may recruit its own SOLDIERS (build-specialists in
the app repo's .claude/agents — e.g. the Field Engineer's FE/BE/DB/DevOps squad) and, when a
focus area needs its own leadership, JUNIOR OFFICERS (sub-leads) who are in turn given soldiers
for sub-tasks. EVERY such hire needs YOUR approval before it stands. New MAJOR officers are the
General's and Drillmaster's call, with the Commander's sign-off. You are the gate on every hire
— keep the corps lean; approve only against a real, repeated need."""


def officers_dir(cfg: Config) -> Path:
    return Path(cfg.audit_path).resolve().parent / "officers"


def list_officers(cfg: Config) -> list[str]:
    d = officers_dir(cfg)
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.md") if p.stem not in ("README", "_TEMPLATE"))


def hire(cfg: Config, name: str, body: str, force: bool = False) -> Path:
    """Apply an APPROVED recruit: write officers/<name>.md."""
    d = officers_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.md"
    if f.exists() and not force:
        raise FileExistsError(f"officer '{name}' is already in post (use force to overwrite)")
    f.write_text(body, encoding="utf-8")
    return f


def retire(cfg: Config, name: str) -> Path:
    """Apply an APPROVED retirement: shelve officers/<name>.md under officers/retired/."""
    d = officers_dir(cfg)
    f = d / f"{name}.md"
    if not f.exists():
        raise FileNotFoundError(f"no officer '{name}' in post")
    ret = d / "retired"
    ret.mkdir(parents=True, exist_ok=True)
    dest = ret / f"{name}.md"
    f.replace(dest)
    return dest


async def propose(cfg: Config) -> str:
    sig = collect_signals(cfg)
    cwd = str(Path(__file__).resolve().parent.parent)   # the General's repo root
    options = ClaudeAgentOptions(
        model=cfg.reviewer_model,
        system_prompt=ADJUTANT_SYSTEM,
        cwd=cwd,
        permission_mode="default",
        allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash"],
        setting_sources=["project"],
        max_turns=14,
        effort="high",
    )
    prompt = "\n".join([
        "The Elite Unit's recent record:", "", format_signals(sig), "",
        f"Current roster (officers in post): {', '.join(list_officers(cfg)) or 'none found'}.",
        "Their files are in officers/*.md — read what you need.", "",
        "Produce a concise personnel review:",
        "1. **Roster read** — who is in post and whether the work justifies them.",
        "2. **Recruit?** — only for a real, repeated gap with no owner; draft the officer file.",
        "3. **Retire / retrain?** — any idle or chronically-underperforming officer.",
        "4. **One personnel action this week** — the single highest-leverage move.",
        "Propose only; the Commander approves.",
    ])
    run = await run_agent(prompt, options, tag="adjutant")
    return run.final or "(Adjutant produced no report.)"
