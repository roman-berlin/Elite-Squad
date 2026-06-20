"""The living roster — who's in the unit and what each one does.

A once-a-day, INFO-ONLY document: the chain of command, every officer and their duty, and the soldiers
each squad commands — plus a short "state of the unit today" line written by the cheapest model (it's
just a status blurb, not a decision). The structure and duties are DETERMINISTIC (read straight from the
code, so the doc can never drift from reality); only the one-line daily status uses a model, and even
that is best-effort — if it fails the doc still generates.

Refreshed at the end of the daily council; also `general roster` on demand, and viewable in the cockpit.
"""
from __future__ import annotations

from pathlib import Path
import time

from .config import Config

# (name, role, duty, which configured model attribute it runs on — None = deterministic, no model)
_OFFICERS = [
    ("The General", "Orchestrator", "Chairs the unit, talks 1:1 with you, synthesises the daily council, "
     "and routes your guidance to the officers.", "discussion_model"),
    ("Adjutant", "S-1 · Personnel", "Owns the roster — proposes hires/retirements when a real capability "
     "gap appears (you approve and apply).", "reviewer_model"),
    ("Product Manager", "S-5 · Product", "Makes the product / IA / scope calls the Builder can't make "
     "alone, so the unit keeps shipping; escalates only the critical, irreversible ones.", "reviewer_model"),
    ("Field Engineer", "Builder", "Implements each ticket on an isolated worktree; for a big ticket, "
     "splits the work across its squad of soldiers.", "builder_model"),
    ("Inspector General", "Reviewer", "Quality & risk gate — reviews every change, demands fixes, and "
     "guards the standard before anything merges.", "reviewer_model"),
    ("Scout", "S-2 · QA / Recon", "Hunts what actually breaks in the running app on DEV — runtime, UX, "
     "accessibility — and files findings as tickets.", "discussion_model"),
    ("Provost Marshal", "Security", "The security gate — blocks a merge on a CRITICAL/HIGH finding "
     "(secrets, tenant-isolation, injection, vulnerable deps).", "discussion_model"),
    ("Quartermaster", "S-4 · Deploy readiness", "Certifies whether DEV can actually ship to MAIN — build, "
     "types, migrations, deps, env, deploy config.", "discussion_model"),
    ("Sentinel", "S-3 · Integration & rollback", "Runs the heavier post-merge suite on the landed DEV and "
     "reverts the merge forward-only if it breaks. Deterministic — no model.", None),
    ("Drillmaster", "Doctrine & Training", "The unit studies every day — proposes the one drill (an edit to "
     "an officer's charter) with the most compounding gain; owns onboarding.", "reviewer_model"),
]

# soldiers a squad can field (read from squad.SQUAD so this can't drift)
def _soldiers() -> list[tuple[str, str]]:
    try:
        from .squad import SQUAD
        return [(label, focus) for (label, focus) in SQUAD.values()]
    except Exception:  # noqa: BLE001
        return []


def _model_for(cfg: Config, attr: str | None) -> str:
    if not attr:
        return "—"
    raw = str(getattr(cfg, attr, "") or "")
    short = raw.split("-")[1] if "-" in raw else raw
    return (short + " (auto)") if getattr(cfg, "auto_model", False) and attr in ("builder_model", "reviewer_model") else short


def mermaid_chart() -> str:
    """Chain-of-command flowchart (renders on GitHub and any Mermaid viewer)."""
    lines = ["```mermaid", "flowchart TD",
             "  C([Commander · Roman]) --> G[The General · orchestrator]"]
    short = {"The General": "G", "Adjutant": "ADJ", "Product Manager": "PM", "Field Engineer": "FE",
             "Inspector General": "IG", "Scout": "SC", "Provost Marshal": "PR", "Quartermaster": "QM",
             "Sentinel": "SN", "Drillmaster": "DM"}
    for name, role, _d, _m in _OFFICERS:
        if name == "The General":
            continue
        lines.append(f"  G --> {short[name]}[{name} · {role}]")
    for i, (label, _focus) in enumerate(_soldiers(), 1):
        lines.append(f"  FE --> S{i}([{label}])")
    lines.append("```")
    return "\n".join(lines)


def build_doc(cfg: Config, status: str = "") -> str:
    """The full roster as Markdown — deterministic structure + duties + chart, plus an optional
    one-line daily status."""
    out = [f"# Elite Unit — Roster", "", f"_As of {time.strftime('%Y-%m-%d %H:%M')}._", ""]
    if status.strip():
        out += ["> " + status.strip().replace("\n", " "), ""]
    out += ["## Chain of command", "", mermaid_chart(), "", "## Officers", "",
            "| Officer | Role | Model | Duty |", "|---|---|---|---|"]
    for name, role, duty, mattr in _OFFICERS:
        out.append(f"| **{name}** | {role} | {_model_for(cfg, mattr)} | {duty} |")
    sol = _soldiers()
    if sol:
        out += ["", "## Soldiers — the Field Engineer's squad (and recon squads)", "",
                "Fielded on demand: a big ticket is split across the relevant soldiers; the read-only "
                "recon officers (Scout · Provost · Quartermaster) can field their own soldiers too.", "",
                "| Soldier | Lane |", "|---|---|"]
        for label, focus in sol:
            out.append(f"| **{label}** | {focus} |")
    out += ["", "_Living document — regenerated daily after the council. Structure & duties are read "
            "from the code, so they can't drift; the status line is info-only._", ""]
    return "\n".join(out)


def doc_path(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("ROSTER.md")


def latest_status(cfg: Config) -> str:
    """The status line from the last-written ROSTER.md (the '> …' blockquote), or ''."""
    try:
        for ln in doc_path(cfg).read_text(encoding="utf-8").splitlines():
            if ln.startswith("> "):
                return ln[2:].strip()
    except OSError:
        pass
    return ""


def html_view(cfg: Config, status: str = "") -> str:
    """The roster rendered for the cockpit: a chain-of-command tree + officer duties + soldiers.
    Pure function of the deterministic structure (+ optional status line)."""
    import html as _h
    esc = _h.escape
    css = (
        "<style>"
        ".rdoc{max-width:1000px}.rstatus{color:#c3cad6;font-size:14px;background:#12161f;border:1px solid "
        "#232936;border-radius:10px;padding:12px 15px;margin:4px 0 18px}"
        ".rtree{margin:6px 0 22px;font-family:ui-monospace,Menlo,monospace;font-size:13px;line-height:1.7;color:#c3cad6}"
        ".rtree .cmd{color:#e9ecf1;font-weight:700}.rtree .gen{color:#7aa2ff;font-weight:700}"
        ".rtree .off{color:#e9ecf1}.rtree .sol{color:#8a929f}"
        ".rsec{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:#8a929f;font-weight:700;margin:18px 0 9px}"
        "table.rtbl{width:100%;border-collapse:collapse;font-size:13px}"
        "table.rtbl th{color:#6b7480;text-align:left;font-weight:600;padding:6px 9px;border-bottom:1px solid #232936}"
        "table.rtbl td{padding:6px 9px;border-bottom:1px solid #1a1f2a;color:#c3cad6;vertical-align:top}"
        "table.rtbl td.nm{color:#e9ecf1;font-weight:650;white-space:nowrap}table.rtbl td.rl{color:#8a929f;white-space:nowrap}"
        "table.rtbl td.md{font-family:ui-monospace,Menlo,monospace;color:#7aa2ff}</style>")
    # chain-of-command tree (no JS)
    tree = ['<div class=rtree>', '<span class=cmd>Commander · Roman</span>',
            '<br>└─ <span class=gen>The General</span> · orchestrator']
    offs = [o for o in _OFFICERS if o[0] != "The General"]
    for i, (name, role, _d, _m) in enumerate(offs):
        elbow = "   └─" if i == len(offs) - 1 else "   ├─"
        tree.append(f'<br>{elbow} <span class=off>{esc(name)}</span> · {esc(role)}')
        if name == "Field Engineer":
            sol = _soldiers()
            for j, (label, _f) in enumerate(sol):
                send = "      └─" if j == len(sol) - 1 else "      ├─"
                bar = "   " if i == len(offs) - 1 else "   │"
                tree.append(f'<br>{bar}{send} <span class=sol>{esc(label)}</span>')
    tree.append('</div>')

    rows = "".join(
        f'<tr><td class=nm>{esc(n)}</td><td class=rl>{esc(r)}</td><td class=md>{esc(_model_for(cfg, m))}</td>'
        f'<td>{esc(d)}</td></tr>' for n, r, d, m in _OFFICERS)
    sol_rows = "".join(f'<tr><td class=nm>{esc(l)}</td><td>{esc(f)}</td></tr>' for l, f in _soldiers())
    parts = [css, '<div class=rdoc>']
    if status:
        parts.append(f'<div class=rstatus>📋 {esc(status)}</div>')
    parts.append('<div class=rsec>Chain of command</div>')
    parts.append("".join(tree))
    parts.append('<div class=rsec>Officers &amp; duties</div>')
    parts.append(f'<table class=rtbl><tr><th>Officer</th><th>Role</th><th>Model</th><th>Duty</th></tr>{rows}</table>')
    if sol_rows:
        parts.append('<div class=rsec>Soldiers — fielded on demand by the Field Engineer (and recon squads)</div>')
        parts.append(f'<table class=rtbl><tr><th>Soldier</th><th>Lane</th></tr>{sol_rows}</table>')
    parts.append('</div>')
    return "".join(parts)


async def _status_line(cfg: Config) -> str:
    """One cheap (Haiku) sentence on the unit's state today, from the record. Best-effort; '' on any
    hiccup. Info only — never a decision."""
    try:
        from .drillmaster import collect_signals, format_signals
        from .agent import run_agent
        from claude_agent_sdk import ClaudeAgentOptions
        from . import memory
        digest = format_signals(collect_signals(cfg))[:2500]
        sys_p = ("You write ONE plain sentence (<=30 words) on the unit's current state from the record "
                 "— what it's been doing, anything notable. Info only, no advice, no markdown.")
        run = await run_agent(
            "The unit's recent record:\n\n" + digest + "\n\nWrite the one-sentence status now.",
            ClaudeAgentOptions(model=cfg.smalltalk_model, system_prompt=memory.preamble() + sys_p,
                               cwd=str(Path(__file__).resolve().parent.parent),
                               permission_mode="bypassPermissions", allowed_tools=["Read", "Grep", "Glob"],
                               disallowed_tools=["Write", "Edit", "Bash"], setting_sources=["project"],
                               max_turns=3, effort="low"), tag="roster")
        return (run.final or run.text or "").strip().split("\n")[0][:240]
    except Exception:  # noqa: BLE001
        return ""


async def refresh(cfg: Config, audit=None) -> Path:
    """Regenerate ROSTER.md (cheap status line + deterministic body) and write it. Returns the path."""
    status = await _status_line(cfg)
    p = doc_path(cfg)
    try:
        p.write_text(build_doc(cfg, status), encoding="utf-8")
        if audit is not None:
            audit.record("roster_refresh", officers=len(_OFFICERS), soldiers=len(_soldiers()))
    except OSError:
        pass
    return p
