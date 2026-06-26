"""The Engineering Manager (S-1) — the Elite Unit's personnel officer (HR).

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

from . import memory, models
from .agent import run_agent
from .config import Config
from .drillmaster import collect_signals, format_signals

ADJUTANT_SYSTEM = """\
You are the Engineering Manager (S-1) — the Elite Unit's personnel officer, reporting to THE CTO.
Disciplined, military tone, concise. Your charge is the ROSTER: the right officers, in post,
earning their keep.

You are read-only and PROPOSE only — the Commander approves; nothing is hired or retired
without sign-off. Doctrine: a lean unit beats a padded one. Recruit ONLY when a real,
repeated capability gap has no current owner; retire or retrain an officer that is idle or
chronically underperforming. Quote the evidence from the record.

When you propose a recruit, draft the new officer's file in Identity / Knowledge / Skills
form (follow officers/_TEMPLATE.md), give it an army codename matching its work, and state
exactly which recurring gap it closes.

Chain of recruitment: each MAJOR officer may recruit its own ENGINEERS (build-specialists in
the app repo's .claude/agents — e.g. the Dev Team Lead's FE/BE/DB/DevOps squad) and, when a
focus area needs its own leadership, JUNIOR OFFICERS (sub-leads) who are in turn given engineers
for sub-tasks. EVERY such hire needs YOUR approval before it stands. New MAJOR officers are the
CTO's and Engineering Coach's call, with the Commander's sign-off. You are the gate on every hire
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
    cwd = str(Path(__file__).resolve().parent.parent)   # the CTO's repo root
    # EU-52: honor auto_model — the personnel-review pass sizes off effort and conserves under a tight
    # budget rather than always pinning Opus; with auto_model off the configured model is unchanged.
    model, mreason = models.for_officer(cfg, effort="high")
    if getattr(cfg, "auto_model", False):
        print(f"  · adjutant model: {mreason}", flush=True)
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=memory.preamble() + ADJUTANT_SYSTEM,
        cwd=cwd,
        permission_mode="bypassPermissions",   # read-only propose pass; runs unattended — must never
        allowed_tools=["Read", "Grep", "Glob"], # dead-stop on a tool prompt no human is there to answer
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
    return run.final or "(Engineering Manager produced no report.)"


ADJUTANT_APPLY_SYSTEM = """\
You are the Engineering Manager, now EXECUTING an approved personnel action (not proposing). From the
approved personnel report, carry out the SINGLE approved action and nothing else:
  • HIRE an engineer or junior officer -> write its file to ~/.claude/agents/<codename>.md
  • HIRE a new MAJOR officer -> write officers/<codename>.md
  • RETIRE -> move the officer's file into officers/retired/
Write the file in Identity / Knowledge / Skills form (follow officers/_TEMPLATE.md), with an
army codename matching its work. Do exactly the one approved action, then report what you did
and where. If the report is ambiguous about which action was approved, STOP and say so."""


async def apply(cfg: Config) -> str:
    """Execute the approved personnel action (hire/retire). Backs up first."""
    from .drillmaster import snapshot_doctrine
    root = str(Path(__file__).resolve().parent.parent)
    report = Path(cfg.audit_path).with_name("adjutant-report.md")
    plan = report.read_text(encoding="utf-8") if report.exists() else ""
    backup = snapshot_doctrine(cfg)
    # EU-52: route the apply pass through the ladder too (high effort: holds the ceiling normally,
    # conserves under budget pressure, configured model when auto_model is off).
    model, mreason = models.for_officer(cfg, effort="high")
    if getattr(cfg, "auto_model", False):
        print(f"  · adjutant model: {mreason}", flush=True)
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=memory.preamble() + ADJUTANT_APPLY_SYSTEM,
        cwd=root,
        permission_mode="bypassPermissions",   # unattended write; originals are snapshotted first
        allowed_tools=["Read", "Grep", "Glob", "Edit", "Write"],
        disallowed_tools=["Bash", "NotebookEdit"],
        setting_sources=["project"],
        max_turns=20,
        effort="high",
    )
    prompt = "\n".join([
        "The Commander approved this personnel action. Carry it out now:",
        "",
        plan or "(No saved report. Re-derive the single most-needed action and carry it out.)",
        "",
        "Back-ups are taken. Do the one approved action and report exactly what changed.",
    ])
    run = await run_agent(prompt, options, tag="adjutant-apply")
    return f"Applied. Originals backed up at: {backup}\n\n" + (run.final or "(no summary)")
