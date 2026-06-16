"""CLI entrypoint — the General you command from the terminal.

The General directs two officers (the Builder and the Reviewer) across your unit
of apps, and lands passing work on dev. You stay in command of dev -> main.

  general doctor                              # one-time preflight: config, keys, repos
  general task automatixy "Fix missing scrollbar on the dashboard" \
      --ac "Scrollbar shows on overflow" --ac "No regression on resize"
  general ticket automatixy AUTO-123 AUTO-130 # work specific Jira tickets
  general drain automatixy                    # drain everything labelled autodev
  general drain                               # drain every backlogged app

Add --live to actually push/merge/update Jira (default is a safe dry-run).
"general" is the wrapper script; equivalently: python -m orchestrator.main <args>
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from . import intake
from .audit import AuditLog
from .config import Config
from .contracts import Outcome


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="general",
        description="The General — commands the Builder and Reviewer officers across your apps")
    p.add_argument("--config", default="config.yaml", help="path to config.yaml (default: ./config.yaml)")
    p.add_argument("--live", action="store_true", help="disable dry-run: push, merge to dev, write to Jira")
    p.add_argument("--max-tickets", type=int, default=None, help="override max_tickets_per_run")
    p.add_argument("--max-iterations", type=int, default=None, help="override max_iterations")
    p.add_argument("--effort", choices=["low", "medium", "high", "max"], default=None,
                   help="override the Builder's effort for this run (low|medium|high|max)")
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("task", help="work a free-text bug/feature (no Jira needed)")
    t.add_argument("app")
    t.add_argument("description", nargs="*", help="what to build, in words (or use --spec-file)")
    t.add_argument("--spec-file", help="path to a markdown/txt spec to use as the description")
    t.add_argument("--title", help="short title when using --spec-file (else the first line is used)")
    t.add_argument("--ac", action="append", default=[], help="an acceptance criterion (repeatable)")

    k = sub.add_parser("ticket", help="work one or more Jira tickets by key")
    k.add_argument("app")
    k.add_argument("keys", nargs="+", help="Jira ticket key(s), e.g. AUTO-123")
    k.add_argument("--spec-file", help="use this markdown/txt file as the build brief instead of the Jira "
                                       "description (keeps the ticket's identity, so status moves still fire)")
    k.add_argument("--title", help="override the summary used for the branch/commit (Jira summary stays as-is)")

    d = sub.add_parser("drain", help="pull ready tickets from the backlog")
    d.add_argument("app", nargs="?", default=None, help="app name; omit to drain every backlogged app")

    sub.add_parser("doctor", help="check config, keys, repos and tooling")
    sub.add_parser("ping", help="send a test Telegram message")
    dash = sub.add_parser("dashboard", help="generate dashboard.html from the audit log")
    dash.add_argument("--open", action="store_true", help="open it in your browser after generating")
    sub.add_parser("status", help="print a quick task table in the terminal")
    srv = sub.add_parser("serve", help="run the control panel web app (your cockpit)")
    srv.add_argument("--port", type=int, default=8787, help="port (default 8787)")
    st = sub.add_parser("standup", help="daily-meeting report (shipped / needs-you / decisions)")
    st.add_argument("--telegram", action="store_true", help="also send it to Telegram")
    dr = sub.add_parser("drill", help="Drillmaster: review the unit's record, propose officer upgrades")
    dr.add_argument("--telegram", action="store_true", help="also send a summary to Telegram")
    dr.add_argument("--apply", action="store_true", help="EXECUTE the approved drill (writes the officer/squad edits; originals backed up first)")
    cnl = sub.add_parser("council", help="hold the Elite Unit's daily council (officers muster, brief you)")
    cnl.add_argument("--topic", help="run an ad-hoc improvement muster focused on this topic")
    adj = sub.add_parser("adjutant", help="Adjutant (S-1): personnel review — propose hires/retirements")
    adj.add_argument("--telegram", action="store_true", help="also brief the Commander on Telegram")
    adj.add_argument("--apply", action="store_true", help="EXECUTE the approved personnel action (hire/retire; originals backed up first)")
    sct = sub.add_parser("scout", help="Scout (S-2): smoke-test DEV in a browser (e2e / a11y) and report")
    sct.add_argument("app")
    sct.add_argument("--url", help="a deployed DEV URL to test (else the app's local dev server)")
    sct.add_argument("--telegram", action="store_true", help="also send the recon report to Telegram")
    return p


def _apply_overrides(cfg: Config, args) -> None:
    if args.live:
        cfg.dry_run = False
    if args.max_tickets is not None:
        cfg.max_tickets_per_run = args.max_tickets
    if args.max_iterations is not None:
        cfg.max_iterations = args.max_iterations
    if getattr(args, "effort", None):
        cfg.builder_effort = args.effort


async def _run_work(cfg: Config, worklist) -> int:
    if not worklist:
        print("Nothing to do (empty worklist).")
        return 0
    auth = cfg.detected_auth()
    print(f"auth: {auth or 'using Claude Code login session (run `claude` to verify)'}")
    audit = AuditLog(cfg.audit_path)
    mode = "DRY-RUN" if cfg.dry_run else "LIVE"
    cap = (f"${cfg.max_cost_usd:.0f} cost cap" if cfg.max_cost_usd and cfg.max_cost_usd > 0
           else "no $ cap (subscription)")
    print(f"★ GENERAL [{mode}] — {len(worklist)} ticket(s) · {cap} · "
          f"up to {cfg.max_iterations} passes/ticket")
    audit.record("run_start", mode=mode, tickets=len(worklist))

    from .loop import run as run_loop
    reports = await run_loop(cfg, worklist, audit)

    charged = bool(os.environ.get("ANTHROPIC_API_KEY"))
    print("\n=== Run summary ===")
    total = 0.0
    for r in reports:
        total += r.cost_usd
        line = f"{(r.app or '-'):<12} {r.ticket_id:<26} {r.outcome.value:<14} passes={r.iterations}"
        if charged:
            line += f" ${r.cost_usd:.2f}"
        if r.pr_url:
            line += f"  {r.pr_url}"
        if r.notes:
            line += f"  ({r.notes})"
        print(line)
    if charged:
        print(f"total cost: ${total:.2f}")
    else:
        print("billing: Max plan — counts against your subscription's Agent-SDK usage, no $ charge.")
    if cfg.dry_run:
        print("\nThis was a DRY-RUN — no branches pushed, no merges, no Jira writes. "
              "Re-run with --live when ready.")
    audit.record("run_end", tickets=len(reports), total_cost_usd=total)
    return 1 if any(r.outcome == Outcome.ERRORED for r in reports) else 0


def _doctor(cfg_path: str) -> int:
    ok, warn = "  ✓", "  ⚠"
    bad = "  ✗"
    problems = 0
    print(f"Config: {cfg_path}")
    try:
        cfg = Config.load(cfg_path)
    except Exception as exc:  # noqa: BLE001
        print(f"{bad} could not load config: {exc}")
        return 1
    print(f"{ok} config loaded ({len(cfg.apps)} app(s): {', '.join(a.name for a in cfg.apps)})")

    print("Secrets / tooling:")
    auth = cfg.detected_auth()
    if auth:
        print(f"{ok} auth: {auth}")
    else:
        print(f"{warn} no auth detected (no env var, no `claude` login found) — run `claude` then /login "
              f"(or `claude setup-token` for headless). Set ANTHROPIC_API_KEY only for per-token API billing.")
    try:
        import claude_agent_sdk  # noqa: F401
        print(f"{ok} claude-agent-sdk importable")
    except Exception:  # noqa: BLE001
        print(f"{bad} claude-agent-sdk not installed (pip install -r requirements.txt)"); problems += 1
    if _which("git"):
        print(f"{ok} git present")
    else:
        print(f"{bad} git not found"); problems += 1
    print(f"{ok if _which('gh') else warn} gh CLI "
          f"{'present' if _which('gh') else 'missing (PRs will be skipped)'}")
    from . import notify
    print(f"{ok if notify.configured() else warn} Telegram "
          f"{'configured' if notify.configured() else 'not configured (status alerts off) — see .env.example'}")

    for app in cfg.apps:
        print(f"App '{app.name}':")
        if not os.path.isdir(os.path.join(os.path.expanduser(app.repo_path), ".git")):
            print(f"{bad} repo_path is not a git repo: {app.repo_path}"); problems += 1
            continue
        print(f"{ok} repo at {app.repo_path}")
        for br, required in ((app.base_branch, True), (app.protected_branch, False)):
            if _branch_exists(app.repo_path, br):
                print(f"{ok} branch '{br}' exists")
            elif required:
                print(f"{bad} base branch '{br}' missing"); problems += 1
            else:
                print(f"{warn} protected branch '{br}' not found locally")
        print(f"{ok if app.gate_commands else warn} gate_commands "
              f"{app.gate_commands if app.gate_commands else 'empty (no tests will run!)'}")
        if getattr(cfg, "use_worktree", False):
            import subprocess as _sp
            ref_ok = _sp.run(["git", "rev-parse", "--verify", "--quiet", f"origin/{app.base_branch}"],
                             cwd=os.path.expanduser(app.repo_path),
                             capture_output=True, text=True).returncode == 0
            if ref_ok:
                print(f"{ok} worktree isolation ready (origin/{app.base_branch} resolves)")
            else:
                print(f"{warn} worktree isolation will fall back to in-tree "
                      f"(origin/{app.base_branch} not found — push '{app.base_branch}' or add an origin remote)")
        if app.backlog_backend == "jira":
            have = os.environ.get("JIRA_EMAIL") and os.environ.get("JIRA_API_TOKEN")
            print(f"{ok if have else bad} Jira creds (JIRA_EMAIL/JIRA_API_TOKEN) "
                  f"{'set' if have else 'missing'}")
            if not have:
                problems += 1

    print("\n" + ("All good." if problems == 0 else f"{problems} problem(s) to fix."))
    return 0 if problems == 0 else 1


def _which(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None


def _branch_exists(repo: str, branch: str) -> bool:
    proc = subprocess.run(["git", "-C", os.path.expanduser(repo), "rev-parse", "--verify", branch],
                          capture_output=True, text=True)
    return proc.returncode == 0


async def _main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return _doctor(args.config)
    if args.command == "ping":
        from . import notify
        if not notify.configured():
            print("Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, then `source .env`.")
            return 1
        ok = notify.send("✅ General test ping — Telegram is wired up.")
        print("sent ✓ (check your Telegram)" if ok else "failed — double-check token / chat id.")
        return 0 if ok else 1

    cfg = Config.load(args.config)
    _apply_overrides(cfg, args)

    if args.command == "serve":
        from . import server
        server.serve(cfg, port=args.port)
        return 0

    if args.command == "standup":
        from . import dashboard as D, notify
        report = D.standup(cfg)
        print(report)
        if getattr(args, "telegram", False):
            notify.send(report)
        return 0

    if args.command == "drill":
        from . import drillmaster, notify
        if getattr(args, "apply", False):
            out = await drillmaster.apply(cfg)
            print(out)
            if getattr(args, "telegram", False):
                notify.send("🎖️ Drill applied:\n\n" + out[:1500])
            return 0
        report = await drillmaster.drill(cfg)
        print(report)
        out = Path(cfg.audit_path).with_name("drill-report.md")
        out.write_text(report, encoding="utf-8")
        print(f"\n(written to {out})")
        if getattr(args, "telegram", False):
            notify.send("🎖️ Drillmaster report ready:\n\n" + report[:1500])
        return 0

    if args.command == "council":
        from . import council
        from .audit import AuditLog
        briefing = await council.hold_council(cfg, topic=getattr(args, "topic", None),
                                              audit=AuditLog(cfg.audit_path))
        print("\n" + briefing)
        return 0

    if args.command == "scout":
        from . import scout, notify
        report = await scout.recon(cfg, args.app, url=getattr(args, "url", None))
        print(report)
        Path(cfg.audit_path).with_name("scout-report.md").write_text(report, encoding="utf-8")
        if getattr(args, "telegram", False):
            notify.send("🛰️ Scout — DEV recon:\n\n" + report[:3000])
        return 0

    if args.command == "adjutant":
        from . import adjutant, notify
        if getattr(args, "apply", False):
            out = await adjutant.apply(cfg)
            print(out)
            if getattr(args, "telegram", False):
                notify.send("🪖 Personnel action applied:\n\n" + out[:1500])
            return 0
        report = await adjutant.propose(cfg)
        print(report)
        Path(cfg.audit_path).with_name("adjutant-report.md").write_text(report, encoding="utf-8")
        if getattr(args, "telegram", False):
            notify.send("🪖 Adjutant — personnel review:\n\n" + report[:3000])
        return 0

    if args.command in ("dashboard", "status"):
        from . import dashboard as D
        charged = bool(os.environ.get("ANTHROPIC_API_KEY"))
        tasks = D.load_tasks(cfg.audit_path)
        if args.command == "status":
            print(D.render_status(tasks, show_cost=charged))
            return 0
        out = Path(cfg.audit_path).with_name("dashboard.html")
        out.write_text(D.render_html(tasks, show_cost=charged), encoding="utf-8")
        print(f"dashboard written: {out}  ({len(tasks)} task(s))")
        if getattr(args, "open", False):
            subprocess.run(["open", str(out)], check=False)
        return 0

    if args.command == "task":
        if args.spec_file:
            spec = Path(args.spec_file).expanduser().read_text(encoding="utf-8")
            title = " ".join(args.description) or args.title or spec.strip().splitlines()[0][:80]
            worklist = intake.from_text(cfg, args.app, title, args.ac, description=spec)
        elif args.description:
            worklist = intake.from_text(cfg, args.app, " ".join(args.description), args.ac)
        else:
            print("Provide a description or --spec-file."); return 2
    elif args.command == "ticket":
        spec = None
        if getattr(args, "spec_file", None):
            spec = Path(args.spec_file).expanduser().read_text(encoding="utf-8")
        worklist = intake.from_tickets(cfg, args.app, args.keys, spec=spec,
                                       title=getattr(args, "title", None))
    elif args.command == "drain":
        worklist = intake.from_drain(cfg, args.app, cfg.max_tickets_per_run)
    else:  # pragma: no cover
        raise SystemExit(f"unknown command {args.command}")

    return await _run_work(cfg, worklist)


def main() -> None:
    try:
        sys.exit(asyncio.run(_main(sys.argv[1:])))
    except KeyboardInterrupt:
        print("\n✗ Interrupted — stopped. Nothing was pushed or merged.", flush=True)
        sys.exit(130)


if __name__ == "__main__":
    main()
