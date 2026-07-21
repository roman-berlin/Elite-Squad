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

EU-223: the sticky pref is also OPTIONALLY overridable PER APP, so two parallel drains (EU-103 —
e.g. the Elite-Unit drain and the automatixy drain running at the same time) can each pin a
different backend. Shape: ``{"backend": "opus", "apps": {"automatixy": "glm"}}``. Precedence for
``active(cfg, app_name)``: the app's own override -> the global ``backend`` -> the config.yaml
default. A flat legacy file (no ``apps`` key) keeps working unchanged — every app just inherits the
global pref, exactly as before EU-223.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import backends, locking


def _registry(cfg=None):
    """EU-236: the cfg-anchored :class:`ModelRegistry` used to tell a real registry id apart from an
    unknown value, so a persisted/selected registry id is preserved instead of being flattened to
    ``opus`` by :func:`backends.normalize`. Deferred import keeps module load cheap and cycle-free."""
    from .model_registry import ModelRegistry
    return ModelRegistry(cfg)


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


def _load(cfg=None) -> dict:
    """Best-effort parse of the whole store file — ``{}`` if missing/unreadable/not an object.
    Single read path shared by ``get``/``get_apps``/``set_active`` so a per-app write never clobbers
    the global key (or a sibling app's override) it didn't touch."""
    try:
        data = json.loads(_file(cfg).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def get(cfg=None) -> str | None:
    """The persisted GLOBAL backend id, or ``None`` if never set / unreadable. EU-236: a valid
    registry id is preserved verbatim (via :func:`backends.resolve_selection`); an alias is
    canonicalized; an unknown/deleted id degrades safely to ``opus``."""
    bk = _load(cfg).get("backend")
    return backends.resolve_selection(bk, _registry(cfg)) if bk else None


def get_apps(cfg=None) -> dict[str, str]:
    """EU-223: the per-app override map ``{app_name: backend_id}`` (values normalized).

    Tolerates the flat legacy shape (no ``apps`` key) by returning ``{}`` — every app then falls
    back to the global pref via :func:`active`, exactly as before EU-223."""
    apps = _load(cfg).get("apps")
    if not isinstance(apps, dict):
        return {}
    reg = _registry(cfg)
    return {name: backends.resolve_selection(bk, reg) for name, bk in apps.items()
            if isinstance(name, str) and bk}


# Marker values meaning "remove this app's override / let it inherit the global pref" when passed
# to set_active(..., app_name=...). Not a valid backend id, so never confused with 'opus'/'glm'.
_INHERIT = (None, "", "inherit")


def set_active(bk: str | None, cfg=None, app_name: str | None = None) -> None:
    """Persist the active backend id. Best-effort — never raises.

    Without ``app_name``: sets the GLOBAL ``backend`` key, leaving any ``apps`` overrides intact.
    With ``app_name``: writes/clears ONE entry under ``apps`` — ``bk`` in
    ``(None, "", "inherit")`` REMOVES that app's override (it then inherits the global pref) —
    while preserving the global ``backend`` and every other app's entry untouched.

    EU-275: the read-modify-write goes through :func:`locking.locked_rmw` (temp-file +
    ``os.replace`` under the shared cross-thread/cross-process lock), mirroring
    ``autopilot.save_blocked`` — so two concurrent callers (e.g. a global write from one drain and
    a per-app write from another) can never race an unlocked read-then-overwrite and clobber or
    torn-read each other's update."""
    reg = _registry(cfg)

    def _mutate(current: dict) -> dict:
        data = dict(current) if isinstance(current, dict) else {}
        if app_name:
            apps = dict(data.get("apps") or {})
            if bk in _INHERIT:
                apps.pop(app_name, None)
            else:
                apps[app_name] = backends.resolve_selection(bk, reg)
            data["apps"] = apps
        else:
            data["backend"] = backends.resolve_selection(bk, reg)
        return data

    try:
        locking.locked_rmw(_file(cfg), _mutate, default={}, corrupt_to_default=True)
    except (OSError, ValueError):
        pass


# 2026-07-19 (Commander order): the MAIN + SECONDARY model pair. "secondary" is the designated
# stand-in the unit switches to when the main model can't run (Claude plan limit hit, GLM token
# missing) — the fallback EU-108/118 promised but never wired. Stored beside the global backend:
# {"backend": "opus", "secondary": "glm", "apps": {...}}. None = no secondary configured.
_NO_SECONDARY = (None, "", "none")


def get_secondary(cfg=None) -> str | None:
    """The persisted SECONDARY backend id, or None when not configured."""
    bk = _load(cfg).get("secondary")
    if not bk or str(bk).lower() in ("none",):
        return None
    return backends.resolve_selection(bk, _registry(cfg))


def set_secondary(bk: str | None, cfg=None) -> None:
    """Persist (or clear — bk in (None,'','none')) the secondary backend. Best-effort."""
    reg = _registry(cfg)

    def _mutate(current: dict) -> dict:
        data = dict(current) if isinstance(current, dict) else {}
        if bk in _NO_SECONDARY or str(bk).lower() == "none":
            data.pop("secondary", None)
        else:
            data["secondary"] = backends.resolve_selection(bk, reg)
        return data

    try:
        locking.locked_rmw(_file(cfg), _mutate, default={}, corrupt_to_default=True)
    except (OSError, ValueError):
        pass


def get_mode(cfg=None) -> str:
    """2026-07-19 (Commander order): when a Secondary is configured, HOW the two models work —
    'hybrid' (both run per-task: heavy thinking on Main, building on Secondary) or 'backup'
    (the Secondary only wakes when the Main can't run — plan limit / key missing). Only
    meaningful with a Secondary set; defaults to 'hybrid'."""
    m = str(_load(cfg).get("mode") or "").strip().lower()
    return m if m in ("hybrid", "backup") else "hybrid"


def set_mode(mode: str, cfg=None) -> None:
    """Persist the two-model mode ('hybrid' | 'backup'). Best-effort; ignores anything else."""
    def _mutate(current: dict) -> dict:
        data = dict(current) if isinstance(current, dict) else {}
        m = str(mode or "").strip().lower()
        if m in ("hybrid", "backup"):
            data["mode"] = m
        return data

    try:
        locking.locked_rmw(_file(cfg), _mutate, default={}, corrupt_to_default=True)
    except (OSError, ValueError):
        pass


def get_hybrid(cfg=None) -> bool:
    """Whether HYBRID per-task routing is active: a Secondary is configured AND the mode is
    'hybrid'. In 'backup' mode this is False — the Secondary is a fallback only (resolve_for_run),
    never per-tag routing — so the whole run stays on the Main model until it can't run."""
    return bool(get_secondary(cfg)) and get_mode(cfg) == "hybrid"


def active(cfg=None, app_name: str | None = None) -> str:
    """The effective active backend for ``app_name`` (global when omitted): the app's own override,
    else the persisted GLOBAL preference, else the ``config.yaml`` default, else Opus. Returns a
    canonical id (``'opus'`` | ``'glm'``) or, EU-236, a registry record id when one is selected."""
    if app_name:
        override = get_apps(cfg).get(app_name)
        if override:
            return override
    return get(cfg) or backends.resolve_selection(
        getattr(cfg, "model_backend", backends.NATIVE), _registry(cfg))
