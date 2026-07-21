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

import contextvars
import inspect
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
from .contracts import AUDIT_EVENT_OUTCOME, PARKED, Outcome
from .loop import _BASE_LEVEL_PREFIXES
from .loop import run as run_loop

# PID file — single source of truth for "is the daemon actually running?"
# Written at startup and removed on clean exit or SIGTERM. The Mac launchd keepalive daemon
# (scripts/install-mac-autopilot-daemon.sh) relies on this file for external status checks.
# EU-355: GENERAL_PID_FILE overrides the machine-global default. tests/run_all.py sets it to a
# per-run temp path in every harness's env, so no harness can read a LIVE drain's PID (false
# "autopilot ON" → drain-halting red base — the 2026-07-16 eu203 incident) or clobber it. One
# central seam closes the whole class, not a per-harness house rule each new test must remember.
_PID_FILE = Path(os.environ.get("GENERAL_PID_FILE") or "/tmp/general-autopilot.pid")

# EU-357: usage-exhaustion circuit breaker tunables. After this many consecutive "barren" cycles
# (non-empty worklist, only unexplained ERRORED reports) the drain pauses for the cooldown rather
# than churning the backlog on an exhausted 5-hour usage window. The cooldown is long enough to
# outlast a transient storm and let a rolling window recover; it auto-resumes with no /unblock.
_MAX_BARREN_CYCLES = 3
_USAGE_HOLD_COOLDOWN_S = 1800.0

# EU-310: exclude a just-merged ticket from re-selection for this long, so a lost or lagging
# post-merge Jira transition can't make the drain rebuild already-merged code. Long enough for board
# propagation + a self-update restart; short enough that a genuinely re-opened ticket isn't stuck.
_MERGED_COOLDOWN_S = 600.0


# ── EU-386 (EU-224b): dirty-tree respawn forensics ────────────────────────────────────────────
# A crash/KeepAlive respawn onto uncommitted changes used to be silent — process_start recorded
# sha+pid only, so nothing distinguished a clean-tree boot from one running stray, unreviewed
# code (the accidental-restart blind spot EU-224's forensics called out). The probe is read-only
# and fail-soft: forensics must never block a boot.

_DIRTY_PATHS_CAP = 50   # bound the audit line — one giant tree must not balloon audit.jsonl


# EU-387: the distinctive exit code for a deliberate self-update restart. EX_TEMPFAIL (75) —
# launchd KeepAlive / systemd Restart=always respawn on any exit, but 75 in the log tells the
# forensic reader "this was the unit restarting itself onto new code", never a crash. (78 is
# taken: the AUTO-128 sentinel revert branch.)
SELF_RESTART_EXIT_CODE = 75

# When THIS process booted — the reference point for the stale-flag crash-loop guard in
# _maybe_self_restart (a restart flag older than the boot has already been honoured).
_PROCESS_START_TS = time.time()


def _self_restart_flag(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("self_restart_pending.json")


def flag_self_update(cfg: Config, ticket_id: str, sha: str = "", audit=None) -> bool:
    """EU-387: called by loop._land when a live land changed the unit's OWN repo. Writes the
    pending-restart flag the drain's cycle boundary consumes — the land site itself is MID-RUN
    (its worklist may hold more tickets), so the exit decision can't be made there.

    Returns True when the flag persisted. A False return means NO automatic restart will happen
    (2026-07-19 stabilization: the old silent swallow let the land site announce "restarting
    automatically" while the unit kept running stale code — the announce-lie variant of the very
    incident this feature exists to end), so the caller must word its notify accordingly."""
    import json as _json
    try:
        _self_restart_flag(cfg).write_text(_json.dumps(
            {"ticket": ticket_id, "sha": sha, "ts": time.time()}), encoding="utf-8")
        return True
    except OSError as exc:
        if audit is not None:
            try:
                audit.record("self_restart_flag_write_failed", ticket_id=ticket_id,
                             error=str(exc)[:200])
            except Exception:  # noqa: BLE001
                pass
        print(f"  ⚠ self-restart flag write failed ({exc}) — manual restart needed", flush=True)
        return False


def consume_self_restart_flag(cfg: Config, audit=None) -> dict | None:
    """EU-387 boot half: one-shot consume. Called from serve boot — a present flag means THIS
    process is the respawn the exit asked for; record it and clear so a second boot is a no-op."""
    import json as _json
    p = _self_restart_flag(cfg)
    if not p.exists():
        return None
    try:
        data = _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    try:
        p.unlink()
    except OSError:
        pass
    if audit is not None:
        try:
            audit.record("self_restart_completed", ticket_id=data.get("ticket"),
                         old_sha=data.get("sha"))
        except Exception:  # noqa: BLE001
            pass
    return data


def _maybe_self_restart(cfg: Config, audit) -> None:
    """EU-387: at a drain cycle boundary, exit the process onto the new code — but ONLY when it is
    safe: the flag is set, the knob is on, NO run is in flight for ANY app (never a mid-build
    kill), and the tree is clean (EU-386: a dirty respawn would silently activate un-gated WIP —
    the mirror image of the stale-process incident this feature exists to end). The keepalive
    (launchd com.roman.general.cockpit / the VPS systemd unit) respawns within ~5s; EU-385's boot
    resume then re-arms the drains whose intent files say RUNNING. Knob off or dirty tree →
    notify-only (flag cleared so it can't ping-pong)."""
    p = _self_restart_flag(cfg)
    if not p.exists():
        return
    # Crash-loop guard (2026-07-19 stabilization): a flag whose ts predates THIS process's start
    # requested a restart that has, by definition, already happened — it survives only when boot's
    # consume could not unlink it (read-only state dir, permissions). Exiting on it again would
    # restart forever. Ignore it loudly instead; cap = one wasted restart, never a loop.
    try:
        import json as _json
        _ts = float(_json.loads(p.read_text(encoding="utf-8")).get("ts", 0) or 0)
    except (OSError, ValueError):
        _ts = 0.0
    if _ts and _ts < _PROCESS_START_TS:
        try:
            p.unlink()
        except OSError:
            pass
        try:
            audit.record("self_restart_stale_flag", flag_ts=_ts)
        except Exception:  # noqa: BLE001
            pass
        print("  ⚠ stale self-restart flag (predates this boot) ignored — check state/ "
              "permissions if this repeats", flush=True)
        return
    if not getattr(cfg, "self_update_auto_restart", True):
        try:
            p.unlink()
        except OSError:
            pass
        return                              # notify-only mode: today's exact behaviour
    from . import cockpit_state as _cs
    if _cs.active_run_count() > 0:
        return                              # something is mid-build somewhere — defer, re-check next cycle
    # 2026-07-21: refuse on WIP that could actually change behaviour — tracked modifications, or
    # untracked EXECUTABLE files. Inert leftovers (a .bak, a stray .md) are reported by the boot
    # warning but must never pin the host to old code (that turned EU-386's safety guard into the
    # EU-335 stale-code bug on the VPS).
    paths = respawn_blocking_paths()
    if paths:
        try:
            p.unlink()
        except OSError:
            pass
        notify.send("⚠️ Self-update restart REFUSED — the working tree carries un-gated changes "
                    f"({', '.join(paths[:5])}{'…' if len(paths) > 5 else ''}). A respawn would "
                    "activate un-gated WIP (EU-386). Falling back to notify-only; restart by hand "
                    "after committing/stashing.")
        audit.record("self_restart_refused", reason="dirty tree", paths=paths[:10])
        return
    audit.record("self_restart_exit", exit_code=SELF_RESTART_EXIT_CODE)
    notify.send("♻️ Self-update restart: the unit is exiting cleanly onto its own landed code — "
                "the keepalive respawns it in seconds and armed drains auto-resume (EU-385).")
    print(f"  ♻️ SELF-UPDATE RESTART — clean exit {SELF_RESTART_EXIT_CODE}; keepalive respawns "
          "on the new sha.", flush=True)
    _remove_pid()
    os._exit(SELF_RESTART_EXIT_CODE)


# 2026-07-21: which UNTRACKED files can actually "activate un-gated code" on a respawn. A stray
# .py/.sh inside the tree can be imported or executed; a leftover .md/.bak/.log cannot. Only the
# former may block a self-update restart — see respawn_blocking_paths.
_EXECUTABLE_SUFFIXES = (".py", ".pyc", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts")


def tree_forensics() -> tuple[bool, list[str]]:
    """Whether the working tree is dirty, and which paths — from ``git status --porcelain``.

    Returns ``(False, [])`` on any probe failure (no git, timeout, not a repo): an unknowable
    tree state must never block or spam a boot. The path list is capped at ``_DIRTY_PATHS_CAP``
    entries; ``dirty`` stays truthful regardless of the cap.

    REPORTING view — every entry, tracked or not (the boot warning must stay honest). The
    narrower RESPAWN-BLOCKING view lives in ``respawn_blocking_paths``."""
    return _tree_status()[:2]


def _tree_status() -> tuple[bool, list[str], list[tuple[str, str]]]:
    """(dirty, paths, [(status, path), …]) from ``git status --porcelain``. Fail-soft to
    ``(False, [], [])`` — an unknowable tree must never block or spam a boot."""
    import subprocess

    try:
        r = subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                           text=True, timeout=5)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False, [], []
    if r.returncode != 0:
        return False, [], []
    paths: list[str] = []
    entries: list[tuple[str, str]] = []
    for ln in r.stdout.splitlines():
        ln = ln.rstrip()
        if not ln:
            continue
        # porcelain v1: two status chars + a space + the path ("R  old -> new" kept whole)
        path = ln[3:].strip() if len(ln) > 3 else ln.strip()
        paths.append(path)
        entries.append((ln[:2], path))
    return bool(paths), paths[:_DIRTY_PATHS_CAP], entries


def respawn_blocking_paths() -> list[str]:
    """The subset of a dirty tree that must REFUSE a self-update respawn (2026-07-21).

    EU-386 exists to stop a respawn from silently activating un-gated WIP — that means TRACKED
    modifications (M/A/D/R/C, staged or not): code the unit never built or gated. An UNTRACKED
    file ('??') is only dangerous when it could execute (see _EXECUTABLE_SUFFIXES); a leftover
    .md/.bak/.log cannot change behaviour.

    Why the distinction matters: on the VPS two harmless leftovers (a timestamped config backup
    and a pre-rename officer charter) made every self-update restart refuse — so the server was
    pinned to old code indefinitely with only a warning, resurrecting the exact EU-335
    stale-cockpit class the self-update feature exists to prevent. Junk must be reported, never
    load-bearing."""
    _dirty, _paths, entries = _tree_status()
    blocking: list[str] = []
    for status, path in entries:
        if status.strip() == "??":
            if any(path.lower().endswith(sfx) for sfx in _EXECUTABLE_SUFFIXES):
                blocking.append(path)
            continue
        blocking.append(path)      # any TRACKED modification is real WIP
    return blocking[:_DIRTY_PATHS_CAP]


def warn_dirty_tree(cfg: Config, role: str, forensics: tuple[bool, list[str]] | None = None,
                    audit: AuditLog | None = None) -> tuple[bool, list[str]]:
    """EU-386: LOUD dirty-tree boot warning — console + Telegram + one ``dirty_tree_start``
    audit line. A clean tree returns quietly with no output at all (the AC's negative half).

    ``forensics`` lets the drain reuse the probe it already ran for its ``process_start``
    fields instead of forking git twice; serve's boot (main.py) calls with no args — its own
    ``process_start`` line lives in server.serve(), a separate surface. The Telegram text is
    deliberately distinct from every routine notify so "restarted onto stray changes" reads
    differently from a normal boot at a glance. Never raises."""
    try:
        dirty, paths = forensics if forensics is not None else tree_forensics()
        if not dirty:
            return False, []
        shown = ", ".join(paths[:5]) + (" …" if len(paths) > 5 else "")
        print(f"⚠️  DIRTY working tree at {role} start — {len(paths)} uncommitted path(s): "
              f"{shown}", flush=True)
        try:
            (audit or AuditLog(cfg.audit_path)).record(
                "dirty_tree_start", role=role, modified_paths=paths)
        except Exception:  # noqa: BLE001 — the warning must still reach the Commander
            pass
        notify.send(f"⚠️ General {role} started on a DIRTY working tree — {len(paths)} "
                    f"uncommitted path(s): {shown}. An accidental respawn may be running "
                    "stray, unreviewed changes.")
        return True, paths
    except Exception:  # noqa: BLE001 — forensics must never block a boot
        return False, []


def _proc_start(pid: int) -> str | None:
    """The OS-reported start time of ``pid``, or None when it can't be determined.

    EU-368: a pid is NOT an identity — it's a slot number the kernel reuses. Pairing it with the
    process's start time is: the kernel will not hand out the same pid twice within the same second
    (recycling requires wrapping through the whole pid space), so (pid, start) names exactly one
    process for as long as it lives. ``ps -o lstart=`` is the only stdlib-free source that works on
    BOTH darwin (no /proc) and the linux VPS, which is why this shells out rather than adding psutil.
    Timeout-bound per EU-366's hang-proofed subprocess seams — a wedged ps must never freeze the
    cockpit badge, which calls this on the status path."""
    import subprocess

    try:
        r = subprocess.run(["ps", "-p", str(int(pid)), "-o", "lstart="],
                           capture_output=True, text=True, timeout=5)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None                       # no such process
    return r.stdout.strip() or None


# Our OWN start time never changes, so probe once and keep it: _pid_file_holds_our_pid() sits behind
# daemon_is_external(), which the cockpit calls on every status render — a `ps` fork per page render
# would be a needless regression of the EU-345 dashboard-latency work. A FOREIGN pid is deliberately
# NOT cached: a stale cache entry is exactly the recycled-pid lie this ticket exists to kill.
_our_start: str | None = None
_our_start_probed = False


def _our_proc_start() -> str | None:
    global _our_start, _our_start_probed
    if not _our_start_probed:
        _our_start = _proc_start(os.getpid())
        _our_start_probed = True
    return _our_start


def _read_pid_record() -> tuple[int, str | None] | None:
    """Parse the PID file into ``(pid, start)``, or None when there's nothing usable there.

    ``start`` is None for a LEGACY bare-int file (written by a pre-EU-368 daemon) or when the writing
    process couldn't probe its own start time — callers must then fall back to a bare liveness probe.
    Never raises: a missing, unreadable or garbled file simply has no record."""
    try:
        raw = _PID_FILE.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        doc = None
    if isinstance(doc, dict):
        try:
            pid = int(doc["pid"])
        except (KeyError, TypeError, ValueError):
            return None
        start = doc.get("start")
        return pid, (str(start) if start else None)
    try:
        return int(raw), None             # legacy bare-int format
    except ValueError:
        return None                       # garbage


# Refcount of live in-process autopilot() runs holding the (process-wide) PID file. The cockpit
# runs each per-app drain as a thread of the ONE serve process, so every drain writes the SAME
# pid — a bare contents==getpid() ownership check let the FIRST drain to exit delete the file
# while a sibling drain was still live, flipping daemon_running() (the cockpit badge's single
# source of truth, EU-73) to False mid-run and opening the daemon_is_external() start guards to
# a second conflicting daemon. _remove_pid() now unlinks only when the LAST in-process holder
# exits; the cross-process ownership check below still protects a foreign daemon's file.
_pid_holders = 0
_pid_lock = threading.Lock()


def _write_pid() -> None:
    """Register this autopilot run as a PID-file holder and (re)write the file (best-effort).

    Callers MUST pair every _write_pid() with exactly one _remove_pid() — autopilot() does this
    via its wrote_pid flag — or the refcount drifts and the file outlives / predeceases the runs.
    """
    global _pid_holders
    with _pid_lock:
        _pid_holders += 1
        try:
            # EU-368: pid + start time, not a bare pid — see _proc_start for why the pair is what
            # makes "is that still OUR daemon?" answerable at all.
            _PID_FILE.write_text(json.dumps({"pid": os.getpid(), "start": _our_proc_start()}))
        except OSError:
            pass


def _remove_pid() -> None:
    """Drop one PID-file hold; unlink only when the LAST in-process holder exits — and even then
    ONLY when the file still points at THIS process.

    Best-effort (failure is non-fatal). Two guards, each covering what the other can't:
      * the holder refcount keeps overlapping per-app drains in ONE process (cockpit threads,
        EU-103) from deleting the shared file while a sibling drain is still live;
      * the contents==os.getpid() ownership check keeps THIS process from deleting a file a
        DIFFERENT process (detached daemon / launchd keepalive) has since overwritten —
        otherwise daemon_running() would read 'not running' while that instance is alive.
    A missing or garbled file simply means there is nothing of ours to remove.
    """
    global _pid_holders
    with _pid_lock:
        if _pid_holders > 0:
            _pid_holders -= 1
        if _pid_holders > 0:
            return   # a sibling in-process drain still runs — the file must outlive THIS exit
        if _pid_file_holds_our_pid():
            try:
                _PID_FILE.unlink(missing_ok=True)
            except OSError:
                pass


def daemon_running() -> bool:
    """Return True when THIS unit's autopilot daemon process is alive.

    Liveness = the recorded pid answers ``os.kill(pid, 0)`` (signal 0 = existence check, no signal
    delivered) AND the process now sitting on that pid is the same one the record describes. Returns
    False if the file is missing, unreadable, garbled, or the process is gone — i.e. any I/O or
    permission error means "not running".

    EU-368 (2026-07-16 total audit): the identity half is the point. A bare kill(0) asks "is SOME
    process alive at this number?", and after a crash the answer drifts to yes as soon as the kernel
    recycles the dead daemon's pid to something unrelated — a long-dead daemon then reads as alive
    forever, autostart (EU-224) refuses to relaunch it, and the cockpit badge lies. Comparing the
    recorded start time against the live pid's start time asks the question we actually mean.

    A LEGACY bare-int record (no identity, written by a pre-EU-368 daemon) deliberately falls back to
    the old kill(0)-only answer rather than reading as stale: during an upgrade the cockpit runs new
    code while the launchd daemon may still be old, and answering "gone" for a daemon that is very
    much alive would open the second-daemon hole this same ticket closes. The window is one daemon
    lifetime — the next _write_pid() re-records it in the identified format.

    This is the single source of truth for the cockpit ON/OFF badge (EU-73): it reflects reality
    even when autopilot was launched outside the cockpit process (terminal that was later closed,
    launchd keepalive daemon) where the in-memory ``_state['autopilot']['on']`` flag is stale.
    """
    rec = _read_pid_record()
    if rec is None:
        return False
    pid, start = rec
    try:
        os.kill(pid, 0)  # probe only — raises OSError(ESRCH) if gone, OSError(EPERM) if alive but not ours
    except (OSError, ValueError):
        return False
    if start is None:
        return True                       # legacy record — no identity to check against
    return _proc_start(pid) == start


def _alert_unclean_restart(audit) -> bool:
    """QW5 (2026-07-05): detect + announce an unclean previous shutdown at startup.

    A leftover PID file whose process is dead means the previous autopilot was killed without
    cleanup — the Jul-1 crash signature (an autopilot_start never closed by autopilot_stop).
    Under the launchd keepalive daemon this fires on every auto-restart after a crash, so a
    restart is never silent: one audit event + one Telegram alert. Returns True when it fired.
    Best-effort — never raises, never blocks startup."""
    rec = _read_pid_record()
    if rec is None:
        return False
    stale = str(rec[0])
    if stale != str(os.getpid()) and not daemon_running():
        audit.record("autopilot_unclean_restart", stale_pid=stale)
        notify.send(f"⚠️ Autopilot restarted after an unclean shutdown "
                    f"(previous PID {stale} died without cleanup).")
        return True
    return False


def _pid_file_holds_our_pid() -> bool:
    """True when the autopilot PID file records THIS process's PID.

    A cockpit Start runs autopilot in a background thread of the cockpit process, which writes this
    process's PID via ``_write_pid()``. This lets the per-app Start guard tell its OWN in-process
    autopilot apart from a detached daemon living in a DIFFERENT process.

    EU-368: the identity is checked too, so a record left by a DEAD predecessor whose pid the kernel
    has since handed to us doesn't read as ours — _remove_pid() sits on this, and deleting on a
    number-only match is how a foreign record gets clobbered in the first place."""
    rec = _read_pid_record()
    if rec is None or rec[0] != os.getpid():
        return False
    return rec[1] is None or rec[1] == _our_proc_start()


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
    except json.JSONDecodeError:
        # A corrupt park file maps to "nothing parked" — every parked ticket silently un-parks.
        # Keep the fail-open direction (a stuck park set is worse) but say it happened
        # (2026-07-19 stabilization: this was a silent swallow).
        print(f"  ⚠ blocked_tickets.json is corrupt — treating as no parked tickets ({p})",
              flush=True)
        return set()
    except OSError:
        return set()


def save_blocked(cfg: Config, blocked: set[str]) -> None:
    # Authoritative overwrite, but taken under the shared cross-thread + cross-process lock so it can't
    # interleave with a concurrent write (the Telegram poller's /unblock) and lose one side's update.
    # EU-381 pattern (2026-07-19 stabilization): one bounded retry before the best-effort swallow — a
    # silently dropped write here un-parks every parked ticket with zero operator signal.
    snapshot = sorted(blocked)
    for attempt in (0, 1):
        try:
            locking.locked_rmw(_blocked_file(cfg), lambda _current: snapshot, default=[])
            return
        except (OSError, ValueError) as exc:
            if attempt:
                print(f"  ⚠ blocked_tickets.json write failed twice ({exc}) — park set may be "
                      "stale", flush=True)
                return
            time.sleep(0.05)


# ── EU-385 (EU-224a): persisted drain-arm intent → serve-boot auto-resume ────────────────────
# The live gap (2026-07-17): a 13:11 crash-respawn left a drain dead for 66+ minutes because
# nothing remembered it was RUNNING. A continuous LIVE drain persists its arm here
# (state=RUNNING); the drain's finally retires it (state=STOPPED + the EU-232 stop reason) on
# EVERY clean stand-down, so only an abrupt process death — the finally never ran — leaves
# RUNNING behind. That is the exact signal resume_armed_drains() (called from main.py's serve
# boot) keys on. Conservative by design: a Commander-ordered stop must never be resurrected, so
# resume also honours the EU-356 autopilot_stop_requested audit trail, which survives a crash
# that beat the finally to this file.

_RESUME_AUDIT_TAIL_BYTES = 2_000_000   # the stop-request scan reads only the audit's recent tail


def _intent_file(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("autopilot_intent.json")


def load_drain_intent(cfg: Config) -> dict:
    """The persisted per-app arm map ({app_or_"": {state, armed_ts, …}}); {} when absent."""
    try:
        data = json.loads(_intent_file(cfg).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def record_drain_intent(cfg: Config, app_name: str | None) -> None:
    """Arm: persist that this drain (``""`` = unit-wide) is RUNNING. Best-effort, never raises."""
    key = app_name or ""

    def _set(cur):
        cur = cur if isinstance(cur, dict) else {}
        cur[key] = {"state": "RUNNING", "armed_ts": time.time(), "pid": os.getpid()}
        return cur

    try:
        locking.locked_rmw(_intent_file(cfg), _set, default={}, corrupt_to_default=True)
    except (OSError, ValueError) as exc:
        # A dropped arm-write silently disarms EU-385 crash-resume — the exact 66-minute
        # dead-drain gap the feature exists to close. Say so (2026-07-19 stabilization).
        print(f"  ⚠ drain-intent arm write failed ({exc}) — crash auto-resume is DISARMED for "
              f"'{key or 'unit-wide'}' until the next arm", flush=True)


def clear_drain_intent(cfg: Config, app_name: str | None, reason: str) -> None:
    """Retire the arm with WHY (kept, not deleted — armed_ts/reason are boot forensics)."""
    key = app_name or ""

    def _set(cur):
        cur = cur if isinstance(cur, dict) else {}
        rec = cur.get(key) if isinstance(cur.get(key), dict) else {}
        rec.update({"state": "STOPPED", "stopped_ts": time.time(), "reason": reason})
        cur[key] = rec
        return cur

    try:
        locking.locked_rmw(_intent_file(cfg), _set, default={}, corrupt_to_default=True)
    except (OSError, ValueError) as exc:
        # A dropped retire-write leaves RUNNING behind — the next boot would resurrect a drain
        # the Commander stopped. Say so (2026-07-19 stabilization).
        print(f"  ⚠ drain-intent retire write failed ({exc}) — '{key or 'unit-wide'}' may "
              "auto-resume on the next boot despite this stop", flush=True)


def _parse_audit_ts(ts) -> float | None:
    """audit.py's local-time ``%Y-%m-%dT%H:%M:%S%z`` stamp → epoch seconds; None if unreadable."""
    from datetime import datetime

    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(ts), fmt).timestamp()
        except (TypeError, ValueError):
            continue
    return None


def _commander_stopped_since(cfg: Config, app_key: str, armed_ts: float) -> bool:
    """True when the audit shows a Commander stop order for this app at/after the arm.

    The intent file alone has a race: Stop clicked → the process dies BEFORE the drain's finally
    retires the arm → the file still says RUNNING. The EU-356 ``autopilot_stop_requested`` event
    is recorded by the cockpit route on the click itself, so it survives that crash — it is the
    discriminator that keeps auto-resume from resurrecting an explicitly-stopped drain. Reads
    only the audit tail (boot-time, once; EU-363 owns whole-history hygiene). Conservative on
    doubt: an unparseable stop-request timestamp BLOCKS the resume."""
    p = Path(cfg.audit_path)
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > _RESUME_AUDIT_TAIL_BYTES:
                f.seek(size - _RESUME_AUDIT_TAIL_BYTES)
                f.readline()          # drop the partial line the seek landed in
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return False                  # no audit at all → nothing ever ordered a stop
    for ln in raw.splitlines():
        if '"autopilot_stop_requested"' not in ln:
            continue
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if row.get("event") != "autopilot_stop_requested":
            continue
        if (row.get("app") or "") != app_key:
            continue
        ts = _parse_audit_ts(row.get("ts"))
        # ≥ armed_ts - 1.0: the audit stamp has 1s resolution — a same-second Stop still blocks.
        if ts is None or ts >= armed_ts - 1.0:
            return True
    return False


def resume_armed_drains(cfg: Config, *, wait_s: float | None = None) -> list[str]:
    """EU-385: serve-boot auto-resume of drains persisted as RUNNING by a previous process.

    Called from main.py's serve path — the one line every cockpit boot (CLI and launchd daemon
    alike) takes — because server.serve() is a separate surface. Mirrors the cockpit Start
    route's shape (claim_run → daemon _bg thread → release_run in its finally) so a resumed
    drain is indistinguishable from a manually-started one, including its own process_start +
    autopilot_start audit pair and its cockpit Stop button. Skips, in order: an external daemon
    owning the queue, an unhealthy unit (intent kept for the next boot), a Commander stop order
    in the audit (intent retired — never resurrect), a vanished app, an unrunnable GLM pick
    (EU-190: no silent fallback), a held run slot. ``wait_s`` joins the spawned threads (tests).
    Never raises: the boot must proceed no matter what."""
    import asyncio
    import copy

    from . import backend_pref, backends, cockpit_state, health

    resumed: list[str] = []
    threads: list[threading.Thread] = []
    try:
        armed = sorted((k, r) for k, r in load_drain_intent(cfg).items()
                       if isinstance(r, dict) and r.get("state") == "RUNNING")
        if not armed:
            return []
        if daemon_is_external():
            print("↻ auto-resume skipped — an external autopilot daemon already owns the queue",
                  flush=True)
            return []
        try:
            healthy = bool(health.summary(cfg).get("healthy"))
        except Exception:  # noqa: BLE001 — a broken health probe must not veto crash recovery
            healthy = True
        if not healthy:
            print("↻ auto-resume skipped — health problems; armed intent kept for the next boot",
                  flush=True)
            return []
        audit = AuditLog(cfg.audit_path)
        for key, rec in armed:
            try:
                # "" sorts first: a resumed unit-wide drain already covers every app, and the
                # cockpit refuses a per-app Start while a unit-wide run is active — mirror it.
                if "" in resumed:
                    break
                app_name = key or None
                armed_ts = float(rec.get("armed_ts") or 0.0)
                if _commander_stopped_since(cfg, key, armed_ts):
                    clear_drain_intent(cfg, app_name, "commander-stop-honoured-at-boot")
                    audit.record("autopilot_resume_skipped", app=key, reason="commander-stop")
                    print(f"↻ auto-resume: NOT resuming {key or 'unit-wide'} — the Commander "
                          "ordered a stop after it armed", flush=True)
                    continue
                if app_name is not None:
                    try:
                        cfg.app(app_name)
                    except (KeyError, AttributeError):
                        clear_drain_intent(cfg, app_name, "app-gone")
                        audit.record("autopilot_resume_skipped", app=key, reason="app-gone")
                        continue
                ap_cfg = copy.copy(cfg)
                ap_cfg.dry_run = False   # a resumed drain is live by definition (mirrors Start)
                # EU-190/EU-223 sticky pick + 2026-07-19 MAIN/SECONDARY resolution
                bk, _fb_why = backends.resolve_for_run(ap_cfg, app_name)
                ap_cfg.model_backend = bk
                if _fb_why:
                    audit.record("model_fallback", app=key, backend=bk, reason=_fb_why)
                    print(f"↻ auto-resume: {_fb_why}", flush=True)
                if bk == backends.GLM and not backends.available("glm"):
                    audit.record("autopilot_resume_skipped", app=key, reason="glm-unconfigured")
                    print(f"↻ auto-resume: NOT resuming {key or 'unit-wide'} — GLM selected but "
                          "unconfigured (intent kept)", flush=True)
                    continue
                ev = threading.Event()
                if not cockpit_state.claim_run(app_name, dry_run=False, stop_event=ev):
                    audit.record("autopilot_resume_skipped", app=key, reason="slot-held")
                    continue
                st = cockpit_state.get_state(app_name)
                st["autopilot_on"] = True

                def _bg(ap_cfg=ap_cfg, app_name=app_name, ev=ev, st=st):
                    try:
                        asyncio.run(autopilot(ap_cfg, app_name, once=False, stop_event=ev))
                    except Exception as exc:  # noqa: BLE001 — mirror server.py's _bg
                        st["last_msg"] = f"autopilot error: {exc}"
                    finally:
                        st["autopilot_on"] = False
                        cockpit_state.release_run(app_name)

                t = threading.Thread(target=_bg, daemon=True)
                t.start()
                threads.append(t)
                audit.record("autopilot_resume", app=key, armed_ts=armed_ts)
                print(f"🔁 auto-resumed drain: {key or 'all backlog apps'} — it was RUNNING when "
                      "the previous process died", flush=True)
                notify.send(f"🔁 Auto-resumed the {key or 'unit-wide'} drain after a restart — "
                            "it was RUNNING when the previous process died (EU-385).")
                resumed.append(key)
            except Exception:  # noqa: BLE001 — one bad entry must not strand its siblings
                continue
        if wait_s is not None:
            for t in threads:
                t.join(timeout=wait_s)
        return resumed
    except Exception:  # noqa: BLE001 — boot must proceed no matter what
        return resumed


# ── EU-398 (2026-07-19 senior-workflow): boot reconcile of In Progress tickets ────────────────
# A killed/crashed serve process (the AUTO-177 kill, 2026-07-19) leaves its ticket In Progress
# with no comment, no transition, no audit — the board shows work happening on a DEAD run, and
# recovery relied on the next drain happening to resume In Progress first via queue_statuses.
# At serve boot we now list In Progress tickets; any with NO active run AND no terminal audit
# event after their last `ticket_start` get a "resumed after an unclean stop" comment + a
# re-queue (or an honest park on recurrence), audited as `boot_reconcile`.

# The single source of truth for "did this run reach a terminal outcome?" is the audit event the
# Outcome enum records through contracts.AUDIT_EVENT_OUTCOME (merged / pr_opened / needs_human /
# ticket_exception / dryrun_land / pm_triage + the no_changes / escalated / scrum_split aliases).
# A `ticket_start` with none of these after it is a run that never closed — the kill signature.
_BOOT_RESUME_COMMENT = (
    "🔁 Resumed after an unclean stop — the previous run on this ticket was interrupted (process "
    "killed/crashed with no terminal outcome); re-queuing so the drain picks it up again (EU-398)."
)
_BOOT_PARK_COMMENT = (
    "⛔ Parked after repeated unclean stops — this ticket was left mid-run more than once, so the "
    "board was showing phantom progress. Moved to Blocked; reply /unblock {tid} or move it back to "
    "To Do to retry (EU-398)."
)


def _dangling_in_progress(in_progress_ids, audit_rows, active_ids) -> list[str]:
    """EU-398 core: which In Progress tickets were left dangling by a killed/crashed run.

    A ticket is DANGLING iff ALL hold:
      - it is In Progress on the board now (``in`` in_progress_ids),
      - the audit shows a ``ticket_start`` for it (the drain once began it),
      - its most-recent ``ticket_start`` has NO terminal audit event at/after it — the run never
        reached a terminal outcome (the ``AUDIT_EVENT_OUTCOME`` keys are the single source of
        truth; a terminal AT/AFTER the last start means the run closed, not a kill),
      - it is not in ``active_ids`` (a run is genuinely in flight for it right now — leave it).

    Pure + order-independent: pass ``audit_rows`` as any iterable of parsed event dicts (a ts the
    audit's own ``%Y-%m-%dT%H:%M:%S%z`` stamp can't parse counts as 0 — conservative: an unreadable
    terminal is treated as older than a readable start, so it can't mask a kill). Returns the sorted
    dangling ids. This is the boot-reconcile seam's one testable core — fail-first pinned by
    tests/eu398_boot_reconcile_test.py §1."""
    terminal = set(AUDIT_EVENT_OUTCOME.keys())
    last_start: dict[str, float] = {}
    last_terminal: dict[str, float] = {}
    for ev in audit_rows:
        tid = str((ev or {}).get("ticket_id") or "")
        if not tid:
            continue
        kind = (ev or {}).get("event")
        if not kind:
            continue
        ts = _parse_audit_ts((ev or {}).get("ts")) or 0.0
        if kind == "ticket_start":
            if ts >= last_start.get(tid, -1.0):
                last_start[tid] = ts
        elif kind in terminal:
            if ts >= last_terminal.get(tid, -1.0):
                last_terminal[tid] = ts
    out: list[str] = []
    for tid in in_progress_ids:
        if tid in active_ids:
            continue                       # a live run is working it — untouched
        start = last_start.get(tid)
        if start is None:
            continue                       # never started in the visible window → can't classify
        if last_terminal.get(tid, -1.0) >= start:
            continue                       # a terminal closed the run at/after its last start
        out.append(tid)
    return sorted(out)


def _read_audit_rows(audit_path) -> list[dict]:
    """Every line of the audit log parsed into event dicts (file order, oldest→newest).

    Best-effort: a missing/garbled file or line is skipped, never raised. Boot-time, once per
    boot — rotation (EU-363) keeps the live file bounded, so this is a bounded read."""
    try:
        text = Path(audit_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict] = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            ev = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(ev, dict):
            rows.append(ev)
    return rows


def _prior_resumed_ids(audit_rows) -> set[str]:
    """EU-398 recurrence trigger: ticket ids a PREVIOUS ``boot_reconcile`` already resumed.

    Re-queueing a ticket that already came back from one unclean stop and got killed AGAIN would
    just loop the kill — so the reconcile honest-PARKs a recurring id instead of resuming it.
    Sourced from the SAME audit read as the dangling core (no second file parse)."""
    out: set[str] = set()
    for ev in audit_rows:
        if (ev or {}).get("event") != "boot_reconcile":
            continue
        for tid in (ev.get("resumed") or []):
            out.add(str(tid))
    return out


def boot_reconcile(cfg: Config, *, audit: "AuditLog | None" = None,
                   active_apps: set[str] | None = None) -> dict:
    """EU-398: at serve boot, reconcile In Progress tickets left dangling by a killed/crashed run.

    Lists In Progress tickets across every jira-backed app (via the same ``get_ready_tasks`` →
    ``_jql_for_status('In Progress')`` path the drain resumes through), and for each that has NO
    active run and NO terminal audit event after its last ``ticket_start`` (see
    ``_dangling_in_progress``): posts a "resumed after an unclean stop" comment and RE-QUEUES it
    (leaves it In Progress so the next drain cycle resumes it). A RECURRING dangle — a prior boot
    already resumed the same id and it is STILL dangling — is HONESTLY PARKED instead (Blocked +
    comment) so a recurring kill can't loop the board into phantom progress forever.

    ``active_apps`` overrides the live run-state check (tests); the default derives from
    ``cockpit_state.active_runs()`` so a ticket an in-flight drain is working right now is untouched.
    Records one ``boot_reconcile`` audit event with the resumed / parked / skipped_active ids.
    Returns ``{resumed, parked, skipped_active, dangling}``. Never raises — the boot proceeds."""
    from .backlog.base import make_backlog

    summary = {"resumed": [], "parked": [], "skipped_active": [], "dangling": []}
    try:
        audit = audit or AuditLog(cfg.audit_path)
        dry = bool(getattr(cfg, "dry_run", False))

        # 1) gather In Progress tickets across jira-backed apps (cache one adapter per app)
        adapters: dict[str, object] = {}

        def _bl(app):
            if app.name not in adapters:
                adapters[app.name] = make_backlog(app)
            return adapters[app.name]

        in_progress: list[tuple] = []
        for app in getattr(cfg, "apps", None) or []:
            if getattr(app, "backlog_backend", "none") == "none":
                continue
            try:
                for t in _bl(app).get_ready_tasks(100):
                    if "progress" in (getattr(t, "status", "") or "").lower():
                        in_progress.append((app, t))
            except Exception:  # noqa: BLE001 — one unreachable board must not abort the pass
                continue
        ip_ids = {t.id for _, t in in_progress}
        by_id = {t.id: (app, t) for app, t in in_progress}

        # 2) a live run's tickets are genuinely running — untouched
        if active_apps is None:
            try:
                from . import cockpit_state
                active_apps = {str(a) for a in cockpit_state.active_runs()}
            except Exception:  # noqa: BLE001 — fall back to "nothing active" on any probe failure
                active_apps = set()
        active_ids = {t.id for app, t in in_progress if app.name in (active_apps or set())}

        # 3) one audit read → dangling core + the recurrence set
        rows = _read_audit_rows(cfg.audit_path)
        dangling = _dangling_in_progress(ip_ids, rows, active_ids)
        prior_resumed = _prior_resumed_ids(rows)

        # 4) re-queue (default) or honest-park (recurrence); skip side-effects in dry-run
        resumed: list[str] = []
        parked: list[str] = []
        for tid in dangling:
            hit = by_id.get(tid)
            if hit is None:
                continue
            app, t = hit
            if tid in prior_resumed:
                parked.append(tid)
            else:
                resumed.append(tid)
            if dry or getattr(t, "ephemeral", False):
                continue                       # dry-run: compute only, no board writes
            try:
                bl = _bl(app)
                if tid in prior_resumed:
                    bl.set_status(t, "Blocked")
                    bl.add_comment(t, _BOOT_PARK_COMMENT.format(tid=tid))
                else:
                    bl.add_comment(t, _BOOT_RESUME_COMMENT)
            except Exception:  # noqa: BLE001 — a board hiccup on one ticket must not abort the pass
                continue

        # 5) one audit event with the affected ids (only when something was touched)
        skipped_active = sorted(active_ids & ip_ids)
        if resumed or parked:
            try:
                audit.record("boot_reconcile", resumed=sorted(resumed), parked=sorted(parked),
                             skipped_active=skipped_active)
            except Exception:  # noqa: BLE001
                pass
            print(f"  · boot reconcile: resumed {sorted(resumed)}; parked {sorted(parked)}; "
                  f"untouched {skipped_active} (active)", flush=True)
        summary = {"resumed": sorted(resumed), "parked": sorted(parked),
                   "skipped_active": skipped_active, "dangling": sorted(dangling)}
        return summary
    except Exception:  # noqa: BLE001 — the boot must proceed no matter what
        return summary


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


# EU-278: the identity of the drain running on this thread/task — the app it was started for, and the
# Jira projects that app owns. Stamped ONCE by autopilot() at drain start (_enter_drain_scope) and
# read back by save_error_counts, whose (cfg, counts) signature deliberately carries no owner: cfg is
# NOT a usable source of scope, because server.py hands each per-app drain a `copy.copy(cfg)` that
# still lists EVERY app — trusting it would make each drain claim every project and clobber its
# neighbours' keys, which is precisely the EU-274 defect.
#
# A ContextVar rather than a thread-local so it survives both shapes the drain runs in: a cockpit
# thread per app, and an asyncio task inside that thread. Threads and tasks each start from an empty
# context, so an unstamped caller reads None and claims nothing.
_drain_scope: contextvars.ContextVar[tuple[str, frozenset[str]] | None] = contextvars.ContextVar(
    "general_drain_scope", default=None)


def _enter_drain_scope(cfg: Config, app_name: str | None) -> None:
    """Stamp THIS drain's identity for the rest of its life. Best-effort: an unresolvable app leaves
    the scope unstamped, which only costs the empty-counts reset below (safe direction)."""
    apps = []
    try:
        apps = [cfg.app(app_name)] if app_name else list(getattr(cfg, "apps", None) or [])
    except (KeyError, AttributeError):
        apps = []
    projects = frozenset(
        p for p in ((getattr(a, "backlog", None) or {}).get("project_key") for a in apps) if p)
    _drain_scope.set((app_name or "*", projects))


# EU-274: per-(file, drain) memory of the last `counts` snapshot THIS drain wrote. It is a SECONDARY
# delete signal, only needed for the one case the primary (project-prefix) signal can't cover: a save
# whose `counts` is EMPTY because the drain's last tracked ticket just parked/succeeded (its key
# popped) — an empty dict carries no prefix to reveal which project it owns.
#
# EU-278: keyed on the DRAIN's identity (its app name), not `threading.get_ident()`. The old key was
# the OS thread's number, which the kernel recycles: _prune_dead_error_counts_seen dropped only rows
# whose ident was NOT live, so a recycled-but-live ident kept its dead predecessor's row and a new
# drain could inherit a baseline that was never its own — the one path left that could reintroduce
# the foreign-key clobber EU-274 fixed. (The old comment here claimed the prune prevented exactly
# that; it did not.) An app name is stable for the drain's whole life and unique across concurrent
# drains — EU-64 gives each project a single writer — so ident reuse is no longer representable, and
# the cache is bounded by app count instead of run-thread churn, which is why the prune is gone.
_error_counts_seen: dict[tuple[str, str], dict[str, int]] = {}
_error_counts_seen_lock = threading.Lock()


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
    #   (b) this same drain wrote the key last save at the value still on disk — the only extra case,
    #       for when (a) has no signal because `counts` emptied as the last tracked ticket parked.
    #
    # A key whose project no other-writer touches and that neither signal claims stays put, so a
    # concurrent foreign increment is never clobbered.
    #
    # EU-278: when `counts` is EMPTY it carries no prefix, so (a) is silent and (b) is the only signal
    # left — and (b) is cold on a drain's first save after a restart. That combination (fresh process,
    # last tracked ticket parks) left the parked key on disk at the threshold, so the next /unblock
    # bought it ~1 retry instead of 3. Falling back to the drain's OWN declared projects gives (a) the
    # call-time signal it's missing. Scope comes from the stamped drain identity, never from cfg.apps
    # — see _drain_scope for why cfg would over-claim. Unstamped => empty => claims nothing.
    path = _error_counts_file(cfg)
    scope = _drain_scope.get()
    cache_key = (str(path), scope[0] if scope else "")
    with _error_counts_seen_lock:
        baseline = _error_counts_seen.get(cache_key, {})
    owned = {_ticket_project(k) for k in counts} or set(scope[1] if scope else ())

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

    # EU-381: ONE bounded retry before the best-effort swallow. Evidence (2026-07-17, EU-218's
    # 10-consecutive-runs AC): eu256's AC3 union went red once in 10 full suites (~5% under maximal
    # box load) with no locking defect — locked_rmw serialises correctly by construction — so the
    # lost increment came from a single transient flock/open OSError being swallowed here, silently
    # dropping the write and resetting park-after-3 progress. A transient clears in milliseconds,
    # so one short-paused retry recovers it; a deterministic failure (e.g. corrupt JSON ->
    # ValueError) just fails the retry too and falls back to the original best-effort contract —
    # never an exception out of a save, so the drain can't break on bookkeeping.
    for attempt in (0, 1):
        try:
            locking.locked_rmw(path, _merge, default={})
        except (OSError, ValueError):
            if attempt:
                return           # both tries failed -> keep the best-effort swallow (drop the write)
            time.sleep(0.05)     # brief pause for the transient to clear, then the one retry
            continue
        with _error_counts_seen_lock:
            _error_counts_seen[cache_key] = dict(counts)
        return


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
                # Consume-on-detect: advance the baseline to THIS answer immediately, so the same
                # comment resumes the ticket once — not every ~2-min cycle forever (the EU-335
                # '▶️ Resuming' Telegram spam loop, 2026-07-16). A later different comment still
                # differs from the new baseline and resumes again.
                decisions.consume_answer(cfg, tid, answer)
    return out


def _is_barren_cycle(reports: list, explained_ids: set) -> bool:
    """EU-357: True when this cycle produced ONLY unexplained instant failures — every report
    ERRORED and none was classified infra/auth/base (those have their own dedicated holds). That
    is the fingerprint of the 5-hour-window refusal storm (calls fail as 'transient', nothing
    lands, nothing is infra). N of these in a row arm the usage-exhaustion breaker. Pure so the
    predicate is unit-testable without driving the whole autopilot loop."""
    if not reports:
        return False
    if not all(r.outcome is Outcome.ERRORED for r in reports):
        return False
    return not any(r.ticket_id in explained_ids for r in reports)


def _assemble_worklist(raw: list, blocked: set, resumed: dict, cap: int) -> list:
    """Three-tier pick order (EU-87 / EU-252 / EU-344), pure so the picker layer is testable.

    Input ``raw`` is ``[(app, ticket)]`` in the adapter's board order (priority DESC, Rank ASC —
    the drain-HIGHEST-first lever). Output preserves that order WITHIN each tier:

      Tier-1  In Progress — resume already-started work first; NEVER capped (an in-flight ticket
              must never be starved by fresh To Do, EU-252).
      Tier-2  answered/resumed — parked tickets the Commander replied to on Jira; additive/uncapped,
              de-duped against anything already surfacing In Progress via the drain.
      Tier-3  To Do — the fresh backlog in board-Rank (priority) order; ``cap`` bounds ONLY this
              slice, applied AFTER the priority sort so a low-cap cycle still takes the HIGHEST
              To Do tickets, never an ascending-key prefix (the EU-344 symptom).

    The blocked filter runs over the FULL drawn window first so a jql-override that ranks an In
    Progress fragment low can't be clipped before Tier-1 rescues it (EU-252)."""
    raw = [(a, t) for (a, t) in raw if t.id not in blocked]
    in_progress = [(a, t) for (a, t) in raw
                   if (s := getattr(t, "status", None)) and "progress" in s.lower()]
    to_do = [(a, t) for (a, t) in raw
             if not ((s := getattr(t, "status", None)) and "progress" in s.lower())][:cap]
    in_drain = {t.id for _, t in raw}
    answered_items = [v for k, v in (resumed or {}).items() if k not in in_drain]
    return in_progress + answered_items + to_do


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
        missing = infra_classify.missing_toolchain(app, cfg)   # EU-322: cfg = unit-wide worktree_setup_cmd fallback
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
        # EU-386: additive dirty-tree fields on the same forensic line — a respawn onto
        # uncommitted changes must be visible in the audit, and LOUD (console + Telegram)
        # when it happens; a clean tree stays silent.
        _dirty, _paths = tree_forensics()
        audit.record("process_start", role="autopilot", app=app_name or "", sha=_sha,
                     pid=os.getpid(), dirty=_dirty, modified_paths=_paths)
        warn_dirty_tree(cfg, "autopilot", forensics=(_dirty, _paths), audit=audit)
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
    wrote_pid = False         # gates the finally's _remove_pid(): every _write_pid() must be paired with
                              # exactly ONE _remove_pid() (the holder refcount), so a raise BEFORE the
                              # write must not decrement a sibling drain's hold on the shared file.
    armed_intent = False      # EU-385: True once this drain persisted its RUNNING arm (continuous
                              # live only) — gates the finally's clear_drain_intent, mirroring wrote_pid.
    try:
        # Write the PID file FIRST, inside the try, so the finally's _remove_pid() always runs — even
        # if any setup below (the signal registration, claim_run, a Telegram send) raises. Otherwise an
        # early raise would orphan /tmp/general-autopilot.pid pointing at this live process and pin
        # daemon_running() True forever (EU-73 — this is the single source of truth for the cockpit badge).
        _alert_unclean_restart(audit)   # QW5: a restart after a crash is never silent
        _write_pid()
        wrote_pid = True
        # EU-278: stamp WHO this drain is before any save_error_counts can fire, so an empty-counts
        # park can tell its own parked key from a concurrent drain's without guessing from cfg.
        _enter_drain_scope(cfg, app_name)
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
        # EU-385: persist this drain's arm so a crash/KeepAlive respawn can auto-resume it at the
        # next serve boot (resume_armed_drains). Only a CONTINUOUS LIVE drain arms — a --once
        # cycle or a dry-run preview is not a standing intent. The finally retires the arm with
        # the stop reason on every clean stand-down; only an abrupt process death (no finally)
        # leaves state=RUNNING behind, which is exactly the signal the boot-resume keys on.
        if not once and not cfg.dry_run:
            record_drain_intent(cfg, app_name)
            armed_intent = True
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
        # EU-356: bind THIS loop's stop_event as the app's authoritative stop signal — whether or not
        # the claim above succeeded. claim_run binds only on success, so on a failed claim (which this
        # function deliberately survives, running on with owns_run_state=False) the state kept pointing
        # at the PREVIOUS owner's Event — the cockpit's Stop would set that dead Event, read it back as
        # "stopping", and this loop, polling the Event nobody could reach, would drain on. (Forensics
        # note: the live 2026-07-15 22:39→01:53 incident was NOT this path — its binding was correct
        # and its root cause was run_loop's disarmed stop checks, fixed at the run_loop call below.
        # This dead-event hole is the ADJACENT stop-path defect the same investigation proved
        # reachable, closed here.) The loop is the authority on its own stop signal, so it rebinds
        # unconditionally on entry.
        #
        # Ordering is load-bearing on BOTH sides: bind BEFORE raising autopilot_on (below), and in the
        # finally drop autopilot_on BEFORE unbinding. autopilot_on is what makes the cockpit offer a
        # Stop button and what gates ``stopping`` — so there must be no instant where it is true while
        # the reachable event isn't this loop's.
        cockpit_state.bind_stop_event(run_key, stop_event)
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
        # EU-357: usage-exhaustion circuit breaker. The 5-hour Max window running out surfaces as a
        # per-call REFUSAL classified "transient" (agent.py) — NOT a plan-limit hit (the probe goes
        # blind, plan_limit_hit fails open) and NOT infra (so no offline-hold). So the drain churned
        # 41 instant-failing picks in 38 min (2026-07-16 12:20 flood). This breaker is provider- and
        # cause-agnostic: N consecutive cycles where a non-empty worklist produced ONLY unexplained
        # ERRORED reports (none infra/auth/base-classified — those have their own holds) → pause for
        # a cooldown, one alert, one audit event; auto-resume when the cooldown expires.
        usage_hold_until = 0.0
        consecutive_barren = 0
        # EU-310: a ticket that modifies the unit's OWN code merges but its post-merge Jira transition
        # can be lost or lag propagation, so the board still shows In Progress and the drain re-picks
        # and rebuilds already-merged code (EU-307, 2026-07-14 — re-picked 19s after merge). Track the
        # ids merged this process and exclude them from the worklist for a short cooldown, regardless
        # of what the board says — the durable prevention that doesn't depend on the transition landing.
        recently_merged: dict[str, float] = {}
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

            # EU-357: usage-exhaustion hold — while the cooldown is active, hold ALL new work. The
            # breaker below arms it after N barren cycles; it auto-clears when the cooldown expires
            # (the 5h window will have rolled by then, or the transient storm passed).
            if time.time() < usage_hold_until:
                if once:
                    stop_reason = "usage-exhaustion"
                    break
                _sleep(max(30, interval), stop_event)
                continue
            if usage_hold_until and time.time() >= usage_hold_until:
                usage_hold_until = 0.0
                consecutive_barren = 0
                audit.record("usage_exhaustion_resume")
                notify.send("✅ Usage-exhaustion hold cleared — autopilot resuming.")
                print("  ✅ usage-exhaustion cooldown elapsed — resuming.", flush=True)


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
                # 2026-07-19 (Commander order): the SECONDARY model absorbs a plan-limit hit —
                # resolve_for_run returns the usable secondary when one is configured, so the
                # drain keeps working on it (loudly) instead of pausing. No secondary → the
                # pause below fires exactly as before.
                from . import backends as _bks
                _fb_bk, _fb_why = _bks.resolve_for_run(cfg, app_name)
                if _fb_why:
                    if getattr(cfg, "model_backend", None) != _fb_bk:
                        cfg.model_backend = _fb_bk
                        audit.record("model_fallback", app=app_name or "", backend=_fb_bk,
                                     reason=_fb_why)
                        notify.send(f"⇄ Plan limit hit — autopilot continues on the secondary "
                                    f"model ({_fb_bk}). It switches back when the limit resets.")
                        print(f"  ⇄ {_fb_why} — drain continues on {_fb_bk}", flush=True)
                    plan_limit_paused = False
                    # fall through to normal work — the secondary carries the drain
                else:
                    over_limits = plan_check.get("over_limits", [])

                    # Halt autopilot when plan limits are hit (no usable secondary configured)
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
            # Clear plan-limit state when no longer hit; also switch back to the MAIN model if
            # the fallback had engaged (the pref didn't change — only this drain's working copy).
            # Best-effort: minimal test cfgs may lack the attrs — never break the drain loop.
            try:
                from . import backend_pref as _bp
                _main_bk = _bp.active(cfg, app_name)
                if not plan_check.get("hit") and getattr(cfg, "model_backend", _main_bk) != _main_bk:
                    cfg.model_backend = _main_bk
                    audit.record("model_fallback_cleared", app=app_name or "", backend=_main_bk)
                    notify.send(f"⇄ Plan limit cleared — autopilot is back on the main model ({_main_bk}).")
            except Exception:  # noqa: BLE001
                pass
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


            # EU-387: a self-land earlier flagged a pending restart — this cycle boundary is where
            # "nothing is mid-build anywhere" is knowable, so the graceful exit decision lives
            # here (never at the land site, which is mid-run by definition). No-op without a flag.
            _maybe_self_restart(cfg, audit)

            blocked = load_blocked(cfg)   # re-read so /unblock takes effect live
            # EU-78: auto-clear ghost-parked tickets whose latest audit run already succeeded
            # (e.g. parked in a previous session, then ran manually and merged).
            blocked = _auto_clear_merged_ghosts(cfg, blocked, audit)
            # EU-229: auto-clear ghost pending decisions whose base ticket already merged
            _auto_clear_decision_ghosts(cfg, audit)
            # 2026-07-19: reconcile the needs stores against LIVE Jira statuses (throttled) — a
            # ticket the Commander moved to Done/QA or re-queued in Jira clears everywhere.
            try:
                from . import needs_sync as _nsync
                _nsync.reconcile(cfg, audit, ttl_s=600.0)
            except Exception:  # noqa: BLE001
                pass
            # EU-61: a parked ticket the Commander answered directly on Jira auto-resumes — lift it out
            # of the skip-set and put it at the FRONT of the queue (resume before taking new work).
            resumed = _resumable_answered(cfg, app_name, blocked)
            if resumed:
                # Re-read right before the write-back (same rule as the park path below): the
                # resume scan above does one Jira fetch per pending decision and can run for
                # MINUTES — writing the stale top-of-cycle snapshot back resurrects any ticket
                # the Commander /unblock'ed mid-scan (EU-218 came back from the dead this way
                # twice on 2026-07-16, 10:54 and 10:56).
                blocked = load_blocked(cfg) - set(resumed)
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
            # EU-310: drop keys merged in the last _MERGED_COOLDOWN_S — the board may still show them
            # In Progress (lost/lagging transition), and re-picking rebuilds already-merged code.
            _now_m = time.time()
            recently_merged = {k: ts for k, ts in recently_merged.items()
                               if _now_m - ts < _MERGED_COOLDOWN_S}
            _pick_exclude = blocked | set(recently_merged)
            # EU-344: the tier split is a pure helper so the picker layer is unit-testable — the
            # ticket's core finding was "priority doesn't steer" with the bug living HERE, yet no
            # regression pinned that To Do is picked in adapter (priority DESC, Rank ASC) order.
            worklist = _assemble_worklist(raw, _pick_exclude, resumed, cap)

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
                # EU-344: no silent pick-time skips — every exclusion emits an audit event naming
                # the tickets + reason, so "priority isn't steering" is diagnosable from the log
                # instead of inferred. (The base-hold is the picker layer's one uncaptured drop.)
                audit.record("pick_skip", reason="base-hold",
                             apps=sorted(red_base_hold), tickets=_dropped_held)

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

            # EU-356 (the ACTUAL 2026-07-15 incident fix): arm run_loop's ticket-boundary stop check
            # with this drain's own event. Until now the event armed only THIS loop's per-cycle check
            # — but one run_loop call IS a whole cycle, and EU-201 fragment injection extends its
            # worklist IN PLACE mid-run, so a "cycle" can run for hours (22:40→01:53 live: EU-321
            # split into EU-350..353 and the drain ordered at ~22:50 wasn't honoured until 01:53).
            # Deliberately the boundary-only channel, NOT stop_event= — the drain's promise is "let
            # the in-flight ticket land on DEV, then stand down", and the full channel would also arm
            # the pre-build/pre-merge aborts that kill or abandon the in-flight build. The signature
            # guard follows the loop.py `_land` idiom: many harnesses stub run_loop with a bare
            # (cfg, worklist, audit) fake, and this seam must not force them all to grow the kwarg.
            if "stop_between_tickets" in inspect.signature(run_loop).parameters:
                reports = await run_loop(cfg, worklist, audit, stop_between_tickets=stop_event)
            else:
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
            # EU-310: remember what merged this cycle so the next pick can't re-select it while the
            # board catches up to the Done/QA transition (or if that transition was lost).
            for r in reports:
                if r.outcome is Outcome.MERGED:
                    recently_merged[r.ticket_id] = time.time()

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

            # EU-357: usage-exhaustion breaker. A "barren" cycle is one where a non-empty worklist
            # produced ONLY ERRORED reports AND none were classified infra/auth/base (those have
            # their own holds above). That's the fingerprint of the 5h-window refusal storm — calls
            # instant-fail as "transient", nothing lands, nothing is infra. N barren cycles in a row
            # → pause for a cooldown so the drain stops churning the whole backlog to no effect.
            _explained = infra_errored | _auth_ids | _base_ids
            _unexplained = _is_barren_cycle(reports, _explained)
            if worklist and _unexplained:
                consecutive_barren += 1
            elif any(r.outcome not in (Outcome.ERRORED,) for r in reports):
                consecutive_barren = 0
            if consecutive_barren >= _MAX_BARREN_CYCLES and not usage_hold_until:
                usage_hold_until = time.time() + _USAGE_HOLD_COOLDOWN_S
                mins = int(_USAGE_HOLD_COOLDOWN_S // 60)
                audit.record("usage_exhaustion_hold", barren_cycles=consecutive_barren,
                             cooldown_s=_USAGE_HOLD_COOLDOWN_S)
                notify.send(f"⛔ Autopilot paused — {consecutive_barren} cycles in a row produced only "
                            f"instant failures with no landed work (usage window likely exhausted). "
                            f"Holding {mins} min to stop churn; auto-resumes after the cooldown.")
                print(f"  ⛔ usage-exhaustion breaker — {consecutive_barren} barren cycles; holding "
                      f"{mins} min to stop churn.", flush=True)

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
        # EU-356: retract this loop's stop signal now that it has stood down, so the Event can never
        # outlive the loop that polled it. A bound-but-dead Event is exactly what made the cockpit lie:
        # Stop set it, ``stopping`` read it back as true, and no loop was left to honour it. Identity-
        # checked inside, so if a NEWER drain for this app already rebound its own event while this one
        # was winding down, we leave the live binding alone instead of blanking it. Unconditional (not
        # gated on owns_run_state) — the mirror of the unconditional bind above.
        if run_state is not None:
            cockpit_state.unbind_stop_event(run_key, stop_event)
        # Release THIS project's run-state if we own it (clears active / run_started / stop_event) and
        # drop the dry/live tag, so the per-project board shows no stale run once autopilot stands down.
        # (run_state may still be None if claim_run() never ran — an early raise during setup.)
        if owns_run_state:
            cockpit_state.release_run(run_key)
            if run_state is not None:
                run_state["dry_run"] = None
        # Drop this run's PID-file hold on any exit path (KeyboardInterrupt, stop_event, budget halt,
        # once=True). The file itself is unlinked only by the LAST in-process holder — overlapping
        # per-app drains (cockpit threads, EU-103) share one process-wide file, and the first drain
        # to exit must not flip daemon_running() False while a sibling is still live. The launchd
        # daemon treats a missing PID file as "not running" — this is the handshake.
        if wrote_pid:
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
        # EU-385: ANY reasoned stand-down that reaches this finally — cockpit-stop, sigterm,
        # budget, plan-limit, an exception — retires the persisted arm, so the next serve boot
        # does not resurrect a drain that deliberately stood down. Only a process death that
        # skipped this finally leaves the RUNNING intent for resume_armed_drains to honour.
        if armed_intent:
            clear_drain_intent(cfg, app_name, stop_reason or "stand-down")
        if started:
            audit.record("autopilot_stop", reason=stop_reason or "once-complete")
