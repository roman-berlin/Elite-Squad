"""EU-228: infra/outage/transient error classification — stop charging environment failures to
tickets.

The 2026-07-10 forensics found two unclassified failure classes silently burning per-ticket error
strikes toward ``autopilot._MAX_TICKET_ERRORS``:

  1. A DNS/network blip (``HTTPSConnectionPool(host='toibis.atlassian.net' ... NameResolutionError``
     on a Jira ``/transitions`` call) charged FOUR tickets one strike each in the same second
     (2026-07-10T16:29:47 — EU-234/235/236/237) — a queue-wide outage, not four broken tickets.
  2. bun-ENOENT toolchain misses (already fixed separately, c2f5f17) — the surviving generalisation
     is any missing binary the app's own gate/worktree-setup toolchain needs under the daemon's
     REAL environment (a launchd/cron shell commonly lacks a user's interactive PATH).

Per the Commander's 2026-07-12 re-scope this module intentionally does NOT touch the third class
from the original ticket brief: an SDK "exit code 1" crash is (8-of-9 sampled cases) turn-limit
exhaustion, which is ticket-attributable and deterministic — never infra. ``classify()`` below
explicitly refuses to tag turn-limit text as infra (reusing ``loop._TURN_LIMIT_MARKERS`` so the two
classifiers can never drift apart), leaving that class to EU-248's turn-limit/scrum-split path
(``loop._is_turn_limit`` / ``loop._exception_report``).

Two entry points, wired into ``autopilot.py``:
  * ``classify(text)``           — infra/transient tag for an ERRORED report's notes, or "" (falsy)
                                    when it's a real ticket-attributable failure (see above).
  * ``connectivity_probe(cfg)``  — True once the outage that triggered an offline-hold has cleared
                                    (Jira base URL reachable AND `git ls-remote` succeeds), used to
                                    auto-resume the loop with no human ``/unblock``.
  * ``missing_toolchain(app)``   — binaries an app's gate/worktree-setup commands need that are NOT
                                    resolvable under the daemon's real environment, used to HOLD the
                                    whole app (one alert) instead of parking its tickets one by one.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

# Reuse the SAME turn-limit markers loop.py uses to route a turn-cap blow-out to the Scrum Master
# split (EU-248) — a turn-limit exception must NEVER be classified as infra/transient here, or the
# offline-hold path would blind-retry an oversized ticket forever at the same turn budget.
from .loop import _TURN_LIMIT_MARKERS

_5XX_RE = re.compile(r"\b5\d\d\b")

# Substring markers -> infra tag. Order matters: checked top to bottom, first match wins.
_INFRA_MARKERS: tuple[tuple[str, str], ...] = (
    ("exec format error", "exec-format"),
    ("enoexec", "exec-format"),
    ("nameresolutionerror", "dns"),
    ("name resolution", "dns"),
    ("temporary failure in name resolution", "dns"),
    ("getaddrinfo", "dns"),
    ("httpsconnectionpool", "network"),
    ("connectionerror", "network"),
    ("connection refused", "network"),
    ("econnrefused", "network"),
    ("network is unreachable", "network"),
    ("remote end closed connection", "network"),
    ("timed out", "timeout"),
    ("timeout", "timeout"),
    ("read timed out", "timeout"),
)


def classify(text: str | None) -> str:
    """Return an infra/transient tag ('dns' | 'network' | 'timeout' | '5xx' | 'exec-format') when
    `text` looks like an environment/outage failure, else '' (falsy) — including for ANY turn-limit
    marker, which is a ticket-attributable "too big for one pass" and must never be retried as if it
    were transient infra (that would blind-retry an oversized ticket forever at the same budget)."""
    t = (text or "").lower()
    if not t:
        return ""
    if any(m in t for m in _TURN_LIMIT_MARKERS):
        return ""
    for marker, tag in _INFRA_MARKERS:
        if marker in t:
            return tag
    if _5XX_RE.search(t):
        return "5xx"
    return ""


# --------------------------------------------------------------------------------------------- #
# connectivity_probe — Jira base URL reachable + `git ls-remote` succeeds. Small, separately
# monkeypatchable helpers so tests can simulate an outage/recovery without any real network I/O.
# --------------------------------------------------------------------------------------------- #

def _jira_base_urls(cfg) -> list[str]:
    urls: set[str] = set()
    for app in getattr(cfg, "apps", None) or []:
        if getattr(app, "backlog_backend", "none") != "jira":
            continue
        url = (getattr(app, "backlog", None) or {}).get("base_url")
        if url:
            urls.add(str(url).rstrip("/"))
    return sorted(urls)


def _probe_jira_url(url: str, timeout: float) -> bool:
    try:
        import requests
        r = requests.get(url, timeout=timeout)
        return r.status_code < 500
    except Exception:  # noqa: BLE001 - unreachable/DNS/etc all mean "not yet recovered"
        return False


def _repo_paths(cfg) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for app in getattr(cfg, "apps", None) or []:
        repo = getattr(app, "repo_path", "") or ""
        if repo and repo not in seen and Path(repo).expanduser().is_dir():
            seen.add(repo)
            paths.append(repo)
    return paths


def _probe_git_remote(repo_path: str, timeout: float) -> bool:
    try:
        r = subprocess.run(["git", "ls-remote", "--exit-code", "-h", "origin"],
                           cwd=os.path.expanduser(repo_path), capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode == 0
    except Exception:  # noqa: BLE001 - a timeout/missing git/etc all mean "not yet recovered"
        return False


def connectivity_probe(cfg, *, timeout: float = 5.0) -> bool:
    """True once the outage that triggered an offline-hold has cleared: every configured Jira
    base URL responds (status < 500) AND at least one configured repo's `git ls-remote origin`
    succeeds. An app class with nothing configured to probe (no jira app / no repo) is skipped
    rather than counted as a failure, so the probe reflects real connectivity, not config
    completeness. Best-effort per check — used to auto-resume the offline-hold with no human
    `/unblock` (EU-228)."""
    urls = _jira_base_urls(cfg)
    jira_ok = all(_probe_jira_url(u, timeout) for u in urls) if urls else True
    repos = _repo_paths(cfg)
    git_ok = any(_probe_git_remote(r, timeout) for r in repos) if repos else True
    return jira_ok and git_ok


# --------------------------------------------------------------------------------------------- #
# missing_toolchain — shutil.which every binary an app's gate/worktree-setup commands need under
# the daemon's REAL environment, so a missing one HOLDS the whole app (one alert) instead of
# burning every one of its tickets an error strike (the bun-ENOENT signature, c2f5f17).
# --------------------------------------------------------------------------------------------- #

_SHELL_KEYWORDS = {"cd", "export", "source", "set", "pushd", "popd", "echo", "&&", "true", "false"}
_SPLIT_RE = re.compile(r"&&|\|\||;|\|")


def _command_binaries(cmd: str) -> list[str]:
    """First executable token of each &&/;/||/| segment of a shell command string, skipping shell
    builtins/keywords (never real binaries on PATH) and env-var assignments (contain '=')."""
    bins: list[str] = []
    for part in _SPLIT_RE.split(cmd):
        part = part.strip()
        if not part:
            continue
        tokens = part.split()
        if not tokens:
            continue
        tok = tokens[0].strip("\"'")
        if not tok or tok in _SHELL_KEYWORDS or "=" in tok:
            continue
        bins.append(tok)
    return bins


def _binary_available(binary: str, repo_path: str | None) -> bool:
    if "/" in binary:
        base = Path(repo_path).expanduser() if repo_path else Path(".")
        p = (base / binary).expanduser()
        return p.is_file() and os.access(p, os.X_OK)
    return shutil.which(binary) is not None


def missing_toolchain(app) -> list[str]:
    """Sorted, de-duped binaries `app`'s gate_commands / gate_commands_by_app / lint_commands /
    worktree_setup_cmd need that are NOT resolvable under the daemon's real environment. Empty when
    the toolchain is intact — the common case, so this is cheap to call every cycle."""
    cmds: list[str] = list(getattr(app, "gate_commands", None) or [])
    cmds += list(getattr(app, "lint_commands", None) or [])
    for extra in (getattr(app, "gate_commands_by_app", None) or {}).values():
        cmds += list(extra or [])
    wsc = getattr(app, "worktree_setup_cmd", None)
    if wsc:
        cmds.append(wsc)

    needed: set[str] = set()
    for cmd in cmds:
        needed.update(_command_binaries(cmd))

    repo_path = getattr(app, "repo_path", None)
    return sorted(b for b in needed if not _binary_available(b, repo_path))
