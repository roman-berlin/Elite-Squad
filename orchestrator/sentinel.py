"""SRE — S-3 · Integration & rollback.

The unit's last line of defence on DEV. The pre-review gate already validates the *exact* merge on a
throwaway branch before anything touches DEV, so SRE is for the checks that only make sense AFTER
the code is actually on DEV — a heavier integration / e2e suite too slow to run on every build pass.
If that suite goes red, SRE **reverts the merge** (forward-only `git revert`, no force-push, no
history rewrite) and hands the ticket back, so DEV is never left broken.

Opt-in: `sentinel_enabled: true` plus a per-app `postmerge_commands:` list. With no post-merge suite
configured it does nothing — the gate already covered the per-pass checks.
"""
from __future__ import annotations

import os
import re as _re

from . import gate, notify
from .config import AppConfig, Config

# Exit codes the SHELL itself owns: 127 = command not found, 126 = found but not executable.
# No test runner uses them (pytest exits 1-5, playwright/vitest/bun exit 1), so they are the one
# unambiguous "the suite never ran" signal — see _is_misconfigured_error.
_MISCONFIG_EXIT_CODES = (126, 127)

# gate.run_commands (gate.py:100) formats every failure block as "$ <cmd>\n(exit <code>)\n<tail>".
# Timeouts and runner explosions get a different second line on purpose, so they never read as a skip.
_EXIT_LINE = _re.compile(r"^\(exit (\d+)\)$")

# $VAR / ${VAR} references in a command. Deliberately does NOT match ${VAR:-default} (carries its own
# fallback, so it is not required), $(subshell), or positionals like $1.
_ENV_REF = _re.compile(r"\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)")


def _is_misconfigured_error(report: str) -> bool:
    """True only when EVERY failing command was rejected by the shell itself (exit 126/127) —
    i.e. the post-merge suite never ran, so there is no verdict to revert on (EU-117 fail-soft).

    Anchored to the block header, never the output body (EU-359). The original free-text match on
    "No such file or directory" / "not found" scanned the whole report, so a GENUINE product failure
    that merely printed those words was skipped instead of reverting — the 2026-07-16 audit probe
    reproduced it with "FAILED test_avatar - FileNotFoundError: [Errno 2] No such file or directory:
    /var/data/avatar.png" (exit 1), a real regression the SRE silently waved onto DEV.

    A report mixing a missing command with a genuine red stays RED: anything we cannot attribute to
    the shell means the suite spoke, and its verdict wins.
    """
    if not report:
        return False

    lines = report.splitlines()
    codes: list[int | None] = []
    for i, line in enumerate(lines):
        if not line.startswith("$ "):
            continue
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        m = _EXIT_LINE.match(nxt)
        codes.append(int(m.group(1)) if m else None)  # None = a timeout/exception block → red

    # No parsable block at all (e.g. guard's own "sentinel suite could not run: …") → red.
    return bool(codes) and all(c in _MISCONFIG_EXIT_CODES for c in codes)


def _required_env_keys(app: AppConfig) -> list[str]:
    """The env keys this app's post-merge suite actually needs, in precedence order:

      1. an explicit ``postmerge_required_env:`` list on the app, else
      2. derived from the app's own postmerge_commands ($VAR / ${VAR} references).

    (1) is read via getattr, so this works whether or not config.py carries the field yet — a suite
    that reads env internally (no $VAR on the command line) is the case that needs the explicit list.
    """
    declared = [k for k in (getattr(app, "postmerge_required_env", None) or []) if k]
    if declared:
        return declared

    keys: list[str] = []
    for cmd in (getattr(app, "postmerge_commands", None) or []):
        for braced, bare in _ENV_REF.findall(cmd or ""):
            key = braced or bare
            if key not in keys:
                keys.append(key)
    return keys


def _check_required_env_vars(app: AppConfig) -> tuple[bool, str | None]:
    """Pre-flight the env the post-merge suite needs. Returns (ok, first_missing_key or None).

    EU-359: this used to demand TENANT_* AND DEV_BASE_URL from *any* app with a non-empty gate_env
    — a shape only two_tenant_smoke.py ever had. Probed against the live config, an app whose
    gate_env is just {"NODE_OPTIONS": "--max-old-space-size=3072"} returned (False, "TENANT_"), so
    guard() answered "misconfigured — skipping" = a structural false-GREEN: the monitor was dead for
    every non-two_tenant app while reporting misconfiguration. Requirements now come from the app
    itself, and an app that declares none RUNS its suite instead of skipping it.

    Checked against the exact env the suite will get (gate._subprocess_env = os.environ minus the
    sensitive keys, plus gate_env), so this cannot disagree with the runner.
    """
    required = _required_env_keys(app)
    if not required:
        return True, None  # nothing declared or derivable → run the suite

    try:
        env = gate._subprocess_env(app)
    except Exception:  # noqa: BLE001 — never let a pre-flight probe take the loop down
        env = {**os.environ, **(getattr(app, "gate_env", None) or {})}

    for key in required:
        if not env.get(key):
            return False, key
    return True, None


def should_run(cfg: Config, app: AppConfig) -> bool:
    """Only when armed AND the app has a post-merge suite distinct from the per-pass gate."""
    return bool(getattr(cfg, "sentinel_enabled", False)) and bool(getattr(app, "postmerge_commands", None))


def guard(cfg: Config, app: AppConfig, ticket, git, merge_sha: str, audit=None) -> tuple[bool, str]:
    """Run the post-merge suite on the landed DEV. Green → (True, note). Red → revert the merge and
    return (False, reason). Never raises into the loop — a SRE hiccup must not corrupt a run."""
    tid = getattr(ticket, "id", "?")
    cmds = list(getattr(app, "postmerge_commands", []) or [])
    print(f"  🛡️ SRE · post-merge suite on {app.base_branch}…", flush=True)

    # EU-117: Check for missing required env vars BEFORE running commands
    env_ok, missing_var = _check_required_env_vars(app)
    if not env_ok:
        msg = (f"🛡️ SRE · {tid} post-merge gate misconfigured — required env var '{missing_var}' is not set "
               f"(declared in postmerge_required_env or referenced by postmerge_commands). Skipping gate.")
        print(f"  {msg}", flush=True)
        notify.send(msg)
        if audit is not None:
            audit.record("sentinel_skip", ticket_id=tid, app=app.name, reason=f"missing env: {missing_var}")
        return True, f"gate misconfigured — missing env var: {missing_var}"

    result = None
    try:
        result = gate.run_commands(app, cmds)
        report = result.report
    except Exception as exc:  # noqa: BLE001 — treat a runner failure as a red, and roll back
        report = f"sentinel suite could not run: {exc}"

    if result is not None and result.passed:
        print(f"  🛡️ SRE · {app.base_branch} green after merge ✓", flush=True)
        if audit is not None:
            audit.record("sentinel_pass", ticket_id=tid, app=app.name)
        return True, "post-merge suite green"

    # EU-117: Check if failure is due to misconfiguration (missing script/env) → fail-soft
    if _is_misconfigured_error(report or ""):
        msg = f"🛡️ SRE · {tid} post-merge gate misconfigured — missing script or env. Skipping gate. Report: {(report or '')[:200]}"
        print(f"  {msg}", flush=True)
        notify.send(f"⚠️ {msg}")
        if audit is not None:
            audit.record("sentinel_skip", ticket_id=tid, app=app.name, reason="misconfigured: missing script/env")
        return True, "gate misconfigured — skipped (missing script or env)"

    print(f"  🛡️ SRE · post-merge RED → reverting {tid} on {app.base_branch}", flush=True)
    reverted = False
    try:
        reverted = git.revert_merge_on_base(merge_sha)
    except Exception as exc:  # noqa: BLE001
        report = (report or "") + f"\n(revert error: {exc})"
    if audit is not None:
        audit.record("sentinel_revert", ticket_id=tid, app=app.name, reverted=reverted)
    tail = (report or "").strip()[-1200:]
    if reverted:
        notify.send(f"🛡️ SRE reverted {tid} — post-merge suite failed on {app.base_branch}; "
                    f"DEV rolled back, ticket handed back.\n\n{tail}")
        return False, f"post-merge suite failed → merge reverted; DEV restored.\n{tail}"
    notify.send(f"⛔ SRE: {tid} post-merge suite failed on {app.base_branch} AND the auto-revert "
                f"did not apply cleanly — DEV needs you.\n\n{tail}")
    return False, f"post-merge suite failed AND revert failed — manual rollback needed.\n{tail}"
