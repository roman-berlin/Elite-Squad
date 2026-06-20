"""orchestrator/sync.py — Mac ↔ server state sync over git.

The brain-stem (councils, chat, small-talk) runs 24/7 on the VPS; implementation (ticket builds) runs
on the Mac. Each machine writes its OWN ``audit.jsonl`` locally. To give both machines one unified
picture — the server's cockpit + councils should reflect what the Mac shipped, and the Mac's cockpit
should reflect the server's councils — every machine PUBLISHES a copy of its audit to
``shared/<host>.jsonl`` and exchanges the ``shared/`` directory through git.

Design choices (and the constraints they satisfy):

* **Single-writer files.** Only one host ever writes ``shared/<host>.jsonl``. Two machines therefore
  never touch the same path, so there is no merge driver, no union-merge, and no append race — git
  fast-forwards cleanly. ``dashboard.audit_lines`` reads the local audit PLUS every other host's file
  and collapses exact-duplicate lines.

* **A dedicated orphan ``unit-state`` branch, in its own clone (``.unit-state/``).** Runtime state is
  NOT committed to ``main``/``dev`` — that would pollute code history and fight the server's
  ``git reset --hard origin/main`` promotion (exactly the "Mac/server pull conflicts" the .gitignore
  warns about). The state clone lives beside the repo, is gitignored, and tracks an orphan branch that
  carries only ``shared/`` — never code. The main working tree stays on whatever branch it was on.

* **Best-effort, never load-bearing.** Any git hiccup (offline, no push auth on the server, a push
  race) is caught and reported in the return dict; the cockpit still works from local audit alone.
  The server only needs to *read* (pull) to reflect the Mac — pushing its own councils back is a
  bonus that no-ops harmlessly if the server has no push credentials.

Host id: ``$GENERAL_HOST_ID`` if set (e.g. ``mac`` / ``server`` in the launchd/systemd env), else the
machine hostname. Set it explicitly on both machines for clean, stable file names.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

from .config import Config

STATE_DIR_NAME = ".unit-state"   # the dedicated state clone, sibling of audit.jsonl (gitignored)
STATE_BRANCH = "unit-state"      # orphan branch: carries only shared/, never code
_CLONE_DEPTH = "50"              # shallow — we only ever need the tip of the state branch


def host_id(cfg: Config | None = None) -> str:
    """Stable, filesystem-safe id for THIS machine's shared file."""
    raw = (os.environ.get("GENERAL_HOST_ID") or socket.gethostname() or "host").strip()
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in raw).strip("-_").lower()
    return safe or "host"


def pull_only() -> bool:
    """A read-only consumer (e.g. the VPS without git write access): pull peers' audits but never try
    to push our own. Set ``GENERAL_SYNC_PULL_ONLY=1`` on that box so sync exits clean instead of 403-ing
    every run. The box's own audit is still read locally by ``dashboard.audit_lines`` — only the
    publish-back is skipped."""
    return os.environ.get("GENERAL_SYNC_PULL_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}


def _repo_root(cfg: Config) -> Path:
    """The General repo root — audit.jsonl lives at the repo root, so its parent is the root."""
    return Path(cfg.audit_path).resolve().parent


def state_dir(cfg: Config) -> Path:
    return _repo_root(cfg) / STATE_DIR_NAME


def shared_dir(cfg: Config) -> Path:
    return state_dir(cfg) / "shared"


def shared_files(cfg: Config) -> list[Path]:
    """Every host's published audit visible to this machine (from the state clone)."""
    d = shared_dir(cfg)
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


def _git(cwd: Path, *args: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )


def _origin_url(root: Path) -> str | None:
    r = _git(root, "remote", "get-url", "origin")
    url = r.stdout.strip()
    return url if (r.returncode == 0 and url) else None


def _set_identity(sd: Path) -> None:
    """Local commit identity for the state clone, so ``commit``/``rebase`` work even on a fresh server
    clone (or in CI) where no global git ``user.*`` is configured."""
    _git(sd, "config", "user.email", "unit@localhost")
    _git(sd, "config", "user.name", "Elite Unit")


def ensure_state_clone(cfg: Config) -> Path | None:
    """Make sure ``.unit-state/`` exists as a clone tracking the orphan ``unit-state`` branch.

    Tries to clone the existing remote branch first; if it does not exist yet (first run ever),
    clones the default branch and switches to a fresh orphan ``unit-state`` (empty working tree).
    Returns the state-clone path, or ``None`` if there is no usable git origin.
    """
    sd = state_dir(cfg)
    if (sd / ".git").exists():
        return sd
    root = _repo_root(cfg)
    url = _origin_url(root)
    if not url:
        return None
    sd.parent.mkdir(parents=True, exist_ok=True)
    if sd.exists():
        shutil.rmtree(sd, ignore_errors=True)
    # Fast path: the state branch already exists on the remote.
    ok = _git(root, "clone", "--branch", STATE_BRANCH, "--single-branch",
              "--depth", _CLONE_DEPTH, url, str(sd))
    if ok.returncode == 0 and (sd / ".git").exists():
        _set_identity(sd)
        return sd
    # First run: no unit-state branch yet. Clone default, then start a clean orphan.
    if sd.exists():
        shutil.rmtree(sd, ignore_errors=True)
    base = _git(root, "clone", "--single-branch", "--depth", _CLONE_DEPTH, url, str(sd))
    if base.returncode != 0 or not (sd / ".git").exists():
        return None
    # `git switch --orphan` (git ≥ 2.23) clears the index + working tree — no code carries over.
    orphan = _git(sd, "switch", "--orphan", STATE_BRANCH)
    if orphan.returncode != 0:
        return None
    _set_identity(sd)
    (sd / "shared").mkdir(parents=True, exist_ok=True)
    return sd


def publish(cfg: Config, sd: Path | None = None) -> Path:
    """Copy this machine's live ``audit.jsonl`` into ``shared/<host>.jsonl`` in the state clone."""
    sd = sd or state_dir(cfg)
    dst = sd / "shared" / f"{host_id(cfg)}.jsonl"
    dst.parent.mkdir(parents=True, exist_ok=True)
    src = Path(cfg.audit_path)
    dst.write_text(src.read_text(encoding="utf-8") if src.exists() else "", encoding="utf-8")
    return dst


def git_sync(cfg: Config) -> dict[str, Any]:
    """Exchange ``shared/`` with the remote: pull every host's latest, publish ours, push it back.

    Returns ``{host, pulled, pushed, hosts, error}``. Best-effort — a failure to push (e.g. the server
    has no credentials) still leaves ``pulled`` true, so the server has the Mac's data either way.
    """
    out: dict[str, Any] = {"host": host_id(cfg), "pulled": False, "pushed": False,
                           "hosts": [], "error": None}
    sd = ensure_state_clone(cfg)
    if sd is None:
        out["error"] = "no git origin for state sync"
        return out
    try:
        # 1) Pull others' latest. fetch + reset (not merge) keeps the branch a clean fast-forward; we
        #    publish AFTER the reset so it never clobbers our freshly-written file.
        fetch = _git(sd, "fetch", "origin", STATE_BRANCH)
        if fetch.returncode == 0:
            # Reset to FETCH_HEAD, not origin/<branch>: the bootstrap clone is --single-branch on the
            # DEFAULT branch, so its remote-tracking refspec never covers unit-state and origin/<branch>
            # may be missing/stale. FETCH_HEAD is exactly what we just fetched — always the right tip.
            _git(sd, "reset", "--hard", "FETCH_HEAD")
            out["pulled"] = True

        if pull_only():
            # Read-only consumer: we've pulled the peers' audits — never attempt a push (no recurring
            # 403, clean exit 0). Our own audit is still read locally by dashboard.audit_lines.
            out["hosts"] = [p.stem for p in shared_files(cfg)]
            out["pushed"] = None
            return out

        # 2) Publish our own audit and stage it.
        publish(cfg, sd)
        out["hosts"] = [p.stem for p in shared_files(cfg)]
        _git(sd, "add", "shared")
        if not _git(sd, "status", "--porcelain").stdout.strip():
            out["pushed"] = True   # nothing changed since last sync — already in step with remote
            return out

        # 3) Commit + push. On a race, rebase our single commit onto the remote tip and retry once.
        _git(sd, "commit", "-m", f"sync: {host_id(cfg)} audit")
        push = _git(sd, "push", "origin", f"HEAD:{STATE_BRANCH}")
        if push.returncode != 0:
            _git(sd, "fetch", "origin", STATE_BRANCH)
            _git(sd, "rebase", "FETCH_HEAD")
            push = _git(sd, "push", "origin", f"HEAD:{STATE_BRANCH}")
        out["pushed"] = push.returncode == 0
        if not out["pushed"]:
            out["error"] = (push.stderr or push.stdout or "push failed").strip()[:200]
    except (subprocess.SubprocessError, OSError) as e:
        out["error"] = str(e)[:200]
    return out


def pull_server_state(cfg: Config) -> dict[str, Any]:
    """Mac-side server→Mac bridge over SSH: copy the server's officer-canonical living log down so the
    Mac's builds read the latest server-learned lessons. One-directional and read-only on the server —
    we just `scp` a file out, so the server never needs git write access.

    No-op unless ``GENERAL_SERVER_SSH`` (e.g. ``ubuntu@1.2.3.4``) is set and we're not the server itself
    (``GENERAL_SYNC_PULL_ONLY``). ``GENERAL_SERVER_REPO`` overrides the remote repo dir (default
    ``General``). Best-effort: any SSH hiccup is reported, never fatal."""
    out: dict[str, Any] = {"attempted": False, "pulled": False, "error": None}
    host = os.environ.get("GENERAL_SERVER_SSH", "").strip()
    if not host or pull_only():
        return out
    out["attempted"] = True
    remote_repo = os.environ.get("GENERAL_SERVER_REPO", "General").strip() or "General"
    src = f"{host}:{remote_repo}/memory/UNIT.live.md"
    dst = _repo_root(cfg) / "memory" / "UNIT.live.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", src, str(dst)],
            capture_output=True, text=True, timeout=60)
        out["pulled"] = r.returncode == 0
        if r.returncode != 0:
            out["error"] = (r.stderr or r.stdout or "scp failed").strip()[:200]
    except (subprocess.SubprocessError, OSError) as e:
        out["error"] = str(e)[:200]
    return out


# ---------------------------------------------------------------------------
# Promote DEV -> main from the cockpit (the "Deploy" button).
# ---------------------------------------------------------------------------

def can_promote() -> bool:
    """Only a cockpit explicitly allowed to push DEV->main shows the Deploy button. The Mac launcher
    sets ``GENERAL_COCKPIT_PROMOTE=1``; the read-only server never does, so its cockpit can't promote
    (and a push there would 403 anyway)."""
    return os.environ.get("GENERAL_COCKPIT_PROMOTE", "").strip().lower() in {"1", "true", "yes", "on"}


def promote_status(cfg: Config) -> dict[str, Any]:
    """How far ``dev`` is ahead of ``main`` — i.e. changes approved/merged to DEV but not yet deployed
    to the server. ``{ahead, subjects, error}``; ``ahead == 0`` means the server is current."""
    repo = _repo_root(cfg)
    out: dict[str, Any] = {"ahead": 0, "subjects": [], "error": None}
    try:
        r = _git(repo, "rev-list", "--count", "main..dev")
        out["ahead"] = int((r.stdout or "0").strip() or "0") if r.returncode == 0 else 0
        if out["ahead"]:
            log = _git(repo, "log", "--oneline", "-8", "main..dev")
            out["subjects"] = [ln.strip() for ln in log.stdout.splitlines() if ln.strip()]
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        out["error"] = str(e)[:200]
    return out


def promote(cfg: Config) -> dict[str, Any]:
    """Promote ``dev`` -> ``main`` and push; the server auto-deploys ``main``. **Fast-forward only** —
    if dev and main have diverged it reports instead of forcing a merge. Always returns the working
    tree to ``dev``. ``{ok, ahead_before, pushed, error}``."""
    repo = _repo_root(cfg)
    out: dict[str, Any] = {"ok": False, "ahead_before": 0, "pushed": False, "error": None}
    if not can_promote():
        out["error"] = "promote not allowed on this cockpit"
        return out
    cur = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "dev"
    out["ahead_before"] = promote_status(cfg).get("ahead", 0)
    if out["ahead_before"] == 0:
        out["ok"] = True   # already in sync — nothing to deploy
        return out
    try:
        co = _git(repo, "checkout", "main")
        if co.returncode != 0:
            out["error"] = "checkout main failed (uncommitted changes on the working tree?): " + (co.stderr or "").strip()[:150]
            return out
        mg = _git(repo, "merge", "--ff-only", "dev")
        if mg.returncode != 0:
            out["error"] = "fast-forward failed — dev and main diverged; resolve in a terminal: " + (mg.stderr or "").strip()[:140]
            return out
        ps = _git(repo, "push", "origin", "main")
        out["pushed"] = ps.returncode == 0
        out["ok"] = ps.returncode == 0
        if not out["pushed"]:
            out["error"] = "push to origin/main failed: " + ((ps.stderr or ps.stdout) or "").strip()[:150]
    except (subprocess.SubprocessError, OSError) as e:
        out["error"] = str(e)[:200]
    finally:
        _git(repo, "checkout", cur if cur != "main" else "dev")   # always leave the tree on DEV
    return out
