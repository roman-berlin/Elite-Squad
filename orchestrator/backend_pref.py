"""EU-190: the persisted **active model backend** (Opus vs GLM), shared by the cockpit and the CLI.

The operator picks a backend once; it sticks across cockpit reloads/restarts and applies to
subsequent runs until changed. Stored in a **gitignored** ``model_backend.json`` at the repo root —
the same location convention as ``connections.py`` — so the long-running cockpit and the one-shot
``./general`` CLI always agree on the active backend.

Precedence: this persisted preference OVERRIDES the ``config.yaml`` ``model_backend`` boot default,
which in turn falls back to Opus. Never stores a secret — only the backend id (``'opus'`` | ``'glm'``).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import backends


def _file() -> Path:
    """Repo-root store — same convention as connections.py so cockpit + CLI never diverge."""
    return Path(__file__).resolve().parent.parent / "model_backend.json"


def get() -> str | None:
    """The persisted backend id (normalized), or ``None`` if never set / unreadable."""
    try:
        bk = json.loads(_file().read_text(encoding="utf-8")).get("backend")
    except (OSError, json.JSONDecodeError):
        return None
    return backends.normalize(bk) if bk else None


def set_active(bk: str) -> None:
    """Persist the active backend id (normalized). Best-effort — never raises."""
    try:
        _file().write_text(
            json.dumps({"backend": backends.normalize(bk)}, indent=2), encoding="utf-8")
    except OSError:
        pass


def active(cfg=None) -> str:
    """The effective active backend: the persisted preference, else the ``config.yaml`` default,
    else Opus. Always a canonical id (``'opus'`` | ``'glm'``)."""
    return get() or backends.normalize(getattr(cfg, "model_backend", backends.NATIVE))
