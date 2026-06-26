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

from . import events, intake, locking, notify, usage
from .audit import AuditLog
from .config import Config
from .contracts import Outcome
from .loop import run as run_loop

# Outcomes that park a ticket IMMEDIATELY (a human decision / a PR is waiting — no point retrying).
# ERRORED is handled separately: a transient blip shouldn't sideline a ticket, so we retry it a few
# times (with a short backoff) before parking. See _MAX_TICKET_ERRORS.
_PARKED = (Outcome.ESCALATED, Outcome.PR_OPENED)

# Consecutive ERRORs tolerated before an errored ticket is parked. The first errors are retried
# next cycle; the Nth consecutive error parks it. Counter resets the moment the ticket stops
# erroring (a success or any other progress). Trade-off: a genuinely broken ticket wastes a
# couple of passes before parking.
_MAX_TICKET_ERRORS = 3
_ERROR_BACKOFF_SEC = 10   # short pause before re-picking a transiently-errored ticket next cycle

# Local git state meaning the Commander is editing history by hand RIGHT NOW (a rebase/merge in
# progress). While that's true the autopilot must NOT ff-push origin/<base> — doing so is exactly what
# moves dev under him and turns his `git pull --rebase` into a non-fast-forward (he hit this 3×). These
# marker files only exist on the machine running the operation, so the check naturally no-ops on the
# unattended server (which has no Commander doing manual git).
_GIT_BUSY_MARKERS = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")


def _commander_mid_git(cfg) -> str | None:
    """Name of a local repo where the Commander is mid-Git (rebase/merge/cherry-pick), else None."""
    seen: set[str] = set()
    for app in getattr(cfg, "apps", []) or []:
        repo = getattr(app, "repo_path", "") or ""
        if not repo or repo in seen:
            continue
        seen.add(repo)
        gitdir = Path(repo) / ".git"
        if gitdir.is_dir() and any((gitdir / m).exists() for m in _GIT_BUSY_MARKERS):
            return app.name
    return None


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
    # Authoritative overwrite, but taken under the shared cross-thread + cross-process lock so it can't
    # interleave with a concurrent write (the Telegram poller's /unblock) and lose one side's update.
    snapshot = sorted(blocked)
    try:
        locking.locked_rmw(_blocked_file(cfg), lambda _current: snapshot, default=[])
    except (OSError, ValueError):
        pass


def _error_counts_file(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("error_counts.json")


def load_error_counts(cfg: Config) -> dict[str, int]:
    """Per-ticket count of CONSECUTIVE ERRORs, so a transient blip is retried (not parked)."""
    p = _error_counts_file(cfg)
    try:
        data = json.loads(p.read_text())
        return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
        return {}


def save_error_counts(cfg: Config, counts: dict[str, int]) -> None:
    try:
        _error_counts_file(cfg).write_text(json.dumps(counts, indent=2, sort_keys=True))
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


def _learn_from_cycle(cfg: Config, reports, audit) -> dict:
    """After a productive cycle, fold any new recurring rejection-lessons into Unit Memory and prune it
    — FREE + deterministic (no model call), so memory compounds every cycle instead of only at the
    06:30 council. The full model-Technical Writer stays on the council cadence. Best-effort: memory hygiene must
    never break the loop. Returns the consolidate report ({} when there was nothing to learn from)."""
    if not reports:
        return {}
    try:
        from . import consolidate
        cr = consolidate.run(cfg)
        if cr.get("added"):
            audit.record("memory_learn", added=len(cr["added"]), pruned=cr.get("pruned", 0))
            print(f"  · memory: folded {len(cr['added'])} new lesson(s) learned this cycle", flush=True)
        return cr
    except Exception as exc:  # noqa: BLE001 - never let memory hygiene sideline the worker
        return {"error": str(exc)}


async def autopilot(cfg: Config, app_name: str | None = None,
                    once: bool = False, interval: int = 60, stop_event=None) -> None:
    audit = AuditLog(cfg.audit_path)
    blocked = load_blocked(cfg)
    error_counts = load_error_counts(cfg)   # per-ticket consecutive-ERROR tally (retry-before-park)
    cap = max(1, cfg.max_tickets_per_run)
    mode = "DRY-RUN" if cfg.dry_run else ("LIVE · automode" if getattr(cfg, "auto_mode", False) else "LIVE")

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
    budget_paused = False    # so the "paused" / "80%" notices each fire once, not every loop
    budget_alerted = False
    # Last-announced idle REASON, as (bool(unreachable), frozenset(unreachable boards)) — or None when
    # not idling. Keying on the reason (not a bare "already announced" flag) is the EU-50 fix: a board
    # going dark AFTER the queue idled clear is a state change that must push once.
    idle_state: tuple[bool, frozenset[str]] | None = None
    git_held = False         # hold (once-announced) while the Commander is mid-rebase/merge locally
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                print("🛸 Autopilot stood down (stopped from the cockpit).", flush=True)
                break

            # Cost governor: never let a runaway loop eat the day's token budget. Pause new tickets
            # once today's burn hits the ceiling (resumes after midnight / when the ceiling is raised).
            bs = usage.budget_status(cfg)
            if bs["over"]:
                if not budget_paused:   # announce the pause ONCE — not once per idle cycle (was log spam)
                    notify.send(f"⛔ Autopilot paused — daily token budget reached "
                                f"({bs['used']:,}/{bs['cap']:,}). Resumes after midnight, or raise "
                                f"`daily_token_budget`.")
                    audit.record("budget_pause", used=bs["used"], cap=bs["cap"])
                    print(f"  · token budget reached ({bs['used']:,}/{bs['cap']:,} today) — holding new "
                          "tickets (resumes after midnight, or raise daily_token_budget).", flush=True)
                    budget_paused = True
                if once:
                    break
                _sleep(max(30, interval), stop_event)
                continue
            budget_paused = False
            if bs["alert"] and not budget_alerted:
                notify.send(f"⚠️ Token budget {int(bs['pct'] * 100)}% used today "
                            f"({bs['used']:,}/{bs['cap']:,}).")
                budget_alerted = True
            elif not bs["alert"]:
                budget_alerted = False

            # Don't race the Commander's manual git. While he's mid-rebase/merge in a local checkout,
            # stand down for a cycle instead of ff-pushing origin/<base> and turning his pull into a
            # non-fast-forward. Builds aren't started, so nothing lands until his tree is clean again.
            busy_repo = _commander_mid_git(cfg)
            if busy_repo:
                if not git_held:
                    notify.send(f"✋ Autopilot holding — you're mid-Git in {busy_repo} (rebase/merge). "
                                f"I won't move its branch under you; resumes once it's clean.")
                    print(f"  ✋ holding — Commander mid-Git in {busy_repo}; not landing so I don't race your "
                          "rebase.", flush=True)
                    audit.record("git_hold", app=busy_repo)
                    git_held = True
                if once:
                    break
                _sleep(max(10, interval), stop_event)
                continue
            git_held = False

            blocked = load_blocked(cfg)   # re-read so /unblock takes effect live
            worklist = intake.from_drain(cfg, app_name, cap + len(blocked) + 5)
            worklist = [(a, t) for (a, t) in worklist if t.id not in blocked][:cap]

            if not worklist:
                # An empty worklist is NOT necessarily a clear queue: a board that failed to drain
                # (bad/expired token, network, renamed project) yields zero items too. Surface that as
                # UNREACHABLE instead of the misleading "queue clear — nothing of yours", which is exactly
                # what hid EU's whole To Do column behind a dead JIRA_API_TOKEN. (Jira answers an
                # unauthenticated search with HTTP 200 + no issues — see backlog/jira._raise_if_unauthenticated.)
                unreachable = dict(intake.LAST_DRAIN_ERRORS)
                # Re-announce on ANY transition: clear↔unreachable, or a changed set of dark boards. The
                # old single `idle_announced` flag was set True by whichever branch fired and only reset
                # when work appeared — so a queue that idled clear and THEN went dark (Jira token expired
                # → empty worklist again) stayed True and silently skipped the UNREACHABLE alert (EU-50).
                state = (bool(unreachable), frozenset(unreachable))
                if state != idle_state:   # say each distinct idle reason once, then stay quiet
                    if unreachable:
                        boards = "; ".join(f"{name} — {msg}" for name, msg in unreachable.items())
                        print(f"  ⚠ NOT a clear queue: {len(unreachable)} board(s) UNREACHABLE this cycle, "
                              f"so your backlog is HIDDEN, not empty → {boards}", flush=True)
                        notify.send(f"⚠️ Autopilot can't read your backlog — {len(unreachable)} Jira "
                                    f"board(s) unreachable:\n{boards}")
                    else:
                        print("  · queue clear — nothing of yours in In Progress / To Do"
                              + (f" (parked: {', '.join(sorted(blocked))})" if blocked else "")
                              + " — idling; I'll pick up new or unblocked tickets automatically.", flush=True)
                    idle_state = state
                if once:
                    break
                await events.after_cycle(cfg, [], audit, blocked)   # quiet cycle — room for life
                _sleep(max(5, interval), stop_event)
                continue
            idle_state = None   # work again → re-announce next time the queue empties

            ids = ", ".join(t.id for _, t in worklist)
            print(f"  · taking {ids}", flush=True)
            reports = await run_loop(cfg, worklist, audit)

            # Park ESCALATED / PR_OPENED immediately. For ERRORED, retry a few times before
            # parking so a transient blip doesn't sideline a ticket for hours.
            park_now = [r.ticket_id for r in reports if r.outcome in _PARKED]
            retrying: list[str] = []
            counts_changed = False
            for r in reports:
                if r.outcome is Outcome.ERRORED:
                    n = error_counts.get(r.ticket_id, 0) + 1
                    if n >= _MAX_TICKET_ERRORS:
                        park_now.append(r.ticket_id)
                        error_counts.pop(r.ticket_id, None)   # parked -> reset for a future /unblock
                    else:
                        error_counts[r.ticket_id] = n
                        retrying.append(r.ticket_id)
                    counts_changed = True
                elif error_counts.pop(r.ticket_id, None) is not None:
                    counts_changed = True   # made progress (didn't error) -> reset its tally
            if counts_changed:
                save_error_counts(cfg, error_counts)

            if retrying:
                print(f"  · ERRORED, retrying next cycle (not parked): "
                      + ", ".join(f"{t} [{error_counts[t]}/{_MAX_TICKET_ERRORS}]" for t in retrying),
                      flush=True)

            # Re-read straight from disk right before the write-back instead of trusting the snapshot
            # taken at the top of the loop (~:219). The Telegram poller may have run /unblock mid-cycle;
            # merging our new parks into the *stale* snapshot would silently resurrect a ticket the
            # Commander just unblocked. (error_counts was already persisted just above, so this reload
            # is a no-op for it — but keeps both in lock-step with the on-disk truth.)
            blocked = load_blocked(cfg)
            error_counts = load_error_counts(cfg)
            newly = [t for t in park_now if t not in blocked]
            if newly:
                blocked.update(newly)
                save_blocked(cfg, blocked)
                notify.send("⏸️ Parked (need you): " + ", ".join(newly)
                            + "\nReply /unblock <id> once handled and I'll retry it.")
            _learn_from_cycle(cfg, reports, audit)   # fold this cycle's lessons into memory (free)
            await events.after_cycle(cfg, reports, audit, blocked)   # the unit may convene itself
            if once:
                break
            # A transient error gets a short backoff before the next look; otherwise a brief breath.
            _sleep(_ERROR_BACKOFF_SEC if retrying else 3, stop_event)
    except KeyboardInterrupt:
        print("\n🛸 Autopilot stood down. Nothing left mid-flight.", flush=True)
    audit.record("autopilot_stop")
