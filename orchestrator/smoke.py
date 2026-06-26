"""Post-merge smoke runner — a fast, flag-only canary on DEV after every land.

Where the SRE (``sentinel.py``) runs a HEAVY post-merge suite and *reverts* the merge when it goes
red, the smoke runner is its light, flag-only counterpart: a single fast ``smoke_command`` (e.g. a
Playwright auth-redirect smoke) run on the freshly-landed DEV. On red it does **not** roll back —
DEV is the live integration branch and the heavier rollback decision belongs to the SRE — instead it
FLAGS the failure loudly so the Commander catches a broken DEV at QA time: a Telegram alert, an audit
``smoke_fail`` event for the cockpit, and (via the loop) a ticket comment. The two are complementary:
arm the SRE for suites that must auto-revert, the smoke runner for a quick canary that just surfaces.

Opt-in: ``smoke_enabled: true`` (armed by default) plus a per-app ``smoke_command:``. With no
``smoke_command`` configured it does nothing (see ``should_run``), so arming the framework costs
nothing until an app opts a command in (EU-60 — the automatixy auth-redirect e2e is tracked in AUTO).
"""
from __future__ import annotations

from . import gate, notify
from .config import AppConfig, Config


def should_run(cfg: Config, app: AppConfig) -> bool:
    """Only when armed AND the app has a post-merge smoke command configured. Inert (a no-op) for any
    app without a ``smoke_command``, exactly like the SRE is for an app without ``postmerge_commands``."""
    return bool(getattr(cfg, "smoke_enabled", False)) and bool(getattr(app, "smoke_command", None))


def run(cfg: Config, app: AppConfig, ticket, audit=None) -> tuple[bool, str]:
    """Run the app's post-merge smoke command on the just-landed DEV. Green → ``(True, note)``.
    Red → FLAG it (a Telegram alert + an audit ``smoke_fail`` event; the loop adds the ticket comment)
    and return ``(False, reason)`` — the merge STANDS, unlike the SRE's revert. Never raises into the
    loop: a smoke hiccup must never corrupt a land that already succeeded, so a runner that throws is
    itself treated as a red flag."""
    tid = getattr(ticket, "id", "?")
    cmd = getattr(app, "smoke_command", None)
    if not cmd:                                  # defensive — should_run already gates this
        return True, "no smoke command configured"
    print(f"  💨 smoke · post-merge smoke on {app.base_branch}…", flush=True)
    result = None
    try:
        result = gate.run_commands(app, [cmd])
        report = result.report
    except Exception as exc:  # noqa: BLE001 — a runner that can't even start IS the red flag
        report = f"smoke command could not run: {exc}"

    if result is not None and result.passed:
        print(f"  💨 smoke · {app.base_branch} green after merge ✓", flush=True)
        if audit is not None:
            audit.record("smoke_pass", ticket_id=tid, app=app.name)
        return True, "post-merge smoke green"

    tail = (report or "").strip()[-1200:]
    print(f"  🚨 smoke · post-merge smoke FAILED for {tid} on {app.base_branch} "
          f"(merge stands — flagged, not reverted)", flush=True)
    if audit is not None:
        audit.record("smoke_fail", ticket_id=tid, app=app.name)
    notify.send(f"🚨 Post-merge smoke FAILED for {tid} on {app.base_branch} — DEV is live with a "
                f"failing smoke. Needs your eyes.\n\n{tail}")
    return False, f"post-merge smoke failed on {app.base_branch}.\n{tail}"
