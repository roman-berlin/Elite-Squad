"""One-command new-product onboarding.

Scaffolds a new app into ``config.yaml`` so the unit serves more than Automatixy (SignalDesk, the MQL5
EAs, …) without hand-editing YAML. It:

* validates the repo path is a real git repo (fail fast — Roman's reflex #1),
* detects the repo's actual base/protected branches instead of guessing,
* refuses a duplicate app name,
* backs up ``config.yaml`` then does a **surgical text insert** under ``apps:`` — it never rewrites the
  file, so existing comments and ordering survive (single source of truth, no churn),
* optionally wires an existing cockpit Jira connection (see ``connections.py``) to the new project, so
  the unit can pull that product's tickets immediately.

Preview-by-default: ``scaffold(..., write=False)`` returns the exact YAML block without touching disk.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

_FALLBACK_PROTECTED = "main"


def _git(repo: Path, *args: str) -> str:
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=10)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:  # noqa: BLE001 - branch detection is best-effort; callers handle "" gracefully
        return ""


def is_git_repo(repo: Path) -> bool:
    return (repo / ".git").exists()


def _branches(repo: Path) -> set[str]:
    """Short branch names across local + remote refs (e.g. {'dev', 'main', 'DEV', 'MAIN'})."""
    raw = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes")
    return {b.split("/")[-1] for b in raw.split() if b and b != "HEAD"}


def detect_default_branch(repo: Path) -> str:
    head = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head.startswith("origin/"):
        return head.split("/", 1)[1]
    return _git(repo, "branch", "--show-current") or _FALLBACK_PROTECTED


def suggest_branches(repo: Path) -> tuple[str, str]:
    """(base, protected): base = where features branch off and merge back; protected = production, never
    touched by the pipeline. Detect a dev-like branch for base and a main-like one for protected, honoring
    the repo's actual casing (Automatixy uses DEV/MAIN, most repos use dev/main). Always returns two
    DIFFERENT names (config.validate() requires base != protected)."""
    have = _branches(repo)
    default = detect_default_branch(repo)
    base = next((d for d in ("dev", "develop", "DEV", "Develop", "development") if d in have), "")
    protected = next((m for m in ("main", "master", "MAIN", "Master", "prod", "production") if m in have), "")
    if not protected:
        protected = default if default != base else _FALLBACK_PROTECTED
    if not base:
        # No dev branch yet: features branch off the default; protect a different main-like name.
        base = default
        if protected == base:
            protected = next((m for m in ("main", "master", "MAIN") if m != base), "main")
    if base == protected:  # last-resort guarantee they differ
        protected = "MAIN" if base.islower() else "main"
    return base, protected


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "app"


def app_names(config_path: str | Path) -> list[str]:
    """Existing app names, read tolerantly straight from the YAML (so a half-broken config still lets us
    guard against duplicates)."""
    try:
        import yaml
        data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        return [a.get("name", "") for a in data.get("apps", []) if isinstance(a, dict)]
    except Exception:  # noqa: BLE001
        return []


def build_block(*, name: str, repo_path: str, base: str, protected: str, prefix: str = "autodev",
                backlog: str = "none") -> str:
    """The YAML list-item for one app (2-space sequence indent, matching config.yaml)."""
    lines = [
        f"  - name: {name}",
        f'    repo_path: "{repo_path}"',
        f'    base_branch: "{base}"           # features branch off & merge back here',
        f'    protected_branch: "{protected}"     # the unit NEVER touches this — you promote after QA',
        f'    branch_prefix: "{prefix}"',
        f'    backlog_backend: "{backlog}"',
    ]
    if backlog == "jira":
        lines += [
            "    backlog:                     # site URL + email + token come from the cockpit Jira",
            '      ready_status: "To Do"      # connection assigned to this project (Jira → Quick connect)',
            '      label: "autodev"',
        ]
    else:
        lines.append("    backlog: {}")
    return "\n".join(lines)


def _insert_under_apps(config_path: str | Path, block: str) -> None:
    """Back up config.yaml, then insert ``block`` as the first item under ``apps:`` — a surgical edit that
    leaves every existing line (and comment) untouched."""
    p = Path(config_path)
    shutil.copyfile(p, p.with_name(p.name + ".bak"))
    lines = p.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and line.rstrip() == "apps:":
            out.extend(block.splitlines())
            inserted = True
    if not inserted:  # no apps: key at all — add one at the end
        out.append("apps:")
        out.extend(block.splitlines())
    p.write_text("\n".join(out) + "\n", encoding="utf-8")


def scaffold(config_path: str | Path, *, name: str, repo_path: str, base: str | None = None,
             protected: str | None = None, backlog: str = "none", connection_id: str = "",
             write: bool = False) -> dict:
    """Preview (write=False) or apply (write=True) a new app entry. Returns a result dict with ok/error,
    the resolved branches, the YAML block, and whether it was written."""
    name = (name or "").strip()
    repo = Path(repo_path or "").expanduser()
    if not name:
        return {"ok": False, "error": "a product name is required"}
    if not repo_path or not repo.exists():
        return {"ok": False, "error": f"repo path not found: {repo_path}"}
    if not is_git_repo(repo):
        return {"ok": False, "error": f"not a git repo (no .git): {repo}"}
    if name in app_names(config_path):
        return {"ok": False, "error": f"an app named '{name}' is already in config — pick another name"}
    if backlog not in ("none", "jira"):
        return {"ok": False, "error": f"backlog must be 'none' or 'jira', got '{backlog}'"}

    sb, sp = suggest_branches(repo)
    base = (base or "").strip() or sb
    protected = (protected or "").strip() or sp
    warnings: list[str] = []
    if base == protected:
        return {"ok": False, "error": f"base and protected branch must differ (both '{base}')"}
    have = _branches(repo)
    if have and base not in have:
        warnings.append(f"branch '{base}' not found in the repo yet — create it before the first run")
    if have and protected not in have:
        warnings.append(f"branch '{protected}' not found in the repo yet")
    if backlog == "jira" and not connection_id:
        warnings.append("no Jira connection attached — connect one in the cockpit (Jira → Quick connect) "
                        "and assign it to this project, or it'll fall back to JIRA_EMAIL/JIRA_API_TOKEN")

    block = build_block(name=name, repo_path=str(repo), base=base, protected=protected, backlog=backlog)
    result = {"ok": True, "name": name, "repo_path": str(repo), "base": base, "protected": protected,
              "backlog": backlog, "block": block, "warnings": warnings, "written": False}
    if write:
        _insert_under_apps(config_path, block)
        if backlog == "jira" and connection_id:
            try:
                from . import connections
                connections.assign(None, name, connection_id)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"saved the app, but couldn't assign the Jira connection: {exc}")
        result["written"] = True
    return result
