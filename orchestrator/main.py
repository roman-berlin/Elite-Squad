"""CLI entrypoint — the CTO you command from the terminal.

The CTO directs two officers (the Builder and the Reviewer) across your unit
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
from .config import Config, normalize_effort
from .contracts import Outcome


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="general",
        description="CTO — commands the Builder and Reviewer officers across your apps")
    p.add_argument("--config", default="config.yaml", help="path to config.yaml (default: ./config.yaml)")
    p.add_argument("--live", action="store_true", help="disable dry-run: push, merge to dev, write to Jira")
    p.add_argument("--max-tickets", type=int, default=None, help="override max_tickets_per_run")
    p.add_argument("--max-iterations", type=int, default=None, help="override max_iterations")
    p.add_argument("--effort", default=None,
                   help="override the Builder's effort for this run, bypassing auto-sizing "
                        "(low|medium|high|xhigh/ultra|max)")
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
    dr = sub.add_parser("drill", help="Engineering Coach: review the unit's record, propose officer upgrades")
    dr.add_argument("--telegram", action="store_true", help="also send a summary to Telegram")
    dr.add_argument("--apply", action="store_true", help="EXECUTE the approved drill (writes the officer/squad edits; originals backed up first)")
    cnl = sub.add_parser("council", help="hold the Elite Unit's daily council (officers muster, brief you)")
    cnl.add_argument("--topic", help="run an ad-hoc improvement muster focused on this topic")
    sub.add_parser("scribe", help="Technical Writer: fold recent council + runs into Unit Memory (memory/UNIT.md)")
    sub.add_parser("roster", help="regenerate the living roster (officers + engineers + hierarchy chart) -> ROSTER.md")
    sub.add_parser("memory", help="print the unit's living protocol (memory/UNIT.md)")
    mtg = sub.add_parser("meeting", help="convene an ad-hoc meeting on a topic (officers debate, the CTO decides)")
    mtg.add_argument("--topic", required=True, help="what the meeting is about")
    mtg.add_argument("--officers", help="comma-separated officer names/keys to attend (default: all relevant)")
    mtg.add_argument("--rounds", type=int, default=None, help="discussion rounds (default: council_rounds)")
    sub.add_parser("smalltalk", help="a corridor exchange between two officers (flavor; sometimes a real insight)")
    sub.add_parser("sync", help="exchange the audit log with the other machine (Mac<->server) so both cockpits agree")
    pmp = sub.add_parser("pm", help="Product Manager (S-5): decide a product/IA question, or escalate a critical one to you")
    pmp.add_argument("app")
    pmp.add_argument("ticket")
    pmp.add_argument("question", nargs="?", default="")
    pmp.add_argument("--telegram", action="store_true", help="also send an ESCALATE proposal to Telegram")
    sr = sub.add_parser("ship-review", help="ready-to-prod review: Release Manager certifies + officers debate -> GO/NO-GO (you promote to MAIN)")
    sr.add_argument("app", nargs="?", help="app to review (default: first configured)")
    adj = sub.add_parser("adjutant", help="Engineering Manager (S-1): personnel review — propose hires/retirements")
    adj.add_argument("--telegram", action="store_true", help="also brief the Commander on Telegram")
    adj.add_argument("--apply", action="store_true", help="EXECUTE the approved personnel action (hire/retire; originals backed up first)")
    sct = sub.add_parser("scout", help="QA Engineer (S-2): smoke-test DEV in a browser (e2e / a11y) and report")
    sct.add_argument("app")
    sct.add_argument("--url", help="a deployed DEV URL to test (else the app's local dev server)")
    sct.add_argument("--telegram", action="store_true", help="also send the recon report to Telegram")
    sct.add_argument("--file", action="store_true", help="file ticket-worthy findings as Jira tickets (assigned to you)")
    prv = sub.add_parser("provost", help="Security Engineer: security recon of the latest DEV changes")
    prv.add_argument("app")
    prv.add_argument("--telegram", action="store_true", help="also send the security report to Telegram")
    prv.add_argument("--file", action="store_true", help="file ticket-worthy findings as Jira tickets (assigned to you)")
    qm = sub.add_parser("quartermaster", help="Release Manager (S-4): certify DEV is deploy-ready before DEV->MAIN")
    qm.add_argument("app")
    qm.add_argument("--telegram", action="store_true", help="also send the readiness report to Telegram")
    qm.add_argument("--file", action="store_true", help="file ticket-worthy findings as Jira tickets (assigned to you)")
    ptl = sub.add_parser("patrol", help="Scheduled patrol: QA Engineer + Security Engineer + Release Manager sweep DEV and file findings")
    ptl.add_argument("app")
    ptl.add_argument("--officers", default=None, help="comma subset (scout,provost,quartermaster); default all three")
    ptl.add_argument("--no-file", action="store_true", help="propose-only — don't create Jira tickets")
    apc = sub.add_parser("autopilot", help="always-on: resume In Progress, else take the top To Do -> QA, continuously")
    apc.add_argument("app", nargs="?", default=None, help="app to work; omit to cover every backlogged app")
    apc.add_argument("--once", action="store_true", help="run a single cycle then exit (good for a live test)")
    apc.add_argument("--interval", type=int, default=60, help="seconds to wait when the queue is empty (default 60)")
    ub = sub.add_parser("unblock", help="clear a parked (escalated) ticket so autopilot retries it")
    ub.add_argument("ticket", nargs="?", default=None, help="ticket id; omit to clear all parked")

    co = sub.add_parser("consolidate", help="tighten Unit Memory: dedup/prune the log + learn from repeated Reviewer rejections")
    co.add_argument("--dry", action="store_true", help="show what would change without writing")

    fx = sub.add_parser("forensics", help="failure taxonomy + repeat offenders; --postmortem <id> writes one")
    fx.add_argument("--postmortem", default=None, metavar="TICKET",
                    help="write/refresh the post-mortem for a ticket and print it")

    ob = sub.add_parser("onboard", help="scaffold a new product into config.yaml (SignalDesk, the EAs, …)")
    ob.add_argument("name", help="short app name, e.g. signaldesk")
    ob.add_argument("repo_path", help="path to the product's git repo")
    ob.add_argument("--base", default=None, help="base branch (features merge here); default: auto-detected")
    ob.add_argument("--protected", default=None, help="protected/production branch; default: auto-detected")
    ob.add_argument("--jira", dest="jira_conn", default="", metavar="CONNECTION_ID",
                    help="attach a cockpit Jira connection id (sets backlog_backend=jira)")
    ob.add_argument("--write", action="store_true",
                    help="write the entry into config.yaml (default: preview only)")
    return p


def _apply_overrides(cfg: Config, args) -> None:
    if args.live:
        cfg.dry_run = False
    if args.max_tickets is not None:
        cfg.max_tickets_per_run = args.max_tickets
    if args.max_iterations is not None:
        cfg.max_iterations = args.max_iterations
    if getattr(args, "effort", None):
        cfg.builder_effort = normalize_effort(args.effort)
        cfg.adaptive_effort = False        # an explicit --effort pins it, bypassing auto-sizing


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


def _consolidate(args) -> int:
    """Dedup/prune the Lessons log + learn from recurring Reviewer rejections."""
    from . import consolidate
    cfg = Config.load(args.config)
    r = consolidate.run(cfg, write=not args.dry)
    pats = r.get("patterns", [])
    if pats:
        print("Recurring Reviewer rejections (the unit keeps getting pulled up on these):\n")
        for p in pats:
            print(f"  ×{p['count']:<3} {p['label']}  ({', '.join(p['tickets'][:5])})")
            print(f"        → {p['action']}")
            print(f"        drill: {p['drill']}")
        print("")
    else:
        print("No recurring rejection pattern (need a theme across 2+ tickets).\n")
    verb = "would change" if args.dry else "changed"
    print(f"Lessons log {verb}: +{len(r.get('added', []))} rejection lesson(s), "
          f"−{r.get('removed_dupes', 0)} duplicate(s), −{r.get('pruned', 0)} pruned "
          f"→ {r.get('kept', 0)} kept.")
    if args.dry:
        print("(--dry — nothing written.)")
    return 0


def _forensics(args) -> int:
    """Print the failure taxonomy + repeat offenders, or write one ticket's post-mortem."""
    from . import forensics
    cfg = Config.load(args.config)
    if args.postmortem:
        p = forensics.write_postmortem(cfg, args.postmortem)
        if not p:
            print(f"  no failures on record for {args.postmortem}")
            return 1
        print(f"  ✓ wrote {p}\n")
        print(p.read_text(encoding="utf-8"))
        return 0
    tax = forensics.taxonomy(cfg)
    if not tax:
        print("No failures on record — clean sheet.")
        return 0
    total = sum(t["count"] for t in tax)
    print(f"Failure taxonomy — {total} failed run(s):\n")
    for t in tax:
        print(f"  {t['count']:>3} · {t['label']}")
        print(f"        → {t['action']}")
    offenders = forensics.repeat_offenders(cfg, threshold=2)
    if offenders:
        print("\nRepeat offenders (failed 2+ times):")
        for o in offenders:
            pm = forensics.postmortem_path(cfg, o["ticket_id"])
            tag = "  · post-mortem written" if pm.exists() else ""
            print(f"  {o['ticket_id']:<16} ×{o['count']:<3} {o['label']}{tag}")
    return 0


def _onboard(args) -> int:
    """Scaffold a new product into config.yaml. Preview by default; --write applies it."""
    from . import onboarding
    r = onboarding.scaffold(
        args.config, name=args.name, repo_path=args.repo_path, base=args.base,
        protected=args.protected, backlog=("jira" if args.jira_conn else "none"),
        connection_id=args.jira_conn, write=args.write)
    if not r["ok"]:
        print(f"  ✗ {r['error']}")
        return 1
    print(f"Product '{r['name']}'  ·  base {r['base']} → protected {r['protected']}  ·  backlog {r['backlog']}")
    for w in r["warnings"]:
        print(f"  ⚠ {w}")
    print("\nconfig.yaml entry:\n")
    print(r["block"])
    if r["written"]:
        print(f"\n  ✓ written into {args.config} (backup: {args.config}.bak). "
              "Restart the cockpit / autopilot to load it.")
    else:
        print(f"\n  (preview only) — re-run with --write to add it to {args.config}.")
    return 0


def _doctor(cfg_path: str) -> int:
    from . import health
    glyph = {"ok": "  ✓", "warn": "  ⚠", "bad": "  ✗"}
    print(f"Config: {cfg_path}")
    try:
        cfg = Config.load(cfg_path)
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ could not load config: {exc}")
        return 1
    print(f"  ✓ config loaded ({len(cfg.apps)} app(s): {', '.join(a.name for a in cfg.apps)})")
    s = health.summary(cfg)
    print(f"  ✓ models: builder {s['models']['builder']} · reviewer {s['models']['reviewer']}")
    for c in s["checks"]:
        line = f"{glyph.get(c['status'], '  ?')} {c['name']}"
        if c["detail"]:
            line += f" — {c['detail']}"
        print(line)
    if s["healthy"]:
        print(f"\nAll good.{' (' + str(s['warnings']) + ' warning(s))' if s['warnings'] else ''}")
    else:
        print(f"\n{s['problems']} problem(s) to fix"
              + (f", {s['warnings']} warning(s)." if s['warnings'] else "."))
    return 0 if s["healthy"] else 1


def _which(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None


def _branch_exists(repo: str, branch: str) -> bool:
    proc = subprocess.run(["git", "-C", os.path.expanduser(repo), "rev-parse", "--verify", branch],
                          capture_output=True, text=True)
    return proc.returncode == 0


async def _main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "consolidate":
        return _consolidate(args)
    if args.command == "forensics":
        return _forensics(args)
    if args.command == "onboard":
        return _onboard(args)
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
    from . import usage
    usage.configure(cfg.audit_path)   # every agent call now meters its token burn here
    usage.prune(cfg)

    if args.command == "serve":
        from . import server
        cfg._source_path = args.config   # so the cockpit's onboard form knows which config.yaml to edit
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
            notify.send("🎖️ Engineering Coach report ready:\n\n" + report[:1500])
        return 0

    if args.command == "council":
        from . import council
        from .audit import AuditLog
        audit = AuditLog(cfg.audit_path)
        # The daily muster IS council + stand-up merged into one (see council.hold_council).
        briefing = await council.hold_council(cfg, topic=getattr(args, "topic", None), audit=audit)
        print("\n" + briefing)
        return 0

    if args.command == "meeting":
        from . import council
        from .audit import AuditLog
        officers = [s.strip() for s in (args.officers or "").split(",") if s.strip()] or None
        decision = await council.hold_meeting(cfg, args.topic, officers=officers,
                                              rounds=getattr(args, "rounds", None),
                                              audit=AuditLog(cfg.audit_path))
        print("\n" + decision)
        return 0

    if args.command == "smalltalk":
        from . import council
        from .audit import AuditLog
        print(await council.small_talk(cfg, audit=AuditLog(cfg.audit_path)))
        return 0

    if args.command == "sync":
        from . import sync
        r = sync.git_sync(cfg)
        peers = ", ".join(r["hosts"]) or "(none yet)"
        pushed = "read-only" if r["pushed"] is None else r["pushed"]
        line = f"sync[{r['host']}] pulled={r['pulled']} pushed={pushed} peers={peers}"
        ll = sync.pull_server_state(cfg)   # Mac-side: pull the server's living log over SSH
        if ll["attempted"]:
            line += f"  livelog={'ok' if ll['pulled'] else 'fail'}"
        print(line + (f"  error: {r['error']}" if r["error"] else "")
              + (f"  livelog-error: {ll['error']}" if ll.get("error") else ""))
        return 0 if not r["error"] else 1

    if args.command == "pm":
        from . import pm
        r = await pm.review(cfg, args.app, args.ticket, getattr(args, "question", "") or "")
        if r["verdict"] == "DECIDE":
            print(f"\n🧭 PM DECISION — {args.ticket}\n\n{r['body']}\n")
        else:
            msg = f"🧭 PM → your call on {args.ticket} (ESCALATE)\n\n{r['body']}"
            print("\n" + msg + "\n")
            if getattr(args, "telegram", False):
                from . import notify
                notify.send(msg[:3500])
        return 0

    if args.command == "ship-review":
        from . import council
        from .audit import AuditLog
        print(await council.ship_review(cfg, getattr(args, "app", None),
                                        audit=AuditLog(cfg.audit_path)))
        return 0

    if args.command == "scribe":
        from . import memory
        print(await memory.scribe(cfg))
        return 0

    if args.command == "roster":
        from . import roster
        p = await roster.refresh(cfg, AuditLog(cfg.audit_path))
        print(f"roster → {p}")
        return 0

    if args.command == "memory":
        from . import memory
        memory.ensure()
        print(memory.load() or "(no Unit Memory yet)")
        return 0

    if args.command == "scout":
        from . import scout, notify, filing
        report = await scout.recon(cfg, args.app, url=getattr(args, "url", None))
        clean, filed, _ = filing.present(report, cfg.app(args.app), "scout", getattr(args, "file", False))
        print(clean + (("\n\n" + filed) if filed else ""))
        Path(cfg.audit_path).with_name("scout-report.md").write_text(clean, encoding="utf-8")
        if getattr(args, "telegram", False):
            notify.send("🛰️ QA Engineer — DEV recon:\n\n" + clean[:2800] + (("\n\n" + filed) if filed else ""))
        return 0

    if args.command == "provost":
        from . import provost, notify, filing
        report = await provost.inspect(cfg, args.app)
        clean, filed, _ = filing.present(report, cfg.app(args.app), "provost", getattr(args, "file", False))
        print(clean + (("\n\n" + filed) if filed else ""))
        Path(cfg.audit_path).with_name("provost-report.md").write_text(clean, encoding="utf-8")
        if getattr(args, "telegram", False):
            notify.send("🛡️ Security Engineer — security recon:\n\n" + clean[:2800] + (("\n\n" + filed) if filed else ""))
        return 0

    if args.command == "quartermaster":
        from . import quartermaster, notify, filing
        report = await quartermaster.inspect(cfg, args.app)
        clean, filed, _ = filing.present(report, cfg.app(args.app), "quartermaster", getattr(args, "file", False))
        print(clean + (("\n\n" + filed) if filed else ""))
        Path(cfg.audit_path).with_name("quartermaster-report.md").write_text(clean, encoding="utf-8")
        if getattr(args, "telegram", False):
            notify.send("📦 Release Manager — deploy readiness:\n\n" + clean[:2800] + (("\n\n" + filed) if filed else ""))
        return 0

    if args.command == "patrol":
        from . import patrol as patrol_mod
        from .audit import AuditLog
        officers = [s.strip().lower() for s in (args.officers or "").split(",") if s.strip()] or None
        await patrol_mod.patrol(cfg, args.app, officers=officers,
                                do_file=not getattr(args, "no_file", False),
                                audit=AuditLog(cfg.audit_path))
        return 0

    if args.command == "autopilot":
        from . import autopilot as autopilot_mod
        await autopilot_mod.autopilot(cfg, args.app, once=getattr(args, "once", False),
                                      interval=getattr(args, "interval", 60))
        return 0

    if args.command == "unblock":
        from . import autopilot as autopilot_mod
        print(autopilot_mod.unblock(cfg, args.ticket))
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
            notify.send("🪖 Engineering Manager — personnel review:\n\n" + report[:3000])
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
