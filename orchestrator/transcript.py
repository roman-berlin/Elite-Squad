"""Per-officer transcript writer — full tool inputs + reasoning with secret redaction.

EU-197: When transcript_enabled is True, persist one JSONL record per ToolUseBlock
(FULL untruncated input), per TextBlock (the officer's reasoning), and a final result
record — to logs/<app>/<date>/<TICKET>-<HHMMSS>-<officer>.jsonl. Secrets are redacted
before write. Best-effort (try/except, never breaks a run). Reuses log_retention_days
purge from run_logger.py.
"""
from __future__ import annotations

import contextvars
import datetime
import json
import re
import threading
from pathlib import Path
from typing import Any, IO

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
# Per-run transcript context: (app_name, ticket_id, timestamp, officer_name)
# Set by the orchestrator at run start so transcript writes know where to go.
_transcript_context: contextvars.ContextVar[tuple[str, str, str, str] | None] = contextvars.ContextVar(
    "transcript_context", default=None
)

# Module-level config reference for transcript_enabled checks
# Set by set_transcript_context when cfg is provided
_transcript_config: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "transcript_config", default=None
)

# Thread-safe transcript file handle cache
_handles: "dict[tuple[str, str, str, str], IO[str]]" = {}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def set_transcript_context(app: str, ticket: str, timestamp: str, officer: str,
                          cfg: Any = None) -> None:
    """Set the per-run transcript context for the current async context.

    This should be called once at the start of each officer run so transcript
    writes know which file to write to. cfg is the Config object.

    Args:
        app: Application name
        ticket: Ticket ID
        timestamp: Run timestamp (HHMMSS format)
        officer: Officer name (e.g., "builder", "reviewer")
        cfg: Config object (for transcript_enabled check and log path)
    """
    _transcript_context.set((app, ticket, timestamp, officer))
    if cfg is not None:
        _transcript_config.set(cfg)


def clear_transcript_context() -> None:
    """Clear the transcript context (end of officer run)."""
    _transcript_context.set(None)
    _transcript_config.set(None)


def write_tool_use(tool_name: str, input_data: Any) -> None:
    """Write a tool use record to the transcript (if enabled).

    Captures the FULL untruncated tool input. Secrets are redacted before write.
    Best-effort: any error is swallowed so transcript failures never break a run.
    """
    try:
        ctx = _transcript_context.get()
        if ctx is None:
            return

        app, ticket, timestamp, officer = ctx

        # Check if transcript is enabled
        cfg = _transcript_config.get()
        if cfg is None or not getattr(cfg, "transcript_enabled", False):
            return

        record = {
            "type": "tool_use",
            "tool": tool_name,
            "input": _redact_secrets(input_data),
            "timestamp": datetime.datetime.now().isoformat()
        }
        _write_record(app, ticket, timestamp, officer, record, cfg)
    except Exception:  # noqa: BLE001 — best-effort
        pass


def write_text(content: str) -> None:
    """Write a TextBlock (officer reasoning) to the transcript.

    Best-effort: any error is swallowed so transcript failures never break a run.
    """
    try:
        ctx = _transcript_context.get()
        if ctx is None:
            return

        app, ticket, timestamp, officer = ctx

        # Check if transcript is enabled
        cfg = _transcript_config.get()
        if cfg is None or not getattr(cfg, "transcript_enabled", False):
            return

        record = {
            "type": "text",
            "content": _redact_secrets(content),
            "timestamp": datetime.datetime.now().isoformat()
        }
        _write_record(app, ticket, timestamp, officer, record, cfg)
    except Exception:  # noqa: BLE001 — best-effort
        pass


def write_result(content: str) -> None:
    """Write the final result to the transcript.

    Best-effort: any error is swallowed so transcript failures never break a run.
    """
    try:
        ctx = _transcript_context.get()
        if ctx is None:
            return

        app, ticket, timestamp, officer = ctx

        # Check if transcript is enabled
        cfg = _transcript_config.get()
        if cfg is None or not getattr(cfg, "transcript_enabled", False):
            return

        record = {
            "type": "result",
            "content": _redact_secrets(content),
            "timestamp": datetime.datetime.now().isoformat()
        }
        _write_record(app, ticket, timestamp, officer, record, cfg)
    except Exception:  # noqa: BLE001 — best-effort
        pass


def close_transcript(app: str, ticket: str, timestamp: str, officer: str) -> None:
    """Close the transcript file for a specific officer run.

    Called when the officer finishes. Best-effort.
    """
    try:
        # Close any handle that matches the app/ticket/timestamp/officer
        # regardless of whether cfg was part of the context key
        with _lock:
            for key in list(_handles.keys()):
                if len(key) == 4 and key[:4] == (app, ticket, timestamp, officer):
                    _close_handle(key)
                    break
    except Exception:  # noqa: BLE001 — best-effort
        pass


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _write_record(app: str, ticket: str, timestamp: str, officer: str, record: dict, cfg: Any) -> None:
    """Write a JSONL record to the transcript file.

    Thread-safe and best-effort.
    """
    key = (app, ticket, timestamp, officer)
    with _lock:
        handle = _handles.get(key)
        if handle is None:
            # Open the transcript file
            handle = _open_transcript_file(app, ticket, timestamp, officer, cfg)
            _handles[key] = handle

        try:
            handle.write(json.dumps(record) + "\n")
            handle.flush()  # Ensure each record is written immediately
        except Exception:  # noqa: BLE001
            pass


def _open_transcript_file(app: str, ticket: str, timestamp: str, officer: str, cfg: Any) -> IO[str]:
    """Open a transcript file for writing.

    Path: logs/<app>/<date>/<TICKET>-<HHMMSS>-<officer>.jsonl
    Creates the directory structure if needed.
    """
    from . import run_logger  # Import here to avoid circular dependency

    now = datetime.datetime.now()
    date_str = now.strftime("%Y-%m-%d")

    # Use the same log root as run_logger
    if cfg is None:
        # Can't get config - bail silently (best-effort)
        raise RuntimeError("Cannot get config for transcript path")

    root = run_logger.log_root(cfg)
    day_dir = root / app / date_str
    day_dir.mkdir(parents=True, exist_ok=True)

    transcript_path = day_dir / f"{ticket}-{timestamp}-{officer}.jsonl"

    # Open in line-buffered append mode
    return transcript_path.open("a", encoding="utf-8", buffering=1)


def _close_handle(key: tuple) -> None:
    """Close and remove the handle for *key*.  **Caller must hold** ``_lock``."""
    handle = _handles.pop(key, None)
    if handle is not None:
        try:
            handle.close()
        except Exception:  # noqa: BLE001
            pass


def _redact_secrets(data: Any) -> Any:
    """Redact secrets from data before writing to transcript.

    Patterns to redact:
    - API keys, tokens (sk-, pk-, etc.)
    - Authorization headers
    - ANTHROPIC_* environment variables
    - GLM_* tokens
    - Password/passphrase fields
    - Bash command strings (may contain secrets)

    Returns a redacted copy of the data.
    """
    if isinstance(data, str):
        return _redact_string(data)
    elif isinstance(data, dict):
        return {k: _redact_secrets(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_redact_secrets(item) for item in data]
    else:
        return data


def _redact_string(s: str) -> str:
    """Redact secrets from a string."""
    if not isinstance(s, str):
        return s

    # Redact common secret patterns
    patterns = [
        (r'(?i)(authorization|token|api[_-]?key|secret|password|passphrase)["\']?\s*[:=]\s*["\']?([^\s"\']+)["\']?', r'\1 [REDACTED]'),
        (r'(sk-[a-zA-Z0-9]{10,})', 'sk-[REDACTED]'),  # Reduced minimum to 10 chars
        (r'(pk-[a-zA-Z0-9]{10,})', 'pk-[REDACTED]'),
        (r'(ANTHROPIC_[A-Z_]+)\s*=\s*[^\s]+', r'\1=[REDACTED]'),
        (r'(GLM_[A-Z_]+)\s*=\s*[^\s]+', r'\1=[REDACTED]'),
        (r'(Bearer\s+[a-zA-Z0-9\-._~+/]+=*)', 'Bearer [REDACTED]'),
    ]

    result = s
    for pattern, replacement in patterns:
        result = re.sub(pattern, replacement, result)

    return result
