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

    Returns the stored entry id — the newly written one on a fresh park, or the EXISTING entry's
    id on a dedup hit (no new row). It does NOT return None to flag a dedup hit, so a caller that
    must page the Commander only on a GENUINELY new park cannot use `is not None`: snapshot the
    parked ids via load() before calling and notify only when the returned id is absent from that
    snapshot (see loop.py's findings-decisions route and eu89_stateful_chat_test._park_and_notify).
    In practice the return is always a non-None id; the ``| None`` annotation is permissive only."""
    # NB: decisions.add is a faithful storage primitive — it records whatever the routing layer hands
    # it (the out-of-scope PROPOSE-FIRST proposal, the EU-83 resume payload, a needs_human ask) and must
    # never silently drop a write. EU-92's "PM owns routine escalations" is enforced UPSTREAM (the PM
    # prompt self-resolves the routine classes; loop._route_out_of_scope auto-files out-of-scope findings
    # instead of paging) — gutting this primitive would just lose the entries those paths depend on.
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


def resolve(cfg, answer: str, ticket_id: str | None = None, *, comment: bool = True) -> dict | None:
    """Pop and return the matching pending decision (by id, else oldest).

    EU-61: by default also posts the answer back to the tracker as a comment (the Commander answered in
    Telegram → echo the decision onto the Jira ticket). Pass ``comment=False`` when the answer ALREADY
    came from a Jira comment (the autopilot's Jira-native resume), so it isn't echoed back."""
    popped: list[dict] = []

    def _mutate(items):
        items = list(items or [])
        if not items:
            return items
        idx = 0
        if ticket_id:
            idx = next((i for i, it in enumerate(items)
                        if it["id"].lower() == ticket_id.lower()), None)
            if idx is None:
                return items   # no match — leave the store untouched
        popped.append(items.pop(idx))
        return items

    # Find-and-remove in one locked read-modify-write so a concurrent add/resolve can't lose a
    # decision or hand the same one to two replies.
    locking.locked_rmw(_store(cfg), _mutate, default=[])
    if not popped:
        return None
    it = popped[0]
    it["answer"] = answer
    if comment:
        _comment_answer(cfg, it, answer)
    return it


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
    if a ticket was resumed."""
    ticket_id, answer = parse_reply(text)
    resolved = resolve(cfg, answer, ticket_id)
    if not resolved:
        return False
    notify.send(f"▶️ Resuming {resolved['id']} with your decision: {answer}")
    audit.record("decision_resumed", ticket_id=resolved["id"], answer=answer)
    # EU-83: out-of-scope sub-decisions must NOT re-run the original build — the '#out-of-scope'
    # id is an internal discriminator that would produce 404/405 REST calls. Instead, file the
    # findings from the stored report and return without queuing a rebuild.
    if str(resolved.get("id", "")).endswith("#out-of-scope"):
        _file_out_of_scope_resume(cfg, resolved)
        return True
    worklist = to_worklist(cfg, resolved)
    # Run the resumed build in a background thread so we never block the Telegram
    # poll thread — /unblock and other replies keep being processed meanwhile.
    _run_bg(cfg, audit, worklist)
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
                    "/drill — train the unit\n/council [topic] — deep WEEKLY council\n"
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
                asyncio.run(council.daily_brief(cfg, audit=audit))
            except Exception as exc:  # noqa: BLE001
                notify.send(f"⚠️ daily failed: {exc}")
        threading.Thread(target=_dly, daemon=True).start()
    elif cmd == "standup":
        notify.send(D.standup(cfg))
    elif cmd == "status":
        notify.send(D.render_status(D.load_tasks(cfg.audit_path), limit=10, show_cost=False))
    elif cmd == "drill":
        notify.send("🎖️ Engineering Coach working…")

        def _d():
            try:
                from . import drillmaster
                rep = asyncio.run(drillmaster.drill(cfg))
                Path(cfg.audit_path).with_name("drill-report.md").write_text(rep, encoding="utf-8")
                notify.send("🎖️ Drill report:\n\n" + rep[:3500])
            except Exception as exc:  # noqa: BLE001
                notify.send(f"⚠️ drill failed: {exc}")
        threading.Thread(target=_d, daemon=True).start()
    elif cmd == "council":
        notify.send("🎖️ Convening the daily council…")

        def _c():
            try:
                from . import council
                asyncio.run(council.hold_council(cfg, topic=arg or None, audit=audit))
            except Exception as exc:  # noqa: BLE001
                notify.send(f"⚠️ council failed: {exc}")
        threading.Thread(target=_c, daemon=True).start()
    elif cmd == "unblock":
        from . import autopilot as ap_mod
        notify.send("▶️ " + ap_mod.unblock(cfg, arg or None) + " — autopilot will retry it.")
    elif cmd in ("run", "drain"):
        import copy
        live = "--live" in arg
        arg = arg.replace("--live", "").strip()
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
    number of replies handled."""
    if not notify.configured():
        return 0
    last = _read_offset(cfg)
    updates = notify.get_updates(offset=(last + 1) if last is not None else None, timeout=0)
    handled = 0
    for uid, text, origin in notify.incoming_texts(updates, cfg):
        if uid is not None:
            _write_offset(cfg, uid)
        if origin != "ops":
            # Defensive only: incoming_texts emits nothing but "ops" since the EU-65 liaison
            # channel was DELETED (Phase-2 §2, 2026-07-06). Anything else is dropped, never routed.
            continue
        if route_message(cfg, audit, text):
            handled += 1
    return handled


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


def poll_loop(cfg, audit, interval: int = 5) -> None:
    """Background loop for the control panel: watch Telegram for decision replies."""
    while True:
        try:
            poll_once(cfg, audit)
        except Exception:  # noqa: BLE001 - never let the listener die
            pass
        time.sleep(interval)
