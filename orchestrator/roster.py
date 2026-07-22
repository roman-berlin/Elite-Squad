"""The living roster — who's in the unit and what each one does.

A once-a-day, INFO-ONLY document: the chain of command and every officer and their duty — plus a short
"state of the unit today" line written by the cheapest model (it's
just a status blurb, not a decision). The structure and duties are DETERMINISTIC (read straight from the
code, so the doc can never drift from reality); only the one-line daily status uses a model, and even
that is best-effort — if it fails the doc still generates.

Refreshed at the end of the daily council; also `general roster` on demand, and viewable in the cockpit.
"""
from __future__ import annotations

from pathlib import Path
import time

from .config import Config
from .officers import display
from . import auth_probe, scrum

# (internal officers key, role, duty, which configured model attribute it runs on — None = deterministic).
# The human-facing display NAME for each key is NOT stored here: it's resolved from officers.OFFICER_NAMES
# (the single source of truth) when the roster is built (see _OFFICERS), so renaming an officer is one edit
# there and this doc can never drift. Keys are stable identifiers and never change.
#
# EU-260: a key belongs here ONLY while code actually implements it — this list is what council.refresh()
# rebuilds ROSTER.md from every morning, so a row for a deleted officer is a phantom the whole unit reads
# as context. `test_engineer` was one: c276155 (Phase-2 §2) deleted its module AND charter, but the row
# survived and re-emitted the phantom daily. Retiring an officer means deleting its row here, not just its
# module. Note "implemented" is not the same as "has a module of its own": `adjutant` kept its row because
# EU-325 deleted only adjutant.py (the `general adjutant` CLI / EU-85 preview) — the Engineering Manager
# still sits on the daily council (council.COUNCIL[0]), which is why its model attr is discussion_model.
# tests/eu260_org_reality_test.py enforces both directions.
_OFFICER_ROWS: list[tuple[str, str, str, str | None]] = [
    ("general", "Orchestrator", "Chairs the unit, talks 1:1 with you, synthesises the daily council, "
     "and routes your guidance to the officers.", "discussion_model"),
    ("adjutant", "S-1 · Personnel", "Owns the roster — proposes hires/retirements when a real "
     "capability gap appears (you approve and apply). Speaks at the daily council; propose-only.",
     "discussion_model"),
    ("pm", "S-5 · Product", "Makes the product / IA / scope calls the Builder can't make "
     "alone, so the unit keeps shipping; escalates only the critical, irreversible ones.", "reviewer_model"),
    ("scrum", "S-6 · Scrum Master", scrum.__doc__.split("\n\n")[0].strip(), "reviewer_model"),
    ("field_engineer", "Builder", "Implements each ticket solo on an isolated worktree, writing the "
     "tests for the change against the plan's acceptance criteria.", "builder_model"),
    ("inspector", "Reviewer", "Quality & risk gate — reviews every change, demands fixes, and "
     "guards the standard before anything merges.", "reviewer_model"),
    ("scout", "S-2 · QA / Recon", "Hunts what actually breaks in the running app on DEV — runtime, UX, "
     "accessibility — and files findings as tickets.", "reviewer_model"),
    ("provost", "Security", "The security gate — blocks a merge on a CRITICAL/HIGH finding "
     "(secrets, tenant-isolation, injection, vulnerable deps).", "reviewer_model"),
    ("quartermaster", "S-4 · Deploy readiness", "Certifies whether DEV can actually ship to MAIN — build, "
     "types, migrations, deps, env, deploy config.", "reviewer_model"),
    ("sentinel", "S-3 · Integration & rollback", "Runs the heavier post-merge suite on the landed DEV and "
     "reverts the merge forward-only if it breaks. Deterministic — no model.", None),
]

# (display name, role, duty, model attr) — the display name is read from the single source of truth
# (officers.OFFICER_NAMES) so a rename is genuinely one edit there. Order = chain of command.
_OFFICERS = [(display(key), role, duty, mattr) for key, role, duty, mattr in _OFFICER_ROWS]


def _model_for(cfg: Config, attr: str | None) -> str:
    if not attr:
        return "—"
    raw = str(getattr(cfg, attr, "") or "")
    short = raw.split("-")[1] if "-" in raw else raw
    return (short + " (auto)") if getattr(cfg, "auto_model", False) and attr in ("builder_model", "reviewer_model") else short


def mermaid_chart() -> str:
    """Chain-of-command flowchart (renders on GitHub and any Mermaid viewer)."""
    lines = ["```mermaid", "flowchart TD",
             f"  C([Commander · Roman]) --> G[{display('general')} · orchestrator]"]
    # node ids are keyed by the STABLE internal key (not the display name) so a rename can't break the chart
    short = {"general": "G", "adjutant": "ADJ", "pm": "PM", "scrum": "SM", "field_engineer": "FE",
             "inspector": "IG", "scout": "SC", "provost": "PR",
             "quartermaster": "QM", "sentinel": "SN"}
    for key, role, _d, _m in _OFFICER_ROWS:
        if key == "general":
            continue
        lines.append(f"  G --> {short[key]}[{display(key)} · {role}]")
    lines.append("```")
    return "\n".join(lines)


def build_doc(cfg: Config, status: str = "") -> str:
    """The full roster as Markdown — deterministic structure + duties + chart, plus an optional
    one-line daily status.

    EU-430 AC5: a provider-error body is NEVER persisted as the status line — drop it, so a dead
    credential can't poison ROSTER.md ("Failed to authenticate. API Error: 401 …" sat here for 2
    days). Belt-and-suspenders: _status_line already returns '' on the same shape."""
    if auth_probe.looks_like_provider_error(status):
        status = ""
    out = [f"# Elite Unit — Roster", "", f"_As of {time.strftime('%Y-%m-%d %H:%M')}._", ""]
    if status.strip():
        out += ["> " + status.strip().replace("\n", " "), ""]
    out += ["## Chain of command", "", mermaid_chart(), "", "## Officers", "",
            "| Officer | Role | Model | Duty |", "|---|---|---|---|"]
    for name, role, duty, mattr in _OFFICERS:
        out.append(f"| **{name}** | {role} | {_model_for(cfg, mattr)} | {duty} |")
    out += ["", "_Living document — regenerated daily after the council. Structure & duties are read "
            "from the code, so they can't drift; the status line is info-only._", ""]
    return "\n".join(out)


def doc_path(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("ROSTER.md")


def latest_status(cfg: Config) -> str:
    """The status line from the last-written ROSTER.md (the '> …' blockquote), or ''.

    EU-430: a status line that is a provider-error string (a relic from a ceremony run while the
    credential was dead) is treated as absent — never surfaced as the unit's state."""
    try:
        for ln in doc_path(cfg).read_text(encoding="utf-8").splitlines():
            if ln.startswith("> "):
                line = ln[2:].strip()
                return "" if auth_probe.looks_like_provider_error(line) else line
    except OSError:
        pass
    return ""


def html_view(cfg: Config, status: str = "") -> str:
    """The roster rendered for the cockpit: a chain-of-command tree + officer duties.
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
            f'<br>└─ <span class=gen>{esc(display("general"))}</span> · orchestrator']
    offs = [r for r in _OFFICER_ROWS if r[0] != "general"]
    for i, (key, role, _d, _m) in enumerate(offs):
        elbow = "   └─" if i == len(offs) - 1 else "   ├─"
        tree.append(f'<br>{elbow} <span class=off>{esc(display(key))}</span> · {esc(role)}')
    tree.append('</div>')

    rows = "".join(
        f'<tr><td class=nm>{esc(n)}</td><td class=rl>{esc(r)}</td><td class=md>{esc(_model_for(cfg, m))}</td>'
        f'<td>{esc(d)}</td></tr>' for n, r, d, m in _OFFICERS)
    parts = [css, '<div class=rdoc>']
    if status:
        parts.append(f'<div class=rstatus>📋 {esc(status)}</div>')
    parts.append('<div class=rsec>Chain of command</div>')
    parts.append("".join(tree))
    parts.append('<div class=rsec>Officers &amp; duties</div>')
    parts.append(f'<table class=rtbl><tr><th>Officer</th><th>Role</th><th>Model</th><th>Duty</th></tr>{rows}</table>')
    parts.append('</div>')
    return "".join(parts)


def _safe_status(text: str) -> str:
    """EU-430 AC5: the model's one-line status, or '' if it is a provider-error string. Producer-side
    guard so a dead credential never becomes ROSTER.md's status line (build_doc is the belt)."""
    line = (text or "").strip().split("\n")[0][:240]
    return "" if auth_probe.looks_like_provider_error(line) else line


async def _status_line(cfg: Config) -> str:
    """One cheap (Haiku) sentence on the unit's state today, from the record. Best-effort; '' on any
    hiccup. Info only — never a decision."""
    try:
        from .signals import collect_signals, format_signals
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
                               disallowed_tools=["Write", "Edit", "Bash", "Task", "Agent"], setting_sources=["project"],
                               max_turns=3, effort="low"), tag="roster")
        return _safe_status(run.final or run.text or "")
    except Exception:  # noqa: BLE001
        return ""


async def refresh(cfg: Config, audit=None) -> Path:
    """Regenerate ROSTER.md (cheap status line + deterministic body) and write it. Returns the path."""
    status = await _status_line(cfg)
    p = doc_path(cfg)
    try:
        p.write_text(build_doc(cfg, status), encoding="utf-8")
        if audit is not None:
            audit.record("roster_refresh", officers=len(_OFFICERS))
    except OSError:
        pass
    return p
