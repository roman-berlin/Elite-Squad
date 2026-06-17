"""Structured health checks — the dashboard's green/red status and `general doctor`.

One source of truth: `checks(cfg)` returns a list of {name, status, detail} where status is
'ok' | 'warn' | 'bad'. A 'bad' check is a blocker — the unit should not start work until it's
fixed; 'warn' is degraded-but-operational. `summary(cfg)` rolls them up for the cockpit.
"""
from __future__ import annotations

import os
import subprocess
from shutil import which
from typing import Any


def _git_ref(repo_path: str, ref: str) -> bool:
    try:
        r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref],
                           cwd=os.path.expanduser(repo_path), capture_output=True, text=True)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def checks(cfg) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str = "") -> None:
        out.append({"name": name, "status": status, "detail": detail})

    # --- platform ---
    auth = cfg.detected_auth()
    add("Claude login", "ok" if auth else "bad",
        auth or "no login — run `claude` then /login (or `claude setup-token`)")
    try:
        import claude_agent_sdk  # noqa: F401
        add("Agent SDK", "ok", "importable")
    except Exception:  # noqa: BLE001
        add("Agent SDK", "bad", "not installed — pip install -r requirements.txt")
    add("git", "ok" if which("git") else "bad", "" if which("git") else "not found on PATH")
    add("gh CLI", "ok" if which("gh") else "warn",
        "present" if which("gh") else "missing — PRs will be skipped")

    from . import notify
    add("Telegram", "ok" if notify.configured() else "warn",
        "configured" if notify.configured() else "off — status alerts disabled")

    jira_have = bool(os.environ.get("JIRA_EMAIL") and os.environ.get("JIRA_API_TOKEN"))

    # --- per app ---
    for app in cfg.apps:
        tag = app.name
        if not os.path.isdir(os.path.join(os.path.expanduser(app.repo_path), ".git")):
            add(f"{tag} · repo", "bad", f"not a git repo: {app.repo_path}")
            continue
        add(f"{tag} · repo", "ok", app.repo_path)
        add(f"{tag} · base branch", "ok" if _git_ref(app.repo_path, app.base_branch) else "bad",
            app.base_branch if _git_ref(app.repo_path, app.base_branch) else f"'{app.base_branch}' missing")
        add(f"{tag} · gate", "ok" if app.gate_commands else "warn",
            "tests configured" if app.gate_commands else "no gate_commands — no tests will run")
        if getattr(cfg, "use_worktree", False):
            ref_ok = _git_ref(app.repo_path, f"origin/{app.base_branch}")
            add(f"{tag} · worktree", "ok" if ref_ok else "warn",
                f"origin/{app.base_branch} resolves" if ref_ok
                else f"origin/{app.base_branch} missing — falls back in-tree")
        if app.backlog_backend == "jira":
            add(f"{tag} · Jira creds", "ok" if jira_have else "bad",
                "set" if jira_have else "JIRA_EMAIL / JIRA_API_TOKEN missing")
    return out


def summary(cfg) -> dict[str, Any]:
    c = checks(cfg)
    problems = [x for x in c if x["status"] == "bad"]
    warnings = [x for x in c if x["status"] == "warn"]
    return {
        "healthy": not problems,
        "problems": len(problems),
        "warnings": len(warnings),
        "checks": c,
        "models": {"builder": getattr(cfg, "builder_model", "?"),
                   "reviewer": getattr(cfg, "reviewer_model", "?")},
    }
