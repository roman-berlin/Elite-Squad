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
import json
import threading
import time
from pathlib import Path

from . import locking, notify
from .contracts import Ticket


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
        *, block: bool = True) -> None:
    """Record a pending decision for the cockpit 'Needs you'. `entry_id` overrides the storage/de-dup
    key (defaults to the ticket id); pass a distinct key — e.g. f'{ticket.id}#out-of-scope' (EU-42) —
    when one ticket carries more than one kind of pending decision, so they don't overwrite each other.

    EU-61 (decision round-trip): parking a ticket's MAIN decision also transitions it to 'Blocked' on
    the tracker, so the open question is visible in Jira and the Commander can answer it right there, and
    snapshots the latest human comment as a resume *baseline* (the autopilot uses it to tell a NEW Jira
    answer from a pre-existing comment). A SUB-decision carrying a distinct ``entry_id`` (the out-of-scope
    proposal) rides alongside without changing the ticket's status; pass ``block=False`` for a hand-back
    that owns its own status (e.g. the readiness gate → 'Needs Human')."""
    eid = entry_id or ticket.id
    entry = {
        "id": eid, "app": app_name, "question": question,
        "summary": ticket.summary, "description": ticket.description,
        "acceptance": ticket.acceptance_criteria, "ephemeral": ticket.ephemeral,
        "ts": time.time(),
    }
    # Only the ticket's own (main) decision parks it to 'Blocked'; a distinct-entry_id sub-decision
    # must not move the ticket's status out from under an in-flight build.
    if block and (entry_id is None or entry_id == ticket.id):
        baseline = _park_on_tracker(cfg, ticket, app_name)
        if baseline is not None:
            entry["answer_baseline"] = baseline

    def _mutate(items):
        items = [i for i in (items or []) if i.get("id") != eid]   # de-dupe by entry id
        items.append(entry)
        return items

    # One atomic read-modify-write so a concurrent add/resolve can't drop this decision.
    locking.locked_rmw(_store(cfg), _mutate, default=[])


def _park_on_tracker(cfg, ticket: Ticket, app_name: str) -> str | None:
    """Transition a parked ticket to 'Blocked' (the visible state of a decision round-trip, EU-61) and
    return the latest human comment currently on it — the baseline the autopilot compares against to
    detect a NEW Commander answer. Best-effort: a no-op (returns None) for dry-run, ephemeral
    (trackerless) tickets, and apps with no backlog; never raises, so a tracker hiccup can't break the
    escalation path."""
    if getattr(cfg, "dry_run", False) or getattr(ticket, "ephemeral", False):
        return None
    try:
        app = cfg.app(app_name)
        if getattr(app, "backlog_backend", "none") == "none":
            return None
        from .backlog.base import make_backlog
        backlog = make_backlog(app)
        backlog.set_status(ticket, "Blocked")
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
    """Rebuild the ticket with the Commander's decision appended, ready to re-run."""
    app = cfg.app(resolved["app"])
    desc = (resolved.get("description") or resolved["summary"])
    desc += f"\n\nCommander's decision on the open question: {resolved['answer']}"
    ticket = Ticket(
        id=resolved["id"], key=resolved["id"], summary=resolved["summary"],
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


def handle_reply(cfg, audit, text: str) -> bool:
    """Resolve a pending decision from a reply and re-run the ticket. Returns True
    if a ticket was resumed."""
    ticket_id, answer = parse_reply(text)
    resolved = resolve(cfg, answer, ticket_id)
    if not resolved:
        return False
    notify.send(f"▶️ Resuming {resolved['id']} with your decision: {answer}")
    audit.record("decision_resumed", ticket_id=resolved["id"], answer=answer)
    worklist = to_worklist(cfg, resolved)
    # Run the resumed build in a background thread so we never block the Telegram
    # poll thread — /unblock and other replies keep being processed meanwhile.
    _run_bg(cfg, audit, worklist)
    return True


def _run_bg(cfg, audit, worklist, *, refuse_if_busy: bool = False) -> bool:
    """Run a worklist in a background thread, claiming the cockpit run-guard (``_state["active"]``).

    Mirrors the run POSTs in ``server.py``: the guard is set under ``_run_lock`` so the War Room badge
    reflects this run and a concurrent cockpit run refuses. Returns True when the run was started.

    ``refuse_if_busy=True`` (Telegram ``/run`` / ``/drain``): if a run/loop is already active, REFUSE
    rather than start an overlapping run on the same app — notify and return False.

    ``refuse_if_busy=False`` (decision resume): a resumed escalation is flock-safe (``loop.py``) and must
    proceed even while autopilot is live — that is the always-on case, and the pending decision has
    already been popped, so dropping it would lose the Commander's answer. It still claims the badge flag
    when it's free, but never refuses.
    """
    from .cockpit_state import _run_lock, _state
    from .loop import run as run_loop

    owns_guard = False
    with _run_lock:
        if _state.get("active"):
            if refuse_if_busy:
                notify.send("⏳ A run is already in progress — try again once it finishes.")
                return False
        else:
            _state["active"] = True
            owns_guard = True

    def _bg():
        try:
            asyncio.run(run_loop(cfg, worklist, audit))
        except Exception as exc:  # noqa: BLE001
            notify.send(f"⚠️ run failed: {exc}")
        finally:
            if owns_guard:
                _state["active"] = False
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
        notify.send("Commands:\n/standup — daily report\n/status — recent tasks\n"
                    "/drill — train the unit\n/council [topic] — convene the daily council\n"
                    "/run <app> <what to build> [--live]\n"
                    "/drain <app> [--live] — work your To-Do queue\n"
                    "/unblock <id> — retry a parked (escalated) ticket\n"
                    "Reply  TICKET: <decision>  to answer a question, or send any note and "
                    "I'll log it as standing guidance for the unit.")
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
    for uid, text in notify.incoming_texts(updates):
        if uid is not None:
            _write_offset(cfg, uid)
        if route_message(cfg, audit, text):
            handled += 1
    return handled


def poll_loop(cfg, audit, interval: int = 5) -> None:
    """Background loop for the control panel: watch Telegram for decision replies."""
    while True:
        try:
            poll_once(cfg, audit)
        except Exception:  # noqa: BLE001 - never let the listener die
            pass
        time.sleep(interval)
