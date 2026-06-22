"""Two-way decisions — the General asks, you answer in Telegram, it resumes.

When the Inspector flags a product/scope decision (`needs_human`), the loop records
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

from . import notify
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
    _store(cfg).write_text(json.dumps(items, indent=2))


def add(cfg, ticket: Ticket, app_name: str, question: str) -> None:
    items = load(cfg)
    # de-dupe by ticket id
    items = [i for i in items if i.get("id") != ticket.id]
    items.append({
        "id": ticket.id, "app": app_name, "question": question,
        "summary": ticket.summary, "description": ticket.description,
        "acceptance": ticket.acceptance_criteria, "ephemeral": ticket.ephemeral,
        "ts": time.time(),
    })
    _save(cfg, items)


def resolve(cfg, answer: str, ticket_id: str | None = None) -> dict | None:
    """Pop and return the matching pending decision (by id, else oldest)."""
    items = load(cfg)
    if not items:
        return None
    idx = 0
    if ticket_id:
        idx = next((i for i, it in enumerate(items)
                    if it["id"].lower() == ticket_id.lower()), None)
        if idx is None:
            return None
    it = items.pop(idx)
    _save(cfg, items)
    it["answer"] = answer
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


def handle_reply(cfg, audit, text: str) -> bool:
    """Resolve a pending decision from a reply and re-run the ticket. Returns True
    if a ticket was resumed."""
    ticket_id, answer = parse_reply(text)
    resolved = resolve(cfg, answer, ticket_id)
    if not resolved:
        return False
    notify.send(f"▶️ Resuming {resolved['id']} with your decision: {answer}")
    audit.record("decision_resumed", ticket_id=resolved["id"], answer=answer)
    from .loop import run as run_loop   # lazy import avoids a cycle
    worklist = to_worklist(cfg, resolved)
    try:
        asyncio.run(run_loop(cfg, worklist, audit))
    except Exception as exc:  # noqa: BLE001
        notify.send(f"⚠️ Resume of {resolved['id']} failed: {exc}")
    return True


def _run_bg(cfg, audit, worklist) -> None:
    from .loop import run as run_loop

    def _bg():
        try:
            asyncio.run(run_loop(cfg, worklist, audit))
        except Exception as exc:  # noqa: BLE001
            notify.send(f"⚠️ run failed: {exc}")
    threading.Thread(target=_bg, daemon=True).start()


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
        notify.send("🎖️ Drillmaster working…")

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
            notify.send(f"▶️ Starting {len(wl)} ticket(s) {'(LIVE)' if live else '(dry-run)'}…")
            _run_bg(rcfg, audit, wl)
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
    if load(cfg):
        return handle_reply(cfg, audit, text)
    # Otherwise: a free-text message (e.g. a reply to a council question). The General
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
            notify.send(f"⚠️ the General couldn't reply ({exc}); logged your note.")
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
