"""Officers raise their own tickets.

Scout / Provost / Quartermaster append a machine-readable block of ticket-worthy findings to
their report. We parse it, de-dup against open tickets, and (only when the Commander passes
`--file`) create them in the backlog — assigned to you, To Do, labeled by the officer. Without
`--file`, we just show what *would* be filed (propose-first).
"""
from __future__ import annotations

import json
import re

from .backlog.base import make_backlog
from .config import AppConfig

# Appended to an officer's system prompt so it emits fileable findings as structured JSON.
TICKET_BLOCK_RULE = """

FINALLY — ticket filing. If (and only if) you found issues that genuinely warrant their own
ticket for a Field Engineer to fix, append a machine block as the VERY LAST thing in your reply,
with nothing after it:
===TICKETS===
[{"title": "<imperative, specific summary>", "type": "Bug" or "Task", "severity": "CRITICAL"|"HIGH"|"MEDIUM"|"LOW", "body": "<what, where (file), why it matters, and acceptance criteria>"}]
===END===
Include ONLY real, actionable findings (a bug, gap, or risk worth a Field Engineer's time) — omit
minor notes. If nothing warrants a ticket, emit an empty list:
===TICKETS===
[]
===END==="""

_BLOCK = re.compile(r"===TICKETS===\s*(.*?)\s*===END===", re.DOTALL)


def parse_tickets(report: str) -> tuple[list[dict], str]:
    """Return (proposals, clean_report) — proposals from the block, report with the block removed."""
    report = report or ""
    m = _BLOCK.search(report)
    clean = _BLOCK.sub("", report).strip()
    if not m:
        return [], clean
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return [], clean
    if not isinstance(data, list):
        return [], clean
    return [d for d in data if isinstance(d, dict) and str(d.get("title", "")).strip()], clean


def file_findings(app: AppConfig, officer_label: str, report: str) -> list[str]:
    """Create a ticket per proposed finding (de-duped). Returns human result lines."""
    proposals, _ = parse_tickets(report)
    if not proposals:
        return []
    backlog = make_backlog(app)
    out: list[str] = []
    for p in proposals:
        title = str(p.get("title", "")).strip()
        if not title:
            continue
        try:
            existing = backlog.find_open_by_summary(title)
            if existing:
                out.append(f"↺ {existing} already open — {title}")
                continue
            key = backlog.create_task(title, str(p.get("body", "")),
                                      labels=[officer_label, "autofiled"],
                                      issue_type=str(p.get("type", "Task")) or "Task")
            out.append(f"✓ {key} filed — {title}" if key else f"✗ filing not supported — {title}")
        except Exception as exc:  # noqa: BLE001 - one bad finding must not sink the rest
            out.append(f"✗ failed ({str(exc)[:80]}) — {title}")
    return out


def present(report: str, app: AppConfig, officer_label: str, do_file: bool) -> tuple[str, str]:
    """Return (clean_report, filing_block). filing_block lists proposals and, if do_file, results."""
    proposals, clean = parse_tickets(report)
    if not proposals:
        return clean, ""
    lines = ["📋 Ticket-worthy findings:"]
    lines += [f"  • [{p.get('severity', '?')}] {p.get('title')}" for p in proposals]
    if do_file:
        lines.append("")
        lines += file_findings(app, officer_label, report)
    else:
        lines.append("   (re-run with --file to create these as tickets, assigned to you)")
    return clean, "\n".join(lines)
