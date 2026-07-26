"""The Daily Council — the Elite Unit's morning muster (compounding improvement).

At 10:00 the officers assemble. Each gives a short SITREP from its lens on the unit's
recent operations; the Engineering Coach proposes one improvement (the unit studies every day);
the Engineering Manager covers personnel; and THE CTO chairs — synthesizing a briefing, the
decisions taken, and the questions only the Commander can answer (product / strategy /
business). The briefing goes to Telegram, the full transcript is saved for the cockpit,
and open questions are pushed to you.

  general council                 # hold the muster now (also runs on the 10:00 schedule)
  general council --topic "…"     # an ad-hoc improvement muster on a specific topic

Officers are read-only here (they Read the record + their own files; they do not write).
The Engineering Manager only *proposes* hires/retirements — you approve and apply.

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
from .signals import collect_signals, format_signals
from .officers import OFFICER_NAMES, display
from . import auth_probe, notify

# Reverse map (display name -> stable internal key). Officer display names are renamed in
# OFFICER_NAMES (EU-17/EU-40); the internal keys are immutable, so selection and run-tags resolve
# through the stable key regardless of what the human-facing name happens to be today.
_NAME_TO_KEY = {name.lower(): key for key, name in OFFICER_NAMES.items()}

# --------------------------------------------------------------------------- #
# The officers who sit on the council (active roster). Each speaks once, briefly,
# in character. Army terminology; disciplined tone. Read-only.
# --------------------------------------------------------------------------- #
_OFFICER_RULES = (
    "You are an officer of an ELITE autonomous software unit reporting to THE CTO "
    "(who reports to the Commander, Roman). Speak in a disciplined, military tone, briefly "
    "(3–6 sentences), strictly from your lens. Only major officers sit on this council; if you "
    "command a squad, consult it as needed but report for it yourself — engineers do not speak "
    "here. Ground every claim in the record or files you read — no invention. End with ONE "
    "concrete recommendation for today. Solve technical and process problems YOURSELVES; "
    "escalate to the Commander ONLY when the call is genuinely his — product direction, "
    "business/strategy, or an irreversible decision with no safe default. When (and only when) "
    "such a decision truly exists, put it on its own line prefixed exactly 'FOR YOU:'. "
    "Most days there is none. Do not write or edit files.\n"
    "This is a real round-table: in later rounds you will see what your fellow officers said — "
    "RESPOND to them, by name, when it touches your lens: agree and build, or push back with a "
    "reason. If you have nothing to add this round, reply with exactly 'PASS'."
)

COUNCIL = [
    ("Engineering Manager", "S-1 · Personnel (HR)",
     "Your lens is the roster. Are the right officers in post for the work coming in? Read "
     "officers/*.md and the record. Recommend at most one personnel action — recruit a new "
     "officer ONLY if a real, repeated capability gap has no owner (draft its role in one "
     "line), or retire/retrain an officer that is idle or underperforming. Propose only."),
    ("Dev Team Lead", "Builder",
     "Your lens is delivery. You command a squad (Vanguard FE · Ordnance BE · Logistics DB · "
     "DevOps · …) — consult their status, but you alone report for them here. What shipped, "
     "what fought back, where did the build burn passes or hit max effort? Name the friction "
     "and the one change that makes the next build cleaner. If the squad needs a new engineer — "
     "or a junior officer (sub-lead) to own a focus area and command engineers of its own — "
     "request it; the Engineering Manager approves the hire."),
    ("Code Reviewer", "Reviewer",
     "Your lens is quality and risk. What recurring defects or spec-gaps are you catching, and "
     "what is slipping through? Call the single quality risk the unit must close, and whether "
     "standards should tighten or ease."),
    ("QA Engineer", "S-2 · Recon (QA)",
     "Your lens is what actually breaks in the running app on DEV — runtime, UX, accessibility "
     "— the defects unit tests and diff review miss. From the record (and any e2e results), name "
     "the biggest live-QA blind spot and the one smoke test worth standing up first. If there is "
     "no browser/e2e coverage yet, say so plainly."),
    ("Security Engineer", "Security",
     "Your lens is security and exposure. From the record and recent changes, name the single "
     "biggest risk the unit is carrying — a secret in code, a tenant-isolation or authz gap, "
     "injection, or a known-vulnerable dependency — and the one control to add. If you have no "
     "signal yet, say so plainly."),
    ("Release Manager", "S-4 · Deploy readiness",
     "Your lens is whether DEV can actually ship to MAIN — build, types, migrations, deps, env, "
     "deploy config. Name the single biggest thing standing between DEV and a clean promotion, "
     "and the one readiness check to add. If readiness is unknown, say what to verify."),
    ("SRE", "S-3 · Integration & rollback",
     "Your lens is the health of DEV right after each landing — does the integrated branch actually "
     "build, test and run once the merge is in? You own the post-merge suite and the rollback: a land "
     "that breaks DEV gets reverted and handed back. Name the biggest integration risk and the one "
     "post-merge check worth standing up. If there's no post-merge suite yet, say so plainly."),
    ("Engineering Coach", "Doctrine & Training",
     "Your lens is improvement and training — the unit studies every day. From recurring "
     "weaknesses, name the ONE drill (a precise edit to an officer's Identity/Knowledge/Skills "
     "file) that yields the most compounding gain tomorrow. You also own onboarding for any "
     "newly recruited officer/engineer and refresher drills for existing ones — flag if anyone is "
     "due one."),
]

_CHAIR_SYSTEM = (
    "You are THE CTO, chairing the Elite Unit's daily council. You have heard each officer. "
    "Produce a SHORT commander's briefing — it lands on Roman's phone, so if he can't skim it in ~15 "
    "seconds it's too long. Keep the WHOLE thing under ~90 words. Summarize; do NOT restate the debate "
    "or recap officer-by-officer. Exactly this markdown shape and nothing else:\n\n"
    "**SITREP** — at most 2 short lines on recent operations from the record.\n\n"
    "**ORDERS** — at most 3 short bullets: the concrete actions the unit will take (who does what). "
    "Fold in the officers' best recommendations; resolve conflicts.\n\n"
    "**FOR YOU** — ONLY decisions that are genuinely Roman's: product direction, "
    "business/strategy, or an irreversible call with no safe default. NOT technical or process "
    "choices the unit should make itself. Hold a high bar — most days this is 'None.' One "
    "question per line ending in '?', or write 'None.' Never invent questions to fill space."
)

_DAILY_SYSTEM = (
    "You are THE CTO writing the Elite Unit's DAILY stand-up — it lands on Roman's phone, so keep the "
    "WHOLE thing skimmable in ~10 seconds, under ~45 words. You are given the deterministic stand-up "
    "already computed (what shipped yesterday, what needs him) — do NOT restate those lines. The daily "
    "is YESTERDAY's result + TODAY's focus + anything that needs him — NOTHING else. Do NOT cite "
    "cumulative or all-time history: no totals or counters like 'X/Y tickets landed', 'avg passes', "
    "'N hit max effort', 'reviewer bounce rate' — Roman does not want the running history, only today. "
    "Output exactly this markdown and nothing else:\n\n"
    "**FOCUS** — one line: the single most important thing the unit should push TODAY (a ticket or "
    "action), grounded in what is open/needs-you right now.\n\n"
    "**FOR YOU** — ONLY a decision that is genuinely Roman's (product direction, business/"
    "strategy, or an irreversible call with no safe default); NOT a technical/process choice the unit "
    "should make itself. Hold a HIGH bar — most days this is 'None.' One question per line ending in "
    "'?', or write 'None.' Never invent a question to fill space."
)

_MEETING_CHAIR_SYSTEM = (
    "You are THE CTO, chairing a focused meeting of the Elite Unit on a single topic. You "
    "have heard the officers debate. Produce a SHORT decision record — it lands on Roman's phone, so "
    "keep the WHOLE thing under ~80 words, skimmable in 15 seconds; summarize, do NOT replay the "
    "debate. Exactly this markdown shape and nothing else:\n\n"
    "**TOPIC** — one line.\n\n"
    "**DECISION** — what the unit will do, who owns it, and the one-line why. Resolve the debate "
    "and take a clear position; do not fence-sit.\n\n"
    "**ACTIONS** — 1–3 bullets: concrete next steps (file a ticket, propose a drill, add a "
    "check, draft a hire). Name the officer who owns each.\n\n"
    "**FOR YOU** — ONLY a decision that is genuinely Roman's (product / strategy / "
    "irreversible, no safe default). One question per line ending in '?', else 'None.'"
)

# SQUAD brand voice: plain-language question for the ship-decision card (BRAND.md).
COUNCIL_SHIP_QUESTION = "Is dev ready to promote to main?"

_SHIP_REVIEW_CHAIR_SYSTEM = (
    "You are THE CTO, chairing a SHIP-REVIEW: is dev ready to promote to main (production)? "
    "You have the Release Manager's readiness report and the officers' debate. CRITICAL: the unit "
    "NEVER promotes to main — that is your call alone. You only recommend. "
    "Output exactly this markdown shape and nothing else:\n\n"
    "**VERDICT** — GO / NO-GO / GO WITH CAVEATS (one blunt line).\n\n"
    "**BLOCKERS** — bullets: anything that must be fixed before prod (build, types, migrations, "
    "deps, env/secrets, security, known defects). Write 'None' only if truly clean.\n\n"
    "**PRE-FLIGHT** — bullets: what to verify or run before promoting.\n\n"
    f"**FOR YOU** — end with the go/no-go, framed as your decision: '{COUNCIL_SHIP_QUESTION} "
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


def _legacy_council_dir(cfg: Config) -> Path:
    """The pre-2026-07-21 council/ location — a sibling of the audit_path's PARENT dir (the repo
    root before the state/ migration moved audit_path one level deeper into state/). ``parent.parent``
    matches the actual one-level migration (audit.jsonl -> state/audit.jsonl); any other layout
    simply yields a non-existent legacy dir -> no adoption (fails safe)."""
    return Path(cfg.audit_path).resolve().parent.parent / "council"


def adopt_legacy_council(cfg: Config, audit=None) -> bool:
    """EU-431: self-heal the council archive the 2026-07-21 state/ migration orphaned.

    Background: the migration moved ``audit_path`` (and with it every ``with_name("council")``
    derivation) one level deeper into ``state/``, but left the real archive — ``index.jsonl`` + N
    transcripts back to 2026-06-20 — in the legacy ``council/`` sibling. ``_council_dir()`` now
    resolves to the empty ``state/council/``; the next ceremony would append a single fresh row to a
    new ``state/council/index.jsonl`` and ``history()`` would permanently lose every prior row (a
    lost-index problem — the files stay on disk but nothing points at them).

    On boot this MOVES the legacy archive into the new location (transcripts + index.jsonl,
    byte-for-byte, so the append ordering — and thus ``history()`` — is preserved), leaving
    ``cron.log`` where the crontab's ``>> council/cron.log`` redirect still writes it (AC1), and
    records a ``council_archive_adopted`` audit event (AC2). Idempotent + no-clobber: a new dir
    that already holds an ``index.jsonl`` is never touched. Best-effort — never raises.

    Mirrors ``backend_pref.migrate``: called ONLY from the live CLI entrypoint (``main._main``), so
    a test around a tmp config can never relocate the operator's real archive. Returns True iff it
    moved the archive in (for observability / tests)."""
    import shutil
    try:
        new = _council_dir(cfg).resolve()              # ensures state/council/ exists
        if (new / "index.jsonl").exists():             # already populated -> never clobber (AC2 pin)
            return False
        legacy = _legacy_council_dir(cfg)
        if legacy == new or not (legacy / "index.jsonl").exists():
            return False                               # nothing to adopt
        moved = 0
        for src in sorted(legacy.iterdir()):
            if src.name == "cron.log":
                continue                               # AC1: the crontab redirect writes here — leave it
            dst = new / src.name
            if dst.exists():
                continue                               # never overwrite a file already present
            shutil.move(str(src), str(dst))
            moved += 1
        rows = 0
        idx = new / "index.jsonl"
        if idx.exists():
            rows = sum(1 for ln in idx.read_text(encoding="utf-8").splitlines() if ln.strip())
        if audit is not None:
            try:
                audit.record("council_archive_adopted", rows=rows, files=moved,
                             from_path=str(legacy), to_path=str(new))
            except Exception:  # noqa: BLE001 — an audit write must never break a ceremony
                pass
        return moved > 0
    except Exception:  # noqa: BLE001 — boot-critical: a migration helper must NEVER break startup
        return False


def _notes_file(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("commander_notes.md")


def recent_commander_notes(cfg: Config, lines: int = 12) -> str:
    """The last few decisions/guidance, one note per line. Bounded so old chatter doesn't pile up
    in every officer's prompt and pull the CTO back toward report-mode."""
    p = _notes_file(cfg)
    if not p.exists():
        return ""
    tail = p.read_text(encoding="utf-8").splitlines()[-lines:]
    return "\n".join(tail).strip()


def add_commander_note(cfg: Config, text: str) -> None:
    """Append one note from the Commander/CTO exchange. Collapsed to a single bounded line so a
    long answer can never become a wall of 'standing guidance' that feeds back into later prompts."""
    p = _notes_file(cfg)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    one_line = " ".join(text.split()).strip()[:240]
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- [{stamp}] {one_line}\n")


# --- Cockpit chat transcript --------------------------------------------------------------------- #
# The FULL Commander<->CTO exchange, separate from commander_notes.md (which is truncated standing
# guidance fed into prompts). The cockpit chat reads THIS so it shows the CTO's real reply inline
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


def _last_chat_line(cfg: Config) -> str:
    """The last raw line of the cockpit chat transcript (or '' if none) — used to dedup an echo the
    cockpit already wrote synchronously (EU-307) against respond_to_commander()'s own leading append."""
    p = _chat_file(cfg)
    if not p.exists():
        return ""
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    return lines[-1].strip() if lines else ""


def chat_transcript(cfg: Config, lines: int = 400) -> str:
    """Recent cockpit chat turns (full Q/A), newest kept. Falls back to the (truncated) commander notes
    if no chat file exists yet, so existing history still shows."""
    p = _chat_file(cfg)
    if not p.exists():
        return recent_commander_notes(cfg, lines=min(lines, 240))
    return "\n".join(p.read_text(encoding="utf-8").splitlines()[-lines:]).strip()


def _honest_commander_section(cfg: Config, text: str) -> str:
    """2026-07-19 (Commander order): '**FOR YOU** — None' while decisions sit in /needs
    is a lie the model kept telling. Deterministic truth: when the briefing says None/— but
    pending decisions exist, splice in the top 3 (one line each) + the count pointer."""
    import re as _re
    m = _re.search(r"\*\*FOR YOU\*\*\s*[—:-]\s*(None|—|-|\(none\))\.?", text, _re.I)
    if not m:
        return text
    try:
        from . import decisions as _dec
        pending = _dec.load(cfg)
    except Exception:  # noqa: BLE001
        pending = []
    if not pending:
        return text
    def _one(q: str) -> str:
        q = " ".join(str(q or "").split())
        return (q[:110] + "…") if len(q) > 110 else q
    top = "\n".join(f"  • {p['id']}: {_one(p.get('question', ''))}" for p in pending[:3])
    more = len(pending) - min(3, len(pending))
    tail = f"\n  …and {more} more — answer in cockpit → /needs" if more > 0 else \
           "\n  Answer in cockpit → /needs"
    return text[:m.start()] + (f"**FOR YOU** — {len(pending)} decision(s) waiting:\n"
                               + top + tail) + text[m.end():]


def latest_focus(cfg: Config) -> str:
    """The FOCUS line of the most recent council/daily — grounds the consult thread in today's
    priority. '' when no transcript or no FOCUS line exists."""
    import re as _re
    try:
        for rec in history(cfg, limit=3):
            body = transcript_text(cfg, rec.get("file", ""))
            m = _re.search(r"\*\*FOCUS\*\*\s*[—:-]\s*(.+)", body)
            if m:
                return " ".join(m.group(1).split())[:200]
    except Exception:  # noqa: BLE001
        pass
    return ""


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
        disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash", "Task", "Agent"],
        setting_sources=["project"],
        max_turns=8,
        effort="medium",
    )


def _officer_key(rank: str) -> str:
    # Prefer the immutable internal key for the display name; fall back to a slug so an
    # unknown/ad-hoc rank (e.g. a name parsed out of a 'MEETING:' line) never crashes a lookup.
    return _NAME_TO_KEY.get(rank.lower(), rank.lower().replace(" ", "-"))


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


async def hold_council(cfg: Config, topic: str | None = None, audit=None, *,
                       broadcast: bool | None = None) -> str:
    """Run the muster as a multi-round debate; the CTO chairs. Returns the briefing,
    saves the full transcript, and sends the briefing to Telegram."""
    _auth_preflight(cfg)                               # EU-430 AC4: warn before the first 401
    sig = collect_signals(cfg)
    digest = format_signals(sig)
    notes = recent_commander_notes(cfg)
    cwd = _general_root()
    # EU-303: single-sender election — elect early so an auth-outage alert (EU-430) respects the
    # same one-host rule as a normal broadcast, and a second scheduler can't double-send it.
    if broadcast is None:
        from . import decisions as _dec
        broadcast = _dec.should_poll_telegram(cfg)[0]

    if topic:
        print("\n🎖️  Muster — officers in session…\n", flush=True)
        said = await discuss(cfg, COUNCIL, digest, notes, topic, getattr(cfg, "council_rounds", 2))
        handoffs: list[str] = []
    else:
        # The DAILY muster IS the stand-up + the CTO's briefing — council and stand-up merged into
        # one daily. Each officer reports Yesterday/Today/Blockers; the CTO synthesises from it.
        print("\n🎖️  Daily muster — officers reporting; the CTO will brief…\n", flush=True)
        _sd, said, handoffs = await _gather_standup(cfg)

    # EU-430 AC2/AC3: if any officer's report is a provider auth error, the model is down — alert
    # the operator and bail BEFORE writing the poisoned stand-up file / the CTO call / the
    # transcript / the roster refresh. (The CTO call is also guarded below for mid-ceremony expiry.)
    _auth_down_report = next((rep for _, rep in said if auth_probe.looks_like_provider_error(rep)),
                             None)
    if _auth_down_report is not None:
        return _ceremony_auth_down(cfg, audit, ceremony="council", broadcast=bool(broadcast),
                                   detail=_auth_down_report)
    if not topic:
        try:
            _standup_file(cfg).write_text(_standup_text(said, handoffs), encoding="utf-8")
        except OSError:
            pass

    # The CTO chairs and synthesizes the briefing.
    print("  · CTO sums up…", flush=True)
    chair_prompt = "\n".join([
        "The Elite Unit's record:", "", digest, "",
        *([f"Commander's standing guidance:\n{notes}\n"] if notes else []),
        *([f"Muster focus: {topic}\n"] if topic else []),
        "The council said:" if topic else "The engineers' stand-up (Yesterday / Today / Blockers):", "",
        *[f"### {who}\n{what}\n" for who, what in said],
        *([f"Hand-offs needing coordination: {'; '.join(handoffs)}\n"] if handoffs else []),
        "Now write the briefing.",
    ])
    from .filing import TICKET_BLOCK_RULE
    chair = await run_agent(chair_prompt, ClaudeAgentOptions(
        model=cfg.discussion_model,
        system_prompt=memory.preamble() + _CHAIR_SYSTEM + TICKET_BLOCK_RULE, cwd=cwd,
        permission_mode="bypassPermissions", allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash", "Task", "Agent"], setting_sources=["project"],
        max_turns=6, effort="high"), tag="the-general")
    chair_provider, chair_model = chair.provider, chair.model_version
    briefing_raw = (chair.final or chair.text or "(no briefing)").strip()
    # EU-430 AC2: the chair call itself can fail auth even if the officers didn't (a credential that
    # lapsed mid-ceremony, or a topic-muster whose discuss() passed but the chair didn't). Guard it
    # the same way — alert, never broadcast/persist the raw error.
    if auth_probe.looks_like_provider_error(briefing_raw):
        return _ceremony_auth_down(cfg, audit, ceremony="council", broadcast=bool(broadcast),
                                   detail=briefing_raw)

    # Route any ticket-worthy items the briefing proposed into the Commander's approval queue
    # (or, with meeting_autospawn, file them) — the block is stripped from the briefing shown.
    briefing, spawn_note = _autospawn_tickets(cfg, briefing_raw, audit,
                                              source=("muster: " + topic) if topic else "council/daily",
                                              officer_label="council")
    briefing = _honest_commander_section(cfg, (briefing + spawn_note).strip())

    saved = _save_transcript(cfg, topic, digest, said, briefing)
    questions = _commander_questions(briefing)

    # broadcast was elected at the top of this function (EU-303 single-sender, EU-430 early).
    # Report up to the Commander.
    header = "🎖️ *Daily Council*" if not topic else f"🎖️ *Muster — {topic}*"
    if broadcast:
        notify.send(f"{header}\n\n{await notify.report_brief(cfg, briefing)}")
        if questions:
            notify.send("❓ *The unit needs your call:*\n" + "\n".join(f"• {q}" for q in questions)
                        + "\n\nReply here — I'll act on it, and open a ticket if it's work.")
    if audit is not None:
        audit.record("council", topic=topic or "daily", provider=chair_provider, model=chair_model,
                     officers=[r for r, _, _ in COUNCIL], questions=len(questions),
                     broadcast=bool(broadcast), transcript=saved.name)
    # The Technical Writer folds this council's lessons into Unit Memory (best-effort — never break the muster).
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
    # EU-378: re-derive the STATE OF DEV brief alongside the roster (same no-LLM, code-derived
    # contract) so every officer's preamble reflects today's code, not last week's councils.
    try:
        from . import devstate
        devstate.refresh(cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"  Dev-state refresh skipped: {exc}", flush=True)
    print(f"\n  council saved → {saved}\n", flush=True)
    return briefing


# Phase-2 §2 (2026-07-06): the officer-raised 'MEETING:' request pipeline
# (extract_meeting_requests / pending_meeting_requests) was deleted with the events.py autonomy
# layer — its only consumer. Meetings are convened on demand (CLI / cockpit / Telegram) only.


def _autospawn_tickets(cfg: Config, decision_raw: str, audit=None, *,
                       source: str = "meeting", officer_label: str = "meeting") -> tuple[str, str]:
    """Strip any ticket block from a council/meeting decision and route the proposed tickets.

    Default (EU-61): the proposals land in the Commander's approval queue (Needs you → Approve to
    file a chosen subset / Deny), never straight to the board. `meeting_autospawn` is the explicit
    opt-in bypass that files them immediately, de-duped, to the primary app (the old all-or-nothing
    behaviour). Drills/hires stay proposal-only elsewhere. Returns (clean_decision, note)."""
    from . import filing
    proposals, clean = filing.parse_tickets(decision_raw)
    if not proposals or not getattr(cfg, "apps", None):
        return clean, ""
    if getattr(cfg, "meeting_autospawn", False):                       # explicit opt-in bypass
        result = filing.file_findings(cfg.apps[0], officer_label, decision_raw)
        if audit is not None:
            audit.record("meeting_autospawn", filed=result.filed_n,
                         deduped=result.deduped_n, failed=result.failed_n)
        return clean, "\n\n🗂️ Auto-filed by the unit:\n" + "\n".join(result.lines)
    # Default: queue for the Commander to approve/deny per ticket.
    from . import approvals
    bid = approvals.enqueue_proposals(cfg, app_name=cfg.apps[0].name, officer_label=officer_label,
                                      source=source, report=decision_raw)
    if audit is not None:
        audit.record("proposals_queued", source=source, batch=bid or "", count=len(proposals))
    listed = "\n".join(f"  • [{p.get('severity', '?')}] {p.get('title')}" for p in proposals)
    return clean, ("\n\n📋 Proposed tickets — queued for your approval "
                   "(cockpit → Needs you):\n" + listed)


async def hold_meeting(cfg: Config, topic: str, officers=None, rounds: int | None = None,
                       audit=None) -> str:
    """An ad-hoc meeting: the relevant officers debate ONE topic, the CTO decides, and the
    outcome is logged to Unit Memory + Telegram. `officers` is a list of names/keys (None = all).
    Convened on demand (cockpit / CLI / Telegram) — the officer-raised 'MEETING:' request
    pipeline was deleted in Phase-2 §2 (see the marker above)."""
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
        "The engineers debated:", "",
        *[f"### {who}\n{what}\n" for who, what in said],
        "Now write the decision record.",
    ])
    # Mirror hold_council: the chair is ALWAYS told to propose a ticket block, so ad-hoc meetings
    # feed the approval queue in the default (queue-for-approval) mode. The meeting_autospawn flag
    # only decides file-vs-queue downstream in _autospawn_tickets — never whether tickets are proposed.
    from .filing import TICKET_BLOCK_RULE
    chair = await run_agent(chair_prompt, ClaudeAgentOptions(
        model=cfg.discussion_model,
        system_prompt=memory.preamble() + _MEETING_CHAIR_SYSTEM + TICKET_BLOCK_RULE, cwd=cwd,
        permission_mode="bypassPermissions", allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash", "Task", "Agent"], setting_sources=["project"],
        max_turns=6, effort="high"), tag="the-general")
    decision_raw = (chair.final or chair.text or "(no decision)").strip()
    chair_provider, chair_model = chair.provider, chair.model_version
    decision_clean, spawn_note = _autospawn_tickets(cfg, decision_raw, audit,
                                                    source=f"meeting: {topic}", officer_label="meeting")
    decision = decision_clean + spawn_note

    saved = _save_transcript(cfg, f"meeting: {topic}", digest, said, decision)
    questions = _commander_questions(decision)
    notify.send(f"🎖️ *Meeting — {topic}*\n\n{await notify.report_brief(cfg, decision)}")
    if questions:
        notify.send("❓ *The unit needs your call:*\n" + "\n".join(f"• {q}" for q in questions)
                    + "\n\nReply here — I'll act on it, and open a ticket if it's work.")
    if audit is not None:
        audit.record("meeting", topic=topic, provider=chair_provider, model=chair_model,
                     officers=[o[0] for o in roster], questions=len(questions), transcript=saved.name)
    try:
        print(f"  {await memory.scribe(cfg)}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  Technical Writer skipped: {exc}", flush=True)
    print(f"\n  meeting saved → {saved}\n", flush=True)
    return decision


async def ship_review(cfg: Config, app_name: str | None = None, audit=None) -> str:
    """A 'ready to prod?' review: the Release Manager certifies deploy-readiness, then Release Manager + Security Engineer
    + Code Reviewer debate it, and the CTO issues a GO / NO-GO recommendation. The unit NEVER
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
    context = digest + "\n\nRelease Manager readiness report:\n" + (qm_report or "(none)")[:3500]
    topic = f"Is {name}'s DEV ready to promote to MAIN (production)?"
    roster = _select_officers(["quartermaster", "provost", "inspector"])
    said = await discuss(cfg, roster, context, notes, topic, rounds=1)

    chair_prompt = "\n".join([
        f"Ship-review for {name}. The unit's record:", "", digest, "",
        "Release Manager readiness report:", "", (qm_report or "(none)")[:3500], "",
        "The engineers debated:", "", *[f"### {who}\n{what}\n" for who, what in said],
        "Now write the recommendation. Remember: only the Commander promotes to MAIN.",
    ])
    chair = await run_agent(chair_prompt, ClaudeAgentOptions(
        model=cfg.discussion_model, system_prompt=memory.preamble() + _SHIP_REVIEW_CHAIR_SYSTEM, cwd=cwd,
        permission_mode="bypassPermissions", allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash", "Task", "Agent"], setting_sources=["project"],
        max_turns=6, effort="high"), tag="the-general")
    decision = (chair.final or chair.text or "(no recommendation)").strip()

    saved = _save_transcript(cfg, f"ship-review: {name}", context, said, decision)
    notify.send(f"🚀 *Ship-review — {name}*\n\n{await notify.report_brief(cfg, decision)}\n\n_Promotion to MAIN is yours, Commander._")
    if audit is not None:
        audit.record("ship_review", app=name, transcript=saved.name)
    try:
        print(f"  {await memory.scribe(cfg)}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  Technical Writer skipped: {exc}", flush=True)
    print(f"\n  ship-review saved → {saved}\n", flush=True)
    return decision


# Phase-2 §2 (2026-07-06): corridor small-talk (small_talk/_corridor_insight) was DELETED —
# the audit's verdict: zero delivery function. Its cron slots and the events.py quiet-cycle roll
# went with it; nothing convenes officers for flavor anymore.

_TICKET_KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+")


async def _ticket_context(cfg: Config, message: str) -> str:
    """If the Commander names a ticket (e.g. AUTO-14), fetch it through the unit's OWN backlog
    adapter — the Jira token it already holds — and hand the CTO the facts inline. That way the
    CTO answers 'is this ticket ok?' from the unit's own access, instead of reaching for an
    ambient Atlassian MCP that would stall on a permission prompt the headless server can't answer.

    Also surfaces the LAST unit-posted ([General]) comment — these carry the Builder's concrete
    next-step instructions (ready diff to paste, secrets to set, CI guardrail notes, etc.) that
    are exactly what the Commander needs when they ask 'what do I need to do about X?'

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
                    backlog = make_backlog(app)
                    t = backlog.get_task(key)
                except Exception:   # noqa: BLE001 — wrong project/app or transient; try the next app
                    continue
                # 3 000 chars fits the description + any appended Commander-feedback section while
                # keeping the prompt payload sane (the 1 500-char cap was cutting off comments).
                desc = " ".join((t.description or "").split())
                block = f"[{t.key}] {t.summary}\n{desc[:3000]}"
                # Explicitly surface the LAST [General] comment — the unit's own post (Builder
                # handoffs, CI guardrail instructions, escalation notes). These are filtered out of
                # the description fed to the Builder (so it doesn't re-read its own generic posts as
                # QA feedback), but they ARE what the Commander needs to know when they ask
                # "what exactly do I have to do about this ticket?"
                try:
                    builder_note = backlog.latest_builder_comment(key)
                    if builder_note:
                        block += (
                            f"\n\nLast unit post on {key} (the Builder/CTO's own comment — "
                            f"read this first when the Commander asks 'what to do'):\n"
                            f"{builder_note[:2000]}"
                        )
                except Exception:   # noqa: BLE001 — best-effort
                    pass
                out.append(block)
                break
        return out

    try:
        found = await asyncio.to_thread(_fetch)
    except Exception:   # noqa: BLE001 — a backlog hiccup must never break the chat reply
        return ""
    return "\n\n".join(found)


# A status / board / ticket question from the Commander → pull a LIVE snapshot of EVERY product's Jira
# board, so the CTO answers across ALL projects, not just the last council's single-app briefing.
_BOARD_Q = re.compile(
    r"(?i)\b(status|tickets?|jira|boards?|backlog|projects?|progress|queue|pipeline|sprint|merged|"
    r"in[\s-]?progress|to[\s-]?do|todo|blocked|where are we|"
    r"what'?s\s+(the|on|left|next|going|happening))\b")


async def _board_status(cfg: Config) -> str:
    """Live, cross-project snapshot of EVERY configured Jira board (open work assigned to the Commander),
    so the CTO answers 'status across all projects' with real data instead of guessing from the last
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


async def _needs_context(cfg: Config) -> str:
    """Real Needs-you counts derived from the four live streams (decisions, approvals, proposals,
    tasks) — injected into every CTO reply so any number it cites is grounded in reality, never
    invented.  Best-effort: a missing/broken stream degrades gracefully."""
    def _compute() -> str:
        try:
            from . import needs as _needs
            s = _needs.summary(cfg)
            parts: list[str] = []
            if s["decisions"]:
                ids = ", ".join(d.get("id", "?") for d in s["decisions"])
                parts.append(f"{len(s['decisions'])} open decision(s) awaiting your answer"
                             f" (ticket(s): {ids})")
            if s["proposals"]:
                parts.append(f"{len(s['proposals'])} proposal batch(es) to approve/deny")
            if s["tasks"]:
                t_ids = ", ".join(
                    t.get("ticket_id", "?") for t in s["tasks"] if t.get("ticket_id"))
                parts.append(f"{len(s['tasks'])} run(s) needing attention"
                             + (f" ({t_ids})" if t_ids else ""))
            if not parts:
                return ("Needs-you (live — cite ONLY this, never guess): 0 items — "
                        "nothing is waiting for the Commander right now.")
            return ("Needs-you (live — cite ONLY these numbers, never guess): "
                    f"{s['total']} total — " + "; ".join(parts) + ".")
        except Exception:  # noqa: BLE001
            return ""
    try:
        return await asyncio.to_thread(_compute)
    except Exception:  # noqa: BLE001
        return ""


def recent_thread_context(cfg: Config, ticket_ref: str | None = None,
                          n_turns: int = 12) -> str:
    """Bundle recent chat turns and any pending proposals that mention ticket_ref.

    (a) Last n_turns lines from commander_chat.md — the untruncated Q/A thread so
        short follow-ups like 'create it' or 'approve' resolve without re-asking.
    (b) Pending proposal batches whose source or proposal titles/bodies mention ticket_ref.

    Returns a formatted string ready to embed in the LLM prompt, or '' when there
    is nothing useful to inject.
    """
    from . import approvals as _approvals

    parts: list[str] = []

    # (a) Recent conversation thread (full text, not the truncated commander_notes summary).
    thread = chat_transcript(cfg, lines=n_turns)
    if thread:
        parts.append(f"Recent conversation thread (last ~{n_turns} turns):\n{thread}")

    # (b) Pending proposal batches the Commander may be referring to with 'approve'/'create it'.
    if ticket_ref:
        ref_up = ticket_ref.strip().upper()
        matched: list[dict] = []
        for batch in _approvals.pending_proposals(cfg):
            haystack = " ".join([
                str(batch.get("source", "")),
                *[p.get("title", "") + " " + p.get("body", "")
                  for p in batch.get("proposals", [])],
            ]).upper()
            if ref_up in haystack:
                matched.append(batch)
        if matched:
            lines: list[str] = []
            for batch in matched:
                lines.append(
                    f"Pending proposal batch '{batch.get('source', '?')}' "
                    f"(id {batch.get('id', '?')}, app {batch.get('app', '?')}):"
                )
                for p in batch.get("proposals", []):
                    lines.append(
                        f"  • [{p.get('severity', '?')}] {p.get('title', '?')}"
                        + (f": {p['body'][:200]}" if p.get("body") else "")
                    )
            parts.append(
                "Pending proposals the Commander may be referring to:\n" + "\n".join(lines)
            )

    return "\n\n".join(parts)


_COMMANDER_TICKET_RULE = (
    "\n\nOPENING WORK: if — and ONLY if — the Commander is telling you to BUILD or FIX something "
    "concrete, or approving a proposed action that needs implementation (e.g. 'yes, do the revert-on-"
    "red check', 'add a copy-invite button to team settings', 'fix the login 500'), then IN ADDITION "
    "to your short reply, append ONE ticket block (the filing format below) so it becomes real work "
    "the unit will build — and tell him in your reply that you have opened it. Hold a HIGH bar: a "
    "question, a greeting, a vague direction, an FYI, or anything already tracked → NO ticket block. "
    "One ticket per genuinely-actionable ask; never invent work to look busy. Tickets you already "
    "opened appear in your standing guidance / thread as '[opened] <KEY>' and open items are in the "
    "board/Needs-you state above — if this ask is already one of them, do NOT open a duplicate; just "
    "say it's already tracked."
)


def _app_for_reply(cfg: Config, msg_refs: list[str]):
    """Which product a Commander-reply ticket files to: the app whose Jira project key matches a
    referenced ticket ('EU-136' → the EU product), else the primary app (apps[0]); None if no apps."""
    apps = getattr(cfg, "apps", None) or []
    if not apps:
        return None
    if msg_refs:
        prefix = str(msg_refs[0]).split("-")[0].upper()
        for a in apps:
            pk = str((getattr(a, "backlog", None) or {}).get("project_key", "") or "").upper()
            if pk and pk == prefix:
                return a
    return apps[0]


def _file_commander_ticket(cfg: Config, msg_refs: list[str], answer: str):
    """If the CTO's reply carried a ticket block, file it (DEDUPED) to the right product. Returns the
    FilingResult, or None when there's nothing to file (or filing failed). Roman 2026-07-07: a reply
    that implies work opens a ticket, so his answer becomes work — not just a standing-guidance note.

    Crash-safe: filing.file_findings builds the backlog via make_backlog() BEFORE its per-finding
    guard, and make_backlog raises on an absent/unknown backend (no Jira creds). That must never sink
    the Commander's reply, so a construction failure degrades to None here."""
    from . import filing
    proposals, _ = filing.parse_tickets(answer)
    if not proposals:
        return None
    app = _app_for_reply(cfg, msg_refs)
    if app is None:
        return None
    try:
        return filing.file_findings(app, "commander", answer)
    except Exception:  # noqa: BLE001 — a backlog/creds/backend failure must not crash the reply
        return None


async def respond_to_commander(cfg: Config, message: str) -> str:
    """The CTO answers a message from the Commander (a reply to a council question, or
    any question) directly in Telegram, grounded on the latest council + record, logs the exchange
    as standing guidance, and — when the reply implies concrete work — opens a deduped ticket."""
    # Show the Commander's message in the cockpit chat right away — UNLESS the cockpit's /api/chat
    # handler already echoed it synchronously (EU-307, so the view sticks to the just-sent message
    # without waiting on this bg reply). Skip the append when the transcript already ends with this
    # exact Q so a freeform message isn't duplicated; Telegram / other callers still append normally.
    if _last_chat_line(cfg) != ("Q: " + " ".join((message or "").split()).strip()):
        append_chat(cfg, "Q", message)
    latest = history(cfg, limit=1)
    context = transcript_text(cfg, latest[0]["file"]) if latest else format_signals(collect_signals(cfg))
    notes = recent_commander_notes(cfg)
    system = (
        "You are THE CTO of an elite autonomous software unit, talking 1:1 with the Commander "
        "(Roman) — like a sharp, trusted colleague, NOT writing a report. BREVITY CONTRACT: default "
        "to 1–3 short, conversational, Telegram-style sentences, plain language, no headers, no "
        "bullet lists, no status dumps, no restating his message. NO preamble filler — never open "
        "with 'Great question!', 'Let me...', 'Sure, I can help with that' or similar throat-clearing; "
        "just answer. Expand into a longer or structured answer ONLY when the Commander explicitly "
        "asks for detail (e.g. 'give me the full picture', 'explain in depth') or the message is a "
        "genuine decision that truly needs structure to be understood — otherwise stay short. If "
        "he's just greeting you or making conversation, chat back like a human and let him steer. "
        "Raise AT MOST ONE thing — and only when it genuinely needs him: a real "
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
        f"writes the code on an isolated git worktree, the gate + Reviewer + {display('provost')} check it, and it lands "
        "on DEV for the Commander's QA. The Commander NEVER hand-implements a ticket, never pastes a prompt "
        "into another tool, never opens 'Claude Code', and the unit never needs to SSH anywhere — building "
        "IS the unit's job. To get a ticket worked, the right answer is one of: autopilot drains it "
        "automatically; the Commander runs it from the cockpit (Choose a ticket); he answers it in the "
        "Needs-you box; or he sends '/unblock <id>' or '/run <app> <what>' here. If he says 'run AUTO-9', "
        "that means the UNIT runs it — reassure him it's queued / tell him to /unblock it, do NOT tell him "
        "to run it elsewhere. NEVER invent a file path: the unit's products and their real repo paths are "
        "listed below — use those exact paths or none. "
        "NEVER invent a count: the real live Needs-you state is always provided below — "
        "if you quote any number (decisions awaiting, tickets blocked, tasks pending) it MUST "
        "come from that provided state; if it is not there, say you don't know rather than guess.")
    ticket_ctx = await _ticket_context(cfg, message)
    board_ctx = await _board_status(cfg) if _BOARD_Q.search(message or "") else ""
    needs_ctx = await _needs_context(cfg)
    # Thread context: last N chat turns + pending proposals matching any ticket key in the
    # message, so 'create it' / 'approve' / 'yes go ahead' can resolve without re-asking.
    _msg_refs = _TICKET_KEY.findall(message or "")
    thread_ctx = recent_thread_context(cfg, ticket_ref=_msg_refs[0] if _msg_refs else None)
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
        # Always inject the real Needs-you state so the CTO can never invent a count.
        *([f"LIVE Needs-you state (ground every count you quote in this — never guess):\n{needs_ctx}\n"]
          if needs_ctx else []),
        *([f"Background you may lean on if relevant — do NOT recite or summarize it:\n{context[:1200]}\n"]
          if context else []),
        *([f"Ticket(s) the Commander referenced — live from the unit's own backlog:\n{ticket_ctx}\n"]
          if ticket_ctx else []),
        *([f"Thread context (recent turns + pending proposals for this ticket — "
           f"use this to resolve 'it' / 'approve' / 'create it' without re-asking):\n{thread_ctx}\n"]
          if thread_ctx else []),
        *([f"What you've already discussed with him:\n{notes}\n"] if notes else []),
        f"The Commander says: {message}", "",
        "Reply like a colleague — short and natural.",
    ])
    from . import filing
    run = await run_agent(prompt, ClaudeAgentOptions(
        model=cfg.discussion_model,
        system_prompt=memory.preamble() + system + _COMMANDER_TICKET_RULE + filing.TICKET_BLOCK_RULE,
        cwd=_general_root(),
        permission_mode="bypassPermissions", allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash", "Task", "Agent"], setting_sources=["project"],
        # Room to glance at a few files before replying — 6 was too tight and errored out when the
        # Commander's message invited a quick look ("investigate…"), so the CTO couldn't answer.
        max_turns=14, effort="low"), tag="the-general")
    answer = (run.final or run.text or "(the CTO had no answer)").strip()
    # Roman 2026-07-07: a reply that implies work opens a DEDUPED ticket. Split the ticket block out
    # so the phone reply stays clean, file it to the right product, and tell him what opened.
    _proposals, clean_answer = filing.parse_tickets(answer)
    # A reply may be ONLY a ticket block (no prose) — never send a bare "🎖️ " or log an empty answer.
    reply_text = clean_answer or ("Opened a ticket for that." if _proposals else answer)
    notify.send(f"🎖️ {await notify.report_brief(cfg, reply_text)}")
    res = _file_commander_ticket(cfg, _msg_refs, answer)   # crash-safe: returns None on any failure
    if res is not None and res.lines:
        notify.send("🎫 " + "\n".join(res.lines))
        if res.filed:   # keep filed keys in the thread so a later paraphrased nudge sees it's tracked
            add_commander_note(cfg, f"[opened] {', '.join(res.filed)}")
    elif _proposals:     # the CTO wanted to file, but the backlog was unavailable — don't fail silently
        notify.send("⚠️ couldn't open the ticket right now — your message is logged.")
    # Log compactly — a colleague chat, not a briefing to be replayed verbatim into future prompts.
    add_commander_note(cfg, f"Q: {message[:120]} → A: {reply_text[:200]}")
    append_chat(cfg, "A", reply_text)    # full reply to the cockpit chat (not only Telegram)
    return reply_text


# --------------------------------------------------------------------------- #
# Group chat — the Commander consults the unit. A cheap triage step (EU-287) routes to the 1–2
# officers whose lane actually owns the message; everyone else stays silent — a real Telegram
# group, not a roundtable where every officer writes a paragraph. The CTO is NOT in this room —
# the Commander talks to the CTO 1:1 in /chat.
# --------------------------------------------------------------------------- #
_GROUP_SYSTEM = (
    "You are an officer of an ELITE autonomous software unit in a GROUP CHAT with the Commander "
    "(Roman) — he is consulting the unit / brainstorming or just talking, not filing a ticket. Speak "
    "in character, plainly, no markdown, conversationally — like a real chat, not a memo: AT MOST 2 "
    "SHORT SENTENCES, always. If the message is genuinely your lane, answer directly and concretely. "
    "If you were addressed 1:1 (a message just to you), answer briefly even if it's a greeting or "
    "small talk — never leave a direct message unanswered. Otherwise, if it is not your lane, reply "
    "with exactly 'PASS' — do NOT chime in with a partial comment just because it touches your lane a "
    "little; the officer whose lane it actually is will answer, and bystanders stay quiet. Build on a "
    "fellow officer's reply only if you're genuinely answering too — agree and extend, or disagree "
    "with a reason, briefly; never repeat them. Ground claims in the unit's record and the files you "
    "can read; no invention. Do not write or edit files."
)

_TRIAGE_SYSTEM = (
    "You are a fast triage classifier for an elite software unit's group chat. Given a message from "
    "the Commander and the roster of officers with their lanes, pick AT MOST 2 officers whose lane "
    "genuinely owns this message — the ones who should actually answer; most messages need only ONE. "
    "If it's a greeting, small talk, or no lane clearly owns it, answer exactly 'NONE'. Output ONLY "
    "the officer rank name(s) exactly as given, comma-separated if two, nothing else — no "
    "explanation, no punctuation beyond the comma."
)

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _brief(text: str, max_sentences: int = 2) -> str:
    """Length guard (EU-287): trims a reply to at most `max_sentences` sentences so the consult
    reads like a chat, not a memo — even if a stubbed/verbose officer returns a multi-paragraph reply."""
    text = " ".join((text or "").split()).strip()
    if not text:
        return text
    return " ".join(_SENT_SPLIT.split(text)[:max_sentences]).strip()


def _triage_prompt(message: str, tail: str) -> str:
    roster = "\n".join(f"- {rank} ({lens_role})" for rank, lens_role, _ in COUNCIL)
    parts = ["Officers and their lanes:", roster, ""]
    if tail:
        parts += ["Recent group chat (context):", tail, ""]
    parts += [f'The Commander says to the group: "{message}"', "",
              "Which officer(s) (at most 2) should answer? Reply with their rank name(s) only, or 'NONE'."]
    return "\n".join(parts)


async def _triage_officers(cfg: Config, message: str, tail: str) -> list[str]:
    """Cheap Haiku triage (EU-287): picks at most 2 officer ranks whose lane owns `message`. Empty
    list means no lane owner (greeting/small talk) — group_chat then falls back to the host officer."""
    try:
        run = await run_agent(
            _triage_prompt(message, tail),
            ClaudeAgentOptions(model=cfg.smalltalk_model, system_prompt=_TRIAGE_SYSTEM,
                               cwd=_general_root(), permission_mode="bypassPermissions",
                               allowed_tools=[], disallowed_tools=["Write", "Edit", "NotebookEdit",
                                                                    "Bash", "Task", "Agent"],
                               setting_sources=["project"], max_turns=1, effort="low"),
            tag="group-triage")
    except Exception:  # noqa: BLE001 — triage must never crash the room; treat as no lane owner
        return []
    text = (run.final or run.text or "").strip()
    if not text or text.lower().rstrip(".!").strip() == "none":
        return []
    return [p.strip() for p in text.replace("\n", ",").split(",") if p.strip()][:2]


def _match_officers(picks: list[str]) -> list[tuple[str, str, str]]:
    """Match triage picks to COUNCIL officers by key/name, in pick order, capped at 2. Unlike
    `_select_officers` (used for the explicit /group?officer= 1:1 mode, where 'no match' should mean
    'show me everyone'), a triage miss must NOT fall back to the whole roster — that would silently
    reopen the noisy whole-council path this ticket closes."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for p in picks:
        pl = str(p).lower().strip()
        if not pl:
            continue
        for o in COUNCIL:
            key = _officer_key(o[0])
            if key in seen:
                continue
            if pl in o[0].lower() or pl in key or o[0].lower() in pl:
                out.append(o)
                seen.add(key)
                break
        if len(out) >= 2:
            break
    return out


def _group_options(cfg: Config, voice: str, cwd: str) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=cfg.discussion_model,           # Sonnet — a group brainstorm is many cheap turns
        system_prompt=memory.preamble() + f"{_GROUP_SYSTEM}\n\nYour lens — {voice}"
        + ((f"\n\nToday's daily FOCUS (ground your takes in it): {latest_focus(cfg)}")
           if latest_focus(cfg) else ""),
        cwd=cwd, permission_mode="bypassPermissions",
        allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash", "Task", "Agent"],
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
    """The Commander consults the unit — like a real Telegram group, not a roundtable (EU-287).

    When `officers` is given (the explicit /group?officer= 1:1 mode), that officer alone answers —
    unchanged, no triage. Otherwise a cheap Haiku triage picks the 1–2 officers whose lane actually
    owns `message`; everyone else stays silent (no whole-council poll, no 'partly relevant' bystander
    comments). Each reply is length-guarded to <=2 sentences. If triage finds no lane owner (a
    greeting / small talk), the host officer answers in exactly one short sentence so the room is
    never empty. Persists the exchange to group_chat.jsonl and returns the officers' replies as
    [(rank, text)]. `echo=False` when the caller already recorded the Commander's message (so the
    web UI can echo instantly)."""
    digest = format_signals(collect_signals(cfg))
    notes = recent_commander_notes(cfg)
    tail = _group_tail(cfg, 12)
    cwd = _general_root()
    if echo:
        _append_group(cfg, "you", message)        # the Commander's message leads the thread
    if officers:
        roster = _select_officers(officers)        # explicit 1:1 — that officer always answers
    else:
        picks = await _triage_officers(cfg, message, tail)
        roster = _match_officers(picks)             # capped at 2; [] means no lane owner
    replies: list[tuple[str, str]] = []
    for rank, lens_role, voice in roster:
        prompt = "\n".join([
            f"You are the {rank} ({lens_role}). The unit's recent record:", "", digest, "",
            *([f"Standing guidance from the Commander:\n{notes}\n"] if notes else []),
            *([f"Recent group chat:\n{tail}\n"] if tail else []),
            *(["Fellow officers have already replied to this message:\n"
               + "\n".join(f"— {w}: {t}" for w, t in replies) + "\n"] if replies else []),
            f'The Commander says to the group: "{message}"', "",
            "Reply per your rules: at most 2 short sentences, answer only if it's genuinely your "
            "lane (or you were addressed 1:1), else 'PASS'.",
        ])
        run = await run_agent(prompt, _group_options(cfg, voice, cwd),
                              tag="group-" + _officer_key(rank))
        s = (run.final or run.text or "").strip()
        if not s or s.lower().rstrip(".!").strip() in _SKIP:
            continue
        s = _brief(s, max_sentences=2)
        replies.append((rank, s))
        _append_group(cfg, rank, s)
        print(f"  · {rank} weighed in", flush=True)
    if not replies:
        # Nobody claimed it (a greeting / small talk / off-lane aside) — the room must never look
        # dead. The host officer answers warmly, in one short sentence, so the Commander always gets
        # a reply.
        rank, lens_role, voice = roster[0] if roster else COUNCIL[0]
        prompt = "\n".join([
            f"You are the {rank} ({lens_role}). The unit's recent record:", "", digest, "",
            *([f"Recent group chat:\n{tail}\n"] if tail else []),
            f'The Commander says to the group: "{message}"', "",
            "No formal lane owns this. Reply warmly in character, in exactly ONE short sentence. Do "
            "NOT say PASS.",
        ])
        run = await run_agent(prompt, _group_options(cfg, voice, cwd), tag="group-host")
        s = (run.final or run.text or "").strip()
        if s and s.lower().rstrip(".!").strip() not in _SKIP:
            s = _brief(s, max_sentences=1)
            replies.append((rank, s))
            _append_group(cfg, rank, s)
    if audit is not None:
        audit.record("group_chat", officers=[r for r, _ in replies])
    return replies


# --------------------------------------------------------------------------- #
# _consult_specialist: single-officer grounded lookup (EU-600)
#
# Consults ONE officer by key, returns a brief answer. NOT yet wired into
# respond_to_commander (later sub-ticket). Wraps the entire flow in one
# try/except so any failure yields None — never raises.
# --------------------------------------------------------------------------- #
async def _consult_specialist(cfg: Config, message: str, officer_key: str) -> str | None:
    """Ask a single officer for a brief (1–2 sentence) answer scoped to their lane.

    Returns None on any error (timeout, malformed response, unknown key).
    Does NOT modify or depend on ``respond_to_commander``."""
    try:
        digest = format_signals(collect_signals(cfg))
        notes = recent_commander_notes(cfg)
        cwd = _general_root()
        # Strict key lookup — iterate COUNCIL; no fallback-to-all like _select_officers.
        officer = None
        for rank, lens_role, voice in COUNCIL:
            if _officer_key(rank) == officer_key:
                officer = (rank, lens_role, voice)
                break
        if officer is None:
            return None
        rank, lens_role, voice = officer
        prompt = "\n".join([
            f"You are the {rank} ({lens_role}). The unit's recent record:", "", digest, "",
            *([f"Standing guidance from the Commander:\n{notes}\n"] if notes else []),
            f'The Commander asks: "{message}"', "",
            "Answer in at most 2 short sentences, strictly from your lane. If this is not your "
            "lane, reply with exactly 'PASS'.",
        ])
        run = await run_agent(prompt, _group_options(cfg, voice, cwd), tag="consult-" + officer_key)
        s = (run.final or run.text or "").strip()
        if not s or s.lower().rstrip(".!").strip() in _SKIP:
            return None
        return _brief(s, max_sentences=2)
    except Exception:  # noqa: BLE001 — best-effort, never raise
        return None


# --------------------------------------------------------------------------- #
# Daily stand-up — each officer reports Yesterday / Today / Blockers from the
# real record and flags hand-offs ('need <Officer>'). Assembled deterministically
# (no extra chair call) and saved for the cockpit.
# --------------------------------------------------------------------------- #
_STANDUP_SYSTEM = (
    "You are an officer of an ELITE autonomous software unit at the daily STAND-UP, reporting to "
    "THE CTO. Report ONLY from your lens, grounded in the unit's record — no invention. Output "
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
            disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash", "Task", "Agent"], setting_sources=["project"],
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
    # Hand-offs/blockers are already concise bullets and this helper is sync, so bulletize() (the
    # deterministic brief) keeps every one as a tight '•' line — no model call, no truncate-and-punt.
    return (f"🫡 *Daily stand-up* — {len(rows)} engineer(s) reported.\n\n"
            f"*Hand-offs & blockers:*\n{notify.bulletize(hb, max_bullets=20)}\n\n_Full round-table in the cockpit._")


# EU-430 (2026-07-22 VPS outage): when the model credential is dead, the SDK returns the provider's
# raw error string (e.g. "Failed to authenticate. API Error: 401 …") AS the run result. The
# ceremonies must treat that as an OUTAGE — alert the operator, NEVER broadcast the raw error as the
# brief, and NEVER persist it into ROSTER.md / last-standup.md / the transcript. For 3 ceremonies
# (07-20 daily, 07-20 council, 07-21 daily) that string went to the Commander's phone verbatim.
_AUTH_DOWN_ALERT = (
    "⚠️ SQUAD: the VPS cannot authenticate to the model — briefs are degraded until re-auth"
)


def _auth_preflight(cfg: Config) -> None:
    """EU-430 AC4: ceremony pre-flight. Warn to Telegram when the Claude credential expires within
    48h OR the refresh token is empty, so a dead login is caught BEFORE the first 401 — not after
    the fourth failed brief. Silent on a healthy / unknown credential (no spam, no false red)."""
    try:
        st = auth_probe.credential_status()
    except Exception:  # noqa: BLE001 — a pre-flight must never break a ceremony
        return
    if st.get("level") != "warn":
        return
    try:
        notify.send(f"⚠️ SQUAD auth pre-flight: {st.get('reason', 'credential expiring')} — "
                    "re-auth on the box before briefs degrade.")
    except Exception:  # noqa: BLE001
        pass


def _ceremony_auth_down(cfg: Config, audit, *, ceremony: str, broadcast: bool,
                        detail: str = "") -> str:
    """EU-430 AC2/AC3: a ceremony's model call failed auth. Do NOT broadcast the provider's raw
    error as the brief — send the operator-shaped alert, skip the poisoned artefacts (the caller
    bails before writing them), record the outage, and invalidate the auth-probe cache so health's
    'Claude auth' check degrades on the next pass instead of rendering the error as content.
    Returns the alert text (what the ceremony hands back to its caller)."""
    try:
        auth_probe.invalidate()           # stale cached 'valid' must not mask the outage in health
    except Exception:  # noqa: BLE001
        pass
    if broadcast:
        try:
            notify.send(_AUTH_DOWN_ALERT)
        except Exception:  # noqa: BLE001
            pass
    if audit is not None:
        try:
            audit.record(ceremony, auth_down=True, broadcast=bool(broadcast), detail=detail[:200])
        except Exception:  # noqa: BLE001
            pass
    return _AUTH_DOWN_ALERT


async def daily_brief(cfg: Config, audit=None, *, broadcast: bool | None = None) -> str:
    """The LIGHT daily stand-up (best-practice: fast daily, deep weekly).

    A deterministic digest — what shipped, what needs the Commander, what awaits a decision
    (dashboard.standup, NO model call) — plus ONE short CTO synthesis: today's focus and, at a high
    bar, the single decision that is genuinely the Commander's. That is ~1 model call, versus the ~8
    of the deep multi-officer council (hold_council), which is now a WEEKLY ceremony. Sends one
    skimmable phone ping; surfaces any Commander decision as a separate 'needs your call'."""
    _auth_preflight(cfg)                               # EU-430 AC4: warn before the first 401
    from . import dashboard
    # EU-303: single-sender election. The daily/council SEND had no host gate — only which machine
    # holds the cron prevented duplicates, so a second cron/timer/host (or install-server-cron run
    # twice) each broadcast its own copy. Elect ONE sender (the same host that owns the Telegram
    # poller, EU-185), unless an interactive caller (cockpit/Telegram /daily) forces broadcast=True.
    # Elected early so an auth-outage alert (EU-430) respects the same single-sender rule.
    if broadcast is None:
        from . import decisions as _dec
        broadcast = _dec.should_poll_telegram(cfg)[0]
    facts = dashboard.standup(cfg)                     # deterministic — yesterday shipped, needs you, awaiting
    notes = recent_commander_notes(cfg)
    cwd = _general_root()
    # The daily is intentionally NOT fed the cumulative signals digest (format_signals) — that carries
    # all-time totals ('X/Y landed', avg passes, max-effort hits) the Commander does not want in the
    # daily. The deterministic facts (yesterday's result + what needs him) are context enough for the
    # CTO to name TODAY's focus. The deep WEEKLY council still gets the full record.
    prompt = "\n".join([
        "Today's deterministic stand-up (already going to the Commander — do NOT repeat these lines):",
        "", facts, "",
        *([f"Commander's standing guidance:\n{notes}\n"] if notes else []),
        "Now write the daily brief: TODAY's single focus, and any decision that is genuinely his.",
    ])
    run = await run_agent(prompt, ClaudeAgentOptions(
        model=cfg.discussion_model,
        system_prompt=memory.preamble() + _DAILY_SYSTEM, cwd=cwd,
        permission_mode="bypassPermissions", allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash", "Task", "Agent"], setting_sources=["project"],
        max_turns=3, effort="low"), tag="the-general")
    raw = (run.final or run.text or "").strip()
    # EU-430 AC2/AC3: a provider auth error is an OUTAGE — alert the operator, never broadcast the
    # raw error as the brief, never persist it as the transcript. Degrade health (probe invalidated).
    if auth_probe.looks_like_provider_error(raw):
        return _ceremony_auth_down(cfg, audit, ceremony="daily_brief", broadcast=bool(broadcast),
                                   detail=raw)
    synth = _honest_commander_section(cfg, raw)
    questions = _commander_questions(synth)

    # broadcast was elected at the top of this function (EU-303 single-sender, EU-430 early).
    # One skimmable phone ping: the deterministic facts + the CTO's focus/decision.
    if broadcast:
        notify.send(f"{facts}\n\n{synth}")
        if questions:
            notify.send("❓ *The unit needs your call:*\n" + "\n".join(f"• {q}" for q in questions)
                        + "\n\nReply here — I'll act on it, and open a ticket if it's work.")
    try:
        _save_transcript(cfg, "daily", facts, [("CTO", synth)], synth)
    except OSError:
        pass
    if audit is not None:
        audit.record("daily_brief", questions=len(questions), broadcast=bool(broadcast),
                     provider=run.provider, model=run.model_version)
    return synth


async def hold_standup(cfg: Config, audit=None) -> str:
    """On-demand stand-up (the daily one now runs inside the muster). Saves last-standup.md + notifies."""
    _auth_preflight(cfg)                               # EU-430 AC4: warn before the first 401
    digest, rows, handoffs = await _gather_standup(cfg)
    # EU-430 AC5: never write a stand-up file whose officer sections are provider-error strings.
    _down = next((rep for _, rep in rows if auth_probe.looks_like_provider_error(rep)), None)
    if _down is not None:
        return _ceremony_auth_down(cfg, audit, ceremony="standup", broadcast=True, detail=_down)
    text = _standup_text(rows, handoffs)
    _standup_file(cfg).write_text(text, encoding="utf-8")
    _save_transcript(cfg, "stand-up", digest, rows, "Daily stand-up — see the round-table below.")
    notify.send(_standup_telegram(rows, handoffs))
    if audit is not None:
        audit.record("standup", officers=[r for r, _ in rows], handoffs=len(handoffs))
    return text


def _save_transcript(cfg: Config, topic, digest, said, briefing) -> Path:
    d = _council_dir(cfg)
    stamp = time.strftime("%Y%m%d-%H%M")
    f = d / f"council-{stamp}.md"
    title = topic or "Daily council"
    body = [f"# {title} — {time.strftime('%Y-%m-%d %H:%M')}", "",
            "## The CTO's briefing", "", briefing, "",
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
    """Pull the questions under the 'FOR YOU' heading."""
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
