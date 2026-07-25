"""EU-234: secure credential storage for model backend API keys.

A standalone store for RAW secret values (API keys / tokens), deliberately kept OUTSIDE
``orchestrator/model_registry.py`` — the registry's ``_ALLOWED_FIELDS`` whitelist
(model_registry.py:39) already guarantees a raw key can never land in a registry record; this
module is where the raw value actually lives, so that guarantee holds in practice and not just on
paper.

Persisted as a **gitignored** JSON document (the ``state/`` prefix is covered by ``.gitignore``
line 73), anchored to the same directory as the caller's other run-state exactly like
``ModelRegistry`` (model_registry.py:88-93) — ``path=`` (explicit) wins, then ``cfg.audit_path``'s
parent, then the live ``state/secrets.json`` fallback. Anchoring to the *config* rather than the
package root is load-bearing for hermeticity: a repo-root store would leak the operator's real
secrets into every test process, so tests build hermetic tmp stores via ``path=``.

Every read-modify-write goes through :func:`orchestrator.locking.locked_rmw` (atomic temp-file +
``os.replace``, tolerant of a missing/corrupt file), and the file is ``chmod``'d to ``0o600`` after
every write — belt-and-suspenders alongside the gitignore entry, since a restrictive mode still
protects the file from other local accounts even if it were ever accidentally tracked.

Option B (plain file, restricted permissions) over Option A (encrypted YAML + master key): this is
a single-user cockpit, so a master-key scheme would add real complexity (where does the master key
itself live?) without a matching threat model. A plain JSON file with 0o600 permissions is simpler,
has no extra dependency, and is sufficient here.

``Secrets`` never logs or renders a raw value — ``presence_display()`` is the only display helper,
and it always returns a fixed mask regardless of what (if anything) is passed to it, so a caller can
never accidentally interpolate a real secret into a log line or a cockpit response by calling the
"display" helper on it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from . import locking

if TYPE_CHECKING:
    from .config import Config

_EMPTY_STORE = {"secrets": {}}

# presence_display() always returns this — 8 bullets, never the real value or its length.
_PRESENCE_MASK = "•" * 8


def _default_path() -> Path:
    """The live location: ``state/secrets.json`` at the repo root."""
    return Path(__file__).resolve().parent.parent / "state" / "secrets.json"


class Secrets:
    """CRUD persistence for raw credential values.

    ``path`` (explicit) takes precedence; otherwise the store is anchored to ``cfg.audit_path``'s
    parent directory; with neither, it falls back to the live ``state/secrets.json``. Mirrors
    ``ModelRegistry.__init__`` (model_registry.py:88-93) so tests are hermetic with tmp stores.
    """

    def __init__(self, cfg: Config | None = None, path: Optional[str | Path] = None):
        if path is not None:
            self._path = Path(path)
        else:
            audit = getattr(cfg, "audit_path", None) if cfg is not None else None
            self._path = Path(audit).resolve().parent / "secrets.json" if audit else _default_path()

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> dict:
        """Best-effort load of the whole store — an empty ``{"secrets": {}}`` document if the file
        is missing, empty, unreadable, corrupt JSON, or not the expected shape. Never raises."""
        try:
            raw = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return {"secrets": {}}
        if not raw:
            return {"secrets": {}}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"secrets": {}}
        if not isinstance(data, dict) or not isinstance(data.get("secrets"), dict):
            return {"secrets": {}}
        return data

    def _chmod(self) -> None:
        """Restrict the store to owner read/write after every write. Best-effort: a filesystem
        that doesn't support unix permissions (or a file that vanished under us) must not crash
        the caller — the gitignore entry is still the primary protection."""
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------ CRUD

    def store(self, ref: str, secret: str) -> None:
        """Persist ``secret`` under ``ref``, creating the state directory + file on first write.
        Overwrites any existing value for the same ``ref``."""

        def _mutate(current: dict) -> dict:
            data = current if isinstance(current, dict) else {}
            values = dict(data.get("secrets") or {})
            values[ref] = secret
            data = dict(data)
            data["secrets"] = values
            return data

        locking.locked_rmw(self._path, _mutate, default=dict(_EMPTY_STORE), corrupt_to_default=True)
        self._chmod()

    def get(self, ref: str) -> Optional[str]:
        """The raw secret for ``ref``, or ``None`` if it doesn't exist (or the store is empty).
        Never raises."""
        return self._read()["secrets"].get(ref)

    def list_refs(self) -> list[str]:
        """Every stored reference name, sorted — never a secret value."""
        return sorted(self._read()["secrets"].keys())

    def delete(self, ref: str) -> bool:
        """Remove ``ref``. Returns ``True`` if it existed and was removed, ``False`` otherwise."""
        removed = False

        def _mutate(current: dict) -> dict:
            nonlocal removed
            data = current if isinstance(current, dict) else {}
            values = dict(data.get("secrets") or {})
            if ref in values:
                values.pop(ref)
                removed = True
            data = dict(data)
            data["secrets"] = values
            return data

        locking.locked_rmw(self._path, _mutate, default=dict(_EMPTY_STORE), corrupt_to_default=True)
        self._chmod()
        return removed

    @staticmethod
    def presence_display(_value: object = None) -> str:
        """A presence-only display helper: always the same 8-bullet mask, regardless of what (if
        anything) is passed in. Never derived from, or reveals anything about, the real value —
        that is the point: callers use this to render "a credential is set" without a code path
        that could ever leak the credential itself into a log line or a cockpit response."""
        return _PRESENCE_MASK

    def __repr__(self) -> str:  # pragma: no cover - defensive: never let a value leak via repr
        return f"Secrets(path={self._path!r})"
