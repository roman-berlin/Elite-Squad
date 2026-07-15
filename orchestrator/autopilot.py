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

from . import gate as gate_mod
from . import auth_probe, infra_classify, intake, locking, notify, usage
from .audit import AuditLog
from .config import Config
from .contracts import PARKED, Outcome
from .loop import _BASE_LEVEL_PREFIXES
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


def _alert_unclean_restart(audit) -> bool:
    """QW5 (2026-07-05): detect + announce an unclean previous shutdown at startup.

    A leftover PID file whose process is dead means the previous autopilot was killed without
    cleanup — the Jul-1 crash signature (an autopilot_start never closed by autopilot_stop).
    Under the launchd keepalive daemon this fires on every auto-restart after a crash, so a
    restart is never silent: one audit event + one Telegram alert. Returns True when it fired.
    Best-effort — never raises, never blocks startup."""
    try:
        stale = _PID_FILE.read_text().strip()
        if stale and stale != str(os.getpid()) and not daemon_running():
            audit.record("autopilot_unclean_restart", stale_pid=stale)
            notify.send(f"⚠️ Autopilot restarted after an unclean shutdown "
                        f"(previous PID {stale} died without cleanup).")
            return True
    except OSError:
        pass
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


# EU-232: single shared launchd label. scripts/install-mac-autopilot-daemon.sh derives its LABEL=
# from this exact assignment (a stdlib-only ``python3 -c`` that ast-parses THIS file for the
# LAUNCHD_LABEL assignment — deliberately NOT an import, which would drag in claude_agent_sdk and
# abort the installer on any shell without the repo .venv), so the installer and this stopper can
# never drift apart again — tests/eu232_launchd_label_test.py
# asserts the installer's LABEL= line matches this string byte-for-byte. Previously these were two
# independent literals: the installer wrote "com.roman.general.autopilot-keepalive" but this file
# targeted the stale "com.romanberlin.general.autopilot", so _stop_launchd_daemon booted out a label
# that was never installed and silently claimed success while KeepAlive respawned the real daemon.
LAUNCHD_LABEL = "com.roman.general.autopilot-keepalive"


def _stop_launchd_daemon(poll_timeout: float = 10.0, poll_interval: float = 0.5) -> bool:
    """Stop the launchd KeepAlive daemon using launchctl, and VERIFY it actually exited.

    Tries launchctl bootout (modern macOS) first, then falls back to launchctl unload (older macOS).
    This is the ONLY way to durably stop a KeepAlive daemon — a plain 'launchctl stop' is respawned.

    EU-232: launchctl's exit code is optimistic — bootout/unload can return success while the daemon
    is still mid-ticket (or launchd itself is lagging) — so this used to claim success unconditionally
    the moment the subprocess call didn't raise. Now, once launchctl has actually run, poll
    ``daemon_running()`` (the same source of truth as the cockpit badge) for up to ``poll_timeout``
    seconds, checking every ``poll_interval``, and return True ONLY once the daemon is confirmed gone
    — False if it's still alive when the deadline passes. Returns False immediately, with no poll,
    when launchctl itself couldn't be invoked at all (platform mismatch / missing plist / subprocess
    failure) — there's nothing to verify in that case.

    EU-120: cockpit's "Finish & stop" (drain) and "Stop" call this for external daemons so the stop
    sticks. The plist path/label matches scripts/install-mac-autopilot-daemon.sh (LAUNCHD_LABEL).
    """
    import platform
    import subprocess

    if platform.system() != "Darwin":
        return False

    # Path from install-mac-autopilot-daemon.sh (same LAUNCHD_LABEL, so this can't drift again)
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

    if not plist_path.exists():
        # No plist installed — nothing to unload
        return False

    launchctl_ran = False
    try:
        # Modern macOS (10.10+): use bootout, which removes the service *and* stops it
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        launchctl_ran = True
    except (OSError, subprocess.TimeoutExpired):
        # bootout failed or not available — fall back to unload (older macOS)
        try:
            subprocess.run(
                ["launchctl", "unload", str(plist_path)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            launchctl_ran = True
        except (OSError, subprocess.TimeoutExpired):
            launchctl_ran = False

    if not launchctl_ran:
        return False

    # EU-232: don't trust launchctl's exit code — poll daemon_running() until it agrees the daemon
    # is actually gone (or the deadline passes), so callers get a verified outcome, not a guess.
    deadline = time.monotonic() + poll_timeout
    while True:
        if not daemon_running():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


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


def _ticket_project(ticket_id: str) -> str:
    """Jira project key of a ticket id — the part before the first '-' ('EU-151' -> 'EU').

    save_error_counts uses this to tell THIS drain's own keys apart from a concurrent drain's:
    every project's backlog is drained by exactly one app's drain at a time (the per-project run
    slot in server.py is single-writer, EU-64), so a key whose project this drain is currently
    writing is unambiguously ours — to add, update, or intentionally drop — while a key of any
    other project belongs to the other drain and must be preserved from the fresh on-disk read."""
    return ticket_id.split("-", 1)[0]


# EU-274: per-(file, thread) memory of the last `counts` snapshot THIS drain wrote. It is a SECONDARY
# delete signal, only needed for the one case the primary (project-prefix) signal can't cover: a save
# whose `counts` is EMPTY because the drain's last tracked ticket just parked/succeeded (its key
# popped) — an empty dict carries no prefix to reveal which project it owns, so without this the drop
# wouldn't propagate and the park-after-3 counter would never reset. Bounded by pruning rows of dead
# threads on every save (see _prune_dead_error_counts_seen), so it can't grow with run-thread churn and
# a recycled OS-thread ident can't inherit a dead predecessor's baseline; the value-match guard in the
# merge is a second belt on ident reuse (a key is dropped via this path only if disk still holds the
# exact value this thread last wrote).
_error_counts_seen: dict[tuple[str, int], dict[str, int]] = {}
_error_counts_seen_lock = threading.Lock()


def _prune_dead_error_counts_seen(live_idents: set[int]) -> None:
    """Drop _error_counts_seen rows for threads that have exited. Call under _error_counts_seen_lock."""
    for key in [k for k in _error_counts_seen if k[1] not in live_idents]:
        _error_counts_seen.pop(key, None)


def save_error_counts(cfg: Config, counts: dict[str, int]) -> None:
    # EU-274: route through locked_rmw (matching save_blocked) instead of a bare write_text. A plain
    # overwrite of a full-dict snapshot taken at load time let a concurrent drain's stale write clobber
    # increments made in between (a stale automatixy-drain snapshot erasing a fresh EU-151 count) — and
    # even a lock around a blind overwrite only serialises the writes, it does not refresh the stale
    # snapshot. locked_rmw re-reads the on-disk value under the lock so we MERGE onto the truth. A key
    # on disk but absent from `counts` is deleted (an intentional pop: a ticket parked or succeeded)
    # only when it is unambiguously THIS drain's — otherwise it is preserved as a concurrent drain's:
    #
    #   (a) its project is one this drain is writing right now — call-time context from `counts`, so it
    #       needs no warm cache and already deletes a stale same-project key on a drain's very FIRST
    #       save after a restart (this closes the cold-cache resurrection gap the earlier attempt had); or
    #   (b) this same thread wrote the key last save at the value still on disk — the only extra case,
    #       for when (a) has no signal because `counts` emptied as the last tracked ticket parked.
    #
    # A key whose project no other-writer touches and that neither signal claims stays put, so a
    # concurrent foreign increment is never clobbered.
    path = _error_counts_file(cfg)
    cache_key = (str(path), threading.get_ident())
    live = {t.ident for t in threading.enumerate()}
    with _error_counts_seen_lock:
        _prune_dead_error_counts_seen(live)
        baseline = _error_counts_seen.get(cache_key, {})
    owned = {_ticket_project(k) for k in counts}

    def _merge(current):
        current = current if isinstance(current, dict) else {}
        merged: dict[str, int] = {}
        for k, v in current.items():
            k = str(k)
            if k in counts:
                continue                      # counts.update below is authoritative for tracked keys
            if _ticket_project(k) in owned or baseline.get(k) == v:
                continue                      # ours, intentionally dropped -> delete (skip)
            merged[k] = v                     # another drain's key -> preserve from the fresh read
        merged.update(counts)
        return merged

    try:
        locking.locked_rmw(path, _merge, default={})
        with _error_counts_seen_lock:
            _error_counts_seen[cache_key] = dict(counts)
    except (OSError, ValueError):
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


def _auto_clear_decision_ghosts(cfg: Config, audit: "AuditLog") -> None:
    """Remove ghost pending decisions — decisions whose base ticket already merged.

    Ghost scenario: a ticket was parked (needs_human), then the Commander ran it
    manually and it merged — but pending_decisions.json was never cleaned up.
    Result: stale 'Needs you' cards for completed work. This function is called
    at the start of each autopilot cycle so the decision store self-heals (EU-229).

    Best-effort: any exception leaves the store unchanged so the loop never breaks."""
    from . import decisions
    from . import dashboard as D

    try:
        pending = decisions.load(cfg)
        if not pending:
            return

        tasks = D.load_tasks(cfg.audit_path)
        # Find the latest run per ticket in the pending store
        latest: dict[str, dict] = {}
        for t in tasks:
            tid = str(t.get("ticket_id") or "")
            if tid not in {p.get("id") for p in pending}:
                continue
            if tid not in latest or D._started_key(t) >= D._started_key(latest[tid]):
                latest[tid] = t

        # Tickets whose most-recent run merged are done — drop their decisions
        to_clear = {p.get("id") for p in pending
                    if p.get("id") in latest and latest[p.get("id")].get("outcome") == "merged→dev"}

        if to_clear:
            remaining = [p for p in pending if p.get("id") not in to_clear]
            decisions._save(cfg, remaining)
            audit.record("decision_ghost_cleared", tickets=sorted(to_clear))
            print(f"  · auto-cleared ghost decisions: {', '.join(sorted(to_clear))} "
                  f"(latest run already merged)", flush=True)
    except Exception:  # noqa: BLE001 — ghost-clearing must never crash the autopilot loop
        pass


def _reopen_on_tracker(cfg: Config, ticket_id: str) -> None:
    """EU-219: the other half of the park->Blocked round-trip. When the Commander /unblocks a ticket,
    best-effort transition it back to 'In Progress' on whichever jira-backed app's board actually has
    it, so the drain's queue_statuses re-picks it next cycle — without this, once the newly-park block
    (below) actually moves a ticket to Blocked, /unblock only cleared the in-memory skip-set and left
    the board silently stuck. Resolves the ticket via get_task across jira-backed apps, first match
    wins (mirrors _resumable_answered's per-app try loop). set_status is a no-op if the ticket isn't
    Blocked, and a wrong-project/network miss on one app just falls through to the next — best-effort
    throughout so a board hiccup can never turn /unblock into an exception."""
    from .backlog.base import make_backlog
    for app in cfg.apps:
        if getattr(app, "backlog_backend", "none") == "none":
            continue
        try:
            backlog = make_backlog(app)
            ticket = backlog.get_task(ticket_id)
            backlog.set_status(ticket, "In Progress")
            return
        except Exception:  # noqa: BLE001 - wrong project for this board, network, etc.
            continue


def unblock(cfg: Config, ticket_id: str | None = None) -> str:
    """Clear a parked ticket (or all). Autopilot will retry it next cycle."""
    blocked = load_blocked(cfg)
    if not blocked:
        return "nothing parked"
    if ticket_id:
        hit = {b for b in blocked if b.lower() == ticket_id.lower()}
        blocked -= hit
        save_blocked(cfg, blocked)
        if not hit:
            return f"{ticket_id} was not parked"
        for tid in hit:
            _reopen_on_tracker(cfg, tid)
        return f"unblocked {ticket_id}"
    ids = sorted(blocked)
    save_blocked(cfg, set())
    for tid in ids:
        _reopen_on_tracker(cfg, tid)
    return f"unblocked all ({len(blocked)})"


def _resumable_answered(cfg: Config, app_name: str | None, blocked: set[str]) -> dict:
    """EU-61 + EU-229: parked tickets the Commander has answered DIRECTLY on their Jira ticket → auto-resume.

    EU-229: universal Jira-answer resume — scans ALL pending decisions with real Jira keys, not just
    blocked ∩ pending. Any parked ticket (or any ticket with a pending decision) that receives a new
    Jira comment auto-resumes, removing the blocked-only constraint from the original EU-61 implementation.

    For each pending decision, fetch it by key and compare the latest human comment against the
    baseline snapshotted at park time (decisions.add). A genuinely-new answer means the Commander resolved
    it on Jira (not Telegram), so it should re-enter the develop queue without a manual /unblock.
    Returns ``{ticket_id: (app, ticket)}``. Fetched by key, so it's independent of the board's
    queue_statuses; best-effort per ticket (a wrong-project/network miss just skips that ticket this cycle)."""
    from . import decisions
    from .backlog.base import make_backlog
    import re
    out: dict = {}

    # EU-229: Load ALL pending decisions, not just blocked ones
    pending = {d.get("id"): d for d in decisions.load(cfg)}
    if not pending:
        return out

    # Filter to decisions with real Jira keys (e.g. AUTO-1, EU-42) — skip internal entry ids
    _JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
    targets = [tid for tid in pending.keys() if tid and _JIRA_KEY_RE.match(str(tid).split("#")[0])]
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


def _park_errored_on_tracker(cfg: Config, by_id: dict, newly: list, errored: set) -> None:
    """EU-219: 2026-07-09 forensics found a ticket_exception left the ticket In Progress with no board
    state change — the single-run path posts an ❌ error comment (12ecb49) but deliberately does not
    transition (ERRORED tickets are retried). The missing half: once the autopilot's error threshold
    actually parks a ticket (n >= _MAX_TICKET_ERRORS, see the caller), the board should show it Blocked
    with a reason, so /unblock's round-trip (_reopen_on_tracker above) has something to reverse.

    Only touches tickets in ``newly`` whose report outcome this cycle was Outcome.ERRORED — PARKED
    outcomes (ESCALATED/PR_OPENED) are NOT re-transitioned here; decisions.add already set those
    Blocked with their own reason comment (_park_on_tracker). Best-effort per ticket: a board hiccup
    (bad token, wrong project, network) must never crash the autopilot cycle."""
    if not newly or not errored:
        return
    from .backlog.base import make_backlog
    for tid in newly:
        if tid not in errored:
            continue
        hit = by_id.get(tid)
        if not hit:
            continue
        app, ticket = hit
        if getattr(cfg, "dry_run", False) or getattr(ticket, "ephemeral", False):
            continue
        try:
            backlog = make_backlog(app)
            backlog.set_status(ticket, "Blocked")
            backlog.add_comment(
                ticket,
                f"⛔ Parked after {_MAX_TICKET_ERRORS} consecutive errors — "
                f"reply /unblock {tid} or answer here to retry.",
            )
        except Exception:  # noqa: BLE001 - a board hiccup must never crash the loop
            pass


def _tally_errored(reports, error_counts: dict[str, int]):
    """EU-228: update `error_counts` IN PLACE for this cycle's reports, splitting ERRORED reports
    into park-worthy (hit `_MAX_TICKET_ERRORS`), retrying, and infra/outage.

    An ERRORED report whose notes classify as infra (network/DNS/timeout/5xx — see
    ``infra_classify.classify``; a turn-limit is NEVER infra, that's EU-248's job) contributes NO
    strike at all: `error_counts` is left exactly as it was for that ticket, so a DNS blip can
    never park — or even nudge the counter toward parking — a healthy ticket (the 2026-07-10
    16:29:47 evidence: one blip charged 4 tickets a strike each before this fix).

    2026-07-15 ~22:05: an EXPIRED Claude login is the same class — every builder call fails with
    "Not logged in · Please run /login" and that burned strikes on 5+ tickets in one drain. A
    report whose notes carry a login-failure marker (``auth_probe.is_login_failure``) is treated
    exactly like infra here (no strike, reported in ``infra``); the caller splits those ids out
    again to arm the dedicated auth-hold (see ``_enter_auth_hold``) instead of the offline-hold.

    Returns ``(park_now_additions, errored_ids, retrying_ids, infra_ids, counts_changed)``. A
    ticket that made progress (no longer ERRORED) still has its tally reset, same as before.
    """
    park_now: list[str] = []
    errored: set[str] = set()
    retrying: list[str] = []
    infra: set[str] = set()
    counts_changed = False
    for r in reports:
        if r.outcome is Outcome.ERRORED:
            errored.add(r.ticket_id)
            if auth_probe.is_login_failure(r.notes) or infra_classify.classify(r.notes):
                infra.add(r.ticket_id)
                continue   # no strike, no counter touch — see docstring
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
    return park_now, errored, retrying, infra, counts_changed


def _enter_offline_hold(cfg: Config, audit: "AuditLog", infra_ids, already_active: bool) -> bool:
    """EU-228: raise ONE offline-hold alert for this cycle's infra-classified errors and return
    True (now/still active). A no-op when there's nothing infra-classed this cycle, and a no-op
    alert-wise when a hold is already active — a single DNS blip that errors several tickets in
    the same cycle must produce ONE Telegram message, not one per ticket."""
    if not infra_ids:
        return already_active
    if not already_active:
        ids = ", ".join(sorted(infra_ids))
        notify.send(f"🌐 Autopilot offline-hold — infra/outage error(s) on {ids} "
                    "(network/DNS/timeout, not a ticket defect). No error strikes were charged; "
                    "holding new tickets and auto-resuming once connectivity returns.")
        audit.record("infra_offline_hold", tickets=sorted(infra_ids))
        print(f"  🌐 offline-hold — infra error(s) on {ids}; no strikes charged, "
              "auto-resume on connectivity.", flush=True)
    return True


def _offline_hold_recheck(cfg: Config, audit: "AuditLog", active: bool) -> bool:
    """EU-228: while an offline-hold is active, probe connectivity (Jira base URL + `git
    ls-remote`) and clear the hold — with one resume alert — the moment it passes. No human
    `/unblock` needed. Returns the (possibly updated) active state."""
    if not active:
        return False
    if infra_classify.connectivity_probe(cfg):
        notify.send("✅ Connectivity restored — autopilot resuming normal operation.")
        audit.record("infra_offline_resume")
        print("  ✅ connectivity restored — resuming normal operation.", flush=True)
        return False
    return True


# While an auth-hold is active, re-verify the login via auth_probe at most this often — cycles run
# every `interval` (~60s), so most rechecks ride the cache and a real re-login is noticed in ≤5 min.
_AUTH_HOLD_RECHECK_S = 300.0

# Telegram damper for the auth-hold alerts. If the login-failure marker ever fires on a failure the
# probe then verifies as valid (e.g. a non-default backend's own credential dying with the same CLI
# message), the hold would churn enter→resume every couple of cycles — each transition must not page
# the Commander again. The enter alert is rate-limited by `_AUTH_ALERT_COOLDOWN_S`; the resume alert
# is PAIRED to it (fires only when its matching enter alert fired), so the Commander never gets an
# orphan "verified again" ping. audit.jsonl still records every transition; only pings are damped.
_AUTH_ALERT_COOLDOWN_S = 1800.0
_last_auth_alert = 0.0
_auth_resume_alert_due = False   # True while an alerted hold awaits its paired resume alert


def _enter_auth_hold(cfg: Config, audit: "AuditLog", auth_ids, already_active: bool) -> bool:
    """2026-07-15 ~22:05 incident: builder failures carrying a login-failure marker ("Not logged
    in · Please run /login" — the Claude Code OAuth token expired) must hold the WHOLE drain with
    ONE "re-login needed" alert, exactly like the EU-228 offline-hold — not burn error strikes
    ticket by ticket (5+ tickets were charged toward parking that night). No counter was touched
    (``_tally_errored`` classified these no-strike), so nothing needs a human ``/unblock``: the
    tickets stay queued and the hold auto-clears once ``_auth_hold_recheck`` verifies the login.

    Invalidates the auth-probe cache on every auth-classified cycle: the failure is hard evidence
    that a cached "valid" (up to 15 min old) is stale — without this the recheck would read that
    stale cache and instantly (wrongly) clear the hold."""
    global _last_auth_alert, _auth_resume_alert_due
    if not auth_ids:
        return already_active
    auth_probe.invalidate()
    if not already_active:
        ids = ", ".join(sorted(auth_ids))
        audit.record("auth_expired_hold", tickets=sorted(auth_ids))
        now = time.time()
        if now - _last_auth_alert >= _AUTH_ALERT_COOLDOWN_S:
            _last_auth_alert = now
            _auth_resume_alert_due = True
            notify.send(f"🔐 Autopilot auth-hold — builder failed with a login error on {ids}. "
                        "The Claude Code login looks EXPIRED — run `claude` then /login "
                        "(or refresh CLAUDE_CODE_OAUTH_TOKEN). No error strikes were charged; "
                        "holding all new work and auto-resuming once the login is valid again.")
        print(f"  🔐 auth-hold — login error(s) on {ids}; no strikes charged, "
              "re-login needed (auto-resume once the probe verifies).", flush=True)
    return True


def _auth_hold_recheck(cfg: Config, audit: "AuditLog", active: bool) -> bool:
    """While an auth-hold is active, re-probe login validity (``auth_probe.probe``, its own ≤5-min
    cadence via ``_AUTH_HOLD_RECHECK_S``) and clear the hold — with one resume alert — the moment
    the probe verifies ``valid``. Returns the (possibly updated) active state.

    Only a VERIFIED ``valid`` clears the hold: ``expired`` obviously keeps it, and ``unreachable``
    / ``unknown`` (network down, probe can't run) keep it too — on a box where the probe can't run
    the builders can't run either (the SDK shells the same CLI), and clearing on no-evidence would
    just re-burn a failing wave per cycle. The hold is in-memory: a drain restart re-tries builds
    immediately, and if the login is still dead it re-holds with no strikes charged."""
    global _auth_resume_alert_due
    if not active:
        return False
    if auth_probe.probe(max_age_s=_AUTH_HOLD_RECHECK_S).get("state") == "valid":
        audit.record("auth_expired_resume")
        if _auth_resume_alert_due:   # paired to the enter alert — never an orphan/churn ping
            _auth_resume_alert_due = False
            notify.send("✅ Claude login verified again — autopilot resuming normal operation.")
        print("  ✅ Claude login verified again — resuming normal operation.", flush=True)
        return False
    return True


def _apply_toolchain_holds(cfg: Config, audit: "AuditLog", worklist, held: frozenset):
    """EU-228: filter `worklist` to drop tickets for any app whose gate/worktree-setup toolchain
    is missing a binary under the daemon's real environment (``infra_classify.missing_toolchain``)
    — a missing binary would otherwise burn every one of that app's tickets an error strike on an
    environment problem, not a real failure. HOLDS the whole app instead, with exactly ONE alert
    per app per hold (and one resume alert once the toolchain is fixed again); touches NO per-
    ticket counters. Only checks apps that actually have tickets in `worklist` this cycle — cheap,
    since a hit toolchain is the common case. Returns ``(filtered_worklist, updated_held)``."""
    apps_in_play = {a.name: a for a, _ in worklist}
    currently: set[str] = set()
    for name, app in apps_in_play.items():
        missing = infra_classify.missing_toolchain(app)
        if not missing:
            continue
        currently.add(name)
        if name not in held:
            notify.send(f"🛠️ Autopilot holding {name} — missing toolchain binaries on this "
                        f"machine: {', '.join(missing)}. No tickets were charged; it resumes "
                        "automatically once the toolchain is fixed.")
            audit.record("infra_toolchain_hold", app=name, missing=missing)
            print(f"  🛠️ holding {name} — missing toolchain binaries: {', '.join(missing)} "
                  "(no tickets charged).", flush=True)
    recovered = held - currently
    if recovered:
        names = ", ".join(sorted(recovered))
        notify.send(f"✅ Toolchain restored — resuming: {names}")
        audit.record("infra_toolchain_resume", apps=sorted(recovered))
        print(f"  ✅ toolchain restored — resuming: {names}", flush=True)
    filtered = [(a, t) for (a, t) in worklist if a.name not in currently]
    return filtered, frozenset(currently)


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

    # EU-232: WHY this run stood down, recorded on the terminal autopilot_stop audit event (see the
    # finally below). Set as early as possible on every exit path — the top-of-loop stop_event check,
    # the SIGTERM handler (before it sets stop_event, so the loop's own check doesn't overwrite it),
    # the budget/plan-limit "once" breaks, and the except clauses. Defaults to "once-complete" at
    # record time for every other break (a normal --once cycle finishing, or a hold's once-break) —
    # see the ``stop_reason or "once-complete"`` in the finally.
    stop_reason: str | None = None

    def _handle_sigterm(signum, frame):  # noqa: ANN001 — signal-handler signature
        """SIGTERM (e.g. a launchd unload of the keepalive daemon) → graceful stand-down: set the stop Event and let
        the loop notice it, finish any in-flight ticket, and run its finally (PID-file cleanup,
        run-state release). Mirrors the cockpit Stop toggle and Ctrl-C."""
        nonlocal stop_reason
        stop_reason = "sigterm"   # EU-232: set BEFORE stop_event, so the loop's own check never overwrites it
        stop_event.set()

    audit = AuditLog(cfg.audit_path)
    # Stale-process forensics (2026-07-09, mirrors server.serve): one audit line per process start
    # (git SHA + pid) so "which code was this drain actually on?" is a single grep — the resident
    # process ran pre-EU-201 code for 11h after the unit landed EU-201 on itself.
    try:
        import subprocess as _sp
        _sha = _sp.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                       text=True, timeout=5).stdout.strip()
        audit.record("process_start", role="autopilot", app=app_name or "", sha=_sha, pid=os.getpid())
    except Exception:  # noqa: BLE001 — forensics must never block the drain
        pass
    from . import cockpit_state
    from .git_ops import clear_parked_repos
    # EU-253: install the stdout Tee here too, idempotently. server.serve() only installs it in the
    # cockpit process (server.py's `if not isinstance(sys.stdout, _Tee)` guard) — an external/CLI
    # daemon running `general autopilot` standalone (main.py -> this function, no cockpit process)
    # never got a Tee, so run_logger.write_line() (fed only by _Tee.write) never received a single
    # line and every automode per-ticket log came out empty. Same guard as server.py: a no-op when
    # this IS the cockpit process (stdout is already wrapped), so it can never double-wrap.
    import sys
    if not isinstance(sys.stdout, cockpit_state._Tee):
        sys.stdout = cockpit_state._Tee(sys.stdout)
    run_key = app_name or None
    owns_run_state = False    # set True only once claim_run succeeds; gates release in the finally
    run_state = None
    started = False           # EU-175: gates autopilot_stop so a setup failure BEFORE autopilot_start
                              # never records an UNPAIRED stop (the mirror image of the ghost-session bug).
    try:
        # Write the PID file FIRST, inside the try, so the finally's _remove_pid() always runs — even
        # if any setup below (the signal registration, claim_run, a Telegram send) raises. Otherwise an
        # early raise would orphan /tmp/general-autopilot.pid pointing at this live process and pin
        # daemon_running() True forever (EU-73 — this is the single source of truth for the cockpit badge).
        _alert_unclean_restart(audit)   # QW5: a restart after a crash is never silent
        _write_pid()
        if _on_main_thread:
            _orig_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _handle_sigterm)

        # EU-128: Clear parked repos ONCE per autopilot run (not every cycle) so notifications
        # fire once per run, not once per cycle. A misconfigured app parks and alerts once;
        # subsequent cycles silently skip it without spamming.
        clear_parked_repos()
        
        # Reap stale/merged git worktrees from dead sessions (EU-117)
        from .git_ops import reap_stale_worktrees
        reap_stale_worktrees(cfg)

        blocked = load_blocked(cfg)
        error_counts = load_error_counts(cfg)   # per-ticket consecutive-ERROR tally (retry-before-park)
        # EU-128: Track tickets previewed in dry-run mode to prevent re-picking them in continuous mode
        previewed_tickets: set[str] = set()
        cap = max(1, cfg.max_tickets_per_run)
        mode = "DRY-RUN" if cfg.dry_run else ("LIVE · automode" if getattr(cfg, "auto_mode", False) else "LIVE")

        # Single always-on brain: also listen to Telegram (/unblock, /council, decision replies).
        # EU-185 (Wave 0): only the elected poller host polls, so an autopilot run on a non-poller
        # host doesn't fight the VPS poller over the one bot token (getUpdates is single-consumer).
        # EU-257: route through ensure_poll_loop, the process-level singleton shared with serve's
        # startup — a cockpit drain Start runs THIS function inside the serve process (server.py's
        # asyncio.run(ap.autopilot(...))), so without the singleton every per-app Start on this host
        # would add its own immortal poller thread. Pass THIS run's stop_event so, if this call is
        # the one that actually starts the poller (no serve/other-drain poller already live), the
        # poller stands down when this finite run stops.
        from . import decisions
        _ap_poll, _ap_why = decisions.should_poll_telegram(cfg)
        if _ap_poll:
            decisions.ensure_poll_loop(cfg, audit, stop_event=stop_event)
        elif notify.configured():
            print(f"  · Telegram listener OFF — {_ap_why}", flush=True)

        scope = app_name or "all backlog apps"
        notify.send(f"🛸 Autopilot {mode} online — working {scope}")
        print(f"🛸 Autopilot {mode} — {scope}. Ctrl-C to stop.", flush=True)
        if cfg.dry_run and not once:
            print("  · continuous + dry-run: previewing tickets once, then idling (won't re-pick). "
                  "Use --live for continuous processing, or --once for a single dry test.", flush=True)

        audit.record("autopilot_start", mode=mode, app=app_name, once=once)
        started = True   # EU-175: from here on, every stand-down MUST record the paired autopilot_stop
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

        # EU-128: Preflight validate all git repos before the main loop. This populates
        # MISSING_REPO_ERRORS early so the first cycle can announce missing repos immediately.
        for app in cfg.apps:
            if app.backlog_backend != "none":
                intake._validate_git_repo(app)

        # EU-128: track missing repo announcement state (app names announced) — dedupe so each app
        # emits exactly one alert per run, not one per cycle. Start empty so the first cycle
        # announces repos detected as missing during preflight.
        repos_announced: frozenset[str] = frozenset()
        # EU-228: infra/outage state. `offline_hold` pauses ALL new work (a Jira/git outage — see
        # _enter_offline_hold/_offline_hold_recheck); `toolchain_held` HOLDS only the specific
        # app(s) missing a gate/worktree-setup binary (see _apply_toolchain_holds). Neither touches
        # per-ticket error_counts — that's the point of this ticket.
        offline_hold = False
        toolchain_held: frozenset[str] = frozenset()
        # 2026-07-15 ~22:05: expired-Claude-login hold. Like offline_hold it pauses ALL new work
        # (every builder call fails the same way) and touches no per-ticket counter, but it is a
        # SEPARATE hold: the offline-hold's connectivity probe checks Jira/git and would announce
        # a misleading "connectivity restored" while the login is still dead — the same lesson as
        # the base-gate-timeout exclusion above. See _enter_auth_hold / _auth_hold_recheck.
        auth_hold = False
        # 2026-07-15: base-level hold, PER APP. A red or timed-out BASE gate applies to the whole
        # app — picking it again before the red-base cache would re-verify (gate._RED_BASE_RED_TTL_S)
        # just parks one more ticket per cycle (the needs_human massacre: ~60 tickets in the 04:20
        # and 17:41 waves that day). Keyed by app name so a unit-wide drain keeps building the
        # healthy apps (EU-87/EU-252 never-starve contracts); the held app's queue stays on the
        # board untouched. Timer-based: the TTL matches when the red cache re-verifies anyway
        # (a base FIX landed mid-hold waits out the remainder; cockpit manual runs bypass it).
        red_base_hold: dict[str, float] = {}
        base_timeout_waves = 0   # consecutive cycles ending in a base-gate TIMEOUT (escalate at 3)

        while True:
            run_state["last_activity"] = time.time()   # per-app heartbeat — proves THIS project's loop is alive
            if stop_event is not None and stop_event.is_set():
                # EU-232: sigterm already claimed this reason via nonlocal above; anything else that
                # set the same stop_event (the cockpit Stop/Drain toggle) is a cockpit-stop.
                if stop_reason is None:
                    stop_reason = "cockpit-stop"
                print("🛸 Autopilot stood down (stopped from the cockpit).", flush=True)
                break

            # EU-228: infra/outage hold — a Jira/git-remote outage errored one or more tickets last
            # cycle (see _enter_offline_hold below). Hold ALL new work until connectivity_probe
            # passes again; no per-ticket counter was touched, so nothing needs a human /unblock.
            offline_hold = _offline_hold_recheck(cfg, audit, offline_hold)
            if offline_hold:
                if once:
                    break
                _sleep(max(10, interval), stop_event)
                continue

            # 2026-07-15: expired-login hold — a builder failed with "Not logged in" last cycle
            # (see _enter_auth_hold below). Hold ALL new work until the auth probe verifies the
            # login again; no per-ticket counter was touched, so nothing needs a human /unblock.
            auth_hold = _auth_hold_recheck(cfg, audit, auth_hold)
            if auth_hold:
                if once:
                    break
                _sleep(max(10, interval), stop_event)
                continue


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
                    stop_reason = "budget"   # EU-232
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
                    stop_reason = "plan-limit"   # EU-232
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

            # EU-128: Check for missing/invalid git repos and announce once per run (not every cycle).
            # Apps with missing repos are already skipped by from_drain, so they don't appear in the worklist.
            # We only announce apps that haven't been announced yet in this run.
            missing_repos = dict(intake.MISSING_REPO_ERRORS)
            if missing_repos:
                newly_missing = frozenset(missing_repos.keys()) - repos_announced
                if newly_missing:
                    repos = sorted(newly_missing)
                    msgs = [f"{name} — {missing_repos[name]}" for name in repos]
                    print(f"  ⚠ skipping {len(repos)} app(s) with missing/invalid git repos:", flush=True)
                    for msg in msgs:
                        print(f"    · {msg}", flush=True)
                    notify.send(f"⚠️ Autopilot skipping {len(repos)} app(s) — git repos missing/invalid:\n" + "\n".join(msgs))
                    audit.record("missing_repos", apps=msgs)
                    # Track that we've announced these repos so we don't spam every cycle
                    repos_announced = frozenset(set(repos_announced) | set(repos))


            blocked = load_blocked(cfg)   # re-read so /unblock takes effect live
            # EU-78: auto-clear ghost-parked tickets whose latest audit run already succeeded
            # (e.g. parked in a previous session, then ran manually and merged).
            blocked = _auto_clear_merged_ghosts(cfg, blocked, audit)
            # EU-229: auto-clear ghost pending decisions whose base ticket already merged
            _auto_clear_decision_ghosts(cfg, audit)
            # EU-61: a parked ticket the Commander answered directly on Jira auto-resumes — lift it out
            # of the skip-set and put it at the FRONT of the queue (resume before taking new work).
            resumed = _resumable_answered(cfg, app_name, blocked)
            if resumed:
                blocked -= set(resumed)
                save_blocked(cfg, blocked)
                notify.send("▶️ Resuming (answered on Jira): " + ", ".join(sorted(resumed)))
                audit.record("decision_resumed", tickets=sorted(resumed), via="jira-comment")
            # Three-tier worklist assembly (EU-87, cap contract fixed by EU-252): In Progress →
            # answered/unblocked → To Do. Pull more than cap so the blocked filter still leaves
            # enough to fill the cap. `cap` bounds only how much FRESH To Do work a cycle pulls —
            # it must never truncate the drawn window before the tier split runs, or a jql override
            # that ranks an In Progress fragment low (by priority/Rank) gets clipped off before
            # tier-1 can rescue it, starving already-in-flight work behind newer To Do filings
            # (EU-252: EU-233..237 sat 29h unpicked this way). So filter blocked over the FULL
            # drawn window first, split tiers over that full window, and cap ONLY the To Do slice.
            # Answered/resumed tickets (Tier-2 below) are work already in flight that the Commander
            # explicitly replied to, so they ride ON TOP of the cap and are never dropped — this is
            # the pre-EU-87 contract eu61_autopilot_resume_queue_test.py pins (capping the *combined*
            # list instead silently truncated the To Do tail when cap was small).
            raw = intake.from_drain(cfg, app_name, cap + len(blocked) + len(resumed) + 5)
            raw = [(a, t) for (a, t) in raw if t.id not in blocked]

            # Tier-1: tickets the board already shows as In Progress — always run these first so
            # a ticket we started in a previous cycle is never delayed by new To Do items. Never
            # bounded by `cap` (EU-252) — an In Progress resume must never be starved by fresh work.
            # Use getattr for robustness in tests / adapters that return plain namespaces.
            in_progress = [(a, t) for (a, t) in raw
                           if (s := getattr(t, "status", None)) and "progress" in s.lower()]
            # Tier-3: ready (To Do) tickets waiting to be picked up, in board-Rank order. `cap` bounds
            # only this fresh-backlog slice (EU-252) — In Progress and answered resumes are additive.
            to_do = [(a, t) for (a, t) in raw
                     if not ((s := getattr(t, "status", None)) and "progress" in s.lower())][:cap]

            # Tier-2: parked tickets the Commander answered directly on Jira. They sit between
            # In Progress and To Do so a replied-to ticket is never left behind a fresh To Do.
            # Unanswered blocked tickets stay in `blocked` and are excluded by the raw filter
            # above — only tickets lifted by _resumable_answered() enter this tier.
            # De-dup against the drain: an answered ticket that is still In Progress on the board
            # comes through tier-1 via the drain and must not appear in tier-2 as well.
            in_drain = {t.id for _, t in raw}
            answered_items = [v for k, v in resumed.items() if k not in in_drain]

            # Final ordering: In Progress → answered → To Do. The cap was already applied to the
            # To Do slice above (EU-252); In Progress and answered resumes are intentionally
            # additive/uncapped (see notes above).
            worklist = in_progress + answered_items + to_do

            # EU-228: hold any app whose gate/worktree-setup toolchain is missing a binary on this
            # machine — one alert for the WHOLE app, its tickets simply don't run this cycle (no
            # error strike, no park).
            worklist, toolchain_held = _apply_toolchain_holds(cfg, audit, worklist, toolchain_held)

            # 2026-07-15: drop apps under an active base-level hold (red/timed-out base last
            # cycle) — their tickets stay queued on the board; expired holds are pruned so the
            # next pick re-verifies the base (the red cache TTL expires on the same clock).
            _now = time.time()
            red_base_hold = {a: ts for a, ts in red_base_hold.items() if ts > _now}
            _dropped_held = [t.id for (a, t) in worklist if a.name in red_base_hold]
            if _dropped_held:
                worklist = [(a, t) for (a, t) in worklist if a.name not in red_base_hold]
                print(f"  ⛔ base-hold active for {', '.join(sorted(red_base_hold))} — "
                      f"{len(_dropped_held)} ticket(s) stay queued until the base re-check.",
                      flush=True)

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
                # Phase-2 §2: the events.py autonomy layer (auto-convened smalltalk/meetings on
                # quiet cycles) was deleted — ceremonies are on-demand only now.
                _sleep(max(5, interval), stop_event)
                continue
            idle_state = None   # work again → re-announce next time the queue empties

            ids = ", ".join(t.id for _, t in worklist)
            print(f"  · taking {ids}", flush=True)

            # Phase-2 §2 (2026-07-06): the EU-107 Senior PM pre-build triage gate is DELETED —
            # it was off by default since 2026-06-29 (it closed [Feature] tickets as "answered"),
            # and its ANSWER/CLOSE/REFILE verdicts move into the Planner's single per-ticket
            # decision. The conservative overrides (AC / [Feature] / [Bug] ⇒ always build) become
            # deterministic pre-checks on that verdict when the Planner lands.

            # EU-219: snapshot (app, ticket) by id BEFORE run_loop so the newly-park block below can
            # best-effort transition a freshly-parked ERRORED ticket to Blocked without re-fetching it.
            by_id = {t.id: (a, t) for (a, t) in worklist}

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
            # EU-228: infra-classified ERRORED reports (network/DNS/timeout/5xx — a turn-limit is
            # NEVER infra, see infra_classify.classify) contribute no strike at all.
            extra_park, errored, retrying, infra_errored, counts_changed = \
                _tally_errored(reports, error_counts)
            park_now += extra_park
            if counts_changed:
                save_error_counts(cfg, error_counts)

            if retrying:
                print(f"  · ERRORED, retrying next cycle (not parked): "
                      + ", ".join(f"{t} [{error_counts[t]}/{_MAX_TICKET_ERRORS}]" for t in retrying),
                      flush=True)

            # 2026-07-15: split out base-level reports FIRST — a base-gate timeout is CPU/load,
            # not connectivity, so it must not enter the offline-hold (whose probe checks Jira/
            # git and would immediately announce a misleading "connectivity restored").
            _base_reports = [r for r in reports
                             if (r.notes or "").startswith(_BASE_LEVEL_PREFIXES)]
            _base_ids = {r.ticket_id for r in _base_reports}

            # 2026-07-15: expired-login failures ("Not logged in · Please run /login") are split
            # out too — _tally_errored already classified them no-strike (they ride in
            # infra_errored), but like base timeouts they must NOT arm the offline-hold, whose
            # connectivity probe checks Jira/git and would announce a misleading "connectivity
            # restored" while the Claude login is still dead. They arm their own auth-hold below.
            _auth_ids = {r.ticket_id for r in reports
                         if r.outcome is Outcome.ERRORED and auth_probe.is_login_failure(r.notes)}

            # EU-228: one or more tickets errored on infra/outage this cycle — hold ALL new work
            # (not just these tickets) until connectivity_probe passes again; no counter touched.
            offline_hold = _enter_offline_hold(
                cfg, audit, infra_errored - _base_ids - _auth_ids, offline_hold)

            # 2026-07-15: expired-login hold — ONE "re-login needed" alert, no strikes, and the
            # drain pauses until _auth_hold_recheck (top of the loop) verifies the login again.
            auth_hold = _enter_auth_hold(cfg, audit, _auth_ids, auth_hold)

            # 2026-07-15: base-level outcome — the app can't build ANYTHING right now. Hold ITS
            # picking until the red-base cache would re-verify, instead of re-parking one more
            # ticket every cycle (the needs_human massacre). A genuine red already pinged the
            # Commander via its one parked ticket; a timeout gets its own accurate ping here
            # (it was excluded from the offline-hold alert above), and three consecutive
            # timeout waves escalate — a persistently overloaded box needs a human look.
            if _base_reports:
                for r in _base_reports:
                    red_base_hold[r.app or app_name or ""] = (
                        time.time() + gate_mod._RED_BASE_RED_TTL_S)
                _hold_m = int(gate_mod._RED_BASE_RED_TTL_S / 60)
                audit.record("red_base_drain_hold", tickets=sorted(_base_ids),
                             apps=sorted({r.app or "" for r in _base_reports}),
                             hold_minutes=_hold_m)
                print(f"  ⛔ base-level failure — holding {_hold_m}m before re-checking the base.",
                      flush=True)
                _timeout_wave = any((r.notes or "").startswith("base gate timed out")
                                    for r in _base_reports)
                if _timeout_wave:
                    base_timeout_waves += 1
                    notify.send(f"⏳ Base gate timed out under load (wave {base_timeout_waves}) — "
                                f"drain holding {_hold_m}m; no tickets were charged or parked.")
                    if base_timeout_waves == 3:
                        notify.send("⚠️ 3rd consecutive base-gate timeout wave — the box looks "
                                    "persistently overloaded. Check system load, or raise "
                                    "gate_timeout_sec for this app.")
                else:
                    base_timeout_waves = 0
            else:
                base_timeout_waves = 0

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
                # EU-219: only the ERRORED-threshold arrivals need a fresh Blocked transition here —
                # PARKED outcomes (ESCALATED/PR_OPENED) were already moved to Blocked by decisions.add.
                _park_errored_on_tracker(cfg, by_id, newly, errored)
                notify.send("⏸️ Parked (need you): " + ", ".join(newly)
                            + "\nReply /unblock <id> once handled and I'll retry it.")
            _learn_from_cycle(cfg, reports, audit)   # fold this cycle's lessons into memory (free)
            # Phase-2 §2: events.after_cycle (the auto-convene reactor) deleted — on-demand only.

            # EU-128: In dry-run mode, track processed tickets so they're not re-picked in
            # subsequent cycles. This prevents continuous+dry-run from re-processing the same
            # tickets forever while still allowing valid apps to drain.
            if cfg.dry_run:
                processed_ids = {r.ticket_id for r in reports}
                previewed_tickets.update(processed_ids)

            if once:
                stop_reason = "once-complete"   # EU-232
                break
            # A transient error gets a short backoff before the next look; otherwise a brief breath.
            _sleep(_ERROR_BACKOFF_SEC if retrying else 3, stop_event)
    except KeyboardInterrupt:
        stop_reason = "keyboard-interrupt"   # EU-232
        print("\n🛸 Autopilot stood down. Nothing left mid-flight.", flush=True)
    except Exception as exc:   # noqa: BLE001 — record WHAT broke, then let it propagate unchanged
        stop_reason = f"exception:{type(exc).__name__}"   # EU-232
        raise
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
        # EU-175: record the terminal stop FROM INSIDE finally so it fires on EVERY stand-down —
        # a non-KeyboardInterrupt exception out of the loop, a budget halt, or a hard kill mid-teardown.
        # Previously this sat AFTER the finally, so any exit that wasn't a graceful return/KeyboardInterrupt
        # skipped it, leaving an unpaired autopilot_start and a phantom "Working" card (the Jul-1 signature).
        # Gated on `started`: a setup failure BEFORE autopilot_start must NOT record an unpaired stop
        # (the mirror-image invariant break the 2026-07-06 review caught).
        # EU-232: every autopilot_stop now carries a reason= — cockpit-stop / sigterm / once-complete /
        # plan-limit / budget / keyboard-interrupt / exception:<type> — set at the exit path above.
        # Falls back to "once-complete" for the handful of other `if once: break` holds (git/pre-flight/
        # graceful-stop/idle-queue) that don't set their own reason — still a genuine --once completion.
        if started:
            audit.record("autopilot_stop", reason=stop_reason or "once-complete")
