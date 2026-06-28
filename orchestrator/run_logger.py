"""Run log file writer — stream per-run stdout to logs/<app>/<YYYY-MM-DD>/<TICKET>-<HHMMSS>.log.

Every captured stdout line is written to the run's log file via the ``_Tee`` in
``cockpit_state``.  The log path is stored in per-app run state as ``state['log_path']``
so the UI can surface an "Open logs" button.

Config keys (set in config.yaml or the Config dataclass):
    log_folder         — root dir for all run logs (default: ``'logs/'``, relative to
                         the directory that contains ``audit_path``).
    log_retention_days — auto-purge day-folders whose date is older than this many days
                         on every run start.  0 or absent = disabled.

Typical log path:  logs/automatixy/2026-06-29/AUTO-42-153000.log
"""
from __future__ import annotations

import datetime
import shutil
import threading
from pathlib import Path
from typing import IO, Any

# ---------------------------------------------------------------------------
# Module-level handle registry
# ---------------------------------------------------------------------------
# Keyed by app_key (str app name OR None for the legacy/default run-key).
# One open file handle per concurrent run; closed by close_run_log().
# Guarded by _lock for thread safety (multiple apps can run concurrently).
_log_handles: "dict[object, IO[str]]" = {}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def log_root(cfg: Any) -> Path:
    """Resolve the log root directory from *cfg*.

    Uses ``cfg.log_folder`` when present, otherwise falls back to ``'logs/'``.
    The path is resolved relative to the directory that contains ``cfg.audit_path``
    (the same anchor the rest of the unit uses for out-of-repo artefacts).
    """
    folder = getattr(cfg, "log_folder", None) or "logs/"
    base = Path(getattr(cfg, "audit_path", "./audit.jsonl")).parent
    return (base / folder).resolve()


def open_run_log(cfg: Any, app_name: str | None, ticket_key: str | None) -> Path:
    """Open a log file for a new run and register it in the handle registry.

    Log path: ``<log_root>/<app>/<YYYY-MM-DD>/<TICKET>-<HHMMSS>.log``

    Also:
    * Auto-purges day-folders older than ``cfg.log_retention_days`` (if set).
    * Stores the log path in the per-app run state under ``state['log_path']`` via
      ``cockpit_state.get_state()``.

    Returns the resolved log :class:`~pathlib.Path`.  Call this *before* starting the
    background thread so every line the ``_Tee`` captures goes straight to the file.
    """
    now = datetime.datetime.now()
    app_slug = _safe_slug(app_name or "default")
    ticket_slug = _safe_slug(ticket_key or "run")
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M%S")

    root = log_root(cfg)
    day_dir = root / app_slug / date_str
    day_dir.mkdir(parents=True, exist_ok=True)

    log_path = day_dir / f"{ticket_slug}-{time_str}.log"

    # Auto-purge old day-folders before we write anything new.
    retention = int(getattr(cfg, "log_retention_days", 0) or 0)
    if retention > 0:
        _purge_old_logs(root / app_slug, retention, now)

    # Open the log file in line-buffered append mode so each line flushes immediately.
    handle: IO[str] = log_path.open("a", encoding="utf-8", buffering=1)

    # The registry key matches the cockpit_state key (app name or None).
    key: object = app_name if app_name else None
    with _lock:
        _close_handle(key)   # close any stale handle (defensive; shouldn't happen)
        _log_handles[key] = handle

    # Store the log path in per-app run state so the UI can surface it.
    try:
        from . import cockpit_state
        cockpit_state.get_state(key)["log_path"] = str(log_path)
    except Exception:  # noqa: BLE001 — state enrichment must never block the run
        pass

    return log_path


def close_run_log(app_name: str | None) -> None:
    """Close and de-register the open log handle for *app_name*.

    Call this in the run ``_bg`` finally-block alongside ``release_run()``.
    """
    key: object = app_name if app_name else None
    with _lock:
        _close_handle(key)


def write_line(app_key: object, line: str) -> None:
    """Write one log line for *app_key* (if a log is open for it).

    Called by ``cockpit_state._Tee.write`` for every captured stdout line.
    Thread-safe and best-effort: any I/O error is swallowed so a log-write failure
    can never abort a run.
    """
    with _lock:
        handle = _log_handles.get(app_key)
    if handle is None:
        return
    try:
        handle.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _safe_slug(text: str) -> str:
    """Replace path-unsafe characters with underscores; truncate to 80 chars."""
    return "".join(c if c.isalnum() or c in "-." else "_" for c in text)[:80]


def _close_handle(key: object) -> None:
    """Close and remove the handle for *key*.  **Caller must hold** ``_lock``."""
    handle = _log_handles.pop(key, None)
    if handle is not None:
        try:
            handle.close()
        except Exception:  # noqa: BLE001
            pass


def _purge_old_logs(app_dir: Path, retention_days: int, now: datetime.datetime) -> None:
    """Delete date-named day-folders under *app_dir* older than *retention_days*.

    Only removes directories whose names parse as ``YYYY-MM-DD``; any other entries
    are left untouched.  ``retention_days <= 0`` disables purging (returns immediately).
    Deletion failures are swallowed (best-effort) so a stale locked file can never
    abort the run that triggered the purge.
    """
    if retention_days <= 0:
        return
    if not app_dir.is_dir():
        return
    cutoff = (now - datetime.timedelta(days=retention_days)).date()
    for entry in app_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            folder_date = datetime.date.fromisoformat(entry.name)
        except ValueError:
            continue   # not a YYYY-MM-DD folder — leave it alone
        if folder_date < cutoff:
            try:
                shutil.rmtree(entry)
            except Exception:  # noqa: BLE001
                pass
