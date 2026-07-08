"""Structured health checks — the dashboard's green/red status and `general doctor`.

One source of truth: `checks(cfg)` returns a list of {name, status, detail} where status is
'ok' | 'warn' | 'bad'. A 'bad' check is a blocker — the unit should not start work until it's
fixed; 'warn' is degraded-but-operational. `summary(cfg)` rolls them up for the cockpit.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from shutil import which
from typing import Any

# EU-18: deps a drifted worktree silently re-pins off DEV. We compare the WHOLE bun.lock
# (the authoritative pin), but surface these names by version when there IS drift — a mismatch
# here is the canonical symptom of the worktree install having floated off DEV.
_PINNED_PKGS = ("@supabase/supabase-js",)

# GUI-launched apps inherit a minimal PATH, so `which` alone can miss Homebrew/bun tools
# (e.g. gh). Also look in the common bin locations.
_EXTRA_BINS = ("/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.bun/bin"))


def _has(cmd: str) -> bool:
    if which(cmd):
        return True
    return any(os.path.exists(os.path.join(p, cmd)) for p in _EXTRA_BINS)


def _git_ref(repo_path: str, ref: str) -> bool:
    try:
        r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref],
                           cwd=os.path.expanduser(repo_path), capture_output=True, text=True)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _resolved_pin(lock_text: str | None, pkg: str) -> str | None:
    """Best-effort resolved version of ``pkg`` in a bun.lock (text format).

    bun.lock spells a resolved dependency as ``"<name>@<version>"`` (e.g.
    ``"@supabase/supabase-js@2.39.0"``). We only need it for the human-readable detail —
    pass/fail rides on the full-file comparison, not this regex."""
    if not lock_text:
        return None
    m = re.search(re.escape(pkg) + r"@(\d[^\"'\s,\]]*)", lock_text)
    return m.group(1) if m else None


def lockfile_drift(worktree_lock: str | None, base_lock: str | None,
                   pkgs: tuple[str, ...] = _PINNED_PKGS) -> tuple[str, str]:
    """Pure check: has the worktree's bun.lock drifted off DEV's pinned bun.lock?

    Returns ``(status, detail)`` with status ``'ok' | 'warn' | 'bad'``. A 'bad' means the
    (reused) worktree install floated off DEV — the exact failure EU-18's frozen-lockfile
    re-pin exists to prevent. 'warn' is can't-tell (no base lock, or worktree not set up)."""
    if base_lock is None:
        return ("warn", "no bun.lock at base ref — nothing to pin against")
    if worktree_lock is None:
        return ("warn", "worktree not set up / no bun.lock yet — re-pinned on next build")
    if worktree_lock == base_lock:
        pin = _resolved_pin(base_lock, _PINNED_PKGS[0])
        return ("ok", "bun.lock matches DEV" + (f" (@supabase/supabase-js@{pin})" if pin else ""))
    bits = []
    for pkg in pkgs:
        wv, bv = _resolved_pin(worktree_lock, pkg), _resolved_pin(base_lock, pkg)
        if wv != bv:
            bits.append(f"{pkg}: worktree {wv} != DEV {bv}")
    detail = "; ".join(bits) or "bun.lock differs from DEV's pin"
    return ("bad", "lockfile drift — " + detail + " (frozen re-pin will restore on next build)")


def _git_show(repo_path: str, ref_path: str) -> str | None:
    """``git show <ref>:<path>`` from a repo's object store, or None if absent."""
    try:
        r = subprocess.run(["git", "show", ref_path],
                           cwd=os.path.expanduser(repo_path), capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def worktree_drift_check(cfg, app) -> tuple[str, str] | None:
    """EU-18 doctor assertion: surface whether the (possibly reused) worktree's bun.lock differs from
    DEV's pin. This is INFORMATIONAL, never blocking: ``loop._repin_worktree_deps`` hard-restores
    ``bun.lock`` from ``origin/<base>`` before EVERY build, so any pre-build drift self-heals and never
    ships. Emitting 'bad' here DEADLOCKED the cockpit run gate (server.py) after any lock-changing land —
    DEV moves forward, the reused worktree is left behind, the whole-lock comparison flags drift, and the
    gate blocks the very build that would re-pin the tree (found live 2026-07-08 on AUTO-57's coverage
    land, and it recurs on any DEV dep bump — including the pinned pkg). So a detected drift is reported
    as 'warn', not 'bad'.

    None = not applicable (in-tree mode, or the app isn't a Bun project at DEV) so the caller emits no
    check line. Otherwise ``(status, detail)`` — 'ok' when in sync, 'warn' when it differs or can't tell."""
    if not getattr(cfg, "use_worktree", False):
        return None
    from . import loop  # lazy: avoids importing the SDK-heavy loop at module load
    base_ref = f"origin/{app.base_branch}"
    base_lock = _git_show(app.repo_path, f"{base_ref}:bun.lock")
    if base_lock is None:
        return None  # no lockfile at DEV (not a Bun app, or origin not fetched) -> skip silently
    wt_lock_path = Path(loop._worktree_path(app, cfg)) / "bun.lock"
    wt_lock = wt_lock_path.read_text() if wt_lock_path.exists() else None
    st, det = lockfile_drift(wt_lock, base_lock)
    if st == "bad":                                    # real drift, but _repin_worktree_deps heals it —
        return ("warn", det + " — informational; re-pinned before the next build, does not block runs")
    return (st, det)


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

    # EU-2 F7: the hard tool-call guard rides on SDK hooks. If hooks_config() is None it silently isn't
    # installed while bypassPermissions stays on (fail-open). Surface it — warn (not bad) so an SDK bump
    # flags the gap without blocking builds.
    from . import guard
    guard_ok = guard.is_installed()
    add("Tool-call guard", "ok" if guard_ok else "warn",
        "PreToolUse denylist installed" if guard_ok
        else "NOT installed — bypassPermissions has no code-level guardrail (SDK hooks unavailable)")
    add("git", "ok" if _has("git") else "bad", "" if _has("git") else "not found on PATH")
    add("gh CLI", "ok" if _has("gh") else "warn",
        "present" if _has("gh") else "missing — PRs will be skipped")

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
        # EU-54: confirm the gate's interpreter can import its declared deps (the EU self-build's most
        # fragile point — a bare python3 gate without the project venv dies on `import requests`).
        if getattr(app, "gate_preflight", None):
            from . import gate
            pf = gate.preflight_imports(app)
            if pf is None:
                add(f"{tag} · gate deps", "ok", "interpreter resolves " + ", ".join(app.gate_preflight))
            else:
                add(f"{tag} · gate deps", "bad", pf.report.splitlines()[0])
        if getattr(cfg, "use_worktree", False):
            ref_ok = _git_ref(app.repo_path, f"origin/{app.base_branch}")
            add(f"{tag} · worktree", "ok" if ref_ok else "warn",
                f"origin/{app.base_branch} resolves" if ref_ok
                else f"origin/{app.base_branch} missing — falls back in-tree")
            drift = worktree_drift_check(cfg, app)
            if drift:
                add(f"{tag} · dep pin", drift[0], drift[1])
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
