"""EU-190: the persisted **active model backend** (Opus vs GLM), shared by the cockpit and the CLI.

The operator picks a backend once; it sticks across cockpit reloads/restarts and applies to
subsequent runs until changed. Stored in a **gitignored** ``model_backend.json`` anchored to the
run's state directory (the parent of ``cfg.audit_path`` — i.e. ``state/`` for the live unit), so the
long-running cockpit and the one-shot ``./general`` CLI always agree on the active backend.

Anchoring to the *config* (not the package root) is load-bearing for hermeticity: the pre-fix
repo-root store leaked the developer's real sticky pref into every test run — 12 harnesses went red
in the main tree (runs blocked on "GLM selected but GLM_AUTH_TOKEN is not configured") while the
worktree-isolated gates stayed green, because only the main tree had the gitignored file
(2026-07-09). Tests build Configs with tmp audit paths, so they now get their own empty store.

``migrate()`` moves a legacy repo-root file into the anchored location once; it is called ONLY from
the live CLI entrypoint (``main.py``), never from ``create_app``/library code, so a test constructing
an app around a tmp config can never relocate the operator's real preference file.

Precedence: this persisted preference OVERRIDES the ``config.yaml`` ``model_backend`` boot default,
which in turn falls back to Opus. Never stores a secret — only the backend id (``'opus'`` | ``'glm'``).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import backends


def _legacy_file() -> Path:
    """The pre-EU-190-hermeticity repo-root store — read only by ``migrate()``."""
    return Path(__file__).resolve().parent.parent / "model_backend.json"


def _file(cfg=None) -> Path:
    """State-dir store, anchored to ``cfg.audit_path``'s parent (``state/`` live; a tmp dir in
    tests). Falls back to the legacy repo-root path only when no config is available."""
    audit = getattr(cfg, "audit_path", None) if cfg is not None else None
    if audit:
        return Path(audit).resolve().parent / "model_backend.json"
    return _legacy_file()


def migrate(cfg) -> None:
    """One-time move of a legacy repo-root ``model_backend.json`` into the cfg-anchored store.
    Best-effort, never raises; a no-op when there is nothing to move or the target already exists.
    Called from the live CLI entrypoint only — see the module docstring for why."""
    try:
        legacy, target = _legacy_file(), _file(cfg)
        if legacy == target or not legacy.exists() or target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        legacy.rename(target)
    except OSError:
        pass


def get(cfg=None) -> str | None:
    """The persisted backend id (normalized), or ``None`` if never set / unreadable."""
    try:
        bk = json.loads(_file(cfg).read_text(encoding="utf-8")).get("backend")
    except (OSError, json.JSONDecodeError):
        return None
    return backends.normalize(bk) if bk else None


def set_active(bk: str, cfg=None) -> None:
    """Persist the active backend id (normalized). Best-effort — never raises."""
    try:
        target = _file(cfg)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"backend": backends.normalize(bk)}, indent=2), encoding="utf-8")
    except OSError:
        pass


def active(cfg=None) -> str:
    """The effective active backend: the persisted preference, else the ``config.yaml`` default,
    else Opus. Always a canonical id (``'opus'`` | ``'glm'``)."""
    return get(cfg) or backends.normalize(getattr(cfg, "model_backend", backends.NATIVE))
