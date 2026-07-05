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


# ── Domain-gap / specialist-roster preview path (EU-85) ─────────────────────────────────────── #
#
# EPHEMERAL vs PERMANENT: a specialist charter with ``ephemeral=True`` lives in memory for a
# SINGLE task build and is NEVER written to ``officers/``. It is synthesised on the spot by HR
# (``hr.synthesize_specialists``) and discarded when the build finishes. A PERMANENT officer
# (``ephemeral=False``) is a committed doctrine file in ``officers/<name>.md``, curated by the
# Engineering Manager (adjutant) and approved by the Commander. Promotion from ephemeral to
# permanent happens only when the same lane_key has been synthesised enough times to prove
# it is a recurring need (see ``hr.AUTO_PROMOTE_THRESHOLD``); it is always a PROPOSE-only step
# — the Commander must approve before the file is written.

def _format_roster_preview(domain: str, charters: list[dict]) -> str:
    """Render an ADVISORY preview of the ephemeral specialist roster for an out-of-lane ticket.

    The roster is exactly what ``hr.synthesize_specialists`` produced for ``domain`` — the FULL
    set of task-scoped specialists the ticket needs (an MQL5 ticket yields its algo developer,
    backtester, strategist, data analyst, …), not a single role.

    This report is **advisory only — it provisions nothing**. The real provisioning happens at
    BUILD time in the squad/HR delegation path (``squad.build_delegated`` →
    ``squad._run_synthesis`` → ``hr.synthesize_specialists``), where each specialist is dispatched
    as a soldier, verifies its own output via its ``domain_gate``, and auto-retires when the build
    completes (ephemeral — never written to ``officers/``). See hr.py for the ephemeral-vs-permanent
    contract and ``AUTO_PROMOTE_THRESHOLD``.

    Args:
        domain: Short domain label from ``detect_domain_gap`` (e.g. ``'mql5'``).
        charters: The roster from ``hr.synthesize_specialists`` (may be empty).

    Returns:
        A markdown string suitable for writing to ``adjutant-report.md``.
    """
    from . import hr  # function-level: avoids any import-order coupling with hr ↔ adjutant
    lines = [
        f"# Specialist-Roster Preview — domain: `{domain}` [EPHEMERAL · ADVISORY]",
        "",
        "> The ticket's primary domain is **outside** the standing web-stack squad lanes.",
        "> Below is the task-scoped specialist roster HR would synthesize for it.",
        "> ",
        "> **This is an advisory preview — it provisions nothing.** The real provisioning runs at",
        "> BUILD time via the squad/HR delegation path (`squad.build_delegated` →",
        "> `squad._run_synthesis` → `hr.synthesize_specialists`): each specialist is dispatched as a",
        "> soldier, verifies its own output via its `domain_gate`, and auto-retires when the build",
        "> finishes (ephemeral — never written to `officers/`).",
        "",
    ]
    if not charters:
        lines.append(
            "_HR returned no usable specialist charters for this domain (synthesis declined, "
            "empty, or unavailable). Nothing to preview._"
        )
        return "\n".join(lines)
    lines.append(f"## Proposed roster — {len(charters)} specialist(s)")
    lines.append("")
    for i, c in enumerate(charters, 1):
        lines.append(f"### {i}. {c.get('name', '?')}  ·  lane `{c.get('lane_key', '?')}`")
        lines.append(hr.render_charter(c))
        lines.append("")
    lines.append(
        "**No action is required here.** When this ticket is built with delegation enabled, the "
        "unit synthesizes and dispatches this roster automatically (Commander-approved in "
        "non-automode via the build-time Telegram gate)."
    )
    return "\n".join(lines)


def officers_dir(cfg: Config) -> Path:
    # Review fix (2026-07-05): officers doctrine is COMMITTED SOURCE at the repo root, not runtime
    # state. audit_path used to sit AT the repo root; QW2 moved it into a state/ subdirectory, so
    # hop out of that convention — otherwise hires would land in a gitignored state/officers/ that
    # no consumer reads. Still cfg-derived (not __file__-anchored) so tests stay in their tmp dirs.
    root = Path(cfg.audit_path).resolve().parent
    if root.name == "state":
        root = root.parent
    return root / "officers"


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


async def propose(cfg: Config, *, ticket_text: str | None = None) -> str:
    """Produce a personnel review, or an advisory specialist-roster preview for a domain-gap ticket.

    **Normal path** (``ticket_text`` is ``None`` or the ticket fits a squad lane):
    The Engineering Manager runs a full read-only agent pass over the audit signals + officer
    files and produces a hire / retire / retrain proposal in markdown.  The Commander approves;
    ``apply()`` executes the single approved action.

    **Domain-gap path** (``ticket_text`` provided — via ``general adjutant --ticket KEY`` — and no
    squad lane covers it):
    Instead of the hire/retire agent pass, the adjutant returns an ADVISORY preview of the FULL
    task-scoped specialist roster the ticket needs, by delegating to ``hr.synthesize_specialists``
    (the SAME synthesizer the build loop uses), so the preview shows every specialist — not a
    single role.  **This preview provisions nothing.**  The real provisioning is performed at build
    time by the squad/HR delegation path (``squad.build_delegated`` → ``squad._run_synthesis`` →
    ``hr.synthesize_specialists``), Commander-approved there via the build-time gate.

    Ephemeral vs permanent:
    - ``ephemeral=True`` means the specialist lives for ONE build only and is NEVER written to
      ``officers/``.  HR synthesises it on the spot (``hr.synthesize_specialists``) and discards
      it when the build finishes.
    - A permanent officer (``ephemeral=False``) is a committed doctrine file in
      ``officers/<name>.md``, curated here by the Engineering Manager and approved by the
      Commander before the file is written.
    - Promotion from ephemeral to permanent is a separate propose step: once the same lane_key
      has been synthesised ``hr.AUTO_PROMOTE_THRESHOLD`` times the Engineering Manager *proposes*
      the promotion — never auto-writes — and the Commander approves.

    Args:
        cfg: Global orchestrator configuration.
        ticket_text: Optional combined ticket summary + description + acceptance criteria.
            When provided, ``detect_domain_gap`` is called first; a detected gap short-circuits
            the normal agent pass and returns the advisory roster preview instead.

    Returns:
        A markdown string suitable for writing to ``adjutant-report.md``.
    """
    # ── EU-85: domain-gap detection — runs BEFORE the expensive agent pass ── #
    # When handed a specific ticket (via `general adjutant --ticket KEY`) whose primary domain
    # falls OUTSIDE the standing web-stack squad lanes, produce an ADVISORY preview of the FULL
    # task-scoped specialist roster the unit would synthesize — by delegating to the SAME
    # synthesizer the build loop uses (hr.synthesize_specialists), so the preview matches reality
    # (an MQL5 ticket previews its algo developer + backtester + strategist + data analyst, not a
    # single role). This PROVISIONS NOTHING: real provisioning happens at build time in the
    # squad/HR delegation path (squad.build_delegated → squad._run_synthesis → synthesize_specialists).
    if ticket_text is not None:
        from .squad import detect_domain_gap, SQUAD
        gap, domain = await detect_domain_gap(ticket_text, SQUAD)
        if gap and domain:
            print(
                f"  · adjutant: domain gap '{domain}' detected — previewing specialist roster",
                flush=True,
            )
            from . import hr
            # The approver auto-returns the roster: this is a READ-ONLY preview, not a
            # provisioning gate (the build-time Telegram gate in squad._run_synthesis is the
            # surface that governs real, Commander-approved provisioning).
            charters = await hr.synthesize_specialists(
                domain, ticket_text, cfg, approver=lambda _p: "y"
            )
            return _format_roster_preview(domain, charters)

    sig = collect_signals(cfg)
    cwd = str(Path(__file__).resolve().parent.parent)   # the CTO's repo root
    # EU-52: honor auto_model — the personnel-review pass sizes off effort and conserves under a tight
    # budget rather than always pinning Opus; with auto_model off the configured model is unchanged.
    from . import provider as _provider
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
    # EU-123: show actual provider+model in the live feed
    if getattr(cfg, "auto_model", False):
        display = _provider.format_provider_model(run.provider, run.model_version)
        print(f"  · adjutant · {display}", flush=True)
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
    from . import provider as _provider
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
    # EU-123: show actual provider+model in the live feed
    if getattr(cfg, "auto_model", False):
        display = _provider.format_provider_model(run.provider, run.model_version)
        print(f"  · adjutant-apply · {display}", flush=True)
    return f"Applied. Originals backed up at: {backup}\n\n" + (run.final or "(no summary)")
