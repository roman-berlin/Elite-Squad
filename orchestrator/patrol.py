"""Scheduled patrols — the recon officers sweep DEV on a cadence and file findings unprompted.

QA Engineer (runtime / UX / a11y), Security Engineer (security), and the Release Manager (deploy-readiness)
each inspect the app and file ticket-worthy findings as Jira tickets — de-duped, assigned to you,
To Do — so the unit continuously finds → files → (with autopilot armed) fixes, without being told.

Robust by design: each officer is isolated, so one failing never aborts the patrol; filing reuses
the existing `filing.present(..., do_file)` path (propose-only when `do_file` is False). MAIN and
the codebase are untouched — a patrol only reads DEV and raises tickets.
"""
from __future__ import annotations

from pathlib import Path

from . import filing, notify
from .config import Config

# patrol order: recon -> security -> readiness. (key, label, report-file)
PATROL_OFFICERS = [
    ("scout", "QA Engineer", "scout-report.md"),
    ("provost", "Security Engineer", "provost-report.md"),
    ("quartermaster", "Release Manager", "quartermaster-report.md"),
]


async def _inspect(key: str, cfg: Config, app_name: str, audit=None) -> str:
    if key == "scout":
        from . import scout
        return await scout.recon(cfg, app_name, audit=audit)
    if key == "provost":
        from . import provost
        return await provost.inspect(cfg, app_name, audit=audit)
    from . import quartermaster
    return await quartermaster.inspect(cfg, app_name, audit=audit)


def _first_line(text: str) -> str:
    for ln in (text or "").splitlines():
        s = ln.strip().lstrip("#*> ").strip()
        if s:
            return s[:120]
    return "(no report)"


async def patrol(cfg: Config, app_name: str, officers=None, do_file: bool = True, audit=None) -> str:
    """Run the recon officers over one app, file (or propose) their findings, and report a muster
    line each. `officers` = subset of keys (None = all three). Returns the Telegram-style summary."""
    app = cfg.app(app_name)
    roster = [o for o in PATROL_OFFICERS if (not officers or o[0] in officers)]
    print(f"\n🛰️  Patrol — {app_name}: {', '.join(o[1] for o in roster)}"
          + ("" if do_file else "  (propose-only)") + "\n", flush=True)

    lines = []
    filed_t = deduped_t = failed_t = proposed_t = 0
    failures: list[tuple[str, str, str]] = []   # (officer_label, finding_title, error)
    for key, label, fname in roster:
        try:
            report = await _inspect(key, cfg, app_name, audit)
            clean, _block, result = filing.present(report, app, key, do_file)
            Path(cfg.audit_path).with_name(fname).write_text(clean, encoding="utf-8")
            head = _first_line(clean)
            if not do_file:
                n = len(filing.parse_tickets(report)[0])
                proposed_t += n
                tail = f" · proposed {n}" if n else " · clean"
                lines.append(f"*{label}* — {head}{tail}")
                print(f"  · {label}: {'proposed ' + str(n) if n else 'no new findings'}", flush=True)
                continue
            # do_file: report the REAL outcome, not the proposal count
            filed_t += result.filed_n
            deduped_t += result.deduped_n
            failed_t += result.failed_n
            failures += [(label, t, e) for (t, e) in result.failed]
            parts = []
            if result.filed_n:
                parts.append(f"{result.filed_n} new")
            if result.deduped_n:
                parts.append(f"{result.deduped_n} already-open")
            if result.failed_n:
                parts.append(f"✗ {result.failed_n} failed")
            tail = (" · " + " · ".join(parts)) if parts else " · clean"
            lines.append(f"*{label}* — {head}{tail}")
            print(f"  · {label}: {' · '.join(parts) if parts else 'no new findings'}", flush=True)
        except Exception as exc:  # noqa: BLE001 - one officer must never abort the patrol
            lines.append(f"*{label}* — patrol error: {str(exc)[:80]}")
            print(f"  · {label}: error — {exc}", flush=True)

    summary = f"🛰️ *Patrol — {app_name}*\n\n" + "\n".join(lines)
    notify.send(summary)
    # Never bury a create error: a real filing failure gets its own loud Telegram + a Needs-you item.
    if failures:
        _escalate_failures(cfg, app_name, failures)
    if audit is not None:
        audit.record("patrol", app=app_name, officers=[o[0] for o in roster],
                     findings=filed_t + deduped_t + failed_t + proposed_t,
                     filed=filed_t, deduped=deduped_t, failed=failed_t)
    if do_file:
        tally = f"{filed_t} new · {deduped_t} already-open · {failed_t} failed"
    else:
        tally = f"{proposed_t} proposed"
    print(f"\n  patrol complete — {tally}\n", flush=True)
    return summary


def _escalate_failures(cfg: Config, app_name: str, failures: list[tuple[str, str, str]]) -> None:
    """A finding that genuinely failed to file is the unsafe direction — surface it loudly on
    Telegram AND in the Needs-you inbox, so a create error (auth / project / field) is never
    silently reported as success. Best-effort: escalation must never break the patrol."""
    detail = "\n".join(f"• {label} — {title}: {err}" for (label, title, err) in failures)
    notify.send(f"🚨 *Patrol filing FAILED — {app_name}*\n\n"
                f"{len(failures)} finding(s) could NOT be filed (auth / project / field?):\n{detail}\n\n"
                f"These were NOT created — investigate the backlog config.")
    try:
        from . import decisions
        from .contracts import Ticket
        body = ("A patrol tried to file findings on DEV but the backlog rejected them:\n\n"
                + detail + "\n\nLikely a backlog misconfiguration (auth, project key, or a "
                "required field). Verify create access, then re-run the patrol with --file.")
        tkt = Ticket(
            id=f"patrol-{app_name}-filing-failure", key=f"patrol-{app_name}-filing-failure",
            summary=f"Patrol could not file {len(failures)} finding(s) on {app_name}",
            description=body, acceptance_criteria=[], app=app_name, ephemeral=True,
        )
        decisions.add(cfg, tkt, app_name,
                      question=f"Patrol filing failed on {app_name} — backlog rejected "
                               f"{len(failures)} finding(s). Fix backlog access, then re-run.")
    except Exception as exc:  # noqa: BLE001 - Needs-you surfacing must never break the patrol
        print(f"  · could not surface filing failure to Needs-you: {exc}", flush=True)
