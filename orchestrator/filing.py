"""Officers raise their own tickets.

QA Engineer / Security Engineer / Release Manager append a machine-readable block of ticket-worthy findings to
their report. We parse it, de-dup against open tickets, and (only when the Commander passes
`--file`) create them in the backlog — assigned to you, To Do, labeled by the officer. Without
`--file`, we just show what *would* be filed (propose-first).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .backlog.base import make_backlog
from .config import AppConfig


@dataclass
class FilingResult:
    """The real outcome of filing a batch of findings — not just a count of proposals.

    `filed`          — keys of tickets newly created this run.
    `deduped`        — keys of already-open tickets a finding matched (nothing created).
    `failed`         — (title, error) for findings that could not be filed (create error /
                       backend can't file). These must be escalated, never buried.
    `lines`          — the human-readable per-finding result lines (kept for Telegram display).
    `filed_critical` — (key, title) for findings NEWLY filed (not deduped) whose declared severity
                       was CRITICAL — EU-284: lets the caller alert the Commander on a critical
                       out-of-scope finding without re-parsing the report.
    """
    filed: list[str] = field(default_factory=list)
    deduped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    filed_critical: list[tuple[str, str]] = field(default_factory=list)

    @property
    def filed_n(self) -> int:
        return len(self.filed)

    @property
    def deduped_n(self) -> int:
        return len(self.deduped)

    @property
    def failed_n(self) -> int:
        return len(self.failed)

# Appended to an officer's system prompt so it emits fileable findings as structured JSON.
TICKET_BLOCK_RULE = """

FINALLY — ticket filing. If (and only if) you found issues that genuinely warrant their own
ticket for a Dev Team Lead to fix, append a machine block as the VERY LAST thing in your reply,
with nothing after it:
===TICKETS===
[{"title": "<imperative, specific summary>", "type": "Bug" or "Task", "severity": "CRITICAL"|"HIGH"|"MEDIUM"|"LOW", "body": "<what, where (file), why it matters, and acceptance criteria>"}]
===END===
Include ONLY real, actionable findings (a bug, gap, or risk worth a Dev Team Lead's time) — omit
minor notes. If nothing warrants a ticket, emit an empty list:
===TICKETS===
[]
===END==="""

_BLOCK = re.compile(r"===TICKETS===\s*(.*?)\s*===END===", re.DOTALL)


def make_block(proposals: list[dict]) -> str:
    """Serialize proposals into the ===TICKETS=== block `parse_tickets` reads — the ONE wire format
    for machine-raised findings. EU-231: lets deterministic callers (forensics' postmortem and
    crash-signature filers) reuse `file_findings`' dedupe + labels + severity→priority mapping
    instead of hand-building the block next to the parser and drifting from it."""
    return "===TICKETS===\n" + json.dumps(list(proposals or []), ensure_ascii=False) + "\n===END==="

# EU-284: an officer's declared severity maps to a Jira-native priority so a CRITICAL finding
# doesn't rot at the project's default (Medium) priority. Absent/unknown severity -> None, which
# leaves the backend's own default untouched (see JiraAdapter.create_task).
_SEVERITY_TO_PRIORITY = {
    "CRITICAL": "Highest",
    "HIGH": "High",
    "MEDIUM": "Medium",
    "LOW": "Low",
}


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


def file_findings(app: AppConfig, officer_label: str, report: str) -> FilingResult:
    """Create a ticket per proposed finding (de-duped). Returns the REAL per-finding outcome
    (filed / deduped / failed) plus the human result lines — so callers report what actually
    happened, never just the number of proposals."""
    proposals, _ = parse_tickets(report)
    res = FilingResult()
    if not proposals:
        return res
    backlog = make_backlog(app)
    for p in proposals:
        title = str(p.get("title", "")).strip()
        if not title:
            continue
        severity = str(p.get("severity", "") or "").strip().upper()
        priority = _SEVERITY_TO_PRIORITY.get(severity)
        try:
            existing = backlog.find_open_by_summary(title)
            if existing:
                res.deduped.append(existing)
                res.lines.append(f"↺ {existing} already open — {title}")
                continue
            key = backlog.create_task(title, str(p.get("body", "")),
                                      labels=[officer_label, "autofiled"],
                                      issue_type=str(p.get("type", "Task")) or "Task",
                                      priority=priority)
            if key:
                res.filed.append(key)
                res.lines.append(f"✓ {key} filed — {title}")
                if severity == "CRITICAL":
                    res.filed_critical.append((key, title))
            else:
                res.failed.append((title, "filing not supported"))
                res.lines.append(f"✗ filing not supported — {title}")
        except Exception as exc:  # noqa: BLE001 - one bad finding must not sink the rest
            err = str(exc)[:80]
            res.failed.append((title, err))
            res.lines.append(f"✗ failed ({err}) — {title}")
    return res


def present(report: str, app: AppConfig, officer_label: str,
            do_file: bool) -> tuple[str, str, FilingResult]:
    """Return (clean_report, filing_block, result). filing_block lists proposals and, if do_file,
    results; `result` is the real filing outcome (empty FilingResult when proposing-only/no
    findings) so the caller can report new/already-open/failed counts and escalate failures."""
    proposals, clean = parse_tickets(report)
    if not proposals:
        return clean, "", FilingResult()
    lines = ["📋 Ticket-worthy findings:"]
    lines += [f"  • [{p.get('severity', '?')}] {p.get('title')}" for p in proposals]
    result = FilingResult()
    if do_file:
        result = file_findings(app, officer_label, report)
        lines.append("")
        lines += result.lines
    else:
        lines.append("   (re-run with --file to create these as tickets, assigned to you)")
    return clean, "\n".join(lines), result
