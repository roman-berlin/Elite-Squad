"""CLI entrypoint — the CTO you command from the terminal.

The CTO directs engineers across your unit
of apps, and lands passing work on dev. You stay in command of dev -> main.

  general doctor                              # one-time preflight: config, keys, repos
  general task automatixy "Fix missing scrollbar on the dashboard" \
      --ac "Scrollbar shows on overflow" --ac "No regression on resize"
  general ticket automatixy AUTO-123 AUTO-130 # work specific Jira tickets
  general drain automatixy                    # drain everything labelled autodev
  general drain                               # drain every backlogged app

Runs are LIVE by default (build + merge to dev + Jira updates); set dry_run: true
in config.yaml for a no-changes preview. --live remains as an explicit override.
"general" is the wrapper script; equivalently: python -m orchestrator.main <args>
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from . import backend_pref
from . import backends
from . import intake
from .audit import AuditLog
from .config import Config, normalize_effort
from .contracts import Outcome


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="general",
        description="CTO — commands the Builder and Reviewer engineers across your apps")
    p.add_argument("--config", default="config.yaml", help="path to config.yaml (default: ./config.yaml)")
    p.add_argument("--live", action="store_true", help="disable dry-run: push, merge to dev, write to Jira")
    p.add_argument("--max-tickets", type=int, default=None, help="override max_tickets_per_run")
    p.add_argument("--max-iterations", type=int, default=None, help="override max_iterations")
    p.add_argument("--effort", default=None,
                   help="override the Builder's effort for this run, bypassing auto-sizing "
                        "(low|medium|high|xhigh/ultra|max)")
    p.add_argument("--model", choices=["opus", "glm"], default=None,
                   help="override the model backend for this run (opus=Claude, glm=Z.ai); "
                        "overrides the persisted/config default")
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

    m = sub.add_parser("model", help="show or set the persisted model backend (opus|glm)")
    m.add_argument("backend", nargs="?", choices=["opus", "glm"], default=None,
                   help="omit to print the current backend; pass opus|glm to persist it")

    sub.add_parser("doctor", help="check config, keys, repos and tooling")
    sub.add_parser("ping", help="send a test Telegram message")
    dash = sub.add_parser("dashboard", help="generate dashboard.html from the audit log")
    dash.add_argument("--open", action="store_true", help="open it in your browser after generating")
    sub.add_parser("status", help="print a quick task table in the terminal")
    srv = sub.add_parser("serve", help="run the control panel web app (your cockpit)")
    srv.add_argument("--port", type=int, default=8787, help="port (default 8787)")
    # EU-405: the SAFE restart wrapper — THE way to restart the cockpit. Refuses while a build is in
    # flight so an operator never kickstarts a live build (the AUTO-177 class, 2026-07-19). Raw
    # `launchctl kickstart` is break-glass only — it skips this guard.
    dep = sub.add_parser("deploy",
                         help="safely restart the cockpit — THE way to restart (kickstart is break-glass)")
    dep.add_argument("--port", type=int, default=8787, help="cockpit port to probe (default 8787)")
    dep.add_argument("--force", action="store_true",
                     help="restart even if a build appears in flight (break-glass — may kill live work)")
    st = sub.add_parser("standup", help="daily-meeting report (shipped / needs-you / decisions)")
    st.add_argument("--telegram", action="store_true", help="also send it to Telegram")
    sub.add_parser("daily", help="light daily stand-up: deterministic digest + one CTO synthesis (cheap; the deep council is weekly)")
    cnl = sub.add_parser("council", help="deep WEEKLY council (engineers muster, brief you) — for the daily use `daily`")
    cnl.add_argument("--topic", help="run an ad-hoc improvement muster focused on this topic")
    cg = sub.add_parser("cron-guard",
        help="wrap a cron job: timestamp + rotate council/cron.log, alert Telegram on repeated failure (EU-432)")
    cg.add_argument("--job", required=True, help="cron job name (the log tag + the per-job state key)")
    cg.add_argument("wrapped", nargs=argparse.REMAINDER,
        help="the command to run, after -- (e.g. -- ./general daily)")
    sub.add_parser("server-watchdog",
        help="one Mac->VPS cross-host watch tick: SSH-probe the VPS + content-check the daily brief, "
             "alert from the Mac (EU-433). No-op without GENERAL_SERVER_SSH.")
    sub.add_parser("scribe", help="Technical Writer: fold recent council + runs into Unit Memory (memory/UNIT.md)")
    sub.add_parser("roster", help="regenerate the living roster (engineers + hierarchy chart) -> ROSTER.md")
    sub.add_parser("memory", help="print the unit's living protocol (memory/UNIT.md)")
    mtg = sub.add_parser("meeting", help="convene an ad-hoc meeting on a topic (engineers debate, the CTO decides)")
    mtg.add_argument("--topic", required=True, help="what the meeting is about")
    mtg.add_argument("--officers", help="comma-separated engineer names/keys to attend (default: all relevant)")
    mtg.add_argument("--rounds", type=int, default=None, help="discussion rounds (default: council_rounds)")
    sub.add_parser("sync", help="exchange the audit log with the other machine (Mac<->server) so both cockpits agree")
    pmp = sub.add_parser("pm", help="Product Manager (S-5): decide a product/IA question, or escalate a critical one to you")
    pmp.add_argument("app")
    pmp.add_argument("ticket")
    pmp.add_argument("question", nargs="?", default="")
    pmp.add_argument("--telegram", action="store_true", help="also send an ESCALATE proposal to Telegram")
    sr = sub.add_parser("ship-review", help="ready-to-prod review: Release Manager certifies + engineers debate -> GO/NO-GO (you promote to MAIN)")
    sr.add_argument("app", nargs="?", help="app to review (default: first configured)")
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
    apc.add_argument("--force", action="store_true",
                     help="start even when a detached daemon already holds the PID file")
    ub = sub.add_parser("unblock", help="clear a parked (escalated) ticket so autopilot retries it")
    ub.add_argument("ticket", nargs="?", default=None, help="ticket id; omit to clear all parked")

    co = sub.add_parser("consolidate", help="tighten Unit Memory: dedup/prune the log + learn from repeated Reviewer rejections")
    co.add_argument("--dry", action="store_true", help="show what would change without writing")

    fx = sub.add_parser("forensics", help="failure taxonomy + repeat offenders; --postmortem <id> writes one")
    fx.add_argument("--postmortem", default=None, metavar="TICKET",
                    help="write/refresh the post-mortem for a ticket and print it")

    bm = sub.add_parser("benchmark", help="SWE-bench Verified builder benchmark — objective quality number")
    bm.add_argument("--sample", type=int, default=20,
                    help="tasks to evaluate (1–50; default 20)")
    bm.add_argument("--budget", type=float, default=5.0,
                    help="cost ceiling in USD; aborts when reached (default 5.0)")
    bm.add_argument("--seed", default=None, metavar="STR",
                    help="random seed for task sampling (default: current ISO-week seed)")
    bm.add_argument("--trend", type=int, default=None, metavar="N",
                    help="also print the last N runs from the audit log after the benchmark")

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
    # EU-190: model backend. --model overrides for THIS run; otherwise run-family commands honour
    # the persisted sticky preference (set from the cockpit or `./general model`), leaving unattended
    # autopilot on the config.yaml default (a deliberate no-silent-automation-egress choice).
    if getattr(args, "model", None):
        cfg.model_backend = backends.normalize(args.model)
    elif getattr(args, "command", None) in ("task", "ticket", "drain"):
        cfg.model_backend = backend_pref.active(cfg)


async def _run_work(cfg: Config, worklist) -> int:
    # EU-190: no silent fallback — block a GLM run when GLM_AUTH_TOKEN isn't configured.
    if (backends.normalize(getattr(cfg, "model_backend", "opus")) == backends.GLM
            and not backends.available("glm")):
        print("GLM is selected but GLM_AUTH_TOKEN is not configured — set it (and restart) "
              "or run with --model opus.")
        return 2
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


async def _benchmark(args) -> int:
    """Run the SWE-bench Verified builder benchmark and optionally print trend.

    Wires ``scripts/swebench_builder.run_benchmark`` and
    ``scripts/swebench_report.report`` into the standard 'general' CLI so the
    benchmark can be invoked via ``python -m orchestrator.main benchmark`` (or
    cron) without knowing the script path.

    Flags:
        --sample N      Tasks to evaluate (default 20, hard-capped at 50).
        --budget FLOAT  Cost ceiling in USD (default 5.0).
        --seed STR      Deterministic seed for task sampling.  Defaults to the
                        current ISO-week seed so weekly cron runs are reproducible.
        --trend N       After the run, print the last N rows from the audit log.

    Returns:
        1 when the run was aborted by the budget ceiling, 0 otherwise.
    """
    import sys as _sys
    from pathlib import Path as _Path

    # Scripts live at <repo_root>/scripts/; make them importable without a
    # package install.  Idempotent: we only insert if the path is absent.
    _scripts_dir = str(_Path(__file__).parent.parent / "scripts")
    if _scripts_dir not in _sys.path:
        _sys.path.insert(0, _scripts_dir)

    import swebench_builder  # noqa: PLC0415 (lazy import by design)
    import swebench_report   # noqa: PLC0415

    # Use the weekly seed when --seed is omitted so the cron run always draws
    # the same task subset within an ISO week and results are comparable.
    seed: str = args.seed if args.seed is not None else swebench_report.weekly_seed()

    summary = await swebench_builder.run_benchmark(
        sample=args.sample,
        cost_ceiling_usd=args.budget,
        random_seed=seed,
        jsonl_path=swebench_report.AUDIT_JSONL,
    )

    # run_benchmark() already printed the pass/fail summary table and appended the
    # single JSONL audit record (it calls swebench_report.report internally with the
    # jsonl_path passed above).  Do NOT call report() again here — a second call
    # double-printed the summary and appended a duplicate trend row (EU-95 iter-2).
    if args.trend is not None:
        swebench_report.print_trend(swebench_report.AUDIT_JSONL, args.trend)

    return 1 if summary.get("aborted") else 0


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


def _cron_guard(args) -> int:
    """EU-432 — wrap a cron job: timestamp + rotate ``council/cron.log`` and alert Telegram on
    repeated failure (exactly one alert per N consecutive failures).

    The wrapped command is everything after ``--`` (a leading ``--`` argparse may leave in the
    REMAINDER is stripped). Paths are the canonical ones the crontab already writes
    (``council/cron.log`` — EU-431 keeps it there; ``state/cron_health.json`` beside the audit
    root), so this needs no config. Returns the wrapped command's real exit code.
    """
    from . import cron_guard
    wrapped = list(getattr(args, "wrapped", None) or [])
    if wrapped and wrapped[0] == "--":   # strip the single options-separator argparse leaves in
        wrapped = wrapped[1:]
    if not wrapped:
        print("cron-guard: no command given. Pass it after `--`, e.g.\n"
              "  ./general cron-guard --job daily -- ./general daily", file=sys.stderr)
        return 2
    return cron_guard.run_guarded(
        args.job, wrapped,
        log_path="council/cron.log",
        state_path="state/cron_health.json",
    )


def _server_watchdog() -> int:
    """EU-433 — one Mac→VPS cross-host watch tick.

    Probes the VPS over SSH (liveness, AC1), content-checks the newest daily brief (missing / provider
    error, AC2), and pages from the MAC (independent of the VPS process, AC3). Dispatched BEFORE the
    config preamble (like cron-guard) so a periodic launchd tick stays cheap; it uses ``$GENERAL_SERVER_SSH``
    + the canonical ``state/server_watchdog_state.json``, never the config. A clean no-op (return 0) when
    no VPS target is configured, so the agent is safe to install on any host. Never raises."""
    from . import server_watchdog
    return server_watchdog.run()


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
    # 2026-07-15: doctor is the Commander's live diagnostic — force a FRESH auth-liveness probe so
    # the "Claude auth" line can't show a ≤15-min-stale verdict right after a /login fix (or right
    # after a token died). health.checks() below then reads this probe from the warm cache.
    try:
        from . import auth_probe
        auth_probe.probe(force=True)
    except Exception:  # noqa: BLE001 - the probe must never crash the doctor
        pass
    s = health.summary(cfg)
    print(f"  ✓ models: builder {s['models']['builder']} · reviewer {s['models']['reviewer']} · backend {s['backend']}")
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


# EU-405 AC1 — the SAFE restart wrapper. `./general deploy` is THE way to restart the cockpit; raw
# `launchctl kickstart` (which kills whatever is running) is break-glass only. See DEPLOYMENT.md.
_MAC_COCKPIT_LABEL = "com.roman.general.cockpit"
_VPS_SERVICE = "general.service"


def _deploy(args) -> int:
    """EU-405: refuse to restart while a build is in flight, then restart via the host's supervisor.

    The guard closes the AUTO-177 class (2026-07-19: a kickstart killed a live build mid-edit,
    orphaning its worktree). Two signals, OR'd: (1) the cockpit's LIVE ``active_run_count`` —
    authoritative, probed over HTTP when the cockpit is reachable; (2) the cross-process audit tail
    (``autopilot.in_flight_builds`` — a ``ticket_start`` with no terminal event), the fallback that
    also names WHICH tickets are running and covers a cockpit that is down. Either non-zero and no
    ``--force`` → refuse with the exact break-glass command. Safe → kickstart/systemd/manual restart.
    """
    from . import autopilot as _ap
    from .config import Config as _Config

    cfg = _Config.load(args.config)
    port = getattr(args, "port", 8787) or 8787
    force = bool(getattr(args, "force", False))

    # 1) Authoritative live signal: ask the running cockpit how many runs are active. None when the
    #    cockpit is unreachable (down, or wedged) — then fall back to the audit signal alone.
    live_active = _probe_active_run_count(port)
    # 2) Cross-process audit signal: any open ticket_start the serve process wrote.
    in_flight = _ap.in_flight_builds(cfg)

    blocking: list[str] = []
    if live_active is not None and live_active > 0:
        blocking.append(f"the cockpit reports {live_active} active run(s) (live probe)")
    if in_flight:
        detail = ", ".join(f"{b['ticket_id']} (started {int(b['started_s_ago'] // 60)}m ago)"
                           for b in in_flight[:5])
        blocking.append(f"open build(s) with no terminal outcome: {detail}")

    if blocking and not force:
        print("⛔ Refusing to restart — a build appears to be in flight:")
        for b in blocking:
            print(f"    · {b}")
        print("\n  This is the AUTO-177 guard: a restart now would kill live work mid-build.")
        print("  Wait for it to finish (watch `./general status` or the cockpit), then re-run.")
        print("  `./general deploy` is THE way to restart. If you are CERTAIN the signature is stale")
        print("  (a killed run the audit never closed), re-run with --force.")
        print("  Break-glass — restart NOW regardless, killing any in-flight work:")
        print(f"    launchctl kickstart -k gui/$(id -u)/{_MAC_COCKPIT_LABEL}   "
              f"(Mac)   ·   sudo systemctl restart {_VPS_SERVICE}   (VPS)")
        return 1

    note = " (--force — a possibly-stale build signature was ignored)" if force and blocking else ""
    return _restart_cockpit(note)


def _probe_active_run_count(port: int) -> int | None:
    """EU-405: the cockpit's live ``active_run_count`` over HTTP, or None if unreachable.

    A best-effort enrichment of the audit signal: when the cockpit is up, this is the AUTHORITATIVE
    'is a build running?' answer (the in-memory truth the audit tail only approximates). When the
    cockpit is down or wedged, None → the caller falls back to ``in_flight_builds``."""
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
            return int(_json.loads(r.read().decode("utf-8")).get("active_run_count", -1))
    except Exception:  # noqa: BLE001 — unreachable/wedged/old-build cockpit → fall back to the audit
        return None


def _restart_cockpit(note: str = "") -> int:
    """EU-405: perform the actual restart via whichever supervisor owns the cockpit.

    Auto-detects launchd (Mac), systemd (VPS), or a foreground `./general serve` (no supervisor).
    Never restarts a second copy onto an occupied port — launchd/systemd handle that themselves."""
    import subprocess as _sp
    plist = os.path.expanduser(f"~/Library/LaunchAgents/{_MAC_COCKPIT_LABEL}.plist")
    if os.path.exists(plist):
        cmd = ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{_MAC_COCKPIT_LABEL}"]
        print(f"♻️  Restarting the cockpit via launchd{note}")
        print(f"    $ {' '.join(cmd)}")
        r = _sp.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            print("  ✓ kickstarted — the cockpit respawns on the current code within ~5s.")
            print("    cockpit: http://127.0.0.1:8787")
            return 0
        print(f"  ✗ kickstart failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()}")
        return 1
    if _systemd_has_service(_VPS_SERVICE):
        cmd = ["sudo", "-n", "systemctl", "restart", _VPS_SERVICE]
        print(f"♻️  Restarting the cockpit via systemd{note}")
        print(f"    $ {' '.join(cmd)}")
        r = _sp.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            print(f"  ✓ restarted {_VPS_SERVICE} — it loads the current code.")
            return 0
        print(f"  ✗ systemctl failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()}")
        print("    (passwordless sudo for `systemctl restart general.service` must be configured —")
        print("     see VPS_DEPLOYMENT.md.)")
        return 1
    print("ℹ️  The cockpit is not under launchd or systemd — it runs as a foreground `./general serve`.")
    print("    Restart it by hand: stop that process (Ctrl-C) and re-run `./general serve`.")
    print("    No build was in flight, so this is safe. (EU-405)")
    return 0


def _systemd_has_service(name: str) -> bool:
    """EU-405: True when a systemd unit file for ``name`` is installed on this host."""
    import subprocess as _sp
    for unit_dir in ("/etc/systemd/system", "/lib/systemd/system",
                     os.path.expanduser("~/.config/systemd/user")):
        if os.path.exists(os.path.join(unit_dir, name)):
            return True
    # Fall back to asking systemctl (covers units from elsewhere / a user manager).
    try:
        r = _sp.run(["systemctl", "cat", name], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:  # noqa: BLE001 — no systemd (Mac) → not a systemd host
        return False


def _which(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None


def _branch_exists(repo: str, branch: str) -> bool:
    proc = subprocess.run(["git", "-C", os.path.expanduser(repo), "rev-parse", "--verify", branch],
                          capture_output=True, text=True)
    return proc.returncode == 0


async def _main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "benchmark":
        return await _benchmark(args)

    # EU-432: cron job guard — dispatched BEFORE the config preamble so a 15-min cron tick stays
    # cheap (no adopt_legacy/usage pass). It uses the canonical, deliberately-not-migrated paths
    # (council/cron.log per EU-431, state/cron_health.json), not cfg.
    if args.command == "cron-guard":
        return _cron_guard(args)
    # EU-433: Mac->VPS cross-host watch tick — dispatched BEFORE the config preamble so a periodic
    # launchd tick stays cheap (no adopt_legacy/usage pass). It reads $GENERAL_SERVER_SSH + the
    # canonical state sidecar, never the config. Safe no-op where no VPS target is configured.
    if args.command == "server-watchdog":
        return _server_watchdog()

    if args.command == "consolidate":
        return _consolidate(args)
    if args.command == "forensics":
        return _forensics(args)
    if args.command == "onboard":
        return _onboard(args)
    if args.command == "doctor":
        return _doctor(args.config)
    if args.command == "deploy":
        return _deploy(args)
    if args.command == "ping":
        from . import notify
        if not notify.configured():
            print("Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, then `source .env`.")
            return 1
        ok = notify.send("✅ General test ping — Telegram is wired up.")
        print("sent ✓ (check your Telegram)" if ok else "failed — double-check token / chat id.")
        return 0 if ok else 1
    if args.command == "model":
        # EU-190: show or set the persisted active model backend (opus|glm). The store is anchored to
        # the config's state dir (hermeticity fix, 2026-07-09), so BOTH paths load config.yaml now.
        cfg = Config.load(args.config)
        backend_pref.migrate(cfg)
        if args.backend is None:
            cur = backend_pref.active(cfg)
            warn = ("  (⚠ GLM_AUTH_TOKEN not set — GLM runs are blocked)"
                    if cur == backends.GLM and not backends.available("glm") else "")
            print(f"model backend: {cur}{warn}")
            return 0
        bk = backends.normalize(args.backend)
        if bk == backends.GLM and not backends.available("glm"):
            print("GLM not configured — set GLM_AUTH_TOKEN in .env (and restart) before selecting GLM.")
            return 2
        backend_pref.set_active(bk, cfg)
        print(f"model backend set to {bk} — applies to subsequent runs.")
        return 0

    cfg = Config.load(args.config)
    _apply_overrides(cfg, args)
    # One-time move of a legacy repo-root model_backend.json into the state dir (live entrypoint only —
    # never from library/app code, so tests around tmp configs can't relocate the operator's real pref).
    backend_pref.migrate(cfg)
    # EU-431: self-heal the council archive the 2026-07-21 state/ migration orphaned — MOVE the
    # legacy council/ (index.jsonl + transcripts) into state/council/ on boot, BEFORE any ceremony
    # can write a fresh index and lose the back-history. Same live-entrypoint-only discipline as
    # backend_pref.migrate: a tmp-config test never relocates the operator's real archive.
    from . import council as _council_mod
    _council_mod.adopt_legacy_council(cfg, AuditLog(cfg.audit_path))
    # EU-436: self-heal the filed-signature dedup ledger the 2026-07-21 state/ migration orphaned —
    # MOVE the legacy signature_filed.json into state/ on boot, BEFORE the next signature_sweep can
    # re-file a postmortem for a crash signature it already surfaced (a duplicate Jira ticket). Same
    # live-entrypoint-only discipline as backend_pref.migrate / adopt_legacy_council.
    from . import forensics as _forensics_mod
    _forensics_mod.adopt_legacy_signature_ledger(cfg, AuditLog(cfg.audit_path))
    # EU-435: self-heal the postmortem archive the 2026-07-21 state/ migration orphaned — MOVE the
    # legacy postmortems/ dir of *.md (one per repeatedly-failing ticket) into state/postmortems/ on
    # boot, BEFORE the next failure can write a fresh postmortem and the irreplaceable failure history
    # stays orphaned. Identical in shape to council/, same live-entrypoint-only discipline as
    # backend_pref.migrate / adopt_legacy_council / adopt_legacy_signature_ledger.
    _forensics_mod.adopt_legacy_postmortems(cfg, AuditLog(cfg.audit_path))
    from . import usage
    usage.configure(cfg.audit_path)   # every agent call now meters its token burn here
    usage.prune(cfg)
    # QW4: every agent call also lands an `agent_call` audit event (model, tokens, duration).
    from . import agent as _agent
    _agent.configure_audit(AuditLog(cfg.audit_path))
    _agent.configure_timeouts(cfg)   # EU-221: per-tag wall-clock budgets (officer/builder)
    # EU-425: anchor the Jira adapter's transition-audit sink to the same resolved audit_path the
    # rest of the process writes to. The adapter is built from `app` alone (base.make_backlog), so
    # it can't see cfg — without this it would fall back to Config's class default and could split
    # ticket_transition events into a different file than the live ledger.
    from .backlog import jira as _jira
    _jira.configure_audit_path(cfg.audit_path)

    if args.command == "serve":
        from . import server
        cfg._source_path = args.config   # so the cockpit's onboard form knows which config.yaml to edit
        # EU-386: dirty-tree respawn forensics — an accidental (crash / KeepAlive) respawn onto
        # uncommitted changes must be loud, not silent. Wired here rather than in server.serve()
        # because this is the one line every cockpit boot takes (CLI and launchd daemon alike);
        # serve's own process_start audit line stays in server.py. Warn FIRST so the dirty-tree
        # message precedes any auto-resumed drain's output.
        from . import autopilot as _ap
        _ap.warn_dirty_tree(cfg, "serve")
        # EU-387: if THIS boot is the respawn a self-update exit asked for, record the completion
        # and clear the one-shot flag (a second boot must not re-consume it).
        from .audit import AuditLog as _AL
        _boot_audit = _AL(cfg.audit_path)
        _self_restart = None
        try:
            _self_restart = _ap.consume_self_restart_flag(cfg, _boot_audit)
        except Exception:  # noqa: BLE001 — boot bookkeeping must never block serving
            pass
        # EU-404 AC3: after a self-repo land, smoke the just-landed code BEFORE the drain resumes
        # onto it — a land that passes the worktree gate but fails at boot (import cycle, schema
        # change, missing dep) otherwise launchd-crash-loops forever with no alert. Red smoke holds
        # the drain resume (intent kept); the cockpit still serves because serving is downstream.
        _resume_block = None
        if _self_restart is not None:
            try:
                _ok, _detail = _ap.boot_smoke(cfg, audit=_boot_audit,
                                              ticket=_self_restart.get("ticket"))
                if not _ok:
                    _resume_block = f"boot-smoke-failed: {_detail}"
            except Exception:  # noqa: BLE001 — boot must proceed; treat an unrunnable smoke as green
                pass                          # (the import in THIS process already succeeded)
        # EU-385 (EU-224a): auto-resume drains persisted as RUNNING when the previous process
        # died — the crash-respawn recovery that closed the 66-minute dead-drain gap. A drain the
        # Commander explicitly stopped is never resurrected (the intent file's STOPPED state and
        # the EU-356 autopilot_stop_requested audit trail are the discriminators). EU-404 adds the
        # crash-loop breaker (per-app) and the boot-smoke hold (global via block_reason). Never raises.
        _ap.resume_armed_drains(cfg, block_reason=_resume_block)
        # EU-398: reconcile In Progress tickets left dangling by a killed/crashed previous run
        # (AUTO-177, 2026-07-19) — resume or honestly park each so the board never shows work
        # happening on a dead run. Runs AFTER resume_armed_drains so a ticket an in-flight drain
        # is already working reads as active and is left untouched. Never raises.
        try:
            _ap.boot_reconcile(cfg)
        except Exception:  # noqa: BLE001 — boot bookkeeping must never block serving
            pass
        server.serve(cfg, port=args.port)
        return 0

    if args.command == "standup":
        from . import dashboard as D, notify
        report = D.standup(cfg)
        print(report)
        if getattr(args, "telegram", False):
            notify.send(report)
        return 0

    if args.command == "daily":
        from . import council
        audit = AuditLog(cfg.audit_path)
        # The LIGHT daily: deterministic digest + one CTO synthesis (~1 model call). The deep
        # multi-officer muster (council) is now weekly.
        brief = await council.daily_brief(cfg, audit=audit)
        print("\n" + brief)
        return 0

    if args.command == "council":
        from . import council
        audit = AuditLog(cfg.audit_path)
        # The DEEP weekly council: officers muster (Yesterday/Today/Blockers) + the CTO's briefing.
        briefing = await council.hold_council(cfg, topic=getattr(args, "topic", None), audit=audit)
        print("\n" + briefing)
        return 0

    if args.command == "meeting":
        from . import council
        officers = [s.strip() for s in (args.officers or "").split(",") if s.strip()] or None
        decision = await council.hold_meeting(cfg, args.topic, officers=officers,
                                              rounds=getattr(args, "rounds", None),
                                              audit=AuditLog(cfg.audit_path))
        print("\n" + decision)
        return 0

    if args.command == "sync":
        from . import sync
        r = sync.git_sync(cfg)
        # EU-428 AC1: peers= carries each peer's NEWEST-EVENT age (parsed from the ts inside the
        # synced file, not its mtime) + a STALE marker, so a pull that transports nothing can't hide
        # behind a healthy pulled=True. pulled= stays the git-pull success bit.
        peers = sync.peer_summary(cfg)
        pushed = "read-only" if r["pushed"] is None else r["pushed"]
        line = f"sync[{r['host']}] pulled={r['pulled']} pushed={pushed} peers={peers}"
        ll = sync.pull_server_state(cfg)   # Mac-side: pull the server's living log over SSH
        if ll["attempted"]:
            line += f"  livelog={'ok' if ll['pulled'] else 'fail'}"
        sa = sync.pull_server_audit(cfg)   # EU-181: pull the server's audit so the Mac cockpit mirrors it
        if sa["attempted"]:
            line += f"  server-audit={'ok' if sa['pulled'] else 'fail'}"
        print(line + (f"  error: {r['error']}" if r["error"] else "")
              + (f"  livelog-error: {ll['error']}" if ll.get("error") else "")
              + (f"  server-audit-error: {sa['error']}" if sa.get("error") else ""))
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
        officers = [s.strip().lower() for s in (args.officers or "").split(",") if s.strip()] or None
        await patrol_mod.patrol(cfg, args.app, officers=officers,
                                do_file=not getattr(args, "no_file", False),
                                audit=AuditLog(cfg.audit_path))
        return 0

    if args.command == "autopilot":
        from . import autopilot as autopilot_mod
        # EU-190: no silent fallback — block autopilot too when GLM is selected (via --model / config)
        # but GLM_AUTH_TOKEN isn't configured. (Headless autopilot uses the config.yaml default, not
        # the cockpit sticky pref — an automation-egress opt-in for that is tracked in Phase 2.)
        if (backends.normalize(getattr(cfg, "model_backend", "opus")) == backends.GLM
                and not backends.available("glm")):
            print("GLM is selected but GLM_AUTH_TOKEN is not configured — set it (and restart) "
                  "or run with --model opus.")
            return 2
        # EU-368: the same detached-daemon guard the cockpit has had since EU-103 (server.py:605).
        # Without it this path walked straight into autopilot()'s unconditional _write_pid(), so the
        # CLI overwrote a live launchd daemon's record and two daemons drained one queue against one
        # PID file — the EU-355 hermeticity incident class, but in production.
        if autopilot_mod.daemon_is_external() and not getattr(args, "force", False):
            print("autopilot is already running as a detached daemon — stop it first (unload the "
                  "launchd keepalive agent, or close the terminal it runs in) before starting "
                  "another, or pass --force if you're sure.")
            return 2
        await autopilot_mod.autopilot(cfg, args.app, once=getattr(args, "once", False),
                                      interval=getattr(args, "interval", 60))
        return 0

    if args.command == "unblock":
        from . import autopilot as autopilot_mod
        print(autopilot_mod.unblock(cfg, args.ticket))
        return 0

    if args.command in ("dashboard", "status"):
        from . import dashboard as D
        charged = bool(os.environ.get("ANTHROPIC_API_KEY"))
        tasks = D.load_tasks(cfg.audit_path)
        if args.command == "status":
            print(D.render_status(tasks, show_cost=charged))
            return 0
        out = Path(cfg.audit_path).with_name("dashboard.html")
        try:
            from . import needs as _needs_mod
            _needs_cnt = _needs_mod.count(cfg)
        except Exception:  # noqa: BLE001
            _needs_cnt = None
        # EU-314: the static `general dashboard` command writes a single global HTML file with no
        # active-project tab concept (no _tab_bar, no ?app= to switch). The per-project pipeline
        # board is scoped to an active tab, so it is intentionally NOT rendered here — app_name is
        # left unset and render_html emits no board, exactly as before. The board is a live-cockpit
        # (/tasks) feature; the static file stays the unscoped multi-project overview.
        out.write_text(D.render_html(tasks, show_cost=charged, needs_count=_needs_cnt), encoding="utf-8")
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
