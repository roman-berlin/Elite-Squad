"""Two-way decisions — the CTO asks, you answer in Telegram, it resumes.

When the Code Reviewer flags a product/scope decision (`needs_human`), the loop records
a *pending decision* here and pings you. While the control panel (`general serve`) is
running, a background poller watches Telegram; when you reply, the matching ticket is
re-run with your decision appended to its spec. Resume = re-run with the answer baked in
(no fragile long-lived paused threads).

Reply format in Telegram:  `AUTO-1: use DD/MM`  (ticket id, colon, your decision)
If you omit the id, the oldest pending decision is used.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from . import locking, notify
from .contracts import Ticket

# Jira-style ticket key (e.g. AUTO-14, EU-89) — used to extract refs from chat history.
_TICKET_KEY_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+")

# Telegram phrases the Commander uses to confirm a queued proposal without repeating its name.
_APPROVAL_PHRASE_RE = re.compile(
    r"^(create\s+it|approve|yes|go\s+ahead|file\s+it|do\s+it)\s*[.!]?\s*$",
    re.IGNORECASE,
)

# EU-372: how long a two-phase claim (resolve(claim=True) → commit()) may sit before we assume the
# claiming process died and re-offer the decision. Generous by design: the real window is seconds
# (a Jira comment round-trip + a thread spawn), so 15 min can only elapse if nobody is coming back.
_CLAIM_TTL_SEC = 900.0


def _question_fingerprint(question: str) -> str:
    """Short SHA-1 of the normalised question text — used as the dedup key (EU-89).
    Normalise whitespace and lower-case so minor rephrasing doesn't defeat the gate."""
    normalised = " ".join(question.lower().split()).strip()
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:12]


def _store(cfg) -> Path:
    return Path(cfg.audit_path).with_name("pending_decisions.json")


def _offset_file(cfg) -> Path:
    return Path(cfg.audit_path).with_name("telegram_offset.txt")


def load(cfg) -> list[dict]:
    p = _store(cfg)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _save(cfg, items: list[dict]) -> None:
    # Write under the shared cross-thread + cross-process lock (the Telegram poller's add/resolve race
    # the autopilot/cockpit on this same file). add()/resolve() below do the full read-modify-write
    # inside a single locked_rmw so two concurrent edits can't clobber each other.
    locking.locked_rmw(_store(cfg), lambda _current: items, default=[])


def _validate_question_format(question: str) -> tuple[bool, str]:
    """EU-229: validate ask quality — reject empty/garbage questions.

    Returns (is_valid, error_reason). Invalid patterns:
    - Empty or whitespace-only
    - Leaked internal monologue (starts with '## ANALYSIS', 'Looking at the', etc.)
    - Raw markdown headers (##, ###) that aren't proper questions

    Well-formed questions:
    - One-line clear question (e.g. "Which date format should we use?")
    - Structured with options (e.g. "Which format?\nOptions:\n1. X\n2. Y")
    - Very short test/data questions (e.g. "q0") - allowed for testing
    """
    if not question or not question.strip():
        return False, "empty question"

    q = question.strip()

    # Detect leaked internal monologue / chain-of-thought - these are exact patterns
    # that should NEVER appear in a question sent to the Commander.
    # Distinctive markers are rejected ANYWHERE (PM's "WHY PM CANNOT RESOLVE" prefix can precede
    # the leaked monologue). Common English openers are only rejected when a LINE STARTS with
    # them (EU-358): as bare substrings they ate legitimate decisions — e.g. any option text
    # containing "…I need to know if sessions matter" silently voided the whole question.
    leaked_anywhere = [
        "## ANALYSIS",
        "Reality check on",
    ]
    leaked_line_start = [
        "Looking at the",
        "I need to",
        "Let me",
        "Based on the",
    ]
    q_lower = q.lower()
    for pattern in leaked_anywhere:
        if pattern.lower() in q_lower:
            return False, f"leaked internal monologue (contains '{pattern[:20]}')"
    for line in q_lower.split("\n"):
        stripped = line.strip()
        for pattern in leaked_line_start:
            if stripped.startswith(pattern.lower()):
                return False, f"leaked internal monologue (contains '{pattern[:20]}')"

    # Raw markdown headers (##, ###) - these should NEVER appear in a Commander-facing question
    # They indicate leaked developer markdown. More lenient check: only if they appear
    # as actual headers (at line start or after newline).
    if "## " in q or "### " in q:
        # Check if it's a markdown header (at start of line)
        for line in q.split("\n"):
            if line.strip().startswith("## ") or line.strip().startswith("### "):
                return False, "leaked markdown header"

    # Allow anything that passes the leaked-pattern checks - the goal is to filter
    # out obvious leaked monologue, not to enforce perfect structure
    return True, ""


def add(cfg, ticket: Ticket, app_name: str, question: str, entry_id: str | None = None,
        *, block: bool = True, extra: dict | None = None) -> str | None:
    """Record a pending decision for the cockpit 'Needs you'. `entry_id` overrides the storage/de-dup
    key (defaults to the ticket id); pass a distinct key — e.g. f'{ticket.id}#out-of-scope' (EU-42) —
    when one ticket carries more than one kind of pending decision, so they don't overwrite each other.

    EU-61 (decision round-trip): parking a ticket's MAIN decision also transitions it to 'Blocked' on
    the tracker, so the open question is visible in Jira and the Commander can answer it right there, and
    snapshots the latest human comment as a resume *baseline* (the autopilot uses it to tell a NEW Jira
    answer from a pre-existing comment). A SUB-decision carrying a distinct ``entry_id`` (the out-of-scope
    proposal) rides alongside without changing the ticket's status; pass ``block=False`` for a hand-back
    that owns its own status (e.g. the readiness gate → 'Needs Human').

    EU-83: ``extra`` is merged into the stored entry verbatim. Use it to attach payload that the resume
    path needs — e.g. ``extra={"out_of_scope_report": report}`` for out-of-scope proposals so the
    original ===TICKETS=== block survives to the Commander's reply and can be filed directly.

    EU-89 dedup gate: if a pending entry with the same base ticket id AND the same question
    fingerprint already exists, the write is skipped and the existing entry's id is returned.
    This prevents the autopilot from stacking duplicate 'which date format?' cards when a
    re-run re-hits the same escalation point.

    EU-229: ask quality enforcement — rejects empty/garbage questions before writing to the store.

    Returns the stored entry id — the newly written one on a fresh park, or the EXISTING entry's
    id on a dedup hit (no new row). Returns None on validation failure (garbage question)."""
    # NB: decisions.add is a faithful storage primitive — it records whatever the routing layer hands
    # it (the out-of-scope PROPOSE-FIRST proposal, the EU-83 resume payload, a needs_human ask) and must
    # never silently drop a write. EU-92's "PM owns routine escalations" is enforced UPSTREAM (the PM
    # prompt self-resolves the routine classes; loop._route_out_of_scope auto-files out-of-scope findings
    # instead of paging) — gutting this primitive would just lose the entries those paths depend on.

    # EU-229: Validate question quality before proceeding
    is_valid, error = _validate_question_format(question)
    if not is_valid:
        # Silently reject — the caller (loop.py escalation path) should regenerate
        return None

    eid = entry_id or ticket.id
    base_tid = str(ticket.id).split("#", 1)[0]
    q_fp = _question_fingerprint(question)

    # Dedup gate (EU-89): scan the store BEFORE parking on the tracker so a duplicate question
    # never triggers a second Jira status transition.  A brief TOCTOU window is acceptable here —
    # if two identical questions slip through concurrently the RMW de-dupe by entry_id still
    # collapses them to a single row.
    for it in load(cfg):
        it_base = str(it.get("id", "")).split("#", 1)[0]
        if it_base == base_tid and it.get("_question_fp") == q_fp:
            return it["id"]   # already pending — skip the write

    entry = {
        "id": eid, "app": app_name, "question": question,
        "summary": ticket.summary, "description": ticket.description,
        "acceptance": ticket.acceptance_criteria, "ephemeral": ticket.ephemeral,
        "ts": time.time(), "_question_fp": q_fp,
    }
    if extra:
        entry.update(extra)
    # Only the ticket's own (main) decision parks it to 'Blocked'; a distinct-entry_id sub-decision
    # must not move the ticket's status out from under an in-flight build.
    if block and (entry_id is None or entry_id == ticket.id):
        baseline = _park_on_tracker(cfg, ticket, app_name, reason=question)
        if baseline is not None:
            entry["answer_baseline"] = baseline

    def _mutate(items):
        items = [i for i in (items or []) if i.get("id") != eid]   # de-dupe by entry id
        items.append(entry)
        return items

    # One atomic read-modify-write so a concurrent add/resolve can't drop this decision.
    locking.locked_rmw(_store(cfg), _mutate, default=[])
    return eid


def _park_on_tracker(cfg, ticket: Ticket, app_name: str, reason: str = "") -> str | None:
    """Transition a parked ticket to 'Blocked' (the visible state of a decision round-trip, EU-61) and
    return the latest human comment currently on it — the baseline the autopilot compares against to
    detect a NEW Commander answer. Best-effort: a no-op (returns None) for dry-run, ephemeral
    (trackerless) tickets, and apps with no backlog; never raises, so a tracker hiccup can't break the
    escalation path. ``reason`` (the escalation question) is posted as a comment so the board shows
    WHY the ticket is Blocked — before 2026-07-09 a park moved the ticket with no Jira-visible cause."""
    if getattr(cfg, "dry_run", False) or getattr(ticket, "ephemeral", False):
        return None
    try:
        app = cfg.app(app_name)
        if getattr(app, "backlog_backend", "none") == "none":
            return None
        from .backlog.base import make_backlog
        backlog = make_backlog(app)
        backlog.set_status(ticket, "Blocked")
        if reason.strip():
            try:
                backlog.add_comment(ticket, "⛔ Blocked — needs the Commander:\n" + reason.strip()[:900])
            except Exception:  # noqa: BLE001 - the reason trace is best-effort
                pass
        try:
            return backlog.latest_answer(ticket) or ""
        except Exception:  # noqa: BLE001 - the resume baseline is optional
            return ""
    except Exception:  # noqa: BLE001 - parking on the tracker must never break escalation
        return None


def consume_answer(cfg, ticket_id: str, answer: str) -> None:
    """Snapshot a just-detected Jira answer as the entry's new ``answer_baseline`` — atomically,
    under the shared store lock — so one comment resumes a ticket exactly ONCE.

    EU-229 widened the Jira-answer resume scan from blocked∩pending to ALL pending decisions but
    left the consume step behind (EU-61's blocked-only version was self-limiting via the blocked
    set): nothing ever advanced the baseline, so `_resumable_answered` re-detected the SAME comment
    every drain cycle and spammed '▶️ Resuming (answered on Jira)' to Telegram every ~2 minutes
    (live incident EU-335, 2026-07-16 10:45–10:56+). A later, genuinely different comment still
    resumes again. Best-effort: a store hiccup must never break the resume path."""
    def _mut(items):
        for e in (items or []):
            if e.get("id") == ticket_id:
                e["answer_baseline"] = answer
        return items or []
    try:
        locking.locked_rmw(_store(cfg), _mut, default=[])
    except (OSError, ValueError):
        pass


def _comment_answer(cfg, resolved: dict, answer: str) -> None:
    """Post the Commander's decision back onto the ticket as a comment (the write half of the EU-61
    round-trip), so the question and its resolution both live in Jira. Best-effort: skipped for dry-run /
    ephemeral / trackerless tickets; never raises."""
    if getattr(cfg, "dry_run", False) or resolved.get("ephemeral"):
        return
    try:
        app = cfg.app(resolved.get("app"))
        if getattr(app, "backlog_backend", "none") == "none":
            return
        key = str(resolved.get("id", "")).split("#", 1)[0]   # strip any sub-decision suffix
        if not key:
            return
        from .backlog.base import make_backlog
        ticket = Ticket(id=key, key=key, summary=resolved.get("summary", ""), description="",
                        app=app.name)
        make_backlog(app).add_comment(ticket, f"Commander's decision: {answer}")
    except Exception:  # noqa: BLE001 - commenting the answer must never break the resume path
        pass


def reply_hint(ticket_id: str | None = None) -> str:
    """One-liner telling the Commander how to answer THIS question in Telegram. Single source of
    truth for the reply syntax (kept in lock-step with parse_reply): prefix with the ticket id to
    target a specific pending question; a bare reply answers the OLDEST pending one. Matters when
    several questions are stacked and you're away from the cockpit."""
    if ticket_id:
        return f"↩️ Reply  {ticket_id}: <your decision>  — or reply plainly to answer the oldest pending."
    return "↩️ Reply  TICKET-ID: <your decision>  to target one — or reply plainly for the oldest pending."


def _reap_stale_claims(items: list[dict]) -> list[dict]:
    """Un-claim in-flight decisions whose claimer is long gone (EU-372).

    A claim (see ``resolve(claim=True)``) is only meaningful while the process that took it is alive
    to commit it. If that process dies mid-resume, the claim would otherwise tombstone the entry
    forever: never committed, never offered to another reply. The claim window is a couple of seconds
    (``_comment_answer``'s Jira round-trip, then the thread spawn), so anything older than the TTL by
    definition belongs to a dead process and is safe to re-offer. Entries with a missing/garbage
    ``claimed_at`` reap too — a claim we can't date can't be trusted to be live.

    Time-based on purpose: the unit's hosts (VPS + Mac) share this store via periodic git sync, so a
    claiming pid is not meaningfully checkable from here — and pid-liveness probes are the known
    flaky class this repo has been digging out of (EU-355)."""
    now = time.time()
    for e in items:
        if not e.get("in_flight"):
            continue
        try:
            claimed_at = float(e.get("claimed_at") or 0)
        except (TypeError, ValueError):
            claimed_at = 0
        if now - claimed_at > _CLAIM_TTL_SEC:
            e.pop("in_flight", None)
            e.pop("claimed_at", None)
    return items


def resolve(cfg, answer: str, ticket_id: str | None = None, *, comment: bool = True,
            claim: bool = False) -> dict | None:
    """Pop and return the matching pending decision (by id, else oldest).

    EU-61: by default also posts the answer back to the tracker as a comment (the Commander answered in
    Telegram → echo the decision onto the Jira ticket). Pass ``comment=False`` when the answer ALREADY
    came from a Jira comment (the autopilot's Jira-native resume), so it isn't echoed back.

    EU-372: ``claim=True`` makes the removal two-phase — the entry is MARKED in-flight and left in the
    store, and the caller must :func:`commit` it once the work it guards has durably started. The
    default (``claim=False``) still pops in one shot, because the other callers have no run to wait
    for and depend on the one-shot pop: the Jira-native resume (``loop._resume_from_jira_answer``)
    and the cockpit's dismiss (``server.needs_resolve``) — a contract also pinned by
    ``eu48_state_writers_concurrency_test`` ("resolve() drains the store"). Only ``handle_reply``,
    the one path with a real gap between the pop and the run starting, opts in.

    Either way the entry is selected under a single lock and an already-claimed entry is never
    offered, so a decision is still resolved exactly once (EU-48)."""
    popped: list[dict] = []

    def _mutate(items):
        items = _reap_stale_claims(list(items or []))
        if not items:
            return items
        # An in-flight entry is being resumed by someone else right now — not on offer.
        free = [(i, it) for i, it in enumerate(items) if not it.get("in_flight")]
        if not free:
            return items
        if ticket_id:
            idx = next((i for i, it in free if it["id"].lower() == ticket_id.lower()), None)
            if idx is None:
                return items   # no match — leave the store untouched
        else:
            idx = free[0][0]   # oldest FREE entry
        if claim:
            # Two-phase: hand the caller a copy but keep the entry until commit() confirms the
            # resume actually started. A crash in between re-surfaces the question instead of
            # stranding the ticket parked with nothing pending.
            entry = items[idx]
            entry["in_flight"] = True
            entry["claimed_at"] = time.time()
            popped.append(dict(entry))
        else:
            popped.append(items.pop(idx))
        return items

    # Find-and-claim/remove in one locked read-modify-write so a concurrent add/resolve can't lose a
    # decision or hand the same one to two replies.
    locking.locked_rmw(_store(cfg), _mutate, default=[])
    if not popped:
        return None
    it = popped[0]
    it["answer"] = answer
    if comment:
        _comment_answer(cfg, it, answer)
    return it


def commit(cfg, entry_id: str) -> None:
    """Finish a ``resolve(claim=True)`` — drop the claimed entry for good (EU-372).

    Call this only once the work the decision guards has durably started; until then the claim is
    what makes a crash recoverable. Idempotent: a re-commit (or an entry already ghost-cleared by
    ``autopilot._clear_ghost_decisions``) is a no-op."""
    def _mutate(items):
        return [e for e in (items or []) if e.get("id") != entry_id]

    locking.locked_rmw(_store(cfg), _mutate, default=[])


def to_worklist(cfg, resolved: dict):
    """Rebuild the ticket with the Commander's decision appended, ready to re-run.

    EU-83: strips any '#…' sub-decision suffix from the ticket id/key — those are decision-store
    discriminators only and must never reach a Jira REST URL as an issue key."""
    app = cfg.app(resolved["app"])
    # EU-83: strip the internal sub-decision discriminator (e.g. '#out-of-scope') so the Ticket
    # always carries a real Jira key. Jira REST calls on 'EU-81#out-of-scope' return 404/405.
    base_key = str(resolved["id"]).split("#", 1)[0]
    desc = (resolved.get("description") or resolved["summary"])
    desc += f"\n\nCommander's decision on the open question: {resolved['answer']}"
    ticket = Ticket(
        id=base_key, key=base_key, summary=resolved["summary"],
        description=desc, acceptance_criteria=resolved.get("acceptance") or [],
        app=app.name, ephemeral=resolved.get("ephemeral", True),
    )
    return [(app, ticket)]


# --------------------------------------------------------------------------- #
# 2026-07-19 (Commander order, the EU-337 decision-request format): a Needs-you decision should
# read as a BRIEF problem + 2-3 numbered options with exactly one (RECOMMENDED). These helpers
# parse that structure out of a stored question so the cockpit can render one-click option
# buttons and a Telegram reply can be just the option number. Purely additive: an unstructured
# question parses to None and every surface falls back to the free-text answer box.
_OPT_LINE = re.compile(r"^\s*(?:(\d)[\.\)]|[-•*])\s+(.+\S)\s*$")
_REC_MARK = re.compile(r"\(?\s*recommended\s*\)?", re.IGNORECASE)
_NUM_REPLY = re.compile(r"^\s*(?:option\s*)?([1-6])\s*[\.\)]?\s*$", re.IGNORECASE)


def parse_options(question: str) -> dict | None:
    """``{"summary": str, "options": [{"n", "text", "recommended"}]}`` — or None when the
    question doesn't carry a recognizable 2-6 option list (free-text remains the surface)."""
    opts: list[dict] = []
    summary: list[str] = []
    for ln in (question or "").splitlines():
        m = _OPT_LINE.match(ln)
        if m and m.group(2):
            raw = m.group(2).strip()
            rec = bool(_REC_MARK.search(raw))
            text = _REC_MARK.sub("", raw).strip(" \t—–-:·")
            if text:
                opts.append({"n": len(opts) + 1, "text": text, "recommended": rec})
        elif not opts:
            s = ln.strip()
            if s and not s.rstrip(":").upper().endswith("OPTIONS"):
                summary.append(s)
    if not 2 <= len(opts) <= 6:
        return None
    # exactly ONE recommended: if the officer marked several (or none), keep flags as parsed —
    # the UI highlights whatever is marked; multiple marks degrade to multiple highlights.
    return {"summary": " ".join(summary)[:400], "options": opts}


def summarize_question(question: str, limit: int = 140) -> str:
    """A one-line brief of a stored decision question: the first meaningful sentence, with
    command dumps / test walls cut off. For the cockpit card headline — the full text stays
    available behind the details fold."""
    q = " ".join(str(question or "").split())
    for cut in (" $ /", " $ python", "``` ", " (exit "):
        i = q.find(cut)
        if i > 20:
            q = q[:i]
    for end in (". ", "? ", "! "):
        i = q.find(end)
        if 30 <= i <= limit:
            return q[:i + 1].strip()
    return (q[:limit].rstrip() + "…") if len(q) > limit else q


# The known UNSTRUCTURED question classes (stored before the EU-337 format, or produced by
# deterministic loop paths) → a brief + 1-3 synthesized options, one recommended. The option
# text is written as an actionable instruction, because choosing it ships through /api/answer:
# a Jira comment + the ticket back to To Do with the instruction baked into the re-run.
_SYNTH_CLASSES: list[tuple[tuple[str, ...], str, list[tuple[str, bool]]]] = [
    (("is RED before any build", "gate fails on the clean base"),
     "This ticket parked while base 'dev' was RED (the gate failed on the clean tree).",
     [("Re-queue now — the base is green again, build this ticket as specced", True),
      ("Split it into smaller tickets first", False)]),
    # 2026-07-19: a turn-limit park now only happens AFTER auto-split declined (depth cap) and a
    # boosted retry also blew out — so "split it" stopped being an honest recommendation. Re-scope
    # is the senior move; a raw retry stays as the manual override.
    (("ran out of turns", "too big for a single pass", "iteration limit"),
     "The build ran out of turns even after auto-split hit its depth cap and one boosted retry.",
     [("Re-scope: simplify this ticket's description to the smallest shippable slice, then re-queue", True),
      ("Retry as-is — give it one more full pass", False)]),
    (("max passes", "PM escalated", "Reviewer's required changes"),
     "The build hit max passes — the Reviewer kept demanding changes.",
     [("Retry with the Reviewer's required changes as the spec", True),
      ("Split this ticket into smaller sub-tickets and build those", False)]),
]


def synthesize_options(question: str) -> dict | None:
    """parse_options-shaped dict for a KNOWN unstructured question class, else None. Gives the
    pre-format decision backlog the same brief + one-click recommended options the new
    escalations carry natively."""
    q = str(question or "")
    for needles, brief, opts in _SYNTH_CLASSES:
        if any(n.lower() in q.lower() for n in needles):
            return {"summary": brief, "synthesized": True,
                    "options": [{"n": i + 1, "text": text, "recommended": rec}
                                for i, (text, rec) in enumerate(opts)]}
    return None


def expand_option_reply(question: str, answer: str) -> str:
    """A bare '2' / 'option 2' reply against a structured question becomes the full option text
    (so the Jira comment and the re-run spec carry the decision, not a bare digit). Any other
    answer — or an unstructured question — passes through unchanged."""
    m = _NUM_REPLY.match(answer or "")
    if not m:
        return answer
    po = parse_options(question)
    if not po:
        return answer
    n = int(m.group(1))
    for o in po["options"]:
        if o["n"] == n:
            return f"Option {n}: {o['text']}"
    return answer


def parse_reply(text: str) -> tuple[str | None, str]:
    """'AUTO-1: use DD/MM' -> ('AUTO-1', 'use DD/MM'); 'use DD/MM' -> (None, 'use DD/MM')."""
    if ":" in text:
        head, rest = text.split(":", 1)
        head = head.strip()
        if head and " " not in head and len(head) <= 24:
            return head, rest.strip()
    return None, text.strip()


# Leading markers that signal an explicit reply to a pending decision even without a
# ticket id (e.g. forwarding the cockpit's "↩️ Reply …" hint, or a plain "re:" prefix).
_REPLY_MARKERS = ("↩️", "↩", "re:", "reply:")


def _strip_reply_marker(text: str) -> str:
    """Drop a leading reply marker so the remainder parses as a normal reply."""
    lowered = text.lstrip()
    for m in _REPLY_MARKERS:
        if lowered[: len(m)].lower() == m:
            return lowered[len(m):].strip()
    return text


def is_explicit_reply(text: str) -> bool:
    """True only when the message *explicitly* targets a parked decision: the
    `TICKET-ID: <decision>` form, or a leading reply marker. Bare free text is NOT a
    reply — it must reach the CTO chat instead of being swallowed by the oldest pending
    decision (F12 fix: free-text notes/questions were being eaten while decisions parked)."""
    stripped = text.lstrip()
    if any(stripped[: len(m)].lower() == m for m in _REPLY_MARKERS):
        return True
    ticket_id, _ = parse_reply(text)
    return ticket_id is not None


def _file_out_of_scope_resume(cfg, resolved: dict) -> None:
    """EU-83: Commander approved filing the out-of-scope findings — file them directly.

    The ===TICKETS=== report is stored in ``resolved['out_of_scope_report']`` by
    ``loop._route_out_of_scope`` (EU-83). If it is absent (a decision entry parked before
    EU-83), fall back gracefully with a notification rather than silently dropping."""
    base_id = str(resolved.get("id", "")).split("#", 1)[0]
    report = resolved.get("out_of_scope_report")
    if not report:
        notify.send(
            f"⚠️ {base_id} out-of-scope proposal has no stored report — nothing to file. "
            "(Decision was parked before the EU-83 fix; set out_of_scope_autofile to avoid "
            "this on future tickets.)"
        )
        return
    try:
        from . import filing as filing_mod
        app = cfg.app(resolved["app"])
        if getattr(cfg, "dry_run", False):
            proposals, _ = filing_mod.parse_tickets(report)
            titles = ", ".join(str(p.get("title", "?")) for p in proposals)
            print(f"  filing · out-of-scope resume (dry-run — not filed): {titles}", flush=True)
            return
        result = filing_mod.file_findings(app, "out-of-scope", report)
        for ln in result.lines:
            print(f"  filing · {ln}", flush=True)
        if result.filed:
            notify.send(f"✓ Out-of-scope findings filed: {', '.join(result.filed)}")
        if result.failed:
            notify.send(f"⚠️ Failed to file out-of-scope findings: "
                        + ", ".join(t for t, _ in result.failed))
    except Exception as exc:  # noqa: BLE001 - filing must not crash the reply handler
        print(f"  filing · out-of-scope resume failed: {exc}", flush=True)
        notify.send(f"⚠️ out-of-scope filing for {base_id} failed: {exc}")


def handle_reply(cfg, audit, text: str) -> bool:
    """Resolve a pending decision from a reply and re-run the ticket. Returns True
    if a ticket was resumed.

    EU-372: the decision is CLAIMED, not popped, until the resume has actually started — and it is
    committed only on the paths that got the work moving. Before this, resolve() removed the entry
    up front and a crash anywhere in the gap below (notably ``_comment_answer``'s Jira round-trip
    inside resolve, and the loop import in ``_run_bg``) left the ticket parked on 'Blocked' with no
    pending entry: the question vanished from 'Needs you' and no run was ever coming. Leaving the
    claim in place on the failure paths means the reaper re-offers the question instead."""
    ticket_id, answer = parse_reply(text)
    # 2026-07-19: a bare option number becomes the full option text (see expand_option_reply) —
    # looked up against the pending entry's stored question before the answer is committed.
    if _NUM_REPLY.match(answer or ""):
        try:
            for e in load(cfg):
                if str(e.get("id", "")).split("#", 1)[0] == str(ticket_id or e.get("id", "")).split("#", 1)[0]:
                    answer = expand_option_reply(str(e.get("question") or ""), answer)
                    break
        except Exception:  # noqa: BLE001 - expansion is sugar; the raw reply still resolves
            pass
    resolved = resolve(cfg, answer, ticket_id, claim=True)
    if not resolved:
        return False
    notify.send(f"▶️ Resuming {resolved['id']} with your decision: {answer}")
    audit.record("decision_resumed", ticket_id=resolved["id"], answer=answer)
    # EU-83: out-of-scope sub-decisions must NOT re-run the original build — the '#out-of-scope'
    # id is an internal discriminator that would produce 404/405 REST calls. Instead, file the
    # findings from the stored report and return without queuing a rebuild.
    if str(resolved.get("id", "")).endswith("#out-of-scope"):
        _file_out_of_scope_resume(cfg, resolved)
        commit(cfg, resolved["id"])   # the filing is the work — it is done
        return True
    worklist = to_worklist(cfg, resolved)
    # Run the resumed build in a background thread so we never block the Telegram
    # poll thread — /unblock and other replies keep being processed meanwhile.
    _run_bg(cfg, audit, worklist)
    commit(cfg, resolved["id"])   # the run thread is live — the decision is durably consumed
    return True


def _worklist_app_key(worklist) -> str | None:
    """EU-64: the per-project run-state key for a worklist — its single app's name, or ``None`` (the
    unit-wide default key) when the worklist is empty, untyped, or spans more than one project (a bare
    ``/drain``). Keying the resume/run on the app means a thread for project A never blocks (or clears)
    project B's run, and two resumes of the SAME project still share one per-app TOCTOU guard (F7)."""
    names: set[str | None] = set()
    for item in (worklist or []):
        app = item[0] if isinstance(item, tuple) else None
        names.add(getattr(app, "name", None))
    names.discard(None)
    return next(iter(names)) if len(names) == 1 else None


def _run_bg(cfg, audit, worklist, *, refuse_if_busy: bool = False) -> bool:
    """Run a worklist in a background thread, claiming THIS project's cockpit run-guard (EU-64).

    Mirrors the run POSTs in ``server.py``, but keyed PER PROJECT via ``cockpit_state.claim_run`` so a
    resume/run for one project runs truly in parallel with another's, while a second run on the SAME
    project still can't double-start (F7, the per-app TOCTOU guard). Returns True when the run started.

    ``refuse_if_busy=True`` (Telegram ``/run`` / ``/drain``): if a run/loop is already active for this
    project (or the parallel-run cap is reached), REFUSE rather than start an overlapping run — notify
    and return False.

    ``refuse_if_busy=False`` (decision resume): a resumed escalation is flock-safe (``loop.py``) and
    must proceed even while a run for its project is live — that is the always-on case, and the pending
    decision has already been popped, so dropping it would lose the Commander's answer. It still claims
    this project's badge when free, but never refuses.
    """
    from . import cockpit_state
    from .loop import run as run_loop

    run_key = _worklist_app_key(worklist)
    owns_guard = cockpit_state.claim_run(run_key)
    if not owns_guard and refuse_if_busy:
        notify.send("⏳ A run is already in progress for this project — try again once it finishes.")
        return False

    def _bg():
        reports = []
        # EU-175: bracket this Telegram/decision-resume run_loop with run_start/run_end so a hard-killed
        # or exception-exiting worker still closes its run boundary (mirroring main.py's CLI path) —
        # otherwise this ghost session leaves an unpaired boundary the audit trail can't close.
        if audit is not None:
            audit.record("run_start", mode=("DRY-RUN" if cfg.dry_run else "LIVE"), tickets=len(worklist or []))
        try:
            reports = asyncio.run(run_loop(cfg, worklist, audit))
        except Exception as exc:  # noqa: BLE001
            notify.send(f"⚠️ run failed: {exc}")
        finally:
            if audit is not None:
                audit.record("run_end", tickets=len(reports or []))
            if owns_guard:
                cockpit_state.release_run(run_key)
    threading.Thread(target=_bg, daemon=True).start()
    return True


def handle_command(cfg, audit, text: str) -> bool:
    """Telegram remote commands (your phone is the remote cockpit while serve runs)."""
    from . import dashboard as D
    from . import intake
    parts = text[1:].strip().split(maxsplit=1)
    cmd = (parts[0].lower() if parts else "")
    arg = (parts[1].strip() if len(parts) > 1 else "")

    if cmd in ("help", "start"):
        notify.send("Commands:\n/daily — quick daily stand-up (cheap)\n/standup — deterministic report\n"
                    "/status — recent tasks\n"
                    "/council [topic] — deep WEEKLY council\n"
                    "/run <app> <what to build> [--live]\n"
                    "/drain <app> [--live] — work your To-Do queue\n"
                    "/unblock <id> — retry a parked (escalated) ticket\n"
                    "Reply  TICKET: <decision>  to answer a question, or just reply in plain "
                    "words — I'll act on it and open a ticket if it's work.")
    elif cmd == "daily":
        notify.send("🫡 Daily stand-up…")

        def _dly():
            try:
                from . import council
                # EU-303: an interactive /daily is a deliberate human request → always broadcast,
                # even if this host isn't the elected scheduled sender.
                asyncio.run(council.daily_brief(cfg, audit=audit, broadcast=True))
            except Exception as exc:  # noqa: BLE001
                notify.send(f"⚠️ daily failed: {exc}")
        threading.Thread(target=_dly, daemon=True).start()
    elif cmd == "standup":
        notify.send(D.standup(cfg))
    elif cmd == "status":
        notify.send(D.render_status(D.load_tasks(cfg.audit_path), limit=10, show_cost=False))
    elif cmd == "council":
        notify.send("🎖️ Convening the daily council…")

        def _c():
            try:
                from . import council
                # EU-303: an interactive /council is a deliberate human request → always broadcast.
                asyncio.run(council.hold_council(cfg, topic=arg or None, audit=audit, broadcast=True))
            except Exception as exc:  # noqa: BLE001
                notify.send(f"⚠️ council failed: {exc}")
        threading.Thread(target=_c, daemon=True).start()
    elif cmd == "unblock":
        from . import autopilot as ap_mod
        notify.send("▶️ " + ap_mod.unblock(cfg, arg or None) + " — autopilot will retry it.")
    elif cmd in ("run", "drain"):
        import copy
        # EU-358: token-anchored — a bare substring test made any argument CONTAINING "--live"
        # (e.g. a /run description mentioning "--liveness") silently flip the run to LIVE.
        toks = arg.split()
        live = "--live" in toks
        arg = " ".join(t for t in toks if t != "--live").strip()
        rcfg = copy.copy(cfg)        # per-invocation config — never mutate the shared cfg
        rcfg.dry_run = not live
        try:
            if cmd == "run":
                app, _, desc = arg.partition(" ")
                if not app or not desc.strip():
                    notify.send("Usage: /run <app> <what to build> [--live]")
                    return True
                wl = intake.from_text(rcfg, app, desc[:60], [], description=desc)
            else:
                wl = intake.from_drain(rcfg, arg or None, rcfg.max_tickets_per_run)
            if not wl:
                notify.send("Nothing to do.")
                return True
            # A manual /run or /drain must not start a second run_loop on top of a live run or
            # autopilot — refuse (the resume path stays exempt). _run_bg notifies on refusal.
            if not _run_bg(rcfg, audit, wl, refuse_if_busy=True):
                return True
            notify.send(f"▶️ Starting {len(wl)} ticket(s) {'(LIVE)' if live else '(dry-run)'}…")
        except Exception as exc:  # noqa: BLE001
            notify.send(f"⚠️ couldn't start: {exc}")
    else:
        notify.send(f"Unknown command /{cmd}. Try /help")
    return True


def route_message(cfg, audit, text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    if text.startswith("/"):
        return handle_command(cfg, audit, text)
    # Only an EXPLICIT reply (id:/marker form) resolves a parked decision; a bare
    # free-text message must NOT pop the oldest pending one — it goes to the CTO chat.
    if load(cfg) and is_explicit_reply(text):
        if handle_reply(cfg, audit, _strip_reply_marker(text)):
            return True
    # Approval shorthand: 'create it' / 'approve' / 'yes' with exactly one matching pending
    # proposal batch → file immediately without a separate prompt (EU-89).  Scan recent chat
    # for the most recently mentioned ticket key and match against pending proposals by source;
    # if ambiguous or no match found, fall through to the CTO (which can clarify or ask).
    if _APPROVAL_PHRASE_RE.match(text):
        from . import approvals as _approvals
        from . import council as _council_mod
        thread = _council_mod.chat_transcript(cfg, lines=20)
        # Collect ticket refs from the thread, newest first (last in file = most recent).
        refs = _TICKET_KEY_RE.findall(thread)
        batch_id = None
        matched_ref = None
        for ref in reversed(refs):
            batches = [b for b in _approvals.pending_proposals(cfg)
                       if ref.upper() in str(b.get("source", "")).upper()]
            if len(batches) == 1:
                batch_id = batches[0]["id"]
                matched_ref = ref
                break
        if batch_id and matched_ref:
            result_msg = _approvals.materialize_proposal(cfg, matched_ref)
            notify.send(result_msg)
            try:
                audit.record("proposal_materialized", ref=matched_ref, batch=batch_id)
            except Exception:  # noqa: BLE001 - audit failure must not crash the reply path
                pass
            return True

    # Otherwise: a free-text message (e.g. a reply to a council question). The CTO
    # answers it in Telegram and logs the exchange as standing guidance for the unit.
    from . import council
    try:
        audit.record("commander_msg", text=text[:300])
    except Exception:  # noqa: BLE001
        pass

    def _answer():
        try:
            asyncio.run(council.respond_to_commander(cfg, text))
        except Exception as exc:  # noqa: BLE001
            council.add_commander_note(cfg, text)   # at least capture it
            notify.send(f"⚠️ the CTO couldn't reply ({exc}); logged your note.")
    threading.Thread(target=_answer, daemon=True).start()
    return True


def _read_offset(cfg) -> int | None:
    f = _offset_file(cfg)
    try:
        return int(f.read_text().strip())
    except (OSError, ValueError):
        return None


def _write_offset(cfg, update_id: int) -> None:
    try:
        _offset_file(cfg).write_text(str(update_id))
    except OSError:
        pass


def poll_once(cfg, audit) -> int:
    """Fetch new Telegram messages and resume any answered decisions. Returns the
    number of replies handled.

    EU-257: the whole fetch-then-ack sequence (read the offset -> ``getUpdates`` -> route each
    message -> advance the offset) is run under the cross-thread + cross-process lock keyed on
    the offset file (``locking.locked_call``), so two pollers racing this window — e.g. a
    leaked second poller thread, or two processes on a misconfigured host — can never both
    fetch the same update batch and each run route_message's side effects on it.

    EU-372: the offset advances only AFTER an update has been routed. Telegram's getUpdates is a
    single-consumer queue — acking past an update means it is never redelivered — so advancing
    first made any crash inside route_message (a SIGKILL, the 5h plan-limit stop, a host reboot)
    silently eat the Commander's message. Acking after routing flips the failure mode to
    at-least-once: a killed process leaves the update queued and Telegram redelivers it on
    restart. The re-route window is one file write wide, and a redelivered reply whose decision
    already resolved finds nothing pending (resolve() is exactly-once under the store lock) and
    falls through to the CTO chat — a benign duplicate, where the old behaviour was a silent,
    permanent loss."""
    if not notify.configured():
        return 0

    def _fetch_and_route() -> int:
        last = _read_offset(cfg)
        updates = notify.get_updates(offset=(last + 1) if last is not None else None, timeout=0)
        handled = 0
        for uid, text, origin in notify.incoming_texts(updates, cfg):
            if origin == "ops":
                try:
                    if route_message(cfg, audit, text):
                        handled += 1
                except Exception as exc:  # noqa: BLE001
                    # A message that fails DETERMINISTICALLY must not wedge the queue behind it:
                    # poll_loop swallows and retries every 5s, so leaving this update un-acked
                    # would re-explode on it forever and no later message would ever be seen.
                    # Report it and consume it. A process CRASH takes the other path — it never
                    # reaches the ack below, so Telegram redelivers (which is the EU-372 fix).
                    try:
                        if audit is not None:
                            audit.record("telegram_route_failed", update_id=uid,
                                         text=(text or "")[:300], error=str(exc))
                    except Exception:  # noqa: BLE001 - audit failure must not break the poller
                        pass
                    notify.send(f"⚠️ couldn't handle your message ({exc}) — it was dropped, "
                                "please re-send it.")
            # else: defensive only — incoming_texts emits nothing but "ops" since the EU-65 liaison
            # channel was DELETED (Phase-2 §2, 2026-07-06). Anything else is dropped, never routed,
            # but is still acked so it can't re-serve forever.
            if uid is not None:
                _write_offset(cfg, uid)
        return handled

    return locking.locked_call(_offset_file(cfg), _fetch_and_route)


def should_poll_telegram(cfg) -> tuple[bool, str]:
    """EU-185 (Wave 0): decide whether THIS host should run the Telegram poller, returning
    (poll, reason). Telegram getUpdates+offset is single-consumer — two hosts polling one bot
    token split/lose the Commander's messages (the VPS's always-on poller + any Mac `./general
    serve` with `.env`). The hosts only share state via periodic git sync, so a live lock file
    can't give real-time mutual exclusion; we elect ONE poller host instead.

    Rules, in order:
      1. Not configured (no bot token / chat id) → don't poll.
      2. Explicit env GENERAL_TELEGRAM_POLLER (1/true/yes ↔ 0/false/no) → honour it (the escape
         hatch for a single-host dev box).
      3. Else poll iff this host's sync id == cfg.telegram_poller_host (default "server").
    """
    from . import notify
    if not notify.configured():
        return False, "telegram not configured"
    override = os.environ.get("GENERAL_TELEGRAM_POLLER")
    if override is not None and override.strip() != "":
        on = override.strip().lower() in ("1", "true", "yes", "on")
        return on, f"GENERAL_TELEGRAM_POLLER={override.strip()}"
    from . import sync
    hid = sync.host_id(cfg)
    poller = getattr(cfg, "telegram_poller_host", "server")
    if hid == poller:
        return True, f"host '{hid}' is the elected poller"
    return False, f"host '{hid}' is not the poller ('{poller}' owns it; set GENERAL_TELEGRAM_POLLER=1 to override)"


def poll_loop(cfg, audit, interval: int = 5, stop_event: threading.Event | None = None) -> None:
    """Background loop for the control panel: watch Telegram for decision replies.

    EU-257: accepts an optional ``stop_event`` so a poller owned by a finite run (a CLI
    ``autopilot`` invocation, or a cockpit per-app drain) stands down when THAT run stops,
    instead of running forever regardless of who started it. With no stop_event (the ``serve``
    startup path, whose natural lifetime IS the poller's) it runs until the process exits, same
    as before. On exit it clears the module-level singleton registration (see
    :func:`ensure_poll_loop`) so a later start can re-establish a poller rather than finding a
    dead thread wedged in the registry forever."""
    me = threading.current_thread()
    try:
        while not (stop_event is not None and stop_event.is_set()):
            try:
                poll_once(cfg, audit)
            except Exception:  # noqa: BLE001 - never let the listener die
                pass
            if stop_event is not None:
                stop_event.wait(interval)
            else:
                time.sleep(interval)
    finally:
        _clear_poller_registration(me)


# EU-257: process-level poller singleton. should_poll_telegram elects ONE HOST (EU-185); this
# tracks the ONE live poll_loop thread on that host. Without it, every spawn site that passes
# should_poll_telegram (serve's startup, plus each autopilot()/cockpit-drain invocation on the
# same elected host) starts its own poll_loop thread — serve's poller plus one more per drain
# Start — all racing on the same unlocked offset file.
_poller_lock = threading.Lock()
_poller_thread: threading.Thread | None = None


def _clear_poller_registration(thread: threading.Thread) -> None:
    """Drop the module-level registration IFF it still points at ``thread`` — called from
    poll_loop's finally on exit. The identity check means a thread that lost the singleton race
    (never actually registered) can't accidentally clear a DIFFERENT, currently-live poller."""
    global _poller_thread
    with _poller_lock:
        if _poller_thread is thread:
            _poller_thread = None


def ensure_poll_loop(cfg, audit, stop_event: threading.Event | None = None,
                      interval: int = 5) -> threading.Thread | None:
    """Start the ONE process-level Telegram poller, or no-op if one is already live.

    EU-257: the single entry point BOTH spawn sites (serve.py's startup and every
    autopilot()/cockpit-drain invocation) must call, instead of unconditionally starting their
    own ``threading.Thread(target=poll_loop, ...)``. Re-checks :func:`should_poll_telegram`
    itself (cheap, side-effect-free) so it's safe to call unconditionally; returns ``None``
    without touching the registry if this host shouldn't poll at all. Otherwise, under
    ``_poller_lock``, returns the existing thread if one is alive, else starts and registers a
    new daemon thread named ``"telegram-poll-loop"`` and returns it.

    ``stop_event`` is honoured only if THIS call is the one that actually starts the poller —
    a no-op call (a poller already live) does not retroactively attach a new stop_event to the
    running thread. That matches the design: the poller's lifetime is owned by whichever run
    started it first (serve's own lifetime if serve started first; a drain's stop_event if a
    drain started first), and repeated Starts on the same host never grow the thread count.
    """
    poll, _reason = should_poll_telegram(cfg)
    if not poll:
        return None
    with _poller_lock:
        global _poller_thread
        if _poller_thread is not None and _poller_thread.is_alive():
            return _poller_thread
        t = threading.Thread(
            target=poll_loop, args=(cfg, audit),
            kwargs={"interval": interval, "stop_event": stop_event},
            name="telegram-poll-loop", daemon=True,
        )
        _poller_thread = t
        t.start()
        return t
