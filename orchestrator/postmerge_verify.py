"""EU-452 — Post-merge dev-HEAD re-verification runner (ENGINE ONLY).

The post-land counterpart to the PRE-land gate pair: ``base_gate_check`` proves the clean base tree
is green BEFORE a build, and ``run_gate`` proves the feature branch is green BEFORE review. This module
closes the loop on the OTHER end — after a merge actually lands on ``<base>`` (DEV), it re-runs the
app's own ``gate_commands`` against the ACTUAL merged tip and returns a green/red verdict. The premise
is the same as the base gate's (EU-174): a green worktree feature that turns DEV red is paid for in
tokens, so proving the merged tip — not just the worktree — catches the merge itself as a failure
source (a semantic-conflict-laced merge, a base that moved mid-land) before the next ticket bills a
max-effort build against a broken DEV.

It is the ENGINE only — it runs the suite and returns ``(bool, str)``. It does NOT wire into the loop,
does NOT revert a red merge (that is the SRE ``sentinel.py``'s job, which carries its own heavier
post-merge suite), and does NOT touch the base-gate cache (``red_base_cache.json`` / ``base_gate_check``).
Wiring, revert and cache ownership live in sibling tickets; this file stays a pure, best-effort
verdict function.

Best-effort / never-raises contract: a verify hiccup must NEVER corrupt a land that already succeeded,
so the function is stricter-toward-green than the SRE — a runner exception or a wrong-tree state yields
a green SKIP, never a red and never a raised exception into the loop (the same contract as
``sentinel.guard`` and ``smoke.run``, but with no revert responsibility so it leans green on any doubt).

Opt-in: ``Config.postmerge_verify: true`` (master switch, OFF until armed) AND a per-app
``AppConfig.postmerge_verify: true`` AND a non-empty ``gate_commands``. With any of those three off it
does nothing — ``should_run`` is False — so the framework costs nothing until an app opts in both ways.
"""
from __future__ import annotations

import os
import shutil
import tempfile

from . import gate
from .config import AppConfig, Config


def should_run(cfg: Config, app: AppConfig) -> bool:
    """Opt-in guard for the post-merge dev-HEAD re-verification — True only when armed at BOTH levels
    AND the app has a suite to run. Unlike the PRE-land ``base_gate_check`` / ``run_gate`` (which run
    unconditionally once the master switch is on, because a pre-merge gate is cheap insurance), this
    POST-merge re-verification runs the app's own full suite AGAIN on the merged tip, so it is gated
    hard: it never fires for an app that hasn't opted in both ways.

    Three factors, all required: the unit-wide ``Config.postmerge_verify`` master switch, the per-app
    ``AppConfig.postmerge_verify`` flag, and a non-empty ``gate_commands``. The master+per-app pair
    mirrors ``sentinel_enabled``+``postmerge_commands`` and ``smoke_enabled``+``smoke_command``: the
    framework can be armed fleet-wide without forcing every app in, and an app can opt in without
    arming the whole unit. The third factor (gate_commands) keeps the per-app flag from being dead
    code — an app with the flag on but no suite configured is still inert rather than erroring at
    run time. False for every app that hasn't opted in both ways, so the engine costs nothing until armed.
    """
    return (bool(getattr(cfg, "postmerge_verify", False))
            and bool(getattr(app, "postmerge_verify", False))
            and bool(getattr(app, "gate_commands", None)))


def verify(cfg: Config, app: AppConfig, git, merge_sha: str, audit=None) -> tuple[bool, str]:
    """Re-run the app's own ``gate_commands`` against the merged dev HEAD; return a green/red verdict.

    The post-merge counterpart to the pre-land ``base_gate_check`` (clean base) and ``run_gate``
    (worktree): where those prove the tree BEFORE it lands, this proves the ACTUAL merged tip AFTER.
    Green -> ``(True, "post-merge dev-HEAD verify green")``; red -> ``(False, evidence)`` where evidence
    is ``gate.extract_failure_evidence(report)`` so the flag names the failing harness, not green-suite
    noise. NEVER raises — a verify hiccup must not corrupt a land that already succeeded (no revert
    responsibility, so on any doubt it leans green-skip, not red).

    Sequence, in order:

    1. WRONG-TREE GUARD (first, before any suite run). Confirm the checked-out repo IS at the merged
       tip — ``git.current_sha() == merge_sha``. If it is not, return
       ``(True, "dev HEAD ≠ merge_sha; skipped")`` and record ``postmerge_verify_skip`` WITHOUT running
       the suite. This is the guard against the unit's #1 recurring escalation class — the
       false-red-base / wrong-tree dead end (EU-157 / EU-355): running the app's own gate against a tree
       that is not the merge tip produces a red that no rebuild can ever fix, parking the ticket and the
       drain on a verdict about the wrong commit. ``current_sha`` safe-fails to ``''`` (git_ops.py), so
       a missing/blank sha also yields the green-skip — we only ever run the suite when we can PROVE the
       tree is the merged tip.

    2. ISOLATION ENV (EU-355). The app's gate for this repo IS ``tests/run_all.py`` under the pinned
       venv interpreter (``gate_commands``), and run_all writes fabricated ticket transitions to its
       audit ledger and clobbers the live autopilot PID file unless ``GENERAL_AUDIT_PATH`` /
       ``GENERAL_PID_FILE`` point elsewhere. So before the subprocess runs, both are redirected to a
       fresh temp dir, and restored (or popped) in a ``finally``. The venv interpreter is already pinned
       INSIDE ``gate_commands`` itself, so this module never invents a bare ``python3`` (which dies on
       ``import requests`` outside the venv). ``gate.run_commands`` builds the child env from
       ``os.environ`` via ``gate._subprocess_env``, so setting these keys here reaches the subprocess.

    3. VERDICT MAP. Green records ``postmerge_verify_green``; a runner exception records
       ``postmerge_verify_skip`` and returns a green-skip; red records ``postmerge_verify_red`` and
       returns the extracted failure evidence. ``audit`` is optional — when ``None`` no events are
       recorded, mirroring ``sentinel.guard`` / ``smoke.run``.
    """
    # (1) WRONG-TREE GUARD — never run the suite against a tree that is not the merged tip.
    try:
        current = str(getattr(git, "current_sha", lambda: "")()).strip()
    except Exception:  # noqa: BLE001 — a git hiccup must never raise into the loop
        current = ""
    if current != str(merge_sha or "").strip():
        if audit is not None:
            audit.record("postmerge_verify_skip", app=app.name, reason="dev HEAD != merge_sha")
        return True, "dev HEAD ≠ merge_sha; skipped"

    # (2) Run the app's own gate suite under the EU-355 isolation env so run_all.py's fabricated
    # ticket transitions + the live PID file land in a throwaway temp dir, never the live state dir.
    result = None
    tmp = None
    prev_audit_path = os.environ.get("GENERAL_AUDIT_PATH")
    prev_pid_file = os.environ.get("GENERAL_PID_FILE")
    try:
        tmp = tempfile.mkdtemp(prefix="postmerge_verify_")
        os.environ["GENERAL_AUDIT_PATH"] = os.path.join(tmp, "audit.jsonl")
        os.environ["GENERAL_PID_FILE"] = os.path.join(tmp, "run_all.pid")
        result = gate.run_commands(app, list(app.gate_commands or []))
    except Exception as exc:  # noqa: BLE001 — a verify hiccup must NEVER corrupt a land that succeeded
        if audit is not None:
            audit.record("postmerge_verify_skip", app=app.name, reason=f"runner error: {exc}")
        return True, f"post-merge dev-HEAD verify skipped: {exc}"
    finally:
        for _key, _prev in (("GENERAL_AUDIT_PATH", prev_audit_path),
                            ("GENERAL_PID_FILE", prev_pid_file)):
            if _prev is None:
                os.environ.pop(_key, None)
            else:
                os.environ[_key] = _prev
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)

    # (3) VERDICT MAP — name the failing harness, not green-suite noise (extract_failure_evidence).
    if result.passed:
        if audit is not None:
            audit.record("postmerge_verify_green", app=app.name)
        return True, "post-merge dev-HEAD verify green"
    evidence = gate.extract_failure_evidence(result.report or "")
    if audit is not None:
        audit.record("postmerge_verify_red", app=app.name)
    return False, evidence
