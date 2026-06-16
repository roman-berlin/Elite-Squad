"""The Daily Council — the Elite Unit's morning muster (compounding improvement).

At 10:00 the officers assemble. Each gives a short SITREP from its lens on the unit's
recent operations; the Drillmaster proposes one improvement (the unit studies every day);
the Adjutant covers personnel; and THE GENERAL chairs — synthesizing a briefing, the
decisions taken, and the questions only the Commander can answer (product / strategy /
business). The briefing goes to Telegram, the full transcript is saved for the cockpit,
and open questions are pushed to you.

  general council                 # hold the muster now (also runs on the 10:00 schedule)
  general council --topic "…"     # an ad-hoc improvement muster on a specific topic

Officers are read-only here (they Read the record + their own files; they do not write).
The Adjutant only *proposes* hires/retirements — you approve and apply.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from .agent import run_agent
from .config import Config
from .drillmaster import collect_signals, format_signals
from . import notify

# --------------------------------------------------------------------------- #
# The officers who sit on the council (active roster). Each speaks once, briefly,
# in character. Army terminology; disciplined tone. Read-only.
# --------------------------------------------------------------------------- #
_OFFICER_RULES = (
    "You are an officer of an ELITE autonomous software unit reporting to THE GENERAL "
    "(who reports to the Commander, Roman). Speak in a disciplined, military tone, briefly "
    "(3–6 sentences), strictly from your lens. Ground every claim in the supplied record or "
    "files you read — no invention. End with ONE concrete recommendation for today. If "
    "anything needs the Commander's judgment (product, scope, business strategy), put it on "
    "its own line prefixed exactly 'FOR THE COMMANDER:' followed by the question. Do not "
    "write or edit files."
)

COUNCIL = [
    ("Adjutant", "S-1 · Personnel (HR)",
     "Your lens is the roster. Are the right officers in post for the work coming in? Read "
     "officers/*.md and the record. Recommend at most one personnel action — recruit a new "
     "officer ONLY if a real, repeated capability gap has no owner (draft its role in one "
     "line), or retire/retrain an officer that is idle or underperforming. Propose only."),
    ("Field Engineer", "Builder",
     "Your lens is delivery. What shipped, what fought back, where did the build burn passes "
     "or hit max effort? Name the friction in the codebase or process and the one change that "
     "would make the next build cleaner."),
    ("Inspector General", "Reviewer",
     "Your lens is quality and risk. What recurring defects or spec-gaps are you catching, and "
     "what is slipping through? Call the single quality risk the unit must close, and whether "
     "standards should tighten or ease."),
    ("Drillmaster", "Doctrine & Training",
     "Your lens is improvement — the unit studies every day. From recurring weaknesses, name "
     "the ONE drill (a precise edit to an officer's Identity/Knowledge/Skills file) that yields "
     "the most compounding gain tomorrow."),
]

_CHAIR_SYSTEM = (
    "You are THE GENERAL, chairing the Elite Unit's daily council. You have heard each "
    "officer. Produce a tight commander's briefing in disciplined tone, in this exact "
    "markdown shape and nothing else:\n\n"
    "**SITREP** — 3–5 lines on the unit's recent operations from the record.\n\n"
    "**ORDERS FOR TODAY** — 2–5 bullets: the concrete actions the unit will take (who does "
    "what). Fold in the officers' best recommendations; resolve conflicts.\n\n"
    "**FOR THE COMMANDER** — the open questions only Roman can answer (product / scope / "
    "business strategy), one per line, each ending in a question mark. Write 'None.' if there "
    "are none. Never invent questions to fill space."
)


# --------------------------------------------------------------------------- #
# storage (kept beside the audit log, OUTSIDE every target repo)
# --------------------------------------------------------------------------- #
def _general_root() -> str:
    """The General's repo root (where officers/ and CLAUDE.md live) — independent of the
    directory the command was invoked from, so officers always ground on the right files."""
    return str(Path(__file__).resolve().parent.parent)


def _council_dir(cfg: Config) -> Path:
    d = Path(cfg.audit_path).with_name("council")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _notes_file(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("commander_notes.md")


def recent_commander_notes(cfg: Config, lines: int = 30) -> str:
    p = _notes_file(cfg)
    if not p.exists():
        return ""
    tail = p.read_text(encoding="utf-8").splitlines()[-lines:]
    return "\n".join(tail).strip()


def add_commander_note(cfg: Config, text: str) -> None:
    """Append a free-text note/answer from the Commander (captured from Telegram)."""
    p = _notes_file(cfg)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- [{stamp}] {text.strip()}\n")


def history(cfg: Config, limit: int = 20) -> list[dict]:
    """Recent councils (newest first) for the cockpit: {ts, title, file, summary}."""
    idx = _council_dir(cfg) / "index.jsonl"
    if not idx.exists():
        return []
    rows = []
    for line in idx.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(rows))[:limit]


def transcript_text(cfg: Config, filename: str) -> str:
    p = _council_dir(cfg) / Path(filename).name   # name-only: no path traversal
    return p.read_text(encoding="utf-8") if p.exists() else ""


# --------------------------------------------------------------------------- #
# the muster
# --------------------------------------------------------------------------- #
def _officer_options(cfg: Config, system: str, cwd: str) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=cfg.builder_model,          # Sonnet for the round-table (lean); Opus chairs
        system_prompt=f"{_OFFICER_RULES}\n\n{system}",
        cwd=cwd,
        permission_mode="default",
        allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash"],
        setting_sources=["project"],
        max_turns=8,
        effort="medium",
    )


def _officer_prompt(rank: str, lens_role: str, digest: str, notes: str,
                    topic: str | None, said: list[tuple[str, str]]) -> str:
    parts = [
        f"You are the {rank} ({lens_role}). The Elite Unit's recent record:",
        "", digest, "",
    ]
    if notes:
        parts += ["Standing guidance from the Commander (recent):", notes, ""]
    if topic:
        parts += [f"Today's muster is focused on: {topic}", ""]
    if said:
        parts += ["Officers who have already spoken:",
                  *[f"— {who}: {what}" for who, what in said], ""]
    parts += ["Give your statement now."]
    return "\n".join(parts)


async def hold_council(cfg: Config, topic: str | None = None, audit=None) -> str:
    """Run the muster: each officer speaks, the General chairs. Returns the briefing,
    saves the full transcript, and sends the briefing to Telegram."""
    sig = collect_signals(cfg)
    digest = format_signals(sig)
    notes = recent_commander_notes(cfg)
    cwd = _general_root()

    print("\n🎖️  Daily Council — officers mustering…\n", flush=True)
    said: list[tuple[str, str]] = []
    for rank, lens_role, voice in COUNCIL:
        print(f"  · {rank} has the floor…", flush=True)
        run = await run_agent(
            _officer_prompt(rank, lens_role, digest, notes, topic, said),
            _officer_options(cfg, voice, cwd), tag=rank.lower().replace(" ", "-"))
        statement = (run.final or run.text or "(no statement)").strip()
        said.append((rank, statement))

    # The General chairs and synthesizes the briefing.
    print("  · The General sums up…", flush=True)
    chair_prompt = "\n".join([
        "The Elite Unit's record:", "", digest, "",
        *([f"Commander's standing guidance:\n{notes}\n"] if notes else []),
        *([f"Muster focus: {topic}\n"] if topic else []),
        "The council said:", "",
        *[f"### {who}\n{what}\n" for who, what in said],
        "Now write the briefing.",
    ])
    chair = await run_agent(chair_prompt, ClaudeAgentOptions(
        model=cfg.reviewer_model, system_prompt=_CHAIR_SYSTEM, cwd=cwd,
        permission_mode="default", allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash"], setting_sources=["project"],
        max_turns=6, effort="high"), tag="the-general")
    briefing = (chair.final or chair.text or "(no briefing)").strip()

    saved = _save_transcript(cfg, topic, digest, said, briefing)
    questions = _commander_questions(briefing)

    # Report up to the Commander.
    header = "🎖️ *Daily Council*" if not topic else f"🎖️ *Muster — {topic}*"
    notify.send(f"{header}\n\n{briefing[:3500]}")
    if questions:
        notify.send("❓ *The unit needs your call:*\n" + "\n".join(f"• {q}" for q in questions)
                    + "\n\nReply here and I'll log it as standing guidance.")
    if audit is not None:
        audit.record("council", topic=topic or "daily", officers=[r for r, _, _ in COUNCIL],
                     questions=len(questions), transcript=saved.name)
    print(f"\n  council saved → {saved}\n", flush=True)
    return briefing


async def respond_to_commander(cfg: Config, message: str) -> str:
    """The General answers a message from the Commander (a reply to a council question, or
    any question) directly in Telegram, grounded on the latest council + record, and logs
    the exchange as standing guidance for the unit."""
    latest = history(cfg, limit=1)
    context = transcript_text(cfg, latest[0]["file"]) if latest else format_signals(collect_signals(cfg))
    notes = recent_commander_notes(cfg)
    system = (
        "You are THE GENERAL of an elite autonomous software unit, answering the Commander "
        "(Roman) directly on Telegram. Be brief and disciplined (a few sentences). Give your "
        "best-practice recommendation with a one-line rationale; if it is a decision, state "
        "plainly what the unit will do and which officer owns it. Ground on the latest council "
        "and the unit's record; you may Read files. No filler, no restating the question.")
    prompt = "\n".join([
        "Latest council / record:", "", context[:4000], "",
        *([f"Standing guidance so far:\n{notes}\n"] if notes else []),
        f"The Commander says: {message}", "", "Answer him now.",
    ])
    run = await run_agent(prompt, ClaudeAgentOptions(
        model=cfg.reviewer_model, system_prompt=system, cwd=_general_root(),
        permission_mode="default", allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash"], setting_sources=["project"],
        max_turns=8, effort="medium"), tag="the-general")
    answer = (run.final or run.text or "(the General had no answer)").strip()
    notify.send(f"🎖️ {answer[:3500]}")
    add_commander_note(cfg, f"Q: {message}\n  A (General): {answer}")
    return answer


def _save_transcript(cfg: Config, topic, digest, said, briefing) -> Path:
    d = _council_dir(cfg)
    stamp = time.strftime("%Y%m%d-%H%M")
    f = d / f"council-{stamp}.md"
    title = topic or "Daily council"
    body = [f"# {title} — {time.strftime('%Y-%m-%d %H:%M')}", "",
            "## The General's briefing", "", briefing, "",
            "## The record", "", "```", digest, "```", "",
            "## Round-table", ""]
    for who, what in said:
        body += [f"### {who}", "", what, ""]
    f.write_text("\n".join(body), encoding="utf-8")
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "title": title,
            "file": f.name, "summary": _first_line(briefing)}
    with (d / "index.jsonl").open("a", encoding="utf-8") as idx:
        idx.write(json.dumps(line) + "\n")
    return f


def _commander_questions(briefing: str) -> list[str]:
    """Pull the questions under the 'FOR THE COMMANDER' heading."""
    out, capturing = [], False
    for raw in briefing.splitlines():
        line = raw.strip()
        low = line.lower().lstrip("*# ").rstrip("*: ")
        if low.startswith("for the commander"):
            capturing = True
            continue
        if capturing:
            if line.startswith("#") or low in ("orders for today", "sitrep"):
                break
            clean = line.lstrip("-•* ").strip()
            if not clean or clean.lower() in ("none", "none."):
                continue
            if clean.endswith("?"):
                out.append(clean)
    return out


def _first_line(text: str) -> str:
    for line in text.splitlines():
        s = line.strip().lstrip("*# ").strip()
        if s:
            return s[:140]
    return "(council)"
