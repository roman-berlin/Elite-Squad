"""EU-233: the model registry data layer — the persistence foundation for user-defined model
backends (custom Anthropic/OpenAI-compatible endpoints), no UI yet.

Stored in a **gitignored** ``model_registry.json``, anchored to the run's state directory (the
parent of ``cfg.audit_path`` — i.e. ``state/`` for the live unit) exactly like
``backend_pref.py``'s ``model_backend.json`` (EU-190). Anchoring to the *config* rather than the
package root is load-bearing for hermeticity: a repo-root store would leak the operator's real
registry into every test process (the exact 2026-07-09 12-suite leak the lesson log warns
about) — tests build ``Config``s with tmp ``audit_path``s (or pass an explicit ``path=``), so
they always get their own empty store.

Store shape: ``{"models": {id: record}}``. Every read-modify-write goes through
:func:`orchestrator.locking.locked_rmw`, which already gives atomic temp-file + ``os.replace``
writes, auto-creates the parent directory on first write, and tolerates a missing/corrupt file by
falling back to ``default`` — satisfying the graceful-degradation acceptance criterion for free.

Only a ``credential_ref`` (a reference to a secret stored elsewhere) is ever persisted here —
never a raw secret/API key/token/password.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from . import locking
from .secrets import Secrets

if TYPE_CHECKING:
    from .config import Config

# The only providers the registry currently understands. Kept as a tuple (not a set) so error
# messages render in a stable, predictable order.
PROVIDERS = ("anthropic", "openai")

# Required on add(); credential_ref is a *reference* to a secret, never the secret itself.
_REQUIRED_FIELDS = ("display_name", "provider", "base_url", "model_id", "credential_ref")

# Every field a record may carry. add()/update() drop anything outside this set so a caller can
# never smuggle a raw secret (or any other stray key) into the persisted store.
_ALLOWED_FIELDS = _REQUIRED_FIELDS + ("small_fast_model_id", "tier")

# 2026-07-19 (Commander order): the capability CLASS of a custom backend's model, relative to
# the Claude ladder — top (Opus-class+), mid (Sonnet-class), light (Haiku-class). Auto-detected
# at add time (one cheap LLM call, editable in the form); "" is treated as mid.
TIERS = ("top", "mid", "light", "")

_EMPTY_STORE = {"models": {}}


def _now() -> str:
    """ISO-8601 UTC timestamp — the same shape for created_at and updated_at."""
    return datetime.now(timezone.utc).isoformat()


def _default_path() -> Path:
    """The live location: ``state/model_registry.json`` at the repo root."""
    return Path(__file__).resolve().parent.parent / "state" / "model_registry.json"


def _validate(fields: dict, *, partial: bool) -> dict:
    """Validate ``fields`` against the schema, returning only the allowed keys.

    ``partial=False`` (add): every field in ``_REQUIRED_FIELDS`` must be present and non-empty.
    ``partial=True`` (update): only the fields actually supplied are checked; unknown keys are
    silently dropped rather than rejected, so callers can pass a loose patch dict.

    Raises ``ValueError`` with a clear message — never lets an invalid ``provider`` or a missing
    required field reach the store.
    """
    if not partial:
        missing = [f for f in _REQUIRED_FIELDS if not str(fields.get(f) or "").strip()]
        if missing:
            raise ValueError(f"missing required field(s): {', '.join(missing)}")

    clean: dict[str, object] = {}
    for key, value in fields.items():
        if key not in _ALLOWED_FIELDS:
            continue
        if key == "provider" and value not in PROVIDERS:
            raise ValueError(f"provider must be one of {PROVIDERS}, got {value!r}")
        if key == "tier" and str(value or "") not in TIERS:
            raise ValueError(f"tier must be one of top|mid|light (or blank), got {value!r}")
        if partial and key in _REQUIRED_FIELDS and not str(value or "").strip():
            raise ValueError(f"{key} cannot be blanked out")
        clean[key] = value
    return clean


class ModelRegistry:
    """CRUD persistence for the model backend registry.

    ``path`` (explicit) takes precedence; otherwise the store is anchored to ``cfg.audit_path``'s
    parent directory; with neither, it falls back to the live ``state/model_registry.json``.
    """

    def __init__(self, cfg: Config | None = None, path: Optional[str | Path] = None):
        if path is not None:
            self._path = Path(path)
        else:
            audit = getattr(cfg, "audit_path", None) if cfg is not None else None
            self._path = Path(audit).resolve().parent / "model_registry.json" if audit else _default_path()

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> dict:
        """Best-effort load of the whole store — an empty ``{"models": {}}`` document if the file
        is missing, empty, unreadable, corrupt JSON, or not the expected shape. Never raises."""
        try:
            raw = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return {"models": {}}
        if not raw:
            return {"models": {}}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"models": {}}
        if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
            return {"models": {}}
        return data

    # ------------------------------------------------------------------ CRUD

    def add(self, fields: dict) -> dict:
        """Create a new record. Validates required fields + the ``provider`` enum first (raising
        ``ValueError`` and writing nothing on failure), then generates ``id``/``created_at``/
        ``updated_at`` and persists atomically. Returns the stored record (a copy)."""
        clean = _validate(fields, partial=False)
        record = {
            "id": str(uuid.uuid4()),
            **clean,
            "created_at": _now(),
        }
        record["updated_at"] = record["created_at"]

        def _mutate(current: dict) -> dict:
            data = current if isinstance(current, dict) else {}
            models = dict(data.get("models") or {})
            models[record["id"]] = record
            data = dict(data)
            data["models"] = models
            return data

        locking.locked_rmw(self._path, _mutate, default=dict(_EMPTY_STORE), corrupt_to_default=True)
        return dict(record)

    def get(self, model_id: str) -> Optional[dict]:
        """The record for ``model_id``, or ``None`` if it doesn't exist (or the store is empty)."""
        record = self._read()["models"].get(model_id)
        return dict(record) if record is not None else None

    def list(self) -> list[dict]:
        """Every record, oldest first (insertion/``created_at`` order)."""
        return [dict(r) for r in self._read()["models"].values()]

    def update(self, model_id: str, fields: dict) -> Optional[dict]:
        """Patch ``model_id`` with ``fields``. Returns the updated record, or ``None`` if
        ``model_id`` doesn't exist — the store is left untouched in that case. ``id`` and
        ``created_at`` are always preserved; ``updated_at`` always advances. Raises ``ValueError``
        (writing nothing) if a supplied field is invalid, e.g. an unknown ``provider``."""
        clean = _validate(fields, partial=True)
        result: dict | None = None

        def _mutate(current: dict) -> dict:
            nonlocal result
            data = current if isinstance(current, dict) else {}
            models = dict(data.get("models") or {})
            existing = models.get(model_id)
            if existing is None:
                return data  # unknown id: no-op, store unchanged
            updated = {**existing, **clean, "id": existing["id"], "created_at": existing["created_at"]}
            updated["updated_at"] = _now()
            models[model_id] = updated
            result = updated
            data = dict(data)
            data["models"] = models
            return data

        locking.locked_rmw(self._path, _mutate, default=dict(_EMPTY_STORE), corrupt_to_default=True)
        return dict(result) if result is not None else None

    def delete(self, model_id: str) -> bool:
        """Remove ``model_id``. Returns ``True`` if it existed and was removed, ``False`` if there
        was nothing to delete (the store is left untouched either way)."""
        removed = False

        def _mutate(current: dict) -> dict:
            nonlocal removed
            data = current if isinstance(current, dict) else {}
            models = dict(data.get("models") or {})
            if model_id in models:
                models.pop(model_id)
                removed = True
            data = dict(data)
            data["models"] = models
            return data

        locking.locked_rmw(self._path, _mutate, default=dict(_EMPTY_STORE), corrupt_to_default=True)
        return removed

    # ------------------------------------------------------------- credentials (EU-234)

    def _secrets_path(self) -> Path:
        """The :class:`~orchestrator.secrets.Secrets` store lives next to this registry's own
        file — same state directory, so both stay hermetic together under a test's tmp ``path=``,
        exactly like the registry's own anchoring (see module docstring)."""
        return self._path.parent / "secrets.json"

    def set_credential(self, model_id: str, api_key: str) -> Optional[dict]:
        """Store ``api_key`` in the standalone ``Secrets`` store and persist only a derived
        ``credential_ref`` on the registry record — the raw key is never written to this file (the
        ``_ALLOWED_FIELDS`` whitelist would strip it even if a caller tried). Returns the updated
        record, or ``None`` if ``model_id`` doesn't exist (the secret is still stored regardless;
        there is simply no record to attach the reference to)."""
        ref = f"secret://{model_id}"
        Secrets(path=self._secrets_path()).store(ref, api_key)
        return self.update(model_id, {"credential_ref": ref})

    def get_credential_for(self, model_id: str) -> Optional[str]:
        """Resolve ``model_id``'s stored ``credential_ref`` through ``Secrets``. ``None`` if the
        backend doesn't exist, has no ``credential_ref``, or the secret itself isn't stored."""
        record = self.get(model_id)
        ref = record.get("credential_ref") if record else None
        if not ref:
            return None
        return Secrets(path=self._secrets_path()).get(ref)

    # ------------------------------------------------------------------- EU-236 (dynamic backends)

    def get_backend_config(self, model_id: str) -> Optional[dict]:
        """A ready-to-use backend dict for :func:`orchestrator.backends.apply`:
        ``{base_url, model_id, small_fast_model_id, auth_token}`` — ``auth_token`` resolved through
        :meth:`get_credential_for` (never the raw ``credential_ref``). ``None`` if ``model_id`` isn't
        a registry record. ``auth_token`` may itself be ``None`` when the credential is missing or
        unresolvable — callers (``backends.apply``) must fail closed on that, exactly like the GLM
        branch does for a missing ``GLM_AUTH_TOKEN``; this method only resolves, never validates."""
        record = self.get(model_id)
        if record is None:
            return None
        return {
            "base_url": record.get("base_url"),
            "model_id": record.get("model_id"),
            "small_fast_model_id": record.get("small_fast_model_id") or record.get("model_id"),
            "auth_token": self.get_credential_for(model_id),
        }
