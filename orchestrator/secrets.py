"""EU-234: credential storage for model backend API keys.

``orchestrator/model_registry.py`` records only ever persist a ``credential_ref`` (a reference to
a secret stored elsewhere — see its module docstring lines 17-18 and the ``_ALLOWED_FIELDS``
whitelist). This module is that "elsewhere": it holds the actual raw secret values, keyed by
``credential_ref``, in a store the registry itself never touches directly.

Stored in a **gitignored** ``state/secrets.yaml`` (``.gitignore`` already covers the whole
``state/`` directory), anchored to the run's state directory exactly like ``model_registry.py``
(the parent of ``cfg.audit_path`` when a config is given; an explicit ``path=`` always wins) —
load-bearing for hermeticity so tests get their own tmp store rather than leaking into (or
reading) the operator's real credentials.

Option B (file-based, chmod 600) per the ticket's design decision: simpler than an encrypted
store and sufficient for a single-user cockpit. Every write goes through
``locking.locked_text_rmw`` (atomic temp-file + ``os.replace``, cross-thread/process lock) and is
followed by ``os.chmod(path, 0o600)`` so the file is restricted to the owner even the first time
it is created, and again after every subsequent write (a deleted-then-recreated file would
otherwise inherit the process umask).

Store shape on disk: a flat YAML mapping ``{ref: raw_value}``. Nothing in this module ever logs,
prints, or returns a raw value except :meth:`Secrets.get` — ``list_refs()`` returns keys only, and
:func:`presence_display` returns a constant bullet mask regardless of its input.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml

from . import locking

# Presence-only display: never the actual secret, never its length, always this fixed mask.
_BULLET_MASK = "•" * 8


def presence_display(_value: Any) -> str:
    """A fixed 8-bullet mask for any stored secret — proves presence without revealing anything
    about the underlying value (not even its length). The argument is intentionally unused."""
    return _BULLET_MASK


def _default_path() -> Path:
    """The live location: ``state/secrets.yaml`` at the repo root."""
    return Path(__file__).resolve().parent.parent / "state" / "secrets.yaml"


class Secrets:
    """CRUD persistence for raw credential values, keyed by an opaque ``credential_ref``.

    ``path`` (explicit) takes precedence; otherwise the store is anchored to ``cfg.audit_path``'s
    parent directory; with neither, it falls back to the live ``state/secrets.yaml``. Mirrors
    ``ModelRegistry.__init__``'s precedence exactly so both stores land in the same state
    directory for a given ``cfg``.
    """

    def __init__(self, cfg: Any = None, path: Optional[str | Path] = None):
        if path is not None:
            self._path = Path(path)
        else:
            audit = getattr(cfg, "audit_path", None) if cfg is not None else None
            self._path = Path(audit).resolve().parent / "secrets.yaml" if audit else _default_path()

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> dict:
        """Best-effort load of the whole store — an empty ``{}`` if the file is missing, empty,
        unreadable, corrupt YAML, or not a mapping. Never raises."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return {}
        if not raw.strip():
            return {}
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError:
            return {}
        return data if isinstance(data, dict) else {}

    def _chmod(self) -> None:
        """Restrict the store to owner read/write only. Called after every write so a file that
        gets recreated (e.g. after delete() empties it) is never left at the process umask."""
        if self._path.exists():
            os.chmod(self._path, 0o600)

    # ------------------------------------------------------------------ CRUD

    def store(self, ref: str, value: str) -> None:
        """Persist ``value`` under ``ref``, overwriting any existing value for that ref."""

        def _mutate(current: str) -> str:
            data = yaml.safe_load(current) if current.strip() else {}
            if not isinstance(data, dict):
                data = {}
            data[ref] = value
            return yaml.safe_dump(data, default_flow_style=False, sort_keys=True)

        locking.locked_text_rmw(self._path, _mutate, default="")
        self._chmod()

    def get(self, ref: str) -> Optional[str]:
        """The raw value stored under ``ref``, or ``None`` if it doesn't exist."""
        value = self._read().get(ref)
        return value if isinstance(value, str) else (None if value is None else str(value))

    def list_refs(self) -> list[str]:
        """Every stored ref (keys only) — never the underlying secret values."""
        return list(self._read().keys())

    def delete(self, ref: str) -> bool:
        """Remove ``ref``. Returns ``True`` if it existed and was removed, ``False`` if there was
        nothing to delete (the store is left untouched either way)."""
        removed = False

        def _mutate(current: str) -> str:
            nonlocal removed
            data = yaml.safe_load(current) if current.strip() else {}
            if not isinstance(data, dict):
                data = {}
            if ref in data:
                data.pop(ref)
                removed = True
            return yaml.safe_dump(data, default_flow_style=False, sort_keys=True)

        locking.locked_text_rmw(self._path, _mutate, default="")
        self._chmod()
        return removed
