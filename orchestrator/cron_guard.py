"""EU-432 — cron job guard: timestamps, rotation, and failure alerting for ``council/cron.log``.

The 2026-07-22 VPS responsibility audit found the server's crontab failing two of its six jobs
every week for a month with zero signal: the crontab set no ``MAILTO``, there is no mail spool,
and ``council/cron.log`` (232 KB, 4,380 lines, born 2026-06-19, never rotated, zero timestamps,
71% of it the identical sync line) had writers but no reader anywhere in the repo. This module
wraps a cron-invoked command so that failure can no longer pass silently:

  • AC5 — every output line is written to ``council/cron.log`` with an ISO-8601 UTC timestamp +
    ``[job]`` tag, and the log is size-rotated (``cron.log`` → ``cron.log.1`` → … capped at
    ``keep``). Standard logrotate semantics: after rotation the path is gone until the next append
    recreates a fresh file, so concurrent ``>>`` appenders (self-update.sh) keep working.
  • AC4 — a job that exits non-zero surfaces to Telegram (via ``notify.send``), raising EXACTLY ONE
    alert per ``threshold`` consecutive failures (3 by default): alert when the failure streak
    crosses into each new block of ``threshold`` (3, 6, 9, …), never one-per-failure. A success
    resets the streak. The pin is the dedup: the defect was a month of silence, not a missing
    pager — so the fix is one signal, not thirty.

The guard never masks a failure and never blocks the command: it returns the wrapped command's
REAL exit code, and every side-effect (timestamping, rotation, state, alert) is best-effort and
individually guarded — a guard error must never be worse than the old silent ``>>`` redirect.

Invoked from the crontab via ``./general cron-guard --job <name> -- <command...>``: the launcher
activates the venv and loads ``.env``, so ``notify.send`` can reach Telegram; the command after
``--`` is the cron job itself. Dispatched early in ``_main`` (before the config preamble) so a
15-min cron tick stays cheap.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Callable

# ── defaults ──────────────────────────────────────────────────────────────────
ALERT_THRESHOLD_DEFAULT = 3          # consecutive failures before the FIRST Telegram alert
LOG_MAX_BYTES_DEFAULT = 2 * 1024 * 1024   # 2 MiB — rotate council/cron.log past this
LOG_KEEP_DEFAULT = 5                 # rotated generations to keep (cron.log.1 .. cron.log.5)

# A notifier takes one message string and returns None; the default wires to Telegram (a no-op
# when TELEGRAM_* is unset, so the guard is safe to run anywhere).
Notifier = Callable[[str], None]


def decide_alert(streak: int, threshold: int, last_alert_blocks: int) -> tuple[bool, int]:
    """Exactly one alert per ``threshold`` consecutive failures.

    Returns ``(should_alert, new_last_alert_blocks)``. Fires when ``streak`` crosses into a NEW
    block of ``threshold`` failures (at 3, 6, 9, … for threshold 3): one alert per block, never
    one-per-failure, never zero for a sustained outage. ``last_alert_blocks`` is the highest block
    already alerted, so a second run at the same streak does not double-fire. ``threshold <= 0``
    disables alerting entirely (returns ``(False, last_alert_blocks)``).
    """
    if threshold <= 0:
        return False, last_alert_blocks
    blocks = streak // threshold
    if blocks > 0 and blocks > last_alert_blocks:
        return True, blocks
    return False, last_alert_blocks


def stamp_line(job: str, text: str, now: float) -> str:
    """Prefix one output line with an ISO-8601 UTC timestamp and ``[job]`` tag.

    A trailing CR/LF is stripped (the appender adds the newline). ``now`` is epoch seconds so
    callers/tests can pin a deterministic timestamp.
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    return f"[{ts}] [{job}] {text.rstrip(chr(10)).rstrip(chr(13))}"


def rotate_log(path: str | Path, max_bytes: int, keep: int) -> None:
    """Size-based rotation: when ``path`` exceeds ``max_bytes``, roll it to ``path.1``, ``.2``, …
    capped at ``keep`` generations (older files are deleted). No-op when the file is missing or
    under the threshold.

    Shifts generations oldest-first: drop ``path.<keep>``, then ``path.<keep-1>`` →
    ``path.<keep>``, …, ``path`` → ``path.1``. After rotation ``path`` no longer exists — the next
    append recreates a fresh file (logrotate semantics; a concurrent ``>>`` appender simply opens
    a new file). Best-effort: any OSError is swallowed (rotation must never break the run).
    """
    p = Path(path)
    try:
        if not p.exists() or p.stat().st_size < max_bytes:
            return
    except OSError:
        return
    try:
        oldest = Path(f"{p}.{keep}")
        if oldest.exists():
            oldest.unlink()
        # Shift each surviving generation up by one (g = keep-1 … 0; g==0 is the base file).
        for g in range(keep - 1, -1, -1):
            src = p if g == 0 else Path(f"{p}.{g}")
            dst = Path(f"{p}.{g + 1}")
            if src.exists():
                src.rename(dst)
    except OSError:
        pass


# ── per-job failure-streak state ──────────────────────────────────────────────

def _load_state(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: str | Path, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)   # atomic on the same filesystem


def _send_alert(notifier: Notifier, job: str, streak: int, threshold: int,
                rc: int, output: str) -> None:
    """Best-effort Telegram alert for a job whose failure streak crossed a threshold."""
    tail = (output or "").strip().splitlines()
    last = tail[-1][:300] if tail else "(no output)"
    msg = (
        f"⚠️ cron job `{job}` has failed {streak} time(s) in a row on the server "
        f"(exit {rc}; alerts once every {threshold} consecutive failures). "
        f"Last output line:\n{last}\nSee council/cron.log."
    )
    try:
        notifier(msg)
    except Exception:  # noqa: BLE001 — alerting must never mask the command's real exit code
        pass


def run_guarded(
    job: str,
    command: list[str],
    *,
    log_path: str | Path,
    state_path: str | Path,
    threshold: int = ALERT_THRESHOLD_DEFAULT,
    max_bytes: int = LOG_MAX_BYTES_DEFAULT,
    keep: int = LOG_KEEP_DEFAULT,
    notifier: Notifier | None = None,
    now: float | Callable[[], float] | None = None,
) -> int:
    """Run ``command`` (a list), capture its combined output, and guard it.

    Writes each output line timestamped+tagged to ``log_path`` (rotating first if oversized),
    tracks the per-job consecutive-failure streak in ``state_path``, and alerts via ``notifier``
    (default ``notify.send``) on each threshold crossing — exactly one alert per ``threshold``
    failures. Returns the wrapped command's REAL exit code (never masked). Every side-effect is
    best-effort: a logging or alerting error cannot prevent the return of the true exit code.
    """
    from . import locking

    if notifier is None:
        from . import notify
        notifier = notify.send
    ts = now() if callable(now) else (time.time() if now is None else now)

    # ── run the command and capture its output + real exit code ───────────────
    rc = 0
    output = ""
    try:
        proc = subprocess.run(list(command), capture_output=True, text=True)
        rc = proc.returncode
        output = (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001 — a capture/exec failure is itself a job failure
        rc = 127
        output = f"cron_guard: could not run command {list(command)!r}: {exc}"

    # ── best-effort timestamp + rotate + append (AC5) ─────────────────────────
    try:
        rotate_log(log_path, max_bytes, keep)
        lines = output.splitlines() or ["(no output)"]
        for raw in lines:
            locking.locked_append(log_path, stamp_line(job, raw, ts))
    except Exception as exc:  # noqa: BLE001 — logging must never mask the exit code
        try:
            locking.locked_append(log_path, stamp_line(job, f"cron_guard: logging error: {exc}", ts))
        except Exception:
            pass

    # ── best-effort streak + alert (AC4, exactly one per threshold) ───────────
    try:
        state = _load_state(state_path)
        js = state.setdefault(job, {"streak": 0, "last_alert_blocks": 0})
        if rc == 0:
            js["streak"] = 0
            js["last_alert_blocks"] = 0
        else:
            js["streak"] = int(js.get("streak", 0)) + 1
            fire, new_blocks = decide_alert(
                js["streak"], threshold, int(js.get("last_alert_blocks", 0)))
            if fire:
                js["last_alert_blocks"] = new_blocks
                _send_alert(notifier, job, js["streak"], threshold, rc, output)
        _save_state(state_path, state)
    except Exception:  # noqa: BLE001 — alerting must never mask the exit code
        pass

    return rc
