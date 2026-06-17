"""Unit Memory — the Elite Unit's living protocol (company memory).

A single curated document, `memory/UNIT.md`, that every officer reads before acting
and the **Scribe** keeps current after each council. Git-versioned, so it doubles as
an audit trail of how the unit's doctrine evolved.

Design:
- **Human-owned sections** (Mission, Standing Orders, Per-App Notes) are edited by the
  Commander and NEVER touched by the Scribe.
- **Scribe-owned section** lives between `<!-- SCRIBE:BEGIN -->` / `<!-- SCRIBE:END -->`
  markers — the only region the Scribe rewrites. This protects your hand edits by
  construction.
- `preamble()` returns a compact block prepended to every officer's system prompt, so the
  whole unit shares one memory. Keep UNIT.md tight — it rides along on every call.

The Scribe agent is READ-ONLY: it returns proposed log bullets from the council transcript
+ recent audit, and *Python* performs the marker-bounded write (deterministic, safe).
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent      # the General repo root
UNIT_PATH = _ROOT / "memory" / "UNIT.md"
_BACKUPS = _ROOT / "memory" / "backups"

_BEGIN = "<!-- SCRIBE:BEGIN -->"
_END = "<!-- SCRIBE:END -->"
_LOG_HEADING = "## Lessons & Decisions  _(Scribe-maintained — newest first)_"

_SEED = f"""# Elite Unit — Living Protocol (Unit Memory)

The unit's shared memory. Every officer reads this before acting. The Commander owns the
Mission, Standing Orders, and Per-App Notes; the Scribe maintains only the Lessons &
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
    """Create a starter UNIT.md if none exists. Returns the path."""
    if not UNIT_PATH.exists():
        UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        UNIT_PATH.write_text(_SEED.replace("{today}", datetime.now().strftime("%Y-%m-%d")),
                             encoding="utf-8")
    return UNIT_PATH


def load() -> str:
    """Full UNIT.md text, or '' if it doesn't exist yet."""
    try:
        return UNIT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def preamble() -> str:
    """Compact block to prepend to an officer's system prompt. '' when there's no memory."""
    text = load()
    if not text:
        return ""
    return ("=== UNIT MEMORY — the unit's living protocol. Read it before you act; obey the "
            "Standing Orders. ===\n" + text + "\n=== END UNIT MEMORY ===\n\n")


# --------------------------------------------------------------------------- #
def _split(text: str) -> tuple[str, str, str]:
    """Return (head, scribe_body, tail) around the SCRIBE markers."""
    if _BEGIN in text and _END in text:
        head, rest = text.split(_BEGIN, 1)
        body, tail = rest.split(_END, 1)
        return head, body, tail
    return text, "", ""


def _backup() -> None:
    if UNIT_PATH.exists():
        _BACKUPS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(UNIT_PATH, _BACKUPS / f"UNIT-{stamp}.md")


def update_log(bullets: str) -> None:
    """Replace ONLY the Scribe-owned section with a fresh log. Backs up first; preserves
    every human-owned section. Creates the seed (and markers) if missing."""
    ensure()
    text = load()
    _backup()
    new_block = f"{_BEGIN}\n{_LOG_HEADING}\n\n{bullets.strip()}\n{_END}"
    if _BEGIN in text and _END in text:
        head, _, tail = _split(text)
        out = head.rstrip() + "\n\n" + new_block + ("\n" + tail.lstrip() if tail.strip() else "\n")
    else:
        out = text.rstrip() + "\n\n" + new_block + "\n"
    UNIT_PATH.write_text(out, encoding="utf-8")


# --------------------------------------------------------------------------- #
SCRIBE_SYSTEM = """\
You are the Scribe of an elite autonomous software unit, keeper of its living protocol
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
    """Run the Scribe: read the latest council + recent audit, rewrite the log section.
    Returns a short status line. Safe to call after every council (best-effort)."""
    from claude_agent_sdk import ClaudeAgentOptions

    from .agent import run_agent
    from .config import normalize_effort
    ensure()
    current = load()
    _, current_log, _ = _split(current)

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
        model=cfg.reviewer_model,
        system_prompt=SCRIBE_SYSTEM,
        cwd=str(_ROOT),
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "NotebookEdit"],   # Python does the write
        setting_sources=[],
        max_turns=12,
        effort=normalize_effort(getattr(cfg, "reviewer_effort", "high")),
    )
    run = await run_agent(prompt, options, tag="scribe")
    bullets = (run.final or "").strip()
    if not bullets:
        return "Scribe: no update produced."
    # Guard: if the model wrapped output in markers/headings, strip them.
    for junk in (_BEGIN, _END, _LOG_HEADING, "## Lessons & Decisions"):
        bullets = bullets.replace(junk, "")
    update_log(bullets.strip())
    return f"Scribe: Unit Memory updated ({UNIT_PATH})."
