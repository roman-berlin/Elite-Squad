"""Unit Memory — the Elite Unit's living protocol (company memory).

A single curated document, `memory/UNIT.md`, that every officer reads before acting
and the **Technical Writer** keeps current after each council. Git-versioned, so it doubles as
an audit trail of how the unit's doctrine evolved.

Design:
- **Human-owned sections** (Mission, Standing Orders, Per-App Notes) are edited by the
  Commander and NEVER touched by the Technical Writer.
- **Technical Writer-owned section** lives between `<!-- SCRIBE:BEGIN -->` / `<!-- SCRIBE:END -->`
  markers — the only region the Technical Writer rewrites. This protects your hand edits by
  construction.
- `preamble()` returns a compact block prepended to every officer's system prompt, so the
  whole unit shares one memory. Keep UNIT.md tight — it rides along on every call.

The Technical Writer agent is READ-ONLY: it returns proposed log bullets from the council transcript
+ recent audit, and *Python* performs the marker-bounded write (deterministic, safe).
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import locking

_ROOT = Path(__file__).resolve().parent.parent      # the CTO repo root
UNIT_PATH = _ROOT / "memory" / "UNIT.md"             # versioned DOCTRINE (Commander-owned; ships via git)
LIVE_PATH = _ROOT / "memory" / "UNIT.live.md"        # runtime LIVING LOG (officer-owned; gitignored)
_BACKUPS = _ROOT / "memory" / "backups"

_BEGIN = "<!-- SCRIBE:BEGIN -->"
_END = "<!-- SCRIBE:END -->"
_LOG_HEADING = "## Lessons & Decisions  _(Technical Writer-maintained — newest first)_"

# How many of the newest/most-recurring lessons to inline into EVERY officer prompt. The log is
# newest-first and Consolidate folds the most-recurring rejection lessons to the top, so the cap keeps
# the highest-signal ones. The full log still lives in the file + on the cockpit /memory page, so this
# bounds per-call tokens (every officer pays for the preamble) without a relevance router.
PREAMBLE_LESSONS = 12

_SEED = f"""# Elite Unit — Living Protocol (Unit Memory)

The unit's shared memory. Every officer reads this before acting. The Commander owns the
Mission, Standing Orders, and Per-App Notes; the Technical Writer maintains only the Lessons &
Decisions log (between the markers). Keep it tight and current.

## Mission

Implement the Commander's Jira tickets across his products autonomously and safely:
build → gate → review → security → land on DEV → QA, on isolated worktrees, never touching
MAIN. Quality and tenant-safety over speed.

## Standing Orders (Commander)

- Only work tickets assigned to ROMAN BERLIN.
- Never touch MAIN; land on DEV, move the ticket to QA.
- Obey each repo's CLAUDE.md and .claude/rules (Bun-only, tenant isolation, zero-trust).
- Escalate to the Commander only for critical product/strategy decisions — solve problems yourself.

## Per-App Notes

### automatixy
- Stack: React/Vite + FastAPI + Supabase. Branches: DEV / MAIN (uppercase). Jira project AUTO.
- (Add conventions, gotchas, and hot spots here as the unit learns them.)

{_BEGIN}
{_LOG_HEADING}

- {{today}}: Unit Memory established. Officers now read this protocol before acting.
{_END}
"""


# --------------------------------------------------------------------------- #
def ensure() -> Path:
    """Create a starter UNIT.md if none exists, and migrate any legacy inline Technical Writer log out to the
    runtime live-log file (one-time). Returns the doctrine path."""
    if not UNIT_PATH.exists():
        UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        UNIT_PATH.write_text(_SEED.replace("{today}", datetime.now().strftime("%Y-%m-%d")),
                             encoding="utf-8")
    if not LIVE_PATH.exists():
        _, inline, _ = _split(load())
        if inline.strip():
            LIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
            LIVE_PATH.write_text(inline.strip() + "\n", encoding="utf-8")
    return UNIT_PATH


def load() -> str:
    """Full UNIT.md text, or '' if it doesn't exist yet."""
    try:
        return UNIT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _doctrine() -> str:
    """The Commander-owned doctrine: UNIT.md with any LEGACY inline Technical Writer section stripped (the log
    now lives in its own runtime file, so it's never shown twice)."""
    text = load()
    if _BEGIN in text and _END in text:
        head, _, tail = _split(text)
        text = (head.rstrip() + "\n" + tail.lstrip()).strip()
    return text


def _live_log(limit: int | None = None) -> str:
    """The officer-maintained living log (runtime file, gitignored). Falls back to a legacy inline
    Technical Writer section in UNIT.md for an un-migrated repo. Includes the log heading. When `limit` is set,
    only the newest `limit` lessons are returned (the rest collapse to a one-line pointer) — this is
    what `preamble()` inlines into every officer prompt, so it stays bounded as the log grows. With
    `limit=None` (the cockpit page) the FULL log is returned."""
    live = ""
    try:
        live = LIVE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        live = ""
    if not live:
        _, inline, _ = _split(load())
        live = inline.strip()
    if not live or limit is None:
        return live
    return _cap_lessons(live, limit)


def _cap_lessons(live: str, limit: int) -> str:
    """Keep the heading + the newest `limit` bullets; the older ones collapse to a single pointer line
    so an officer prompt never carries the whole log."""
    head: list[str] = []
    kept: list[str] = []
    extra = 0
    for ln in live.splitlines():
        if ln.strip().startswith("- "):
            if len(kept) < limit:
                kept.append(ln)
            else:
                extra += 1
        elif not kept:                 # non-bullet lines before the first bullet = the heading
            head.append(ln)
    out = head + kept
    if extra:
        out.append(f"- …(+{extra} older lessons — full log on the cockpit /memory page)")
    return "\n".join(out).strip()


def preamble() -> str:
    """Compact block prepended to every officer's system prompt: the code-derived STATE OF DEV
    brief, then the versioned doctrine, then the live officer-maintained log. '' when there's no
    memory at all.

    EU-378 ordering is load-bearing twice over: (1) precedence — the brief is derived from code
    and cannot drift, while the scribe-written log can (and did: 5 of 12 lessons described
    officers that don't exist), so the brief must outrank it; (2) survival — builder._trim_preamble
    truncates the TAIL on oversize, so a brief appended at the end would be exactly what gets cut."""
    from . import devstate
    ds = devstate.brief()
    doctrine, live = _doctrine(), _live_log(limit=PREAMBLE_LESSONS)
    if not ds and not doctrine and not live:
        return ""
    parts = []
    if ds:
        parts.append("=== STATE OF DEV (code-derived; where a lesson below disagrees with this, "
                     "THIS wins) ===\n" + ds + "\n=== END STATE OF DEV ===")
    if doctrine or live:
        body = (doctrine + ("\n\n" + live if live else "")).strip()
        parts.append("=== UNIT MEMORY — the unit's living protocol. Read it before you act; obey "
                     "the Standing Orders. ===\n" + body + "\n=== END UNIT MEMORY ===")
    return "\n\n".join(parts) + "\n\n"


# --------------------------------------------------------------------------- #
def _split(text: str) -> tuple[str, str, str]:
    """Return (head, scribe_body, tail) around the SCRIBE markers."""
    if _BEGIN in text and _END in text:
        head, rest = text.split(_BEGIN, 1)
        body, tail = rest.split(_END, 1)
        return head, body, tail
    return text, "", ""


# EU-364: how many snapshots to keep. The dir grew a file per update and was never pruned (47 files /
# 264K at the 2026-07-16 audit). Note _backup/update_log stamp per-SECOND, so several updates inside one
# second collapse onto one filename — the growth is slow, which is exactly why nobody noticed it.
_MAX_BACKUPS = 20

# --------------------------------------------------------------------------- #
# EU-364 — Unit Memory sanitation.
#
# UNIT.live.md is prepended to EVERY officer's system prompt by preamble(), which makes it the unit's
# highest-leverage internal surface: one line written here becomes standing instructions for every
# future officer, permanently. The Technical Writer's raw LLM output used to land here verbatim (only
# marker-stripping), and untrusted text reaches that model — it summarises the council transcript and
# the audit digest, either of which can carry a hostile Jira ticket description.
#
# So the log is an ALLOWLIST, not a denylist: an entry survives only if it looks like the log's own
# documented format (`- YYYY-MM-DD: <lesson>`, per SCRIBE_SYSTEM), and is dropped if it is
# instruction-shaped. Deliberately conservative — dropping a legitimate lesson costs one council's
# worth of hygiene, admitting one injected line is permanent and invisible.
_BULLET_RE = re.compile(r"^- \d{4}-\d{2}-\d{2}: \S")
_MAX_BULLET_LEN = 300
_MAX_BULLETS = 24              # matches consolidate.run's max_bullets, so the two agree on the cap

_INSTRUCTION_SHAPED = re.compile(
    r"ignore\s+(all\s+|any\s+)?(previous|prior|earlier|the\s+above)"
    r"|disregard\s+(all\s+|any\s+|the\s+)?[\w\s]{0,24}(instruction|order|rule|guidance|above)"
    r"|system\s+prompt"
    r"|you\s+(are\s+now|must|should\s+now|will\s+now)"
    r"|new\s+(standing\s+)?orders?\b"
    r"|override\s+(the\s+)?(guard|denylist|standing)"
    # markup smuggling INSIDE an otherwise well-formed bullet — a heading/fence/comment that reopens
    # structure in the prompt. (A line that merely STARTS with one of these is already dropped by
    # _BULLET_RE, so only the mid-bullet case needs a rule.)
    r"|<!--|-->|```",
    re.IGNORECASE,
)


def _sanitize(bullets: str) -> str:
    """Reduce proposed log text to the entries that are safe to inject into every officer prompt.

    Applied inside update_log() rather than in scribe(), so consolidate.py's writer goes through the
    same chokepoint. Returns '' when nothing survives — update_log treats that as "refuse the update"
    rather than wiping the unit's memory."""
    kept: list[str] = []
    for raw in bullets.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if not _BULLET_RE.match(ln):
            continue                       # not the documented `- YYYY-MM-DD: …` entry format
        if _INSTRUCTION_SHAPED.search(ln):
            continue                       # imperative/markup-shaped — never a durable "lesson"
        if len(ln) > _MAX_BULLET_LEN:
            ln = ln[:_MAX_BULLET_LEN].rstrip() + " …"   # truncate: a long lesson is still a lesson
        kept.append(ln)
        if len(kept) >= _MAX_BULLETS:
            break                          # newest-first, so the cap keeps the highest-signal entries
    return "\n".join(kept)


def _prune_backups(pattern: str, keep: int = _MAX_BACKUPS) -> None:
    """Keep only the newest ``keep`` snapshots matching ``pattern`` (EU-364). The stamp format sorts
    lexicographically = chronologically, so a plain sort finds the oldest. Best-effort: a snapshot we
    cannot prune must never break a council."""
    try:
        snaps = sorted(_BACKUPS.glob(pattern))
        for old in snaps[:-keep] if keep else snaps:
            old.unlink(missing_ok=True)
    except OSError:  # noqa: BLE001 - housekeeping never breaks a run
        pass


def _backup() -> None:
    if UNIT_PATH.exists():
        _BACKUPS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(UNIT_PATH, _BACKUPS / f"UNIT-{stamp}.md")
        _prune_backups("UNIT-*.md")


def update_log(bullets: str) -> None:
    """Write the officer-maintained living log to its OWN runtime file (gitignored), so it persists on
    the server across self-update (`git reset --hard` never touches it) and never collides with the
    Commander's versioned doctrine. Backs up the prior log first.

    EU-364: ``bullets`` is UNTRUSTED — it is the Technical Writer's raw LLM output, summarised from a
    council transcript that can carry a hostile ticket description — so it is sanitised here, at the one
    chokepoint both writers (scribe + consolidate.run) share. The write itself goes through
    ``locking.locked_text_rmw``: the previous bare ``write_text`` truncated-then-wrote, and every
    officer reads this file via preamble() on every call, so a reader in that window saw a TORN log
    (measured pre-fix: 97 partial reads of 411 against 6 concurrent writers). locked_text_rmw writes a
    temp file and ``os.replace``s it under an flock, so a reader sees only the whole old or whole new
    file, and the scribe/consolidate writers serialise across processes."""
    safe = _sanitize(bullets)
    if not safe:
        # Everything was dropped (an all-injection or all-prose update). Keep the existing memory
        # rather than replacing it with an empty log — a wipe is not a safe failure mode here.
        return
    LIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LIVE_PATH.exists():
        _BACKUPS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(LIVE_PATH, _BACKUPS / f"UNIT.live-{stamp}.md")
        _prune_backups("UNIT.live-*.md")
    locking.locked_text_rmw(LIVE_PATH, lambda _current: f"{_LOG_HEADING}\n\n{safe}\n")


# --------------------------------------------------------------------------- #
SCRIBE_SYSTEM = """\
You are the Technical Writer of an elite autonomous software unit, keeper of its living protocol
(Unit Memory). After a council, you update ONLY the "Lessons & Decisions" log: a concise,
durable record of what the unit learned and decided — the things future officers must know.

Rules:
- Output ONLY the log bullets (no headings, no markers, no preamble). Markdown "- " bullets.
- Newest first. Each bullet: `YYYY-MM-DD: <durable lesson / decision / standing change>`.
- Keep it TIGHT: at most ~20 bullets total. MERGE duplicates, DROP transient noise (one-off
  errors, routine merges). Preserve still-relevant older entries you're given.
- Capture: recurring friction, conventions discovered, decisions + their reason, anything
  that should change how officers act next time. Not a changelog of every ticket.
- Be specific and short. No fluff. If nothing meaningful changed, return the existing log
  unchanged."""


async def scribe(cfg) -> str:
    """Run the Technical Writer: read the latest council + recent audit, rewrite the log section.
    Returns a short status line. Safe to call after every council (best-effort)."""
    from claude_agent_sdk import ClaudeAgentOptions

    from .agent import run_agent
    from .config import normalize_effort
    ensure()
    current_log = _live_log()

    # Context: latest council transcript + a compact recent-audit digest.
    council_txt = ""
    try:
        from . import council
        hist = council.history(cfg, limit=1)
        if hist:
            council_txt = council.transcript_text(cfg, hist[0]["file"]) or ""
    except Exception:  # noqa: BLE001
        pass

    audit_digest = ""
    try:
        from . import dashboard
        tasks = dashboard.load_tasks(cfg.audit_path)[:12]
        lines = []
        for t in tasks:
            lines.append(f"- {t.get('ticket_id')} [{t.get('app')}] "
                         f"{t.get('outcome') or 'running'} · {t.get('passes')} pass(es)"
                         + (f" · note: {t['note'][:80]}" if t.get("note") else ""))
        audit_digest = "\n".join(lines)
    except Exception:  # noqa: BLE001
        pass

    prompt = "\n".join([
        "Update the unit's Lessons & Decisions log.",
        "",
        "CURRENT LOG (preserve still-relevant entries; newest first):",
        current_log.strip() or "(empty)",
        "",
        "LATEST COUNCIL TRANSCRIPT:",
        council_txt.strip()[:6000] or "(no council transcript)",
        "",
        "RECENT RUNS (audit digest):",
        audit_digest or "(no recent runs)",
        "",
        "Return ONLY the updated bullet list (newest first, ~20 max).",
    ])

    options = ClaudeAgentOptions(
        model=cfg.discussion_model,        # summarization, not implementation — keep it off Opus
        system_prompt=SCRIBE_SYSTEM,
        cwd=str(_ROOT),
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "NotebookEdit", "Task", "Agent"],   # Python does the write
        setting_sources=[],
        max_turns=12,
        effort=normalize_effort(getattr(cfg, "reviewer_effort", "high")),
    )
    run = await run_agent(prompt, options, tag="scribe")
    bullets = (run.final or "").strip()
    if not bullets:
        return "Technical Writer: no update produced."
    # Guard: if the model wrapped output in markers/headings, strip them.
    for junk in (_BEGIN, _END, _LOG_HEADING, "## Lessons & Decisions"):
        bullets = bullets.replace(junk, "")
    update_log(bullets.strip())
    # Deterministic hygiene after the AI pass: dedup/prune the log + fold in any recurring
    # Reviewer-rejection lessons. Free (no model call); never breaks the scribe.
    note = ""
    try:
        from . import consolidate
        r = consolidate.run(cfg)
        bits = []
        if r.get("added"):
            # EU-399: `added` now also carries recurrence lessons (needs_human / exception / gate
            # signatures), not just reviewer-rejection themes — so the label says "lesson(s)".
            bits.append(f"+{len(r['added'])} lesson(s)")
        if r.get("removed_dupes"):
            bits.append(f"−{r['removed_dupes']} dup(s)")
        if r.get("pruned"):
            bits.append(f"−{r['pruned']} pruned")
        if bits:
            note = " · consolidated (" + ", ".join(bits) + ")"
    except Exception:  # noqa: BLE001
        note = ""
    return f"Technical Writer: Unit Memory updated ({UNIT_PATH}){note}."
