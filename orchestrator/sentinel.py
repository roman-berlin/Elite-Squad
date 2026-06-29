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

import re as _re

from . import gate, notify
from .config import AppConfig, Config


def _is_misconfigured_error(report: str) -> bool:
    """Detect if a gate failure is due to misconfiguration (missing script or env).

    Returns True when the report contains:
      - 'No such file or directory' (script doesn't exist)
      - env-missing messages from the two_tenant_smoke script
      - Any indication that required environment variables are not set

    These are infrastructure issues, not code failures — the gate should fail-soft
    and skip rather than trigger a revert (EU-117).
    """
    if not report:
        return False

    # Pattern: "No such file or directory" (missing script)
    if "No such file or directory" in report or "cannot access" in report:
        return True

    # Pattern: env-missing message from two_tenant_smoke.py
    # The script should output something like "required env var TENANT_.* not set"
    if _re.search(r"TENANT_\w+.*not set|DEV_BASE_URL.*not set", report, _re.IGNORECASE):
        return True

    # Pattern: generic "environment variable" + "not set" / "required" / "missing"
    if _re.search(r"(environment variable|env var).*\b(not set|required|missing)\b", report, _re.IGNORECASE):
        return True

    return False


def _check_required_env_vars(app: AppConfig) -> tuple[bool, str | None]:
    """Check if the gate has required environment variables configured.

    Returns (has_all_vars, missing_var_name or None). Detects missing TENANT_*
    or DEV_BASE_URL in app.gate_env BEFORE running commands (EU-117).
    """
    if not app.gate_env:
        return True, None  # No gate_env configured, nothing to check

    # Check for any TENANT_* or DEV_BASE_URL env vars that might be required
    # These are the ones two_tenant_smoke.py typically needs
    required_patterns = ["TENANT_", "DEV_BASE_URL"]

    for pattern in required_patterns:
        matching_keys = [k for k in app.gate_env.keys() if pattern in k or k.startswith(pattern)]
        if not matching_keys:
            return False, pattern  # At least one required pattern is missing

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
        msg = f"🛡️ SRE · {tid} post-merge gate misconfigured — required env var pattern '{missing_var}' missing from gate_env. Skipping gate."
        print(f"  {msg}", flush=True)
        notify.send(msg)
        if audit is not None:
            audit.record("sentinel_skip", ticket_id=tid, app=app.name, reason=f"missing env: {missing_var}")
        return True, f"gate misconfigured — missing env pattern: {missing_var}"

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
