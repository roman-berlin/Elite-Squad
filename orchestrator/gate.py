"""Verification gate — runs an app's own tests/lint/typecheck.

Used twice: on the feature branch (before review) and again on dev after a merge
(to keep dev green). Cheap filter: never spend a review cycle on something the
test suite would catch.
"""
from __future__ import annotations

import os
import subprocess

from .config import AppConfig
from .contracts import GateResult


def run_commands(app: AppConfig, commands: list[str], cwd: str | None = None) -> GateResult:
    """Run a list of shell commands in the app's worktree; fail on the first non-zero exit.
    Shared by the pre-review gate and the post-merge Sentinel."""
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


def run_gate(app: AppConfig) -> GateResult:
    if not app.gate_commands:
        return GateResult(passed=True, report="(no gate commands configured)")
    return run_commands(app, app.gate_commands)
