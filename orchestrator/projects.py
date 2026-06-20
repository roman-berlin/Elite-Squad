"""Project switcher support — Recent projects (VS-Code style) + nearby-repo discovery.

The cockpit already switches between the apps configured in config.yaml. This adds two niceties:
- **Recent projects:** a small runtime list (gitignored) of the projects you've most recently opened,
  surfaced at the top of the switcher so the one you're working on is one click away.
- **Discovery:** scans the folders that hold your configured repos (and ``$GENERAL_PROJECTS_DIR`` /
  ``~/Projects``) for OTHER git repos, so you can see what's there and add it to config to work it.

Pure + filesystem-only — no agent, no network.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import Config

_RECENT_MAX = 6


def _recent_file(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("recent_projects.json")


def recents(cfg: Config, limit: int = _RECENT_MAX) -> list[str]:
    """Most-recently-opened project names, newest first. Filtered to apps that still exist."""
    try:
        data = json.loads(_recent_file(cfg).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    names = {a.name for a in cfg.apps}
    out = [n for n in data if isinstance(n, str) and n in names]
    return out[:limit]


def record_recent(cfg: Config, app_name: str | None) -> None:
    """Mark a project as just-opened (move it to the front). No-op for the all-projects view or an
    unknown app. Best-effort — never raises into the request path."""
    if not app_name or app_name == "*" or app_name not in {a.name for a in cfg.apps}:
        return
    cur = recents(cfg, limit=_RECENT_MAX * 2)
    cur = [app_name] + [n for n in cur if n != app_name]
    try:
        _recent_file(cfg).write_text(json.dumps(cur[:_RECENT_MAX], indent=2), encoding="utf-8")
    except OSError:
        pass


def _scan_dirs(cfg: Config) -> list[Path]:
    """Where to look for repos: the parents of every configured repo, plus $GENERAL_PROJECTS_DIR /
    ~/Projects. Deduped, existing dirs only."""
    dirs: list[Path] = []
    for a in cfg.apps:
        try:
            dirs.append(Path(a.repo_path).expanduser().resolve().parent)
        except (OSError, RuntimeError):
            continue
    extra = os.environ.get("GENERAL_PROJECTS_DIR", "").strip()
    dirs.append(Path(extra).expanduser() if extra else Path.home() / "Projects")
    seen, out = set(), []
    for d in dirs:
        rd = str(d)
        if rd not in seen and d.is_dir():
            seen.add(rd)
            out.append(d)
    return out


def discover_repos(cfg: Config, parents: list[Path] | None = None, limit: int = 24) -> list[dict[str, str]]:
    """Git repos found under the scan dirs that are NOT already configured apps. ``[{name, path}]``."""
    configured = {str(Path(a.repo_path).expanduser().resolve()) for a in cfg.apps}
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for parent in (parents if parents is not None else _scan_dirs(cfg)):
        try:
            children = sorted(parent.iterdir())
        except (OSError, PermissionError):
            continue
        for child in children:
            if not child.is_dir() or not (child / ".git").exists():
                continue
            rp = str(child.resolve())
            if rp in configured or rp in seen:
                continue
            seen.add(rp)
            out.append({"name": child.name, "path": rp})
            if len(out) >= limit:
                return out
    return out
