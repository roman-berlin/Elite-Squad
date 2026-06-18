"""Scheduled patrols — the recon officers sweep DEV on a cadence and file findings unprompted.

Scout (runtime / UX / a11y), Provost Marshal (security), and the Quartermaster (deploy-readiness)
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
    ("scout", "Scout", "scout-report.md"),
    ("provost", "Provost Marshal", "provost-report.md"),
    ("quartermaster", "Quartermaster", "quartermaster-report.md"),
]


async def _inspect(key: str, cfg: Config, app_name: str) -> str:
    if key == "scout":
        from . import scout
        return await scout.recon(cfg, app_name)
    if key == "provost":
        from . import provost
        return await provost.inspect(cfg, app_name)
    from . import quartermaster
    return await quartermaster.inspect(cfg, app_name)


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

    lines, total = [], 0
    for key, label, fname in roster:
        try:
            report = await _inspect(key, cfg, app_name)
            proposals, _ = filing.parse_tickets(report)
            clean, _block = filing.present(report, app, key, do_file)
            Path(cfg.audit_path).with_name(fname).write_text(clean, encoding="utf-8")
            n = len(proposals)
            total += n
            verb = "filed" if do_file else "proposed"
            lines.append(f"*{label}* — {_first_line(clean)}" + (f" · {verb} {n}" if n else " · clean"))
            print(f"  · {label}: {verb + ' ' + str(n) if n else 'no new findings'}", flush=True)
        except Exception as exc:  # noqa: BLE001 - one officer must never abort the patrol
            lines.append(f"*{label}* — patrol error: {str(exc)[:80]}")
            print(f"  · {label}: error — {exc}", flush=True)

    summary = f"🛰️ *Patrol — {app_name}*\n\n" + "\n".join(lines)
    notify.send(summary)
    if audit is not None:
        audit.record("patrol", app=app_name, officers=[o[0] for o in roster],
                     findings=total, filed=do_file)
    print(f"\n  patrol complete — {total} finding(s) {'filed' if do_file else 'proposed'}\n", flush=True)
    return summary
