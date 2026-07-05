"""Shared file-locking helpers (generalised from the F8 audit-log lock).

The orchestrator has several files that are written concurrently by threads inside one
process (cockpit run thread, autopilot, decisions poller) *and* sometimes by separate
processes. The F8 fix in ``audit.py`` proved the pattern for one such file (the JSONL audit
log); ``blocked``/``pending_decisions``/``usage_ledger`` still race and lose writes because
each rolled its own (or no) locking. This module lifts that pattern into two reusable
primitives so every hot file can share one correct implementation:

* :func:`locked_append` — append a single line to a ``.jsonl`` file.
* :func:`locked_rmw`    — atomic read-modify-write of a ``.json`` file.

Both combine three layers of protection:

1. a *process-wide* :class:`threading.Lock` (keyed per path) so threads in this process
   serialise — ``fcntl`` locks are per-open-file-description and do **not** serialise threads
   sharing one process reliably;
2. ``fcntl.flock`` advisory locking so *separate processes* serialise on the same file; and
3. for the read-modify-write case, a temp-file + :func:`os.replace` so a concurrent reader
   never observes a half-written file (``os.replace`` is atomic on POSIX).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

try:  # POSIX advisory file locking; absent on Windows.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


# One threading.Lock per absolute path. fcntl.flock serialises across processes but NOT across
# threads of one process (the lock is owned by the open file description, not the thread), so we
# still need an in-process lock — and it must be the *same* object for every writer of a given
# file, hence this registry rather than a lock per caller.
_LOCKS: dict[str, threading.Lock] = {}
_REGISTRY_LOCK = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _REGISTRY_LOCK:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def locked_append(path: str | Path, line: str) -> None:
    """Append ``line`` to the JSONL file at ``path`` under a cross-thread + cross-process lock.

    A trailing newline is added if ``line`` does not already end with one, so callers may pass
    either ``json.dumps(row)`` or a pre-terminated line. Parent directories are created on demand.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not line.endswith("\n"):
        line += "\n"
    with _lock_for(p):
        with p.open("a", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
            finally:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def locked_rmw(path: str | Path, mutate_fn: Callable[[Any], Any], *, default: Any = None,
               corrupt_to_default: bool = False) -> Any:
    """Atomically read-modify-write the JSON file at ``path``.

    Reads the current JSON value (or ``default`` if the file is missing or empty), passes it to
    ``mutate_fn``, and writes whatever ``mutate_fn`` returns back atomically (temp file +
    :func:`os.replace`). The whole cycle is serialised across threads and processes, so two
    concurrent read-modify-writes can never clobber each other's update. Returns the new value
    that was written.

    ``mutate_fn`` should return the full new document; it may mutate the value in place and return
    it, or build and return a fresh object.

    ``corrupt_to_default=True`` treats an unparsable (corrupt) existing file as ``default``
    instead of raising — matching the tolerant `_load()` helpers this primitive replaces, whose
    callers self-heal a corrupt sidecar on the next write (approvals/proposals, EU-48 family).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(p):
        # An exclusive flock spanning the read and the write requires holding an fd open across the
        # whole critical section. We lock a sidecar ``.lock`` file rather than the data file itself,
        # because os.replace swaps the data file's inode mid-section and would strand a lock taken
        # on the old inode.
        lock_path = p.with_name(p.name + ".lock")
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)

            current = default
            if p.exists():
                raw = p.read_text(encoding="utf-8").strip()
                if raw:
                    try:
                        current = json.loads(raw)
                    except json.JSONDecodeError:
                        if not corrupt_to_default:
                            raise
                        current = default

            new_value = mutate_fn(current)

            tmp_path = p.with_name(p.name + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as tf:
                json.dump(new_value, tf, default=str)
                tf.flush()
                os.fsync(tf.fileno())
            os.replace(tmp_path, p)
            return new_value
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
