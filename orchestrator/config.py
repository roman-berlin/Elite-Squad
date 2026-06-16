"""Configuration: YAML for behaviour, environment for secrets.

The pipeline is the PM for *any* app you develop. Each app is its own repo with
its own dev branch, tests, and (optionally) Jira project. `main` is never touched
by the pipeline — you merge that yourself after QA in dev.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class AppConfig:
    name: str
    repo_path: str
    base_branch: str = "dev"            # features branch off (and merge back to) dev
    protected_branch: str = "main"      # NEVER touched by the pipeline
    branch_prefix: str = "autodev"
    gate_commands: list[str] = field(default_factory=list)   # tests/lint/typecheck
    gate_timeout_sec: int = 1800
    gate_env: dict[str, str] = field(default_factory=dict)    # extra env for gate cmds (e.g. NODE_OPTIONS, worker caps)
    backlog_backend: str = "jira"       # "jira" | "notion" | "none"
    backlog: dict[str, Any] = field(default_factory=dict)
    # runtime-resolved working dir for officers + gate (the worktree in isolated mode).
    # None -> use repo_path. Set automatically at run start; not normally in YAML.
    workdir: Optional[str] = None

    def validate(self) -> None:
        repo = Path(self.repo_path).expanduser().resolve()
        if not (repo / ".git").exists():
            raise ValueError(f"[{self.name}] repo_path is not a git repo: {repo}")
        if self.base_branch == self.protected_branch:
            raise ValueError(f"[{self.name}] base_branch must differ from protected_branch "
                             f"('{self.protected_branch}'); the pipeline must never build on main")
        if self.backlog_backend not in ("jira", "notion", "none"):
            raise ValueError(f"[{self.name}] unknown backlog_backend: {self.backlog_backend}")


@dataclass
class Config:
    apps: list[AppConfig]

    # --- models (role asymmetry: a strong reviewer is worth it) ---
    builder_model: str = "claude-sonnet-4-6"
    reviewer_model: str = "claude-opus-4-8"

    # --- effort (thinking depth): low | medium | high | max ---
    builder_effort: str = "high"
    reviewer_effort: str = "high"
    escalate_effort_on_retry: bool = True   # bump the Builder's effort when a pass is rejected

    # --- loop bounds / cost ---
    max_iterations: int = 4
    max_cost_usd: float = 0.0           # 0 = no cap (subscription). Set a number only for API billing.
    max_tickets_per_run: int = 1        # per app, per run

    # --- merge behaviour (auto-merge to dev if green) ---
    merge_to_dev: bool = True           # merge feature -> dev when review passes & dev stays green
    open_pr_on_block: bool = True       # if it can't merge safely, open a PR into dev instead
    mark_done_on_merge: bool = False    # False = leave In Progress for your manual QA on dev;
                                        # True = move the ticket to Done as soon as it merges to dev

    # --- isolation (so the General never fights your manual git checkout) ---
    use_worktree: bool = True           # run each app in a dedicated linked git worktree based on origin/<base>
    worktree_dir: Optional[str] = None  # parent dir for worktrees; default: <repo_parent>/.general-worktrees/<app>
    worktree_setup_cmd: Optional[str] = None  # run ONCE when a worktree is first created (e.g. "bun install")

    # --- safety ---
    dry_run: bool = True                # full loop incl. trial merge, but NO push / PR / backlog writes

    # --- notifications ---
    notify_verbose: bool = False        # also Telegram on implemented / verdict / pushed (not just key events)

    # --- audit ---
    audit_path: str = "./audit.jsonl"   # keep OUTSIDE every target repo

    @staticmethod
    def load(path: str | Path) -> "Config":
        import yaml
        data = yaml.safe_load(Path(path).read_text()) or {}
        apps = [AppConfig(**a) for a in data.pop("apps", [])]
        cfg = Config(apps=apps, **data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.apps:
            raise ValueError("config must define at least one app under 'apps:'")
        names = [a.name for a in self.apps]
        if len(names) != len(set(names)):
            raise ValueError("app names must be unique")
        for app in self.apps:
            app.validate()

    def app(self, name: str) -> AppConfig:
        for a in self.apps:
            if a.name == name:
                return a
        raise KeyError(f"no app named '{name}' in config (have: {[a.name for a in self.apps]})")

    # Auth comes from the environment / Claude Code login, never the YAML file.
    # The officers run on Claude Code, which accepts EITHER a Max/Pro subscription
    # (via `claude` /login or a `claude setup-token` OAuth token) OR an API key.
    def detected_auth(self) -> str | None:
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "ANTHROPIC_API_KEY (per-token API billing)"
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return "CLAUDE_CODE_OAUTH_TOKEN (subscription / Max plan)"
        if _claude_login_present():
            return "claude login (subscription / Max plan)"
        return None


def _claude_login_present() -> bool:
    """Best-effort: is there a `claude` subscription login the agent SDK can use even
    with no auth env var? Checks the Claude Code config/credentials files and the macOS
    Keychain. Never reads a secret (so it never triggers a prompt) and never raises."""
    home = Path.home()
    # credentials file (headless / Linux installs)
    for p in (home / ".claude" / ".credentials.json",
              home / ".config" / "claude" / ".credentials.json"):
        try:
            if p.is_file() and p.stat().st_size > 0:
                return True
        except OSError:
            pass
    # Claude Code's config records the logged-in account
    try:
        cj = home / ".claude.json"
        if cj.is_file() and "oauthAccount" in cj.read_text(encoding="utf-8", errors="ignore"):
            return True
    except OSError:
        pass
    # macOS keeps the OAuth credentials in the login Keychain
    if sys.platform == "darwin":
        for service in ("Claude Code-credentials", "Claude Code"):
            try:
                r = subprocess.run(["security", "find-generic-password", "-s", service],
                                   capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    return True
            except Exception:  # noqa: BLE001 - detection must never break doctor
                pass
    return False
