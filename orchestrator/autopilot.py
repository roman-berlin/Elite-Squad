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
import os
import signal
import threading
import time
from pathlib import Path

from . import events, intake, locking, notify, usage
from .audit import AuditLog
from .config import Config
from .contracts import PARKED, Outcome
from .gate import prebuild_gate
from .loop import run as run_loop

# PID file — single source of truth for "is the daemon actually running?"
# Written at startup and removed on clean exit or SIGTERM. The Mac launchd keepalive daemon
# (scripts/install-mac-autopilot-daemon.sh) relies on this file for external status checks.
_PID_FILE = Path("/tmp/general-autopilot.pid")


def _write_pid() -> None:
    """Write the current process PID to _PID_FILE (best-effort; failure is non-fatal)."""
    try:
        _PID_FILE.write_text(str(os.getpid()))
    except OSError:
        pass


def _remove_pid() -> None:
    """Remove the PID file on clean exit — but ONLY when it still points at THIS process.

    Best-effort (failure is non-fatal). The ownership check hardens the single-instance design: if a
    second autopilot ever overwrote the file with its own PID, this exiting instance must NOT delete it
    — otherwise daemon_running() (the single source of truth for the cockpit badge, EU-73) would read
    'not running' while that other instance is still alive. We own the file only when its contents equal
    os.getpid(); a missing or garbled file simply means there is nothing of ours to remove.
    """
    try:
        if _PID_FILE.read_text().strip() == str(os.getpid()):
            _PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def daemon_running() -> bool:
    """Return True when the autopilot daemon process is alive.

    Reads _PID_FILE and probes the recorded PID with ``os.kill(pid, 0)`` (signal 0 = existence
    check, no signal delivered). Returns False if the file is missing, unreadable, non-numeric,
    or the process is no longer alive — i.e. any I/O or permission error means "not running".

    This is the single source of truth for the cockpit ON/OFF badge (EU-73): it reflects reality
    even when autopilot was launched outside the cockpit process (terminal that was later closed,
    launchd keepalive daemon) where the in-memory ``_state['autopilot']['on']`` flag is stale.
    """
    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, 0)  # probe only — raises OSError(ESRCH) if gone, OSError(EPERM) if alive but not ours
        return True
    except (OSError, ValueError):
        return False


def _pid_file_holds_our_pid() -> bool:
    """True when the autopilot PID file records THIS process's PID.

    A cockpit Start runs autopilot in a background thread of the cockpit process, which writes this
    process's PID via ``_write_pid()``. This lets the per-app Start guard tell its OWN in-process
    autopilot apart from a detached daemon living in a DIFFERENT process."""
    try:
        return _PID_FILE.read_text().strip() == str(os.getpid())
    except OSError:
        return False


def daemon_is_external() -> bool:
    """True only when a FOREIGN process holds the autopilot PID file — a detached terminal run or the
    launchd keepalive — as opposed to this cockpit's own in-process autopilot threads.

    EU-103: the per-app autopilot Start guard uses this INSTEAD of the bare ``daemon_running()`` so a
    cockpit that has already started one project's autopilot (and therefore written THIS process's PID)
    can still start a SECOND project's autopilot — the two cockpit-managed loops are tracked per-app
    and run in parallel. A genuinely detached daemon (different PID) is still refused, since it owns the
    whole queue and this cockpit can't coordinate with it."""
    return daemon_running() and not _pid_file_holds_our_pid()


def _stop_launchd_daemon() -> bool:
    """Stop the launchd KeepAlive daemon using launchctl.

    Tries launchctl bootout (modern macOS) first, then falls back to launchctl unload (older macOS).
    This is the ONLY way to durably stop a KeepAlive daemon — a plain 'launchctl stop' is respawned.
    Returns True on success, False on failure (best-effort: the daemon may already be gone).

    EU-120: cockpit's "Finish & stop" (drain) calls this for external daemons so the stop sticks.
    The plist path matches scripts/install-mac-autopilot-daemon.sh.
    """
    import platform
    import subprocess

    if platform.system() != "Darwin":
        return False

    # Path from install-mac-autopilot-daemon.sh
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.romanberlin.general.autopilot.plist"

    if not plist_path.exists():
        # No plist installed — nothing to unload
        return False

    try:
        # Modern macOS (10.10+): use bootout, which removes the service *and* stops it
        result = subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/com.romanberlin.general.autopilot"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # bootout returns 0 even if the service wasn't running (idempotent)
        return True
    except (OSError, subprocess.TimeoutExpired):
        # bootout failed or not available — fall back to unload (older macOS)
        try:
            result = subprocess.run(
                ["launchctl", "unload", str(plist_path)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


# `PARKED` (the outcomes that park a ticket IMMEDIATELY — a human decision / a PR is waiting, no point
# retrying) is the canonical tuple in contracts.py (EU-56), imported above. ERRORED is handled
# separately: a transient blip shouldn't sideline a ticket, so we retry it a few times (with a short
# backoff) before parking. See _MAX_TICKET_ERRORS.

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


def _auto_clear_merged_ghosts(cfg: Config, blocked: set[str], audit: "AuditLog") -> set[str]:
    """Remove ghost-parked tickets from ``blocked`` — tickets whose latest audit run already succeeded.

    Ghost scenario: a ticket was parked in a previous session (escalated / PR opened), then the
    Commander ran it manually and it merged — but blocked_tickets.json was never cleaned up.
    Result: the parked KPI card over-counted and the row sat there forever. This function is called
    at the start of each autopilot cycle so the count self-heals without manual intervention (EU-78).

    Best-effort: any exception leaves ``blocked`` unchanged so the loop never breaks on cleanup."""
    if not blocked:
        return blocked
    try:
        from . import dashboard as D
        tasks = D.load_tasks(cfg.audit_path)
        # Find the latest run per blocked ticket.
        latest: dict[str, dict] = {}
        for t in tasks:
            tid = str(t.get("ticket_id") or "")
            if tid not in blocked:
                continue
            if tid not in latest or D._started_key(t) >= D._started_key(latest[tid]):
                latest[tid] = t
        # Tickets whose most-recent run merged into DEV are done — drop them from the parked set.
        to_clear = {tid for tid, t in latest.items() if t.get("outcome") == "merged→dev"}
        if to_clear:
            blocked_fresh = load_blocked(cfg)
            blocked_fresh -= to_clear
            save_blocked(cfg, blocked_fresh)
            audit.record("parked_ghost_cleared", tickets=sorted(to_clear))
            print(f"  · auto-cleared ghost-parked: {', '.join(sorted(to_clear))} "
                  f"(latest run already merged)", flush=True)
            return blocked - to_clear
    except Exception:  # noqa: BLE001 — ghost-clearing must never crash the autopilot loop
        pass
    return blocked


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


def _resumable_answered(cfg: Config, app_name: str | None, blocked: set[str]) -> dict:
    """EU-61: parked tickets the Commander has answered DIRECTLY on their Jira ticket → auto-resume.

    For each parked ticket that still has an OPEN pending decision, fetch it by key and compare the
    latest human comment against the baseline snapshotted at park time (decisions.add). A genuinely-new
    answer means the Commander resolved it on Jira (not Telegram), so it should re-enter the develop
    queue without a manual /unblock. Returns ``{ticket_id: (app, ticket)}``. Fetched by key, so it's
    independent of the board's queue_statuses; best-effort per ticket (a wrong-project/network miss just
    skips that ticket this cycle)."""
    from . import decisions
    from .backlog.base import make_backlog
    out: dict = {}
    if not blocked:
        return out
    pending = {d.get("id"): d for d in decisions.load(cfg)}
    targets = [tid for tid in blocked if tid in pending]
    if not targets:
        return out
    apps = [cfg.app(app_name)] if app_name else [a for a in cfg.apps if a.backlog_backend != "none"]
    for app in apps:
        try:
            backlog = make_backlog(app)
        except Exception:  # noqa: BLE001 - a misconfigured/unreachable board must not abort the others
            continue
        for tid in targets:
            if tid in out:
                continue
            try:
                ticket = backlog.get_task(tid)
                answer = backlog.latest_answer(ticket)
            except Exception:  # noqa: BLE001 - wrong project for this board, network, etc.
                continue
            if answer and answer != pending[tid].get("answer_baseline"):
                out[tid] = (app, ticket)
    return out


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
    # Only the MAIN thread may install signal handlers — signal.signal() raises ValueError on any
    # other thread. The cockpit Start button runs autopilot() in a background thread (server.py's
    # _bg), so guard BOTH the SIGTERM registration (below) and its restore (in the finally) to the
    # main thread. EU-73: the previously-unguarded registration crashed every cockpit-started
    # autopilot with `signal only works in main thread of the main interpreter`.
    _on_main_thread = threading.current_thread() is threading.main_thread()
    _orig_sigterm = None
    # Always have a stop Event so SIGTERM (e.g. a launchd unload of the keepalive daemon) can stand
    # the loop down GRACEFULLY — the CLI/launchd daemon path passes none. The loop and _sleep() poll it, so
    # an in-flight ticket finishes landing on DEV before we exit, rather than the daemon swallowing
    # SIGTERM and being SIGKILLed (which would skip the PID-file cleanup in the finally).
    if stop_event is None:
        stop_event = threading.Event()

    def _handle_sigterm(signum, frame):  # noqa: ANN001 — signal-handler signature
        """SIGTERM (e.g. a launchd unload of the keepalive daemon) → graceful stand-down: set the stop Event and let
        the loop notice it, finish any in-flight ticket, and run its finally (PID-file cleanup,
        run-state release). Mirrors the cockpit Stop toggle and Ctrl-C."""
        stop_event.set()

    audit = AuditLog(cfg.audit_path)
    from . import cockpit_state
    from .git_ops import clear_parked_repos
    run_key = app_name or None
    owns_run_state = False    # set True only once claim_run succeeds; gates release in the finally
    run_state = None
    try:
        # Write the PID file FIRST, inside the try, so the finally's _remove_pid() always runs — even
        # if any setup below (the signal registration, claim_run, a Telegram send) raises. Otherwise an
        # early raise would orphan /tmp/general-autopilot.pid pointing at this live process and pin
        # daemon_running() True forever (EU-73 — this is the single source of truth for the cockpit badge).
        _write_pid()
        if _on_main_thread:
            _orig_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _handle_sigterm)

        # EU-128: Clear parked repos ONCE per autopilot run (not every cycle) so notifications
        # fire once per run, not once per cycle. A misconfigured app parks and alerts once;
        # subsequent cycles silently skip it without spamming.
        clear_parked_repos()

        blocked = load_blocked(cfg)
        error_counts = load_error_counts(cfg)   # per-ticket consecutive-ERROR tally (retry-before-park)
        # EU-128: Track tickets previewed in dry-run mode to prevent re-picking them in continuous mode
        previewed_tickets: set[str] = set()
        cap = max(1, cfg.max_tickets_per_run)
        mode = "DRY-RUN" if cfg.dry_run else ("LIVE · automode" if getattr(cfg, "auto_mode", False) else "LIVE")

        # Single always-on brain: also listen to Telegram (/unblock, /council, decision replies).
        if notify.configured():
            from . import decisions
            threading.Thread(target=decisions.poll_loop, args=(cfg, audit), daemon=True).start()

        scope = app_name or "all backlog apps"
        notify.send(f"🛸 Autopilot {mode} online — working {scope}")
        print(f"🛸 Autopilot {mode} — {scope}. Ctrl-C to stop.", flush=True)
        if cfg.dry_run and not once:
            print("  · continuous + dry-run: previewing tickets once, then idling (won't re-pick). "
                  "Use --live for continuous processing, or --once for a single dry test.", flush=True)

        audit.record("autopilot_start", mode=mode, app=app_name, once=once)
        budget_paused = False    # so the "paused" / "80%" notices each fire once, not every loop
        budget_alerted = False
        # EU-118: plan-limit pause flag
        plan_limit_paused = False
        # Last-announced idle REASON, as (bool(unreachable), frozenset(unreachable boards)) — or None
        # when not idling. Keying on the reason (not a bare "already announced" flag) is the EU-50 fix:
        # a board going dark AFTER the queue idled clear is a state change that must push once.
        idle_state: tuple[bool, frozenset[str]] | None = None
        git_held = False         # hold (once-announced) while the Commander is mid-rebase/merge locally

        # EU-64: reflect this autopilot on its OWN project's cockpit run-state (keyed per app), so a
        # per-project board shows autopilot working THIS project without reading/writing another's
        # state. ``app_name=None`` (all backlog apps) maps to the unit-wide default key. We claim the
        # slot for the project; if it's already held — e.g. server.py holds the unit-wide guard for
        # autopilot's whole lifetime, or a manual run owns this app — we run anyway but DON'T own the
        # release, so we never clear or clobber someone else's run-state.
        owns_run_state = cockpit_state.claim_run(run_key, dry_run=cfg.dry_run, stop_event=stop_event)
        run_state = cockpit_state.get_state(run_key)
        # EU-103: flag THIS app's run as an autopilot run (distinct from a manual cockpit/answer-box
        # run, which sets ``active`` but not ``autopilot_on``). This is the dedicated signal the cockpit
        # control reads via get_autopilot_status(app), so a manual run never renders as "Autopilot ON".
        # Set whether or not we own the run-state release (the cockpit Start may already hold the slot);
        # the loop running IS the autopilot, and the finally clears it again.
        run_state["autopilot_on"] = True
        while True:
            run_state["last_activity"] = time.time()   # per-app heartbeat — proves THIS project's loop is alive
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

            # EU-118: Plan-limit governor — halt when Claude Max subscription limits (session/weekly/per-model)
            # are hit. This prevents silent churn where the autopilot spins on rate-limit errors.
            plan_check = usage.plan_limit_hit(cfg)
            if plan_check.get("hit"):
                over_limits = plan_check.get("over_limits", [])

                # Halt autopilot when plan limits are hit (no fallback switch implemented yet)
                if not plan_limit_paused:
                    limit_names = [limit.get("label", limit.get("key", "unknown")) for limit in over_limits]
                    reset_times = list(set(limit.get("resets_in", "unknown") for limit in over_limits))

                    # Send severe Telegram alert (one-shot per session)
                    notify.plan_limit_alert(over_limits, reset_times)

                    # Also send regular notification for logs
                    notify.send(f"⛔ Autopilot paused — Claude plan limit(s) reached: {', '.join(limit_names)}. "
                                f"Resets at: {', '.join(reset_times)}. "
                                f"Resuming could cause API errors and silent churn.")

                    # Set cockpit state so the banner appears
                    try:
                        from . import cockpit_state as _cs
                        # Find the earliest reset timestamp
                        reset_at = None
                        for limit in over_limits:
                            ts = limit.get("resets_at")
                            if ts:
                                epoch = ts if isinstance(ts, (int, float)) else 0
                                if reset_at is None or epoch < reset_at:
                                    reset_at = epoch
                        _cs.set_plan_limit_hit(app_name, hit=True, reset_at=reset_at)
                    except Exception:  # noqa: BLE001 - state update must never break autopilot
                        pass

                    audit.record("plan_limit_pause", over_limits=over_limits)
                    print(f"  · Claude plan limit(s) reached: {', '.join(limit_names)} — holding new tickets "
                          f"to prevent silent churn (resets: {', '.join(reset_times)}).", flush=True)
                    plan_limit_paused = True
                if once:
                    break
                _sleep(max(30, interval), stop_event)
                continue
            # Clear plan-limit state when no longer hit
            if plan_limit_paused:
                try:
                    from . import cockpit_state as _cs
                    _cs.set_plan_limit_hit(app_name, hit=False, reset_at=None)
                except Exception:  # noqa: BLE001
                    pass
                # Reset the plan-limit alert flag so a new alert can be sent if the limit is hit again
                # (e.g., in the next billing period or after the session window rolls over)
                notify.reset_plan_limit_alert()
            plan_limit_paused = False

            # EU-122: Mid-run graceful stop check — finish current ticket, then stop/switch.
            # If a provider has crossed the low-watermark (budget_bad_threshold), we should stop
            # or switch after finishing the current in-flight ticket (never mid-build).
            graceful_check = usage.graceful_stop_check(cfg)
            if graceful_check["should_stop"]:
                critical_provider = graceful_check.get("critical_provider")
                if critical_provider:
                    # Send Telegram alert for the low-watermark crossing (one-shot per provider)
                    if critical_provider == "claude":
                        notify.dual_low_watermark_alert("claude", graceful_check["status"]["claude"])
                    elif critical_provider == "glm":
                        notify.dual_low_watermark_alert("glm", graceful_check["status"]["glm"])

                    # Log the graceful stop condition
                    audit.record("graceful_stop_triggered", provider=critical_provider,
                                reason=graceful_check["reason"])
                    print(f"  · graceful stop: {graceful_check['reason']} — finishing current ticket then stopping.",
                          flush=True)

                    # Hold new tickets (similar to plan limit pause, but with graceful finish)
                    if once:
                        break
                    _sleep(max(10, interval), stop_event)
                    continue

            # Don't race the Commander's manual git. While he's mid-rebase/merge in a local checkout,
            # stand down for a cycle instead of ff-pushing origin/<base> and turning his pull into a
            # non-fast-forward. Builds aren't started, so nothing lands until his tree is clean again.
            busy_repo = _commander_mid_git(cfg)

            # EU-122: Pre-flight budget check — don't start a ticket you can't finish.
            # If either provider is near exhaustion (within the ticket estimate), skip starting new work.
            pre_flight = usage.pre_flight_check(cfg)
            if pre_flight["should_skip"]:
                if not git_held:  # reuse the same one-shot flag
                    notify.send(f"⛔ Autopilot holding — {pre_flight['reason']}. "
                                f"Finishing current work, then stopping/switching to prevent mid-build cutoff.")
                    audit.record("budget_preflight_hold", reason=pre_flight["reason"], provider=pre_flight["provider"])
                    print(f"  · pre-flight budget check: {pre_flight['reason']} — holding new tickets.", flush=True)
                if once:
                    break
                _sleep(max(10, interval), stop_event)
                continue
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
            # EU-78: auto-clear ghost-parked tickets whose latest audit run already succeeded
            # (e.g. parked in a previous session, then ran manually and merged).
            blocked = _auto_clear_merged_ghosts(cfg, blocked, audit)
            # EU-61: a parked ticket the Commander answered directly on Jira auto-resumes — lift it out
            # of the skip-set and put it at the FRONT of the queue (resume before taking new work).
            resumed = _resumable_answered(cfg, app_name, blocked)
            if resumed:
                blocked -= set(resumed)
                save_blocked(cfg, blocked)
                notify.send("▶️ Resuming (answered on Jira): " + ", ".join(sorted(resumed)))
                audit.record("decision_resumed", tickets=sorted(resumed), via="jira-comment")
            # Three-tier worklist assembly (EU-87): In Progress → answered/unblocked → To Do.
            # Pull more than cap so the blocked filter still leaves enough to fill the cap, then cap
            # the DRAWN (new) work here — `cap` bounds only how much fresh backlog a cycle pulls.
            # Answered/resumed tickets (Tier-2 below) are work already in flight that the Commander
            # explicitly replied to, so they ride ON TOP of the cap and are never dropped — this is
            # the pre-EU-87 contract eu61_autopilot_resume_queue_test.py pins (capping the *combined*
            # list instead silently truncated the To Do tail when cap was small).
            raw = intake.from_drain(cfg, app_name, cap + len(blocked) + len(resumed) + 5)
            raw = [(a, t) for (a, t) in raw if t.id not in blocked][:cap]

            # Tier-1: tickets the board already shows as In Progress — always run these first so
            # a ticket we started in a previous cycle is never delayed by new To Do items.
            # Use getattr for robustness in tests / adapters that return plain namespaces.
            in_progress = [(a, t) for (a, t) in raw
                           if (s := getattr(t, "status", None)) and "progress" in s.lower()]
            # Tier-3: ready (To Do) tickets waiting to be picked up, in board-Rank order.
            to_do = [(a, t) for (a, t) in raw
                     if not ((s := getattr(t, "status", None)) and "progress" in s.lower())]

            # Tier-2: parked tickets the Commander answered directly on Jira. They sit between
            # In Progress and To Do so a replied-to ticket is never left behind a fresh To Do.
            # Unanswered blocked tickets stay in `blocked` and are excluded by the raw filter
            # above — only tickets lifted by _resumable_answered() enter this tier.
            # De-dup against the drain: an answered ticket that is still In Progress on the board
            # comes through tier-1 via the drain and must not appear in tier-2 as well.
            in_drain = {t.id for _, t in raw}
            answered_items = [v for k, v in resumed.items() if k not in in_drain]

            # Final ordering: In Progress → answered → To Do. The cap was already applied to the
            # drawn work above; answered resumes are intentionally additive (see note above).
            worklist = in_progress + answered_items + to_do

            # EU-128: In continuous+dry-run mode, filter out tickets that were already previewed
            # to prevent re-picking the same ticket forever. Track previewed tickets so they're
            # skipped in subsequent cycles, but valid apps still drain.
            if cfg.dry_run and not once and previewed_tickets:
                worklist = [(a, t) for (a, t) in worklist if t.id not in previewed_tickets]
                if worklist:
                    print(f"  · skipping {len(previewed_tickets)} previewed ticket(s) (dry-run mode)", flush=True)
                else:
                    print(f"  · all tickets previewed — idling (dry-run mode)", flush=True)

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
                        # Leave a durable trail in audit.jsonl — without this a backlog that went dark at
                        # 3am (expired token, network) had ZERO record of WHY the queue looked empty.
                        audit.record("backlog_unreachable", boards=dict(unreachable))
                    else:
                        print("  · queue clear — nothing of yours in In Progress / To Do"
                              + (f" (parked: {', '.join(sorted(blocked))})" if blocked else "")
                              + " — idling; I'll pick up new or unblocked tickets automatically.", flush=True)
                        # Record the benign empty-queue too, so the log distinguishes "genuinely clear"
                        # from "hidden behind a dead board" instead of being silent on both.
                        audit.record("queue_clear", parked=sorted(blocked))
                    idle_state = state
                if once:
                    break
                await events.after_cycle(cfg, [], audit, blocked)   # quiet cycle — room for life
                _sleep(max(5, interval), stop_event)
                continue
            idle_state = None   # work again → re-announce next time the queue empties

            ids = ", ".join(t.id for _, t in worklist)
            print(f"  · taking {ids}", flush=True)

            # EU-107: Run pre-build gate (Senior PM triage) to filter tickets that can be
            # resolved without a build (ANSWER, CLOSE, REFILE) before reaching the Builder.
            worklist = await prebuild_gate(cfg, worklist, audit)

            # Only proceed to build if tickets remain after triage
            if not worklist:
                print("  · all tickets resolved by pre-build gate — nothing to build", flush=True)
                await events.after_cycle(cfg, [], audit, blocked)
                if once:
                    break
                _sleep(max(5, interval), stop_event)
                continue

            reports = await run_loop(cfg, worklist, audit)

            # Park ESCALATED / PR_OPENED immediately. For ERRORED, retry a few times before
            # parking so a transient blip doesn't sideline a ticket for hours.
            #
            # Re-escalation loop guard (EU-87): if an answered/resumed ticket escalates again,
            # the loop calls decisions.add() which re-parks it on the tracker AND records a NEW
            # answer_baseline (the current latest Jira comment). Next cycle, _resumable_answered()
            # compares against that new baseline — the same comment now MATCHES the baseline, so
            # auto-resume does NOT fire. The cycle is Blocked→(Commander answers again)→resume,
            # not Blocked→retry→Blocked. No extra gate needed here; this property holds as long as
            # decisions.add() always snapshots the baseline at park time (verified in _park_on_tracker).
            park_now = [r.ticket_id for r in reports if r.outcome in PARKED]
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

            # EU-128: In dry-run mode, track processed tickets so they're not re-picked in
            # subsequent cycles. This prevents continuous+dry-run from re-processing the same
            # tickets forever while still allowing valid apps to drain.
            if cfg.dry_run:
                processed_ids = {r.ticket_id for r in reports}
                previewed_tickets.update(processed_ids)

            if once:
                break
            # A transient error gets a short backoff before the next look; otherwise a brief breath.
            _sleep(_ERROR_BACKOFF_SEC if retrying else 3, stop_event)
    except KeyboardInterrupt:
        print("\n🛸 Autopilot stood down. Nothing left mid-flight.", flush=True)
    finally:
        # EU-103: the autopilot loop has stood down — clear THIS app's autopilot signal so the cockpit
        # control reflects OFF, independent of who owns the run-state release. (Idempotent: the cockpit
        # Start path's own finally also clears it.)
        if run_state is not None:
            run_state["autopilot_on"] = False
        # Release THIS project's run-state if we own it (clears active / run_started / stop_event) and
        # drop the dry/live tag, so the per-project board shows no stale run once autopilot stands down.
        # (run_state may still be None if claim_run() never ran — an early raise during setup.)
        if owns_run_state:
            cockpit_state.release_run(run_key)
            if run_state is not None:
                run_state["dry_run"] = None
        # Remove the PID file on any clean exit path (KeyboardInterrupt, stop_event, budget halt, once=True).
        # The launchd daemon treats a missing PID file as "not running" — this is the handshake.
        _remove_pid()
        # Restore the original SIGTERM handler — main thread only, mirroring the registration guard
        # above (signal.signal() raises ValueError off the main thread, e.g. the cockpit _bg path).
        if _on_main_thread and _orig_sigterm is not None:
            signal.signal(signal.SIGTERM, _orig_sigterm)
    audit.record("autopilot_stop")
