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


def run_gate(app: AppConfig) -> GateResult:
    if not app.gate_commands:
        return GateResult(passed=True, report="(no gate commands configured)")

    failures: list[str] = []
    for cmd in app.gate_commands:
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=(app.workdir or app.repo_path),
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
    return GateResult(passed=True, report="all gate commands passed")
