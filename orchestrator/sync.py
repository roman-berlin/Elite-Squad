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
import re
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .config import Config

STATE_DIR_NAME = ".unit-state"   # the dedicated state clone, sibling of audit.jsonl (gitignored)
STATE_BRANCH = "unit-state"      # orphan branch: carries only shared/, never code
_CLONE_DEPTH = "50"              # shallow — we only ever need the tip of the state branch


def _safe_host(raw: str) -> str:
    """Filesystem-safe host token: alnum / - / _ only, lowercased; every other char (space, '/', '.')
    becomes '-'. Never empty. Used for every ``shared/<host>.jsonl`` token so a token can never contain
    a path separator (escaping shared/) or whitespace (breaking the whitespace-split exclude guard)."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in (raw or "")).strip("-_").lower()
    return safe or "host"


def host_id(cfg: Config | None = None) -> str:
    """Stable, filesystem-safe id for THIS machine's shared file."""
    return _safe_host((os.environ.get("GENERAL_HOST_ID") or socket.gethostname() or "host").strip())


def pull_only() -> bool:
    """A read-only consumer (e.g. the VPS without git write access): pull peers' audits but never try
    to push our own. Set ``GENERAL_SYNC_PULL_ONLY=1`` on that box so sync exits clean instead of 403-ing
    every run. The box's own audit is still read locally by ``dashboard.audit_lines`` — only the
    publish-back is skipped."""
    return os.environ.get("GENERAL_SYNC_PULL_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}


def _repo_root(cfg: Config) -> Path:
    """The General repo root, derived from audit_path so tests stay isolated in their tmp dirs.

    Review fix (2026-07-05): audit.jsonl used to live AT the repo root; QW2 moved the runtime
    state into a state/ subdirectory, so the audit dir's PARENT is the root whenever the dir uses
    that convention. Without this, .unit-state/ and the pulled memory/UNIT.live.md would land
    under state/ while every reader (memory.py, cockpit) keeps using the repo root."""
    root = Path(cfg.audit_path).resolve().parent
    if root.name == "state":
        root = root.parent
    return root


def state_dir(cfg: Config) -> Path:
    return _repo_root(cfg) / STATE_DIR_NAME


def shared_dir(cfg: Config) -> Path:
    return state_dir(cfg) / "shared"


def shared_files(cfg: Config) -> list[Path]:
    """Every host's published audit visible to this machine (from the state clone)."""
    d = shared_dir(cfg)
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


def _git(cwd: Path, *args: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    # GIT_TERMINAL_PROMPT=0: a push over HTTPS with no cached credentials must FAIL FAST, never block
    # forever waiting for a username/password the cockpit thread can't answer (the "stuck spinner").
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
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

        # 2) Publish our own audit and stage ONLY it. Single-writer: a host owns exactly
        #    shared/<its-host>.jsonl — never `git add shared` (which would stage a peer/server file we
        #    pulled in over SSH, e.g. pull_server_audit's shared/<server>.jsonl, and push it back,
        #    breaking the single-writer invariant and making that host read its own audit doubled).
        publish(cfg, sd)
        out["hosts"] = [p.stem for p in shared_files(cfg)]
        _git(sd, "add", f"shared/{host_id(cfg)}.jsonl")
        if _git(sd, "diff", "--cached", "--quiet").returncode == 0:
            out["pushed"] = True   # nothing staged since last sync — already in step with remote
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


def pull_server_audit(cfg: Config) -> dict[str, Any]:
    """Mac-side server→Mac AUDIT bridge over SSH: scp the server's live ``audit.jsonl`` down so the Mac
    cockpit MIRRORS the server's runs (EU-181).

    The server runs sync pull-only (no git push credentials), so it never publishes its own audit to the
    ``unit-state`` git branch — without this the Mac never sees the server and the two cockpits diverge.
    We pull it over the SAME SSH bridge as ``UNIT.live.md`` (``pull_server_state``) and land it in the
    machine-LOCAL ``<audit-dir>/shared/<server>.jsonl`` — NOT the git-tracked state clone. That path is
    read by ``dashboard._audit_paths`` (the ``<audit-dir>/shared`` probe) and gitignored, so the pulled
    server audit shows in the Mac cockpit but can NEVER be re-published to the shared git branch (which
    would break the single-writer invariant and make the server double-count its own audit). Keeping it
    out of the git clone entirely sidesteps every tracking / reset --hard / exclude hazard.

    No-op unless ``GENERAL_SERVER_SSH`` is set and we are not the server (``GENERAL_SYNC_PULL_ONLY``).
    ``GENERAL_SERVER_REPO`` (default ``General``) + ``GENERAL_SERVER_AUDIT`` (default
    ``state/audit.jsonl``, relative to the repo) locate the remote file; ``GENERAL_SERVER_HOST_ID``
    (default ``server``, sanitised) names the local file. Best-effort — any hiccup is reported, never fatal."""
    out: dict[str, Any] = {"attempted": False, "pulled": False, "host": None, "error": None}
    host = os.environ.get("GENERAL_SERVER_SSH", "").strip()
    if not host or pull_only():
        return out
    out["attempted"] = True
    server_host = _safe_host(os.environ.get("GENERAL_SERVER_HOST_ID", "").strip() or "server")
    out["host"] = server_host
    remote_repo = os.environ.get("GENERAL_SERVER_REPO", "General").strip() or "General"
    rel = os.environ.get("GENERAL_SERVER_AUDIT", "").strip() or "state/audit.jsonl"
    src = f"{host}:{remote_repo}/{rel}"
    # Land it beside the LOCAL audit.jsonl (gitignored, NOT the .unit-state git clone) — the cockpit
    # reads <audit-dir>/shared/*.jsonl; nothing here ever touches the shared git branch.
    dst = Path(cfg.audit_path).parent / "shared" / f"{server_host}.jsonl"
    try:
        # All filesystem writes are INSIDE the guard — a permission/FS hiccup must degrade to a
        # reported error, never raise into the `general sync` command.
        dst.parent.mkdir(parents=True, exist_ok=True)
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
    """Promote ``dev`` -> ``main`` on the REMOTE (the server auto-deploys ``main``) **without touching
    the working tree** — the unit constantly writes runtime files, so a dirty tree must never block a
    deploy (it was the old checkout-based version's "stuck spinner"). Pushes ``dev`` straight onto
    ``main``, **fast-forward only**; if they've diverged the push is rejected (never forced).
    ``{ok, ahead_before, pushed, error}``."""
    repo = _repo_root(cfg)
    out: dict[str, Any] = {"ok": False, "ahead_before": 0, "pushed": False, "error": None}
    if not can_promote():
        out["error"] = "promote not allowed on this cockpit"
        return out
    out["ahead_before"] = promote_status(cfg).get("ahead", 0)
    if out["ahead_before"] == 0:
        out["ok"] = True   # already in sync — nothing to deploy
        return out
    try:
        ps = _git(repo, "push", "origin", "dev:main")   # ff-only by default; the tree is never touched
        out["pushed"] = out["ok"] = ps.returncode == 0
        if out["ok"]:
            _git(repo, "branch", "-f", "main", "dev")   # advance the local main ref (main isn't checked out)
        else:
            err = ((ps.stderr or ps.stdout) or "").strip()
            out["error"] = ("dev and main have diverged — resolve in a terminal"
                            if ("non-fast-forward" in err or "rejected" in err)
                            else "push to origin/main failed: " + err[:150])
    except subprocess.TimeoutExpired:
        out["error"] = "push timed out — check your network / GitHub credentials"
    except (subprocess.SubprocessError, OSError) as e:
        out["error"] = str(e)[:200]
    return out


# ---------------------------------------------------------------------------
# Ship an APP's DEV -> MAIN (production) from the cockpit (the "Ship → MAIN" button).
# This is the user's own product (e.g. Automatixy), not the unit's own code.
# ---------------------------------------------------------------------------

def app_promote_status(app) -> dict[str, Any]:
    """How far an app's base branch (DEV) is ahead of its protected branch (MAIN) — work that's tested
    on DEV but not yet shipped to production. ``{ahead, base, prot, error}``."""
    repo = Path(app.repo_path).expanduser()
    base, prot = app.base_branch, app.protected_branch
    out: dict[str, Any] = {"ahead": 0, "base": base, "prot": prot, "error": None}
    try:
        r = _git(repo, "rev-list", "--count", f"{prot}..{base}")
        out["ahead"] = int((r.stdout or "0").strip() or "0") if r.returncode == 0 else 0
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        out["error"] = str(e)[:200]
    return out


_TICKET_KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+")


def app_promote_commits(app, limit: int = 300) -> list[dict[str, str]]:
    """The commits on the app's DEV not yet on MAIN — exactly what 'Ship' will deploy — newest first:
    ``[{sha, subject, ticket}]``. ``ticket`` is the first AUTO-style key in the subject, or ''."""
    repo = Path(app.repo_path).expanduser()
    base, prot = app.base_branch, app.protected_branch
    out: list[dict[str, str]] = []
    try:
        r = _git(repo, "log", f"{prot}..{base}", "--pretty=format:%h%x1f%s", f"-{int(limit)}")
        if r.returncode != 0:
            return out
        for line in (r.stdout or "").splitlines():
            if "\x1f" not in line:
                continue
            sha, subj = line.split("\x1f", 1)
            m = _TICKET_KEY.search(subj)
            out.append({"sha": sha.strip(), "subject": subj.strip(), "ticket": m.group(0) if m else ""})
    except (subprocess.SubprocessError, OSError):
        return out
    return out


def promote_app(app) -> dict[str, Any]:
    """Ship an app's DEV -> MAIN (production): a **real merge** of DEV into MAIN (MAIN keeps its own
    commits — e.g. earlier PR merges — and DEV's commits are added), then push MAIN. Done in a throwaway
    git worktree so the user's (often dirty) checkout is never touched, and so it works even when DEV
    and MAIN have diverged (a fast-forward can't). If MAIN is a protected branch the push is rejected
    and we say so — ship via a PR. The cockpit Ship button. ``{ok, ahead_before, pushed, error, ...}``."""
    repo = Path(app.repo_path).expanduser()
    base, prot = app.base_branch, app.protected_branch
    out: dict[str, Any] = {"ok": False, "ahead_before": 0, "pushed": False, "error": None,
                           "base": base, "prot": prot, "app": app.name}
    if not can_promote():
        out["error"] = "shipping is disabled on this cockpit (read-only box)"
        return out
    out["ahead_before"] = app_promote_status(app).get("ahead", 0)
    if out["ahead_before"] == 0:
        out["ok"] = True   # already shipped — nothing ahead
        return out
    wt = tempfile.mkdtemp(prefix="general-ship-")
    try:
        _git(repo, "fetch", "origin", base, prot, timeout=120)
        add = _git(repo, "worktree", "add", "--detach", "--force", wt, f"origin/{prot}")
        if add.returncode != 0:
            out["error"] = "couldn't stage the merge: " + ((add.stderr or add.stdout) or "").strip()[:150]
            return out
        wtp = Path(wt)
        mg = _git(wtp, "merge", "--no-edit", "-m", f"Ship {base} -> {prot} (production)", f"origin/{base}")
        if mg.returncode != 0:
            _git(wtp, "merge", "--abort")
            out["error"] = f"merging {base} into {prot} hit conflicts — resolve in a terminal or a PR"
            return out
        ps = _git(wtp, "push", "origin", f"HEAD:{prot}")
        out["pushed"] = out["ok"] = ps.returncode == 0
        if out["ok"]:
            _git(repo, "branch", "-f", prot, f"origin/{prot}")   # advance local MAIN so the cockpit shows 0 ahead
        else:
            err = ((ps.stderr or ps.stdout) or "").strip()
            out["error"] = (f"{prot} is a protected branch — ship via a Pull Request instead"
                            if ("protected" in err.lower() or "denied" in err.lower() or "hook" in err.lower())
                            else f"push to origin/{prot} failed: " + err[:150])
    except subprocess.TimeoutExpired:
        out["error"] = f"merge/push to {prot} timed out — check your network / credentials"
    except (subprocess.SubprocessError, OSError) as e:
        out["error"] = str(e)[:200]
    finally:
        _git(repo, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)
    return out
