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

Permission mode is ``bypassPermissions`` for every officer here: they run UNATTENDED (the VPS on
a schedule, the Mac via autopilot / Telegram), so there is never a human present to answer a tool
approval prompt. With writes already disallowed, bypass just means "use your read tools without
dead-stopping" — the autonomy the unit needs to keep working remotely, never blocked on a popup.
"""
from __future__ import annotations

import asyncio
import json
import re
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
    ("Sentinel", "S-3 · Integration & rollback",
     "Your lens is the health of DEV right after each landing — does the integrated branch actually "
     "build, test and run once the merge is in? You own the post-merge suite and the rollback: a land "
     "that breaks DEV gets reverted and handed back. Name the biggest integration risk and the one "
     "post-merge check worth standing up. If there's no post-merge suite yet, say so plainly."),
    ("Drillmaster", "Doctrine & Training",
     "Your lens is improvement and training — the unit studies every day. From recurring "
     "weaknesses, name the ONE drill (a precise edit to an officer's Identity/Knowledge/Skills "
     "file) that yields the most compounding gain tomorrow. You also own onboarding for any "
     "newly recruited officer/soldier and refresher drills for existing ones — flag if anyone is "
     "due one."),
]

_CHAIR_SYSTEM = (
    "You are THE GENERAL, chairing the Elite Unit's daily council. You have heard each officer. "
    "Produce a SHORT commander's briefing — it lands on Roman's phone, so if he can't skim it in ~15 "
    "seconds it's too long. Keep the WHOLE thing under ~90 words. Summarize; do NOT restate the debate "
    "or recap officer-by-officer. Exactly this markdown shape and nothing else:\n\n"
    "**SITREP** — at most 2 short lines on recent operations from the record.\n\n"
    "**ORDERS** — at most 3 short bullets: the concrete actions the unit will take (who does what). "
    "Fold in the officers' best recommendations; resolve conflicts.\n\n"
    "**FOR THE COMMANDER** — ONLY decisions that are genuinely Roman's: product direction, "
    "business/strategy, or an irreversible call with no safe default. NOT technical or process "
    "choices the unit should make itself. Hold a high bar — most days this is 'None.' One "
    "question per line ending in '?', or write 'None.' Never invent questions to fill space."
)

_MEETING_CHAIR_SYSTEM = (
    "You are THE GENERAL, chairing a focused meeting of the Elite Unit on a single topic. You "
    "have heard the officers debate. Produce a SHORT decision record — it lands on Roman's phone, so "
    "keep the WHOLE thing under ~80 words, skimmable in 15 seconds; summarize, do NOT replay the "
    "debate. Exactly this markdown shape and nothing else:\n\n"
    "**TOPIC** — one line.\n\n"
    "**DECISION** — what the unit will do, who owns it, and the one-line why. Resolve the debate "
    "and take a clear position; do not fence-sit.\n\n"
    "**ACTIONS** — 1–3 bullets: concrete next steps (file a ticket, propose a drill, add a "
    "check, draft a hire). Name the officer who owns each.\n\n"
    "**FOR THE COMMANDER** — ONLY a decision that is genuinely Roman's (product / strategy / "
    "irreversible, no safe default). One question per line ending in '?', else 'None.'"
)

_SHIP_REVIEW_CHAIR_SYSTEM = (
    "You are THE GENERAL, chairing a SHIP-REVIEW: is DEV ready to promote to MAIN (production)? "
    "You have the Quartermaster's readiness report and the officers' debate. CRITICAL: the unit "
    "NEVER promotes to MAIN — that is the Commander's (Roman's) call alone. You only recommend. "
    "Output exactly this markdown shape and nothing else:\n\n"
    "**VERDICT** — GO / NO-GO / GO WITH CAVEATS (one blunt line).\n\n"
    "**BLOCKERS** — bullets: anything that must be fixed before prod (build, types, migrations, "
    "deps, env/secrets, security, known defects). Write 'None' only if truly clean.\n\n"
    "**PRE-FLIGHT** — bullets: what the Commander should verify or run before promoting.\n\n"
    "**FOR THE COMMANDER** — end with the go/no-go, framed as his decision: 'Promote DEV→MAIN? "
    "Your call.' (Only you promote to prod.)"
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


def recent_commander_notes(cfg: Config, lines: int = 12) -> str:
    """The last few decisions/guidance, one note per line. Bounded so old chatter doesn't pile up
    in every officer's prompt and pull the General back toward report-mode."""
    p = _notes_file(cfg)
    if not p.exists():
        return ""
    tail = p.read_text(encoding="utf-8").splitlines()[-lines:]
    return "\n".join(tail).strip()


def add_commander_note(cfg: Config, text: str) -> None:
    """Append one note from the Commander/General exchange. Collapsed to a single bounded line so a
    long answer can never become a wall of 'standing guidance' that feeds back into later prompts."""
    p = _notes_file(cfg)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    one_line = " ".join(text.split()).strip()[:240]
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- [{stamp}] {one_line}\n")


# --- Cockpit chat transcript --------------------------------------------------------------------- #
# The FULL Commander<->General exchange, separate from commander_notes.md (which is truncated standing
# guidance fed into prompts). The cockpit chat reads THIS so it shows the General's real reply inline
# instead of the answer only landing in Telegram. Format matches cockpit_views._chat_bubbles.
def _chat_file(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("commander_chat.md")


def append_chat(cfg: Config, role: str, text: str) -> None:
    """role 'Q' (Commander) or 'A' (General). One line per turn, full text (not truncated)."""
    line = ("Q: " if role == "Q" else "A (General): ") + " ".join((text or "").split()).strip()
    try:
        with _chat_file(cfg).open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def chat_transcript(cfg: Config, lines: int = 400) -> str:
    """Recent cockpit chat turns (full Q/A), newest kept. Falls back to the (truncated) commander notes
    if no chat file exists yet, so existing history still shows."""
    p = _chat_file(cfg)
    if not p.exists():
        return recent_commander_notes(cfg, lines=min(lines, 240))
    return "\n".join(p.read_text(encoding="utf-8").splitlines()[-lines:]).strip()


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
        model=cfg.discussion_model,          # Sonnet — discussions are cheap; Opus stays for implementation
        system_prompt=memory.preamble() + f"{_OFFICER_RULES}\n\n{system}",
        cwd=cwd,
        permission_mode="bypassPermissions",
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

    if topic:
        print("\n🎖️  Muster — officers in session…\n", flush=True)
        said = await discuss(cfg, COUNCIL, digest, notes, topic, getattr(cfg, "council_rounds", 2))
        handoffs: list[str] = []
    else:
        # The DAILY muster IS the stand-up + the General's briefing — council and stand-up merged into
        # one daily. Each officer reports Yesterday/Today/Blockers; the General synthesises from it.
        print("\n🎖️  Daily muster — officers reporting; the CTO will brief…\n", flush=True)
        _sd, said, handoffs = await _gather_standup(cfg)
        try:
            _standup_file(cfg).write_text(_standup_text(said, handoffs), encoding="utf-8")
        except OSError:
            pass

    # The General chairs and synthesizes the briefing.
    print("  · CTO sums up…", flush=True)
    chair_prompt = "\n".join([
        "The Elite Unit's record:", "", digest, "",
        *([f"Commander's standing guidance:\n{notes}\n"] if notes else []),
        *([f"Muster focus: {topic}\n"] if topic else []),
        "The council said:" if topic else "The officers' stand-up (Yesterday / Today / Blockers):", "",
        *[f"### {who}\n{what}\n" for who, what in said],
        *([f"Hand-offs needing coordination: {'; '.join(handoffs)}\n"] if handoffs else []),
        "Now write the briefing.",
    ])
    chair = await run_agent(chair_prompt, ClaudeAgentOptions(
        model=cfg.discussion_model, system_prompt=memory.preamble() + _CHAIR_SYSTEM, cwd=cwd,
        permission_mode="bypassPermissions", allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash"], setting_sources=["project"],
        max_turns=6, effort="high"), tag="the-general")
    briefing = (chair.final or chair.text or "(no briefing)").strip()
    from . import governor
    governor.note_call(cfg, len(said) + 1)

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
        print(f"  Technical Writer skipped: {exc}", flush=True)
    # Refresh the living roster (daily, info-only, cheapest model) — best-effort.
    try:
        from . import roster
        await roster.refresh(cfg, audit)
    except Exception as exc:  # noqa: BLE001
        print(f"  Roster refresh skipped: {exc}", flush=True)
    print(f"\n  council saved → {saved}\n", flush=True)
    return briefing


_MEETING_REQ = re.compile(r"^\s*MEETING:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def extract_meeting_requests(text: str) -> list[str]:
    """Pull officer-raised 'MEETING: <topic>' requests out of a transcript (trimmed, de-duped)."""
    out: list[str] = []
    for m in _MEETING_REQ.finditer(text or ""):
        topic = m.group(1).strip()
        topic = re.split(r"\b(attendees?|officers?)\s*:", topic, maxsplit=1, flags=re.IGNORECASE)[0]
        topic = topic.strip().rstrip(" ,.;:—-").strip()
        if len(topic) > 4 and topic.lower() not in (t.lower() for t in out):
            out.append(topic)
    return out


def pending_meeting_requests(cfg: Config) -> tuple[str, list[str]]:
    """(latest council/meeting file, the MEETING: topics raised in it). ('', []) when none."""
    hist = history(cfg, limit=1)
    if not hist:
        return "", []
    f = hist[0]["file"]
    return f, extract_meeting_requests(transcript_text(cfg, f))


def _autospawn_tickets(cfg: Config, decision_raw: str, audit=None) -> tuple[str, str]:
    """Strip any ticket block from a meeting decision; if `meeting_autospawn` is on, FILE those
    tickets (de-duped, to the primary app) and return a note. Drills/hires stay proposal-only —
    only the Commander approves those. Returns (clean_decision, note)."""
    from . import filing
    proposals, clean = filing.parse_tickets(decision_raw)
    if not proposals:
        return clean, ""
    if not getattr(cfg, "meeting_autospawn", False) or not getattr(cfg, "apps", None):
        listed = "\n".join(f"  • [{p.get('severity', '?')}] {p.get('title')}" for p in proposals)
        return clean, "\n\n📋 Proposed tickets (set meeting_autospawn to file these):\n" + listed
    result = filing.file_findings(cfg.apps[0], "meeting", decision_raw)
    if audit is not None:
        audit.record("meeting_autospawn", filed=result.filed_n,
                     deduped=result.deduped_n, failed=result.failed_n)
    return clean, "\n\n🗂️ Auto-filed by the unit:\n" + "\n".join(result.lines)


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
    from .filing import TICKET_BLOCK_RULE
    autospawn = getattr(cfg, "meeting_autospawn", False)
    chair_system = _MEETING_CHAIR_SYSTEM + (TICKET_BLOCK_RULE if autospawn else "")
    chair = await run_agent(chair_prompt, ClaudeAgentOptions(
        model=cfg.discussion_model, system_prompt=memory.preamble() + chair_system, cwd=cwd,
        permission_mode="bypassPermissions", allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash"], setting_sources=["project"],
        max_turns=6, effort="high"), tag="the-general")
    decision_raw = (chair.final or chair.text or "(no decision)").strip()
    decision_clean, spawn_note = _autospawn_tickets(cfg, decision_raw, audit)
    decision = decision_clean + spawn_note

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
        print(f"  Technical Writer skipped: {exc}", flush=True)
    print(f"\n  meeting saved → {saved}\n", flush=True)
    return decision


async def ship_review(cfg: Config, app_name: str | None = None, audit=None) -> str:
    """A 'ready to prod?' review: the Quartermaster certifies deploy-readiness, then QM + Provost
    + Inspector debate it, and the General issues a GO / NO-GO recommendation. The unit NEVER
    promotes to MAIN — this only tells the Commander whether it's safe; the promotion is his."""
    app = cfg.app(app_name) if app_name else (cfg.apps[0] if getattr(cfg, "apps", None) else None)
    name = app.name if app else (app_name or "the app")
    cwd = _general_root()

    print(f"\n🎖️  Ship-review — {name}: is DEV ready for MAIN?\n", flush=True)
    qm_report = ""
    try:
        from . import quartermaster
        print("  · Release Manager certifying deploy-readiness…", flush=True)
        qm_report = await quartermaster.inspect(cfg, name)
    except Exception as exc:  # noqa: BLE001
        qm_report = f"(Release Manager check unavailable: {exc})"

    digest = format_signals(collect_signals(cfg))
    notes = recent_commander_notes(cfg)
    context = digest + "\n\nQuartermaster readiness report:\n" + (qm_report or "(none)")[:3500]
    topic = f"Is {name}'s DEV ready to promote to MAIN (production)?"
    roster = _select_officers(["quartermaster", "provost", "inspector"])
    said = await discuss(cfg, roster, context, notes, topic, rounds=1)

    chair_prompt = "\n".join([
        f"Ship-review for {name}. The unit's record:", "", digest, "",
        "Quartermaster readiness report:", "", (qm_report or "(none)")[:3500], "",
        "The officers debated:", "", *[f"### {who}\n{what}\n" for who, what in said],
        "Now write the recommendation. Remember: only the Commander promotes to MAIN.",
    ])
    chair = await run_agent(chair_prompt, ClaudeAgentOptions(
        model=cfg.discussion_model, system_prompt=memory.preamble() + _SHIP_REVIEW_CHAIR_SYSTEM, cwd=cwd,
        permission_mode="bypassPermissions", allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash"], setting_sources=["project"],
        max_turns=6, effort="high"), tag="the-general")
    decision = (chair.final or chair.text or "(no recommendation)").strip()

    saved = _save_transcript(cfg, f"ship-review: {name}", context, said, decision)
    notify.send(f"🚀 *Ship-review — {name}*\n\n{decision[:3500]}\n\n_Promotion to MAIN is yours, Commander._")
    if audit is not None:
        audit.record("ship_review", app=name, transcript=saved.name)
    try:
        print(f"  {await memory.scribe(cfg)}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  Technical Writer skipped: {exc}", flush=True)
    print(f"\n  ship-review saved → {saved}\n", flush=True)
    return decision


_SMALLTALK_SYSTEM = (
    "You are an officer of an elite autonomous software unit, caught in a brief CORRIDOR "
    "exchange with a fellow officer — not a formal meeting. Speak in character, 1–3 sentences, "
    "informal but professional. React to what's actually going on in the unit's record; be wry "
    "or human if it fits, and if a genuinely useful observation surfaces, land it plainly. No "
    "headers, no markdown — just talk. Do not write or edit files."
)


async def small_talk(cfg: Config, audit=None) -> str:
    """A light, in-character corridor exchange between two officers — flavor, occasionally a real
    insight. Cheap (two short turns). Saved like a council so it shows up in history."""
    import random
    from . import governor
    if not governor.under_budget(cfg):
        print("  · corridor small-talk skipped (hourly usage cap reached)", flush=True)
        return ""
    pair = random.sample(COUNCIL, 2)
    digest = format_signals(collect_signals(cfg))
    cwd = _general_root()
    convo: list[tuple[str, str]] = []
    for i, (rank, lens_role, voice) in enumerate(pair):
        if i == 0:
            prompt = "\n".join([
                f"You are the {rank} ({lens_role}). The unit's recent record:", "", digest, "",
                f"You run into the {pair[1][0]} in the corridor. Open with a casual remark about how "
                "things are going — something real from the record.",
            ])
        else:
            prompt = "\n".join([
                f"You are the {rank} ({lens_role}). The unit's recent record:", "", digest, "",
                f'The {pair[0][0]} just said: "{convo[0][1]}"', "",
                "Reply in kind — a sentence or two. Banter is welcome; land a real point if you have one.",
                "",
                "If — and ONLY if — this exchange surfaced a genuinely useful, actionable idea worth the "
                "Commander's attention, add a final line starting exactly 'INSIGHT:' with a one-sentence "
                "summary. Most corridor chats won't have one — that's fine, leave it off.",
            ])
        run = await run_agent(prompt, ClaudeAgentOptions(
            model=cfg.smalltalk_model, system_prompt=memory.preamble() + _SMALLTALK_SYSTEM, cwd=cwd,
            permission_mode="bypassPermissions", allowed_tools=["Read", "Grep", "Glob"],
            disallowed_tools=["Write", "Edit", "Bash"], setting_sources=["project"],
            max_turns=4, effort="low"), tag="smalltalk")
        convo.append((rank, (run.final or run.text or "…").strip()))
    governor.note_call(cfg, len(convo))
    insight = _corridor_insight(convo)
    saved = _save_transcript(cfg, f"corridor: {pair[0][0]} & {pair[1][0]}", digest, convo,
                             f"(corridor small-talk — {'insight surfaced' if insight else 'no decision'})")
    if insight:
        # Most corridor chats are flavor; when a real idea lands, ping the Commander on Telegram.
        notify.send(f"💡 *Corridor insight* — {pair[0][0]} & {pair[1][0]}:\n{insight}")
    if audit is not None:
        audit.record("smalltalk", officers=[p[0] for p in pair], transcript=saved.name,
                     insight=bool(insight))
    print(f"  · corridor: {pair[0][0]} & {pair[1][0]}" + ("  💡 insight → Telegram" if insight else ""),
          flush=True)
    return "\n".join(f"{who}: {what}" for who, what in convo)


def _corridor_insight(convo: list[tuple[str, str]]) -> str:
    """Pull a single actionable 'INSIGHT: …' if the corridor exchange produced one — tolerant of
    the marker on its own line or inline. Returns the text up to the end of that line."""
    for _who, what in convo:
        idx = what.upper().rfind("INSIGHT:")
        if idx != -1:
            rest = what[idx + len("INSIGHT:"):]
            first = rest.splitlines()[0] if rest.strip() else ""
            return first.strip()
    return ""


_TICKET_KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+")


async def _ticket_context(cfg: Config, message: str) -> str:
    """If the Commander names a ticket (e.g. AUTO-14), fetch it through the unit's OWN backlog
    adapter — the Jira token it already holds — and hand the General the facts inline. That way the
    General answers 'is this ticket ok?' from the unit's own access, instead of reaching for an
    ambient Atlassian MCP that would stall on a permission prompt the headless server can't answer.
    Best-effort: an unmatched key, wrong project, or transient failure just yields no context."""
    keys = list(dict.fromkeys(_TICKET_KEY.findall(message or "")))
    if not keys:
        return ""

    def _fetch() -> list[str]:
        from .backlog.base import make_backlog
        out: list[str] = []
        for key in keys:
            for app in getattr(cfg, "apps", []) or []:
                if getattr(app, "backlog_backend", "none") == "none":
                    continue
                try:
                    t = make_backlog(app).get_task(key)
                except Exception:   # noqa: BLE001 — wrong project/app or transient; try the next app
                    continue
                desc = " ".join((t.description or "").split())
                out.append(f"[{t.key}] {t.summary}\n{desc[:1500]}")
                break
        return out

    try:
        found = await asyncio.to_thread(_fetch)
    except Exception:   # noqa: BLE001 — a backlog hiccup must never break the chat reply
        return ""
    return "\n\n".join(found)


# A status / board / ticket question from the Commander → pull a LIVE snapshot of EVERY product's Jira
# board, so the General answers across ALL projects, not just the last council's single-app briefing.
_BOARD_Q = re.compile(
    r"(?i)\b(status|tickets?|jira|boards?|backlog|projects?|progress|queue|pipeline|sprint|merged|"
    r"in[\s-]?progress|to[\s-]?do|todo|blocked|where are we|"
    r"what'?s\s+(the|on|left|next|going|happening))\b")


async def _board_status(cfg: Config) -> str:
    """Live, cross-project snapshot of EVERY configured Jira board (open work assigned to the Commander),
    so the General answers 'status across all projects' with real data instead of guessing from the last
    council. Best-effort per app — one unreachable board never blanks the rest."""
    from . import intake
    lines: list[str] = []
    for a in (getattr(cfg, "apps", None) or []):
        if getattr(a, "backlog_backend", "none") != "jira":
            continue
        pk = (getattr(a, "backlog", None) or {}).get("project_key", "?")
        try:
            items = intake.from_drain(cfg, a.name, 15)
            keys = ", ".join(getattr(t, "id", "?") for _, t in items[:10]) or "nothing open"
            lines.append(f"- {a.name} (Jira {pk}): {len(items)} open assigned to you — {keys}")
        except Exception as exc:  # noqa: BLE001 - one board down must not sink the others
            lines.append(f"- {a.name} (Jira {pk}): board unreachable — {str(exc)[:60]}")
    return "\n".join(lines)


async def respond_to_commander(cfg: Config, message: str) -> str:
    """The General answers a message from the Commander (a reply to a council question, or
    any question) directly in Telegram, grounded on the latest council + record, and logs
    the exchange as standing guidance for the unit."""
    append_chat(cfg, "Q", message)   # show the Commander's message in the cockpit chat right away
    latest = history(cfg, limit=1)
    context = transcript_text(cfg, latest[0]["file"]) if latest else format_signals(collect_signals(cfg))
    notes = recent_commander_notes(cfg)
    system = (
        "You are THE GENERAL of an elite autonomous software unit, talking 1:1 with the Commander "
        "(Roman) — like a sharp, trusted colleague, NOT writing a report. Talk naturally and SHORT: "
        "2–4 sentences, plain language, no headers, no bullet lists, no status dumps, no restating "
        "his message. If he's just greeting you or making conversation, chat back like a human and "
        "let him steer. Raise AT MOST ONE thing — and only when it genuinely needs him: a real "
        "decision that's his to make, or a problem the unit can't resolve itself. Otherwise do not "
        "manufacture orders or briefings — the unit runs its own work and the daily council already "
        "covers status. You may quietly Read a file to ground a point. The unit tracks MULTIPLE products "
        "(every one is listed below) — NEVER claim it tracks only one. When the Commander asks about "
        "status, tickets, a board, or 'all projects', a LIVE snapshot of EVERY product's Jira backlog is "
        "provided to you below — answer from it, across all projects; when he names a specific ticket its "
        "live details are provided too. Don't try to open an external tracker yourself — rely on what's "
        "provided plus the files you can Read. Reply in English.\n\n"
        "HOW THE UNIT WORKS — ground every answer in this, never improvise around it: **YOU are the unit "
        "that builds the tickets.** The unit implements the Commander's Jira tickets ITSELF — its Builder "
        "writes the code on an isolated git worktree, the gate + Reviewer + Provost check it, and it lands "
        "on DEV for the Commander's QA. The Commander NEVER hand-implements a ticket, never pastes a prompt "
        "into another tool, never opens 'Claude Code', and the unit never needs to SSH anywhere — building "
        "IS the unit's job. To get a ticket worked, the right answer is one of: autopilot drains it "
        "automatically; the Commander runs it from the cockpit (Choose a ticket); he answers it in the "
        "Needs-you box; or he sends '/unblock <id>' or '/run <app> <what>' here. If he says 'run AUTO-9', "
        "that means the UNIT runs it — reassure him it's queued / tell him to /unblock it, do NOT tell him "
        "to run it elsewhere. NEVER invent a file path: the unit's products and their real repo paths are "
        "listed below — use those exact paths or none.")
    ticket_ctx = await _ticket_context(cfg, message)
    board_ctx = await _board_status(cfg) if _BOARD_Q.search(message or "") else ""
    apps_brief = "\n".join(
        f"- {a.name}: repo {getattr(a, 'repo_path', '?')} · branches "
        f"{getattr(a, 'base_branch', '?')}/{getattr(a, 'protected_branch', '?')}"
        + (f" · Jira {(getattr(a, 'backlog', None) or {}).get('project_key')}"
           if (getattr(a, 'backlog', None) or {}).get("project_key") else "")
        for a in cfg.apps) or "(no products configured yet)"
    prompt = "\n".join([
        f"The unit's products and where they REALLY live (use these exact paths — never invent one):\n{apps_brief}\n",
        *([f"LIVE board status across ALL the unit's products — open work assigned to the Commander, "
           f"pulled from each Jira just now:\n{board_ctx}\n"] if board_ctx else []),
        *([f"Background you may lean on if relevant — do NOT recite or summarize it:\n{context[:1200]}\n"]
          if context else []),
        *([f"Ticket(s) the Commander referenced — live from the unit's own backlog:\n{ticket_ctx}\n"]
          if ticket_ctx else []),
        *([f"What you've already discussed with him:\n{notes}\n"] if notes else []),
        f"The Commander says: {message}", "",
        "Reply like a colleague — short and natural.",
    ])
    run = await run_agent(prompt, ClaudeAgentOptions(
        model=cfg.discussion_model, system_prompt=memory.preamble() + system, cwd=_general_root(),
        permission_mode="bypassPermissions", allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash"], setting_sources=["project"],
        # Room to glance at a few files before replying — 6 was too tight and errored out when the
        # Commander's message invited a quick look ("investigate…"), so the General couldn't answer.
        max_turns=14, effort="low"), tag="the-general")
    answer = (run.final or run.text or "(the General had no answer)").strip()
    notify.send(f"🎖️ {answer[:3500]}")
    # Log compactly — a colleague chat, not a briefing to be replayed verbatim into future prompts.
    add_commander_note(cfg, f"Q: {message[:120]} → A: {answer[:200]}")
    append_chat(cfg, "A", answer)    # full reply to the cockpit chat (not only Telegram)
    return answer


# --------------------------------------------------------------------------- #
# Group chat — the Commander consults the whole unit (brainstorm). The relevant
# officers answer; others may add a short comment; off-lane officers PASS. The
# General is NOT in this room — the Commander talks to the General 1:1 in /chat.
# --------------------------------------------------------------------------- #
_GROUP_SYSTEM = (
    "You are an officer of an ELITE autonomous software unit in a GROUP CHAT with the Commander "
    "(Roman) — he is consulting the unit / brainstorming or just talking, not filing a ticket. Speak "
    "in character, plainly, no markdown, conversationally — short. If the question touches your lens, "
    "ANSWER directly (2–4 sentences) with a concrete, useful take. If it only partly touches your "
    "lane, add a brief comment. Reply with exactly 'PASS' ONLY when another officer is clearly better "
    "placed and you'd add nothing — NEVER for a greeting, a general or social message, or small talk, "
    "where a short friendly in-character reply is exactly right. Build on what fellow officers already "
    "said — agree and extend, or disagree with a reason; never repeat them. Ground claims in the "
    "unit's record and the files you can read; no invention. Do not write or edit files."
)


def _group_options(cfg: Config, voice: str, cwd: str) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=cfg.discussion_model,           # Sonnet — a group brainstorm is many cheap turns
        system_prompt=memory.preamble() + f"{_GROUP_SYSTEM}\n\nYour lens — {voice}",
        cwd=cwd, permission_mode="bypassPermissions",
        allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash"],
        setting_sources=["project"], max_turns=5, effort="low")


def _group_file(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("group_chat.jsonl")


def group_messages(cfg: Config, limit: int = 200) -> list[tuple[str, str]]:
    """The group thread as [(who, text)] — 'you' is the Commander, else an officer rank."""
    p = _group_file(cfg)
    if not p.exists():
        return []
    out: list[tuple[str, str]] = []
    for line in p.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append((r.get("who", ""), r.get("text", "")))
    return out


def _append_group(cfg: Config, who: str, text: str) -> None:
    with _group_file(cfg).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                            "who": who, "text": text.strip()}) + "\n")


def _group_tail(cfg: Config, n: int = 12) -> str:
    label = {"you": "Commander"}
    return "\n".join(f"{label.get(w, w)}: {t}" for w, t in group_messages(cfg, limit=n))


async def group_chat(cfg: Config, message: str, officers=None, audit=None,
                     echo: bool = True) -> list[tuple[str, str]]:
    """The Commander consults the unit. Each relevant officer replies (others PASS). Persists the
    exchange to group_chat.jsonl and returns the officers' replies as [(rank, text)]. `echo=False`
    when the caller already recorded the Commander's message (so the web UI can echo instantly)."""
    digest = format_signals(collect_signals(cfg))
    notes = recent_commander_notes(cfg)
    tail = _group_tail(cfg, 12)
    roster = _select_officers(officers)
    cwd = _general_root()
    if echo:
        _append_group(cfg, "you", message)        # the Commander's message leads the thread
    replies: list[tuple[str, str]] = []
    for rank, lens_role, voice in roster:
        prompt = "\n".join([
            f"You are the {rank} ({lens_role}). The unit's recent record:", "", digest, "",
            *([f"Standing guidance from the Commander:\n{notes}\n"] if notes else []),
            *([f"Recent group chat:\n{tail}\n"] if tail else []),
            *(["Fellow officers have already replied to this message:\n"
               + "\n".join(f"— {w}: {t}" for w, t in replies) + "\n"] if replies else []),
            f'The Commander says to the group: "{message}"', "",
            "Reply per your rules: answer if it's your lane, a short comment if it partly is, else 'PASS'.",
        ])
        run = await run_agent(prompt, _group_options(cfg, voice, cwd),
                              tag="group-" + _officer_key(rank))
        s = (run.final or run.text or "").strip()
        if not s or s.lower().rstrip(".!").strip() in _SKIP:
            continue
        replies.append((rank, s))
        _append_group(cfg, rank, s)
        print(f"  · {rank} weighed in", flush=True)
    if not replies:
        # Nobody claimed it (a greeting / small talk / off-lane aside) — the room must never look
        # dead. The host officer answers warmly so the Commander always gets a reply.
        rank, lens_role, voice = roster[0]
        prompt = "\n".join([
            f"You are the {rank} ({lens_role}). The unit's recent record:", "", digest, "",
            *([f"Recent group chat:\n{tail}\n"] if tail else []),
            f'The Commander says to the group: "{message}"', "",
            "No formal lane owns this. Reply warmly and briefly in character (1–3 sentences) — and if "
            "it helps, gently point him to what the unit can take on. Do NOT say PASS.",
        ])
        run = await run_agent(prompt, _group_options(cfg, voice, cwd), tag="group-host")
        s = (run.final or run.text or "").strip()
        if s and s.lower().rstrip(".!").strip() not in _SKIP:
            replies.append((rank, s))
            _append_group(cfg, rank, s)
    if audit is not None:
        audit.record("group_chat", officers=[r for r, _ in replies])
    return replies


# --------------------------------------------------------------------------- #
# Daily stand-up — each officer reports Yesterday / Today / Blockers from the
# real record and flags hand-offs ('need <Officer>'). Assembled deterministically
# (no extra chair call) and saved for the cockpit.
# --------------------------------------------------------------------------- #
_STANDUP_SYSTEM = (
    "You are an officer of an ELITE autonomous software unit at the daily STAND-UP, reporting to "
    "THE GENERAL. Report ONLY from your lens, grounded in the unit's record — no invention. Output "
    "exactly three labelled lines and nothing else:\n"
    "Yesterday: <what you or your squad actually did — or 'quiet'>\n"
    "Today: <the one thing you'll focus on>\n"
    "Blockers: <'none', or the blocker; if you need another officer, append 'need <Officer>: <why>'>"
)


def _standup_file(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("last-standup.md")


def last_standup(cfg: Config) -> str:
    p = _standup_file(cfg)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _standup_handoffs(rows: list[tuple[str, str]]) -> list[str]:
    """Pull 'need <Officer>: why' hand-off requests out of the officers' Blockers lines."""
    import re
    out: list[str] = []
    for rank, rep in rows:
        for m in re.finditer(r"need\s+([A-Za-z][A-Za-z .]+?)\s*:\s*([^\n]+)", rep, re.IGNORECASE):
            out.append(f"{rank} → {m.group(1).strip()}: {m.group(2).strip()}")
    return out


async def _gather_standup(cfg: Config) -> tuple[str, list[tuple[str, str]], list[str]]:
    """Each officer's Yesterday/Today/Blockers from the record + the cross-officer hand-offs.
    Returns (digest, rows, handoffs). No side effects — shared by the daily muster and /standup."""
    digest = format_signals(collect_signals(cfg))
    notes = recent_commander_notes(cfg)
    cwd = _general_root()
    print("\n🫡  Stand-up — officers reporting…\n", flush=True)
    rows: list[tuple[str, str]] = []
    for rank, lens_role, voice in COUNCIL:
        prompt = "\n".join([
            f"You are the {rank} ({lens_role}). The unit's recent record:", "", digest, "",
            *([f"Commander's standing guidance:\n{notes}\n"] if notes else []),
            "Give your stand-up now — three lines, grounded in the record.",
        ])
        run = await run_agent(prompt, ClaudeAgentOptions(
            model=cfg.discussion_model,
            system_prompt=memory.preamble() + f"{_STANDUP_SYSTEM}\n\nYour lens — {voice}",
            cwd=cwd, permission_mode="bypassPermissions", allowed_tools=["Read", "Grep", "Glob"],
            disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash"], setting_sources=["project"],
            max_turns=5, effort="low"), tag="standup-" + _officer_key(rank))
        rows.append((rank, (run.final or run.text or "(no report)").strip()))
        print(f"  · {rank} reported", flush=True)
    return digest, rows, _standup_handoffs(rows)


def _standup_text(rows: list[tuple[str, str]], handoffs: list[str]) -> str:
    body = [f"🫡 Stand-up — {time.strftime('%Y-%m-%d %H:%M')}", ""]
    for rank, rep in rows:
        body += [f"### {rank}", rep, ""]
    body += ["### Hand-offs & blockers", *([f"- {h}" for h in handoffs] or ["- none"])]
    return "\n".join(body)


def _standup_telegram(rows: list[tuple[str, str]], handoffs: list[str]) -> str:
    """Phone-sized stand-up: a one-line roll-up + only the hand-offs/blockers (the part that needs the
    Commander). The full per-officer round-table is NOT pushed to Telegram — it stays in the cockpit
    (last-standup.md + the saved transcript), so the daily ping is skimmable instead of a wall of chat."""
    hb = "\n".join(f"- {h}" for h in handoffs) or "- none"
    return (f"🫡 *Daily stand-up* — {len(rows)} officer(s) reported.\n\n"
            f"*Hand-offs & blockers:*\n{hb[:1500]}\n\n_Full round-table in the cockpit._")


async def hold_standup(cfg: Config, audit=None) -> str:
    """On-demand stand-up (the daily one now runs inside the muster). Saves last-standup.md + notifies."""
    from . import governor
    digest, rows, handoffs = await _gather_standup(cfg)
    text = _standup_text(rows, handoffs)
    _standup_file(cfg).write_text(text, encoding="utf-8")
    _save_transcript(cfg, "stand-up", digest, rows, "Daily stand-up — see the round-table below.")
    notify.send(_standup_telegram(rows, handoffs))
    governor.note_call(cfg, len(rows))
    if audit is not None:
        audit.record("standup", officers=[r for r, _ in rows], handoffs=len(handoffs))
    return text


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
