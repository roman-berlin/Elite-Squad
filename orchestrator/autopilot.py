"""Autopilot — the always-on Jira worker.

Continuously: **resume what's In Progress, else take the top To Do** (assigned to you, by
board Rank), build -> review -> land on DEV -> move to QA. Then loop. Tickets that escalate
or error are PARKED in a skip-set so the loop never spins on a stuck ticket — you clear them
with /unblock once handled. While it runs it also listens to Telegram (so /unblock, /council
and decision replies work), making it a single always-on brain.

  general --live autopilot automatixy           # run continuously (Ctrl-C to stop)
  general --live autopilot automatixy --once     # one cycle (handy for a live test)
  general autopilot automatixy --once            # dry-run a single cycle (no merge/QA writes)

Continuous mode needs --live: in dry-run nothing actually moves to QA, so the same ticket
would be re-picked every cycle. Use --once for dry-run checks.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import intake, notify
from .audit import AuditLog
from .config import Config
from .contracts import Outcome
from .loop import run as run_loop

_PARKED = (Outcome.ESCALATED, Outcome.PR_OPENED, Outcome.ERRORED)


def _sleep(seconds: float, stop_event=None) -> None:
    """Sleep, but wake immediately if asked to stop (so the cockpit toggle is responsive)."""
    end = time.time() + seconds
    while time.time() < end:
        if stop_event is not None and stop_event.is_set():
            return
        time.sleep(min(1.0, max(0.0, end - time.time())))


def _blocked_file(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("blocked_tickets.json")


def load_blocked(cfg: Config) -> set[str]:
    p = _blocked_file(cfg)
    try:
        return set(json.loads(p.read_text()))
    except (OSError, json.JSONDecodeError):
        return set()


def save_blocked(cfg: Config, blocked: set[str]) -> None:
    try:
        _blocked_file(cfg).write_text(json.dumps(sorted(blocked), indent=2))
    except OSError:
        pass


def unblock(cfg: Config, ticket_id: str | None = None) -> str:
    """Clear a parked ticket (or all). Autopilot will retry it next cycle."""
    blocked = load_blocked(cfg)
    if not blocked:
        return "nothing parked"
    if ticket_id:
        hit = {b for b in blocked if b.lower() == ticket_id.lower()}
        blocked -= hit
        save_blocked(cfg, blocked)
        return f"unblocked {ticket_id}" if hit else f"{ticket_id} was not parked"
    save_blocked(cfg, set())
    return f"unblocked all ({len(blocked)})"


async def autopilot(cfg: Config, app_name: str | None = None,
                    once: bool = False, interval: int = 60, stop_event=None) -> None:
    audit = AuditLog(cfg.audit_path)
    blocked = load_blocked(cfg)
    cap = max(1, cfg.max_tickets_per_run)
    mode = "DRY-RUN" if cfg.dry_run else "LIVE"

    # Single always-on brain: also listen to Telegram (/unblock, /council, decision replies).
    if notify.configured():
        from . import decisions
        threading.Thread(target=decisions.poll_loop, args=(cfg, audit), daemon=True).start()

    scope = app_name or "all backlog apps"
    notify.send(f"🛸 Autopilot {mode} online — working {scope}")
    print(f"🛸 Autopilot {mode} — {scope}. Ctrl-C to stop.", flush=True)
    if not cfg.dry_run and not once:
        pass
    elif cfg.dry_run and not once:
        print("  ⚠ continuous + dry-run would re-pick the same ticket forever; use --live for "
              "continuous, or keep --once for a dry test.", flush=True)

    audit.record("autopilot_start", mode=mode, app=app_name, once=once)
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                print("🛸 Autopilot stood down (stopped from the cockpit).", flush=True)
                break
            blocked = load_blocked(cfg)   # re-read so /unblock takes effect live
            worklist = intake.from_drain(cfg, app_name, cap + len(blocked) + 5)
            worklist = [(a, t) for (a, t) in worklist if t.id not in blocked][:cap]

            if not worklist:
                print("  · queue clear — nothing of yours in In Progress / To Do"
                      + (f" (parked: {', '.join(sorted(blocked))})" if blocked else ""), flush=True)
                if once:
                    break
                _sleep(max(5, interval), stop_event)
                continue

            ids = ", ".join(t.id for _, t in worklist)
            print(f"  · taking {ids}", flush=True)
            reports = await run_loop(cfg, worklist, audit)

            newly = [r.ticket_id for r in reports if r.outcome in _PARKED]
            if newly:
                blocked.update(newly)
                save_blocked(cfg, blocked)
                notify.send("⏸️ Parked (need you): " + ", ".join(newly)
                            + "\nReply /unblock <id> once handled and I'll retry it.")
            if once:
                break
            _sleep(3, stop_event)   # brief breath, then look for the next ticket
    except KeyboardInterrupt:
        print("\n🛸 Autopilot stood down. Nothing left mid-flight.", flush=True)
    audit.record("autopilot_stop")
