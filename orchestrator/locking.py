"""Shared file-locking helpers (generalised from the F8 audit-log lock).

The orchestrator has several files that are written concurrently by threads inside one
process (cockpit run thread, autopilot, decisions poller) *and* sometimes by separate
processes. The F8 fix in ``audit.py`` proved the pattern for one such file (the JSONL audit
log); ``blocked``/``pending_decisions``/``usage_ledger`` still race and lose writes because
each rolled its own (or no) locking. This module lifts that pattern into two reusable
primitives so every hot file can share one correct implementation:

* :func:`locked_append`   — append a single line to a ``.jsonl`` file.
* :func:`locked_rmw`      — atomic read-modify-write of a ``.json`` file.
* :func:`locked_text_rmw` — atomic read-modify-write of a plain text file (e.g. markdown).
* :func:`locked_call`     — run an arbitrary critical section (e.g. fetch-then-ack) under the
  same lock, for callers whose critical section isn't a single read-modify-write.

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


def locked_rewrite(path: str | Path, keep_fn: Callable[[list[str]], list[str]]) -> None:
    """Atomically rewrite the JSONL file at ``path`` in place, keeping the lines ``keep_fn``
    returns (it receives the current lines, freshly read under the lock).

    This is the prune/compact counterpart of :func:`locked_append`, and the pairing is
    load-bearing: both take the per-path thread lock AND an exclusive ``flock`` on the DATA
    file's own fd, so a rewrite can never truncate mid-append or drop a row that landed
    between its read and its write (the 2026-07-05 audit §7.4 races: governor's hourly prune
    vs note_call, usage.prune on CLI start vs the serve process's record()). In-place
    seek(0)+truncate is deliberate — a temp-file + ``os.replace`` swap (locked_rmw's pattern)
    would strand a concurrently blocked appender on the orphaned old inode, losing its row
    invisibly, because ``flock`` serialises on the inode the appender already has open.
    """
    p = Path(path)
    if not p.exists():
        return
    with _lock_for(p):
        with p.open("r+", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                kept = keep_fn(f.read().splitlines())
                f.seek(0)
                f.truncate()
                if kept:
                    f.write("\n".join(kept) + "\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def locked_text_rmw(path: str | Path, mutate_fn: Callable[[str], str], *, default: str = "") -> str:
    """Atomically read-modify-write a plain TEXT file at ``path`` (the ``locked_rmw`` sibling for
    files that aren't JSON, e.g. a markdown changelog).

    Reads the current text (or ``default`` if the file is missing), passes it to ``mutate_fn``, and
    writes whatever ``mutate_fn`` returns back atomically (temp file + :func:`os.replace`), under the
    same cross-thread + cross-process sidecar-``.lock`` critical section as :func:`locked_rmw` — so a
    concurrent read can never observe pre-write state and then clobber it. ``mutate_fn`` receives the
    text read INSIDE the lock (never a pre-lock snapshot) and must return the full new file content;
    it is responsible for any header/formatting the file needs.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(p):
        # Sidecar lock, not the data file itself, for the same reason as locked_rmw: os.replace
        # swaps the data file's inode mid-section and would strand a lock taken on the old inode.
        lock_path = p.with_name(p.name + ".lock")
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)

            current = p.read_text(encoding="utf-8") if p.exists() else default
            new_value = mutate_fn(current)

            tmp_path = p.with_name(p.name + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as tf:
                tf.write(new_value)
                tf.flush()
                os.fsync(tf.fileno())
            os.replace(tmp_path, p)
            return new_value
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def locked_call(path: str | Path, fn: Callable[[], Any]) -> Any:
    """Run ``fn()`` while holding the cross-thread + cross-process lock keyed on ``path``'s
    sidecar ``.lock`` file — the same guarantee as :func:`locked_rmw`/:func:`locked_text_rmw`
    (a process-wide ``threading.Lock`` plus an ``fcntl.flock`` on a sidecar file), but for a
    critical section that is neither a JSON nor a plain-text read-modify-write.

    EU-257: the Telegram poller's fetch-then-ack sequence (read the offset -> ``getUpdates`` ->
    route each message -> advance the offset, possibly writing it more than once per batch) is
    exactly this shape against the plain-text offset file. Two pollers racing that window could
    both fetch the same ``getUpdates`` batch and each run route_message's side effects (ticket
    resume, Jira comment/transition, council reply) on the same update. Wrapping the whole
    sequence in ``locked_call`` keyed on the offset file serialises it across threads AND
    processes, so only one poller ever runs it at a time.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(p):
        lock_path = p.with_name(p.name + ".lock")
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            return fn()
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


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
