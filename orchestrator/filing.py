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

# Per-officer runtime notes appended to TICKET_BLOCK_RULE so a low-precision cycle can tighten
# an officer's ticket-filing bar in its own system prompt without touching the base constant.
_OFFICER_BLOCK_NOTES: dict[str, str] = {}


def set_officer_block_note(officer_label: str, note: str) -> None:
    """Set (or update) a per-officer sentence appended to its TICKET_BLOCK_RULE at the next run."""
    _OFFICER_BLOCK_NOTES[officer_label] = note


def ticket_block_rule_for(officer_label: str) -> str:
    """Return ``TICKET_BLOCK_RULE`` plus any runtime-set per-officer note (empty string when
    the officer has no note). The bare ``TICKET_BLOCK_RULE`` is always a substring of the return
    value, so existing ``assert TICKET_BLOCK_RULE in REVIEWER_SYSTEM`` tests stay green.

    Labels are the officer's CANONICAL DISPLAY NAMES, spaced exactly as officers.display()
    renders them — never hyphenated id-style spellings: any capitalized engineer-token that is
    not one of the spaced compounds the EU-40 rename-leak guard exempts makes
    officer_rename_regression_test.py red (and keep each spaced compound on ONE source line —
    the guard also catches names wrapped across a line break)."""
    return TICKET_BLOCK_RULE + _OFFICER_BLOCK_NOTES.get(officer_label, "")


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
# EU-586: the map knew only the CRITICAL/HIGH/MEDIUM/LOW vocabulary of the block contract above,
# but the Reviewer speaks a DIFFERENT scale — reviewer.py:49 declares
# `"severity": "blocker|major|minor"` and reviewer.py:1204 lowercases it, so after filing's
# `.upper()` the value arrives as BLOCKER / MAJOR / MINOR. None of those matched, the lookup
# returned None, and every reviewer finding landed on the project default. Measured on the board:
# BLOCKER -> Medium, MAJOR -> Medium — a blocker-level finding sat at the same priority as a test
# nitpick, competing directly with the Commander's own work.
#
# Both vocabularies are mapped here so neither officer can file blind.
#
# CRITICAL stays "Highest" — that is EU-284's deliberate contract (a critical finding must not rot
# at the project default) and it is rare AND pages the Commander, so it is not queue noise. The
# REVIEWER scale is the high-volume one (every build can emit minors), so it is capped at High: the
# unit's routine self-findings must never outrank a ticket the Commander himself marked Highest.
_SEVERITY_TO_PRIORITY = {
    # the findings-block contract (officers writing an explicit severity)
    "CRITICAL": "Highest",
    "HIGH": "High",
    "MEDIUM": "Medium",
    "LOW": "Low",
    # the Reviewer's quality-issue scale (reviewer.py:49) — previously unmapped
    "BLOCKER": "High",
    "MAJOR": "Medium",
    "MINOR": "Low",
}

# Severities that page the Commander when NEWLY filed (see FilingResult.filed_critical). EU-586:
# BLOCKER joins CRITICAL — a blocker-level finding filed silently at the project default was the
# exact failure this pair guards against.
_PAGING_SEVERITIES = ("CRITICAL", "BLOCKER")


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


_ANCHOR_RE = re.compile(r"[A-Za-z0-9_]+(?:[./][A-Za-z0-9_]+)+|[a-z0-9]+(?:_[a-z0-9]+){2,}")

# EU-439: anchors that are SHARED BOILERPLATE, not a finding's distinguishing subject. Every
# forensics.signature_sweep report opens with "Auto-filed by forensics.signature_sweep (EU-231)
# ...", and "forensics.signature_sweep" was the longest anchor in that text — so it dominated
# the hash and three unrelated [infra-signature] titles collapsed to ONE fp- (EU-422/423/424),
# silently suppressing every later crash pattern as "already filed". These anchors are excluded
# from contention; when they are the ONLY anchors, the title text is hashed instead (see below).
# If a future auto-filer introduces its own boilerplate anchor, add it HERE.
_BOILERPLATE_ANCHORS = {"forensics.signature_sweep", "signature_sweep"}


def subject_fingerprint(title: str, body: str = "") -> str | None:
    """A stable de-dup label for a finding's SUBJECT (2026-07-21). Six differently-worded reports
    of one failing test (EU-409..414) sailed past the exact-title match — but every one of them
    named the same code anchors (`eu255_env_minimization_test.py`). Extract the path-like /
    snake_case identifiers, keep the single most distinctive (longest), and hash it into a
    Jira-safe label. Prose-only findings (no anchors) return None — the title match stays their
    only key.

    EU-439 (2026-07-23): boilerplate anchors are excluded before the "longest wins" pick, so a
    shared report prefix can no longer dominate the hash. When the body is boilerplate-DOMINATED
    (the only anchors left after exclusion are boilerplate), the normalized TITLE text is hashed
    instead — that is what gives every distinct [infra-signature] prose title its own fp-. A
    title-preference rewrite was REJECTED: it breaks the EU-409..414 pin (_w1's title yields a
    truncated anchor while _w2's title yields none — the fuller body anchor must stay in
    contention), so the fix is exclusion + a title fallback, not title-preference."""
    text = f"{title or ''}\n{body or ''}"
    anchors: set[str] = set()
    for m in _ANCHOR_RE.finditer(text):
        a = m.group(0).lower().rstrip(".").rsplit("/", 1)[-1]     # basename — path prefixes vary
        for ext in (".py", ".ts", ".tsx", ".js", ".sh", ".md", ".yaml", ".yml", ".json"):
            if a.endswith(ext):
                a = a[: -len(ext)]
                break
        if len(a) >= 8 and not a.replace(".", "").replace("_", "").isdigit():
            anchors.add(a)
    real = {a for a in anchors if a not in _BOILERPLATE_ANCHORS}
    import hashlib
    if real:
        # ONE key: the single most distinctive (longest) real anchor. Two findings that orbit
        # the same file/identifier are the same SUBJECT for de-dup purposes — exactly the
        # EU-409..414 class (six phrasings, one failing test). Boilerplate never wins this pick.
        top = sorted(sorted(real), key=len, reverse=True)[0]
        return "fp-" + hashlib.sha1(top.encode("utf-8")).hexdigest()[:12]
    if anchors:
        # Boilerplate-dominated body, no real anchor: the TITLE is the only thing that
        # distinguishes this finding, so hash its normalized text. Distinct titles -> distinct
        # fp- (the three infra signatures diverge); a re-stated title -> same fp- (stable).
        norm = " ".join(str(title or "").lower().split())
        if norm:
            return "fp-" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]
    # Truly anchor-free AND boilerplate-free: prose-only finding — the title match stays its key.
    return None


# EU-456: the EXACT sentinel prefixes JiraAdapter._to_ticket appends to Ticket.description — the
# Commander's comment thread and the downloaded image paths. relabel_fingerprint re-reads a LIVE
# ticket and fingerprints its body, so it must strip these synthetic blocks first: a comment citing
# a code anchor (or an image basename) is a longer "real anchor" than the clean body carries, so it
# would win subject_fingerprint's "longest wins" pick and the recomputed fp would diverge from the
# one a clean new filing stamps — silently re-breaking the very collision AC4 fixed. These are the
# SINGLE source of truth: backlog/jira.py (the writer) imports them, so the writer and this stripper
# can NEVER drift on the appended wording — which is exactly the drift that would re-introduce this
# bug the moment someone edits _to_ticket's appended text.
_COMMANDER_COMMENTS_PREFIX = "\n\nCommander's comments (oldest -> newest) —"
_TICKET_IMAGES_PREFIX = "\n\nTicket images — OPEN and VIEW each"


def _clean_ticket_description(description: str) -> str:
    """Return the ticket's ORIGINAL body with the synthetic blocks JiraAdapter._to_ticket appends
    (Commander comments, downloaded images) stripped off. Cuts at the FIRST occurrence of either
    sentinel — whichever sorts earliest — so the text relabel_fingerprint fingerprints is the same
    text a clean new filing would stamp. A description carrying neither append passes through whole
    (so the EU-439 BOILERPLATE case is an exact no-op)."""
    description = description or ""
    cut = len(description)
    for prefix in (_COMMANDER_COMMENTS_PREFIX, _TICKET_IMAGES_PREFIX):
        i = description.find(prefix)
        if i != -1 and i < cut:
            cut = i
    return description[:cut]


def relabel_fingerprint(backlog, key: str) -> str | None:
    """EU-439: correct a mis-stamped subject-fingerprint label on an EXISTING ticket. Re-reads the
    ticket's CURRENT title + body, recomputes the correct ``subject_fingerprint``, and swaps any
    wrong ``fp-`` label for it (non-clobbering — the officer / autofiled / infra-signature labels
    stay put). Returns the correct fp (or None when the finding has no fingerprint). A no-op when
    the ticket already carries the right label, so it is safe to re-run.

    Why re-read live: the ticket the collision shadowed (EU-422/423/424) may have been title-edited
    since filing; the recomputed fp simply follows the edited title. ``run_all`` cannot exercise
    this (it stubs the network) — it is verified against a stub backlog in eu439 and run live once
    against the real tickets."""
    ticket = backlog.get_task(key)
    title = getattr(ticket, "summary", "") or ""
    # EU-456: fingerprint the ORIGINAL body only. _to_ticket appends the Commander's comment thread
    # + image paths to description; a comment code anchor (or image basename) would otherwise win
    # subject_fingerprint's "longest real anchor" pick and the recomputed fp would diverge from the
    # one a clean new filing stamps — defeating this relabel's whole purpose.
    body = _clean_ticket_description(getattr(ticket, "description", "") or "")
    fp = subject_fingerprint(title, body)
    labels = list(getattr(ticket, "labels", None) or [])
    fp_labels = [l for l in labels if str(l).startswith("fp-")]
    add = [fp] if (fp and fp not in fp_labels) else []
    remove = [l for l in fp_labels if l != fp]
    if add or remove:
        backlog.set_labels(key, add=add, remove=remove)
    return fp


def file_findings(app: AppConfig, officer_label: str, report: str, audit=None) -> FilingResult:
    """Create a ticket per proposed finding (de-duped). Returns the REAL per-finding outcome
    (filed / deduped / failed) plus the human result lines — so callers report what actually
    happened, never just the number of proposals.

    EU-439: ``audit`` (optional AuditLog) records a ``filing_suppressed`` event on EVERY dedup
    (label or summary hit) carrying the matched key, the suppressed title, how it matched, and the
    fingerprint — so a suppression is never again a silent drop (the EU-422/423/424 collision went
    unnoticed for exactly that reason). Default ``None`` keeps every other caller unchanged."""
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
        body = str(p.get("body", ""))
        fp = subject_fingerprint(title, body)
        try:
            # Subject-level de-dup FIRST (2026-07-21): the fingerprint label survives rewording,
            # the exact-title match stays as the fallback (and the only key for prose findings).
            # getattr-guarded like _with_default_timeout: bare test stubs and older adapters
            # without the method just skip straight to the title match.
            _by_label = getattr(backlog, "find_open_by_label", None)
            existing = None
            matched_by = None
            if fp and callable(_by_label):
                existing = _by_label(fp)
                if existing:
                    matched_by = "label"
            if not existing:
                got = backlog.find_open_by_summary(title)
                if got:
                    existing = got
                    matched_by = "summary"
            if existing:
                res.deduped.append(existing)
                res.lines.append(f"↺ {existing} already open — {title}")
                # EU-439: a suppression leaves a trace. Not requiring an exact-title corroboration
                # for a label hit (that would re-break EU-409..414, whose point is title-INSENSITIVE
                # dedup) — the audit event IS the corroboration the ticket asks for.
                if audit is not None:
                    try:
                        audit.record("filing_suppressed", key=existing, title=title,
                                     matched_by=matched_by, fp=fp)
                    except Exception:  # noqa: BLE001 — an audit hiccup must never change filing
                        pass
                continue
            key = backlog.create_task(title, body,
                                      labels=[officer_label, "autofiled"] + ([fp] if fp else []),
                                      issue_type=str(p.get("type", "Task")) or "Task",
                                      priority=priority)
            if key:
                res.filed.append(key)
                res.lines.append(f"✓ {key} filed — {title}")
                if severity in _PAGING_SEVERITIES:
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
            do_file: bool, audit=None) -> tuple[str, str, FilingResult]:
    """Return (clean_report, filing_block, result). filing_block lists proposals and, if do_file,
    results; `result` is the real filing outcome (empty FilingResult when proposing-only/no
    findings) so the caller can report new/already-open/failed counts and escalate failures.

    EU-439: ``audit`` is passed through to ``file_findings`` so the present() path (scout /
    provost / quartermaster filing) records `filing_suppressed` on dedup too."""
    proposals, clean = parse_tickets(report)
    if not proposals:
        return clean, "", FilingResult()
    lines = ["📋 Ticket-worthy findings:"]
    lines += [f"  • [{p.get('severity', '?')}] {p.get('title')}" for p in proposals]
    result = FilingResult()
    if do_file:
        result = file_findings(app, officer_label, report, audit=audit)
        lines.append("")
        lines += result.lines
    else:
        lines.append("   (re-run with --file to create these as tickets, assigned to you)")
    return clean, "\n".join(lines), result
