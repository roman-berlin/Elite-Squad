"""Verification gate — runs an app's own tests/lint/typecheck.

Used twice: on the feature branch (before review) and again on dev after a merge
(to keep dev green). Cheap filter: never spend a review cycle on something the
test suite would catch.
"""
from __future__ import annotations

import os
import subprocess
import sys

from .config import AppConfig
from .contracts import GateResult


def run_commands(app: AppConfig, commands: list[str], cwd: str | None = None) -> GateResult:
    """Run a list of shell commands in the app's worktree; fail on the first non-zero exit.
    Shared by the pre-review gate and the post-merge SRE."""
    if not commands:
        return GateResult(passed=True, report="(no commands configured)")
    where = cwd or app.workdir or app.repo_path
    failures: list[str] = []
    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=where,
                capture_output=True, text=True, timeout=app.gate_timeout_sec,
                env={**os.environ, **app.gate_env},   # e.g. cap node heap / vitest workers
            )
        except subprocess.TimeoutExpired:
            failures.append(f"$ {cmd}\n(timed out after {app.gate_timeout_sec}s)")
            continue
        if proc.returncode != 0:
            tail = (proc.stdout + "\n" + proc.stderr).strip()[-4000:]
            failures.append(f"$ {cmd}\n(exit {proc.returncode})\n{tail}")
    if failures:
        return GateResult(passed=False, report="\n\n".join(failures))
    return GateResult(passed=True, report="all commands passed")


def gate_interpreter(commands: list[str] | None) -> str | None:
    """The python interpreter a gate will use: the leading token of the first command that
    points at a python executable (e.g. an absolute `.venv/bin/python`). None when the gate
    isn't python-invoked (e.g. a Bun `tsc` typecheck). (EU-54)"""
    for cmd in commands or []:
        toks = (cmd or "").strip().split()
        if toks and os.path.basename(toks[0]).startswith("python"):
            return toks[0]
    return None


def preflight_imports(app: AppConfig, commands: list[str] | None = None) -> GateResult | None:
    """EU-54 health check. Before running the suite, confirm the gate's python interpreter can
    import every module in ``app.gate_preflight``. Returns a FAILED GateResult (with an actionable
    venv hint) when one is missing; None when there's nothing to check or all imports resolve.

    Guards the unit's most fragile path — an EU self-build whose bare ``python3`` gate inherits a
    PATH without the project virtualenv and dies on ``import requests``, turning the gate red for a
    reason unrelated to the ticket and burning retries until it parks."""
    mods = list(getattr(app, "gate_preflight", None) or [])
    if not mods:
        return None
    interp = gate_interpreter(commands if commands is not None else app.gate_commands) or sys.executable
    try:
        proc = subprocess.run(
            [interp, "-c", "import " + ", ".join(mods)],
            capture_output=True, text=True, timeout=min(app.gate_timeout_sec, 120),
            env={**os.environ, **app.gate_env},
        )
    except Exception as exc:  # noqa: BLE001 - a broken/missing interpreter IS the finding
        return GateResult(passed=False, report=f"gate health check: cannot run interpreter '{interp}': {exc}")
    if proc.returncode != 0:
        miss = (proc.stderr.strip().splitlines() or ["import failed"])[-1]
        return GateResult(passed=False, report=(
            f"gate health check FAILED — interpreter '{interp}' cannot import required modules "
            f"({', '.join(mods)}).\n{miss}\n"
            "The gate is not running under the project virtualenv. Pin gate_commands to the venv "
            "interpreter (an absolute .venv/bin/python path) or launch autopilot with the venv on PATH."))
    return None


def touched_components(changed_paths: list[str]) -> list[str]:
    """Component (app/package) directory names a diff touches, in first-seen order.
    A path like 'apps/landing-page/src/x.ts' -> 'landing-page'; 'packages/ui/y.ts' -> 'ui'.
    Paths outside apps/ and packages/ are ignored. (EU-19)"""
    seen: list[str] = []
    for p in changed_paths:
        parts = p.replace("\\", "/").split("/")
        if len(parts) >= 2 and parts[0] in ("apps", "packages") and parts[1]:
            if parts[1] not in seen:
                seen.append(parts[1])
    return seen


def select_gate_groups(app: AppConfig, changed_paths: list[str]) -> list[tuple[str, list[str]]]:
    """Pick the per-app gate command groups for the components a diff touched.

    Returns a list of (component_name, commands). An empty list means detection was
    AMBIGUOUS — the caller falls back to the repo-wide `gate_commands` default. (EU-19)

      • No per-app config, or no changed paths under apps/ or packages/  -> [] (fall back).
      • A changed package listed in gate_shared_packages also pulls in the apps that
        depend on it, so a shared dependency re-gates its consumers.
      • Only touched components that HAVE a per-app entry contribute a group; if none of
        the touched components are configured -> [] (fall back, don't silently skip).
    """
    by_app = app.gate_commands_by_app or {}
    if not by_app or not changed_paths:
        return []
    touched = touched_components(changed_paths)
    if not touched:
        return []
    # Expand shared packages to the apps that depend on them.
    shared = app.gate_shared_packages or {}
    selected: list[str] = list(touched)
    for name in touched:
        for dep in shared.get(name, []):
            if dep not in selected:
                selected.append(dep)
    groups = [(name, by_app[name]) for name in selected if by_app.get(name)]
    return groups


def run_gate(app: AppConfig, changed_paths: list[str] | None = None) -> GateResult:
    """Run the verification gate. When per-app gate commands are configured and the diff's
    changed paths map to one or more configured components, run ONLY those components' gates
    (naming each in the failure report). Otherwise fall back to the repo-wide `gate_commands`
    (the original single-command behaviour). (EU-19)"""
    # EU-54: fail fast and clearly if the gate interpreter can't even import its deps, before we
    # spend the whole suite producing a confusing mid-run ModuleNotFoundError.
    pf = preflight_imports(app)
    if pf is not None:
        return pf
    groups = select_gate_groups(app, changed_paths or [])
    if not groups:
        if not app.gate_commands:
            return GateResult(passed=True, report="(no gate commands configured)")
        return run_commands(app, app.gate_commands)

    failures: list[str] = []
    passed_apps: list[str] = []
    for name, commands in groups:
        res = run_commands(app, commands)
        if res.passed:
            passed_apps.append(name)
        else:
            # Name the app that failed so the audit report points at the right component.
            failures.append(f"[{name}] gate FAILED\n{res.report}")
    if failures:
        return GateResult(passed=False, report="\n\n".join(failures))
    return GateResult(passed=True, report="app gates passed: " + ", ".join(passed_apps))
