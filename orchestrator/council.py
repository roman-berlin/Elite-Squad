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

from . import memory
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
    "(3–6 sentences), strictly from your lens. Only major officers sit on this council; if you "
    "command a squad, consult it as needed but report for it yourself — soldiers do not speak "
    "here. Ground every claim in the record or files you read — no invention. End with ONE "
    "concrete recommendation for today. Solve technical and process problems YOURSELVES; "
    "escalate to the Commander ONLY when the call is genuinely his — product direction, "
    "business/strategy, or an irreversible decision with no safe default. When (and only when) "
    "such a decision truly exists, put it on its own line prefixed exactly 'FOR THE COMMANDER:'. "
    "Most days there is none. Do not write or edit files.\n"
    "This is a real round-table: in later rounds you will see what your fellow officers said — "
    "RESPOND to them, by name, when it touches your lens: agree and build, or push back with a "
    "reason. If you have nothing to add this round, reply with exactly 'PASS'. If a specific "
    "problem genuinely needs a focused cross-officer huddle, end with a line prefixed exactly "
    "'MEETING:' naming the topic and which officers should attend."
)

COUNCIL = [
    ("Adjutant", "S-1 · Personnel (HR)",
     "Your lens is the roster. Are the right officers in post for the work coming in? Read "
     "officers/*.md and the record. Recommend at most one personnel action — recruit a new "
     "officer ONLY if a real, repeated capability gap has no owner (draft its role in one "
     "line), or retire/retrain an officer that is idle or underperforming. Propose only."),
    ("Field Engineer", "Builder",
     "Your lens is delivery. You command a squad (Vanguard FE · Ordnance BE · Logistics DB · "
     "DevOps · …) — consult their status, but you alone report for them here. What shipped, "
     "what fought back, where did the build burn passes or hit max effort? Name the friction "
     "and the one change that makes the next build cleaner. If the squad needs a new soldier — "
     "or a junior officer (sub-lead) to own a focus area and command soldiers of its own — "
     "request it; the Adjutant approves the hire."),
    ("Inspector General", "Reviewer",
     "Your lens is quality and risk. What recurring defects or spec-gaps are you catching, and "
     "what is slipping through? Call the single quality risk the unit must close, and whether "
     "standards should tighten or ease."),
    ("Scout", "S-2 · Recon (QA)",
     "Your lens is what actually breaks in the running app on DEV — runtime, UX, accessibility "
     "— the defects unit tests and diff review miss. From the record (and any e2e results), name "
     "the biggest live-QA blind spot and the one smoke test worth standing up first. If there is "
     "no browser/e2e coverage yet, say so plainly."),
    ("Provost Marshal", "Security",
     "Your lens is security and exposure. From the record and recent changes, name the single "
     "biggest risk the unit is carrying — a secret in code, a tenant-isolation or authz gap, "
     "injection, or a known-vulnerable dependency — and the one control to add. If you have no "
     "signal yet, say so plainly."),
    ("Quartermaster", "S-4 · Deploy readiness",
     "Your lens is whether DEV can actually ship to MAIN — build, types, migrations, deps, env, "
     "deploy config. Name the single biggest thing standing between DEV and a clean promotion, "
     "and the one readiness check to add. If readiness is unknown, say what to verify."),
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
    "**FOR THE COMMANDER** — ONLY decisions that are genuinely Roman's: product direction, "
    "business/strategy, or an irreversible call with no safe default. NOT technical or process "
    "choices the unit should make itself. Hold a high bar — most days this is 'None.' One "
    "question per line ending in '?', or write 'None.' Never invent questions to fill space."
)

_MEETING_CHAIR_SYSTEM = (
    "You are THE GENERAL, chairing a focused meeting of the Elite Unit on a single topic. You "
    "have heard the officers debate. Produce a tight decision record in disciplined tone, in "
    "this exact markdown shape and nothing else:\n\n"
    "**TOPIC** — one line.\n\n"
    "**DECISION** — what the unit will do, who owns it, and the one-line why. Resolve the debate "
    "and take a clear position; do not fence-sit.\n\n"
    "**ACTIONS** — 1–4 bullets: concrete next steps (file a ticket, propose a drill, add a "
    "check, draft a hire). Name the officer who owns each.\n\n"
    "**FOR THE COMMANDER** — ONLY a decision that is genuinely Roman's (product / strategy / "
    "irreversible, no safe default). One question per line ending in '?', else 'None.'"
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
        system_prompt=memory.preamble() + f"{_OFFICER_RULES}\n\n{system}",
        cwd=cwd,
        permission_mode="default",
        allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash"],
        setting_sources=["project"],
        max_turns=8,
        effort="medium",
    )


def _officer_key(rank: str) -> str:
    return rank.lower().replace(" ", "-")


def _select_officers(keys):
    """Pick council officers by key or name (case-insensitive substring). Falsy -> all."""
    if not keys:
        return list(COUNCIL)
    out = []
    for o in COUNCIL:
        hay = (_officer_key(o[0]) + " " + o[0].lower())
        if any(str(q).lower().strip() in hay for q in keys):
            out.append(o)
    return out or list(COUNCIL)


def _discuss_prompt(rank, lens_role, digest, notes, topic, transcript, rnd, rounds) -> str:
    parts = [f"You are the {rank} ({lens_role}). The Elite Unit's recent record:", "", digest, ""]
    if notes:
        parts += ["Standing guidance from the Commander (recent):", notes, ""]
    if topic:
        parts += [f"This session is focused on: {topic}", ""]
    if transcript:
        parts += ["The discussion so far:", *[f"— {who}: {what}" for who, what in transcript], ""]
    if rnd == 1:
        parts += ["Give your opening statement now."]
    else:
        parts += [f"Round {rnd} of {rounds}. Respond to your fellow officers where it touches "
                  "your lens — agree and build, or push back with a reason. Reply with exactly "
                  "'PASS' if you have nothing to add."]
    return "\n".join(parts)


_SKIP = {"pass", "nothing to add", "none", "no comment", "no further comment"}


async def discuss(cfg: Config, officers, digest: str, notes: str,
                  topic: str | None, rounds: int) -> list[tuple[str, str]]:
    """Multi-round round-table: officers see the discussion so far and respond. Returns the
    transcript as list[(speaker, statement)]. Converges early if a whole round passes."""
    cwd = _general_root()
    transcript: list[tuple[str, str]] = []
    rounds = max(1, int(rounds or 1))
    for rnd in range(1, rounds + 1):
        spoke = 0
        for rank, lens_role, voice in officers:
            run = await run_agent(
                _discuss_prompt(rank, lens_role, digest, notes, topic, transcript, rnd, rounds),
                _officer_options(cfg, voice, cwd), tag=_officer_key(rank))
            statement = (run.final or run.text or "").strip()
            if not statement or statement.lower().rstrip(".!").strip() in _SKIP:
                continue
            label = rank if rnd == 1 else f"{rank} · r{rnd}"
            transcript.append((label, statement))
            spoke += 1
            print(f"  · {label} spoke", flush=True)
        if rnd > 1 and spoke == 0:
            break   # the debate has converged — nobody had more to add
    return transcript


async def hold_council(cfg: Config, topic: str | None = None, audit=None) -> str:
    """Run the muster as a multi-round debate; the General chairs. Returns the briefing,
    saves the full transcript, and sends the briefing to Telegram."""
    sig = collect_signals(cfg)
    digest = format_signals(sig)
    notes = recent_commander_notes(cfg)
    cwd = _general_root()

    print("\n🎖️  Daily Council — officers in session…\n", flush=True)
    said = await discuss(cfg, COUNCIL, digest, notes, topic, getattr(cfg, "council_rounds", 2))

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
        model=cfg.reviewer_model, system_prompt=memory.preamble() + _CHAIR_SYSTEM, cwd=cwd,
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
    # The Scribe folds this council's lessons into Unit Memory (best-effort — never break the muster).
    try:
        print(f"  {await memory.scribe(cfg)}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  Scribe skipped: {exc}", flush=True)
    print(f"\n  council saved → {saved}\n", flush=True)
    return briefing


async def hold_meeting(cfg: Config, topic: str, officers=None, rounds: int | None = None,
                       audit=None) -> str:
    """An ad-hoc meeting: the relevant officers debate ONE topic, the General decides, and the
    outcome is logged to Unit Memory + Telegram. `officers` is a list of names/keys (None = all);
    any officer can request one by ending a council turn with a 'MEETING:' line."""
    sig = collect_signals(cfg)
    digest = format_signals(sig)
    notes = recent_commander_notes(cfg)
    cwd = _general_root()
    roster = _select_officers(officers)
    rounds = rounds or getattr(cfg, "council_rounds", 2)

    print(f"\n🎖️  Meeting — {topic}\n   attending: {', '.join(o[0] for o in roster)}\n", flush=True)
    said = await discuss(cfg, roster, digest, notes, topic, rounds)

    chair_prompt = "\n".join([
        f"Meeting topic: {topic}", "", "The unit's record:", "", digest, "",
        *([f"Commander's standing guidance:\n{notes}\n"] if notes else []),
        "The officers debated:", "",
        *[f"### {who}\n{what}\n" for who, what in said],
        "Now write the decision record.",
    ])
    chair = await run_agent(chair_prompt, ClaudeAgentOptions(
        model=cfg.reviewer_model, system_prompt=memory.preamble() + _MEETING_CHAIR_SYSTEM, cwd=cwd,
        permission_mode="default", allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash"], setting_sources=["project"],
        max_turns=6, effort="high"), tag="the-general")
    decision = (chair.final or chair.text or "(no decision)").strip()

    saved = _save_transcript(cfg, f"meeting: {topic}", digest, said, decision)
    questions = _commander_questions(decision)
    notify.send(f"🎖️ *Meeting — {topic}*\n\n{decision[:3500]}")
    if questions:
        notify.send("❓ *The unit needs your call:*\n" + "\n".join(f"• {q}" for q in questions)
                    + "\n\nReply here and I'll log it as standing guidance.")
    if audit is not None:
        audit.record("meeting", topic=topic, officers=[o[0] for o in roster],
                     questions=len(questions), transcript=saved.name)
    try:
        print(f"  {await memory.scribe(cfg)}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  Scribe skipped: {exc}", flush=True)
    print(f"\n  meeting saved → {saved}\n", flush=True)
    return decision


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
        model=cfg.reviewer_model, system_prompt=memory.preamble() + system, cwd=_general_root(),
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
