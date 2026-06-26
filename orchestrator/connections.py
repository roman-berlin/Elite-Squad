"""Jira connections — the cockpit's quick-connect store.

Roman runs several products against DIFFERENT Jira accounts (e.g. Automatixy on one account, the
algo-trading robot on another). This stores named Jira connections and which one each project uses, so
the cockpit can pick a Jira per project and quick-connect a new one without hand-editing .env / config.

Stored in a **gitignored** ``jira_connections.json`` next to the audit log — tokens never enter git, and
the cockpit only ever shows them masked (last 4). The Jira adapter reads the FULL connection for the
active project; if none is assigned it falls back to the old `email_env`/`token_env` env-var path, so
nothing existing breaks.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .backlog.jira import ROMAN_ACCOUNT_ID


def _file(cfg=None) -> Path:
    """The store location — ALWAYS the repo root, so the cockpit (which has a cfg) and the Jira adapter
    (which has only an app, no cfg) read and write the exact same file and can never diverge. ``cfg`` is
    accepted for call-site symmetry but deliberately ignored for the path."""
    return Path(__file__).resolve().parent.parent / "jira_connections.json"


def _load(cfg=None) -> dict:
    try:
        d = json.loads(_file(cfg).read_text(encoding="utf-8"))
        d.setdefault("connections", [])
        d.setdefault("by_project", {})
        return d
    except (OSError, json.JSONDecodeError):
        return {"connections": [], "by_project": {}}


def _save(cfg, data: dict) -> None:
    try:
        _file(cfg).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def _mask(token: str) -> str:
    token = token or ""
    return ("••••" + token[-4:]) if len(token) >= 4 else ("set" if token else "—")


def public(c: dict) -> dict:
    """A connection safe to render — the raw token is replaced by a masked hint."""
    return {"id": c.get("id"), "name": c.get("name"), "base_url": c.get("base_url"),
            "email": c.get("email"), "project_key": c.get("project_key", ""),
            "token_hint": _mask(c.get("token", "")), "added": c.get("added", "")}


def list_connections(cfg=None) -> list[dict]:
    """All saved connections, masked — for the cockpit list."""
    return [public(c) for c in _load(cfg).get("connections", [])]


def add(cfg, *, name: str, base_url: str, email: str, token: str, project_key: str = "",
        assignee: str = ROMAN_ACCOUNT_ID) -> str:
    """Save a new Jira connection. Returns its id. ``assignee`` is pinned to Roman by default so every
    board onboarded via quick-connect inherits him — the adapter then assigns him on new tickets."""
    data = _load(cfg)
    cid = uuid.uuid4().hex[:8]
    data["connections"].append({
        "id": cid, "name": name.strip() or base_url, "base_url": base_url.strip().rstrip("/"),
        "email": email.strip(), "token": token.strip(), "project_key": project_key.strip(),
        "assignee": (assignee or "").strip(), "added": time.strftime("%Y-%m-%d"),
    })
    _save(cfg, data)
    return cid


def remove(cfg, conn_id: str) -> None:
    data = _load(cfg)
    data["connections"] = [c for c in data["connections"] if c.get("id") != conn_id]
    data["by_project"] = {a: cid for a, cid in data["by_project"].items() if cid != conn_id}
    _save(cfg, data)


def assign(cfg, app_name: str, conn_id: str) -> None:
    """Use this connection for this project. ``conn_id`` '' clears the assignment (back to env vars)."""
    data = _load(cfg)
    if conn_id:
        data["by_project"][app_name] = conn_id
    else:
        data["by_project"].pop(app_name, None)
    _save(cfg, data)


def assigned_id(cfg, app_name: str) -> str:
    return _load(cfg).get("by_project", {}).get(app_name, "")


def for_app(app_name: str, cfg=None) -> dict | None:
    """The FULL connection (with token) the adapter should use for this app, or None to fall back to the
    env-var path. Called from the Jira adapter, which has no cfg — defaults to the repo-root store."""
    data = _load(cfg)
    cid = data.get("by_project", {}).get(app_name)
    if not cid:
        return None
    return next((c for c in data.get("connections", []) if c.get("id") == cid), None)
