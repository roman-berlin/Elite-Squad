"""Sentinel — S-3 · Integration & rollback.

The unit's last line of defence on DEV. The pre-review gate already validates the *exact* merge on a
throwaway branch before anything touches DEV, so Sentinel is for the checks that only make sense AFTER
the code is actually on DEV — a heavier integration / e2e suite too slow to run on every build pass.
If that suite goes red, Sentinel **reverts the merge** (forward-only `git revert`, no force-push, no
history rewrite) and hands the ticket back, so DEV is never left broken.

Opt-in: `sentinel_enabled: true` plus a per-app `postmerge_commands:` list. With no post-merge suite
configured it does nothing — the gate already covered the per-pass checks.
"""
from __future__ import annotations

from . import gate, notify
from .config import AppConfig, Config


def should_run(cfg: Config, app: AppConfig) -> bool:
    """Only when armed AND the app has a post-merge suite distinct from the per-pass gate."""
    return bool(getattr(cfg, "sentinel_enabled", False)) and bool(getattr(app, "postmerge_commands", None))


def guard(cfg: Config, app: AppConfig, ticket, git, merge_sha: str, audit=None) -> tuple[bool, str]:
    """Run the post-merge suite on the landed DEV. Green → (True, note). Red → revert the merge and
    return (False, reason). Never raises into the loop — a Sentinel hiccup must not corrupt a run."""
    tid = getattr(ticket, "id", "?")
    cmds = list(getattr(app, "postmerge_commands", []) or [])
    print(f"  🛡️ Sentinel · post-merge suite on {app.base_branch}…", flush=True)
    result = None
    try:
        result = gate.run_commands(app, cmds)
        report = result.report
    except Exception as exc:  # noqa: BLE001 — treat a runner failure as a red, and roll back
        report = f"sentinel suite could not run: {exc}"

    if result is not None and result.passed:
        print(f"  🛡️ Sentinel · {app.base_branch} green after merge ✓", flush=True)
        if audit is not None:
            audit.record("sentinel_pass", ticket_id=tid, app=app.name)
        return True, "post-merge suite green"

    print(f"  🛡️ Sentinel · post-merge RED → reverting {tid} on {app.base_branch}", flush=True)
    reverted = False
    try:
        reverted = git.revert_merge_on_base(merge_sha)
    except Exception as exc:  # noqa: BLE001
        report = (report or "") + f"\n(revert error: {exc})"
    if audit is not None:
        audit.record("sentinel_revert", ticket_id=tid, app=app.name, reverted=reverted)
    tail = (report or "").strip()[-1200:]
    if reverted:
        notify.send(f"🛡️ Sentinel reverted {tid} — post-merge suite failed on {app.base_branch}; "
                    f"DEV rolled back, ticket handed back.\n\n{tail}")
        return False, f"post-merge suite failed → merge reverted; DEV restored.\n{tail}"
    notify.send(f"⛔ Sentinel: {tid} post-merge suite failed on {app.base_branch} AND the auto-revert "
                f"did not apply cleanly — DEV needs you.\n\n{tail}")
    return False, f"post-merge suite failed AND revert failed — manual rollback needed.\n{tail}"
