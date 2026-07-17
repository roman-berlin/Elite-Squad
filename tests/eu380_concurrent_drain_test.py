"""EU-380 — the concurrent drain: N builder slots, safe by construction, default OFF.

Research (2026-07-17, from 292 landed tickets): the drain's serialism lives in _run_inner's while
loop, not in the gate — _land already re-gates every ticket against the fresh dev tip and lands
fast-forward-only, so concurrency does not weaken correctness. The overlap worth having is the
awaited LLM passes (59-65% of wall clock); Amdahl on measured phases gives ~1.7x at N=2.

Prereqs pinned here alongside the scheduler:
  - EU-272: stdout attribution via ContextVar (per-slot, survives asyncio interleaving);
  - EU-379: a lost cross-process land race re-trials in-process (its own harness);
  - split-sibling mutex: two fragments of one split never build concurrently.

Pins:
  (1) default OFF: max_concurrent_builders=1 routes to the untouched serial body;
  (2) N=2 runs tickets CONCURRENTLY (overlap observed via an in-flight high-water mark);
  (3) split siblings never overlap (the mutex defers the second until the first completes);
  (4) every ticket still gets exactly one report (nothing dropped, nothing duplicated);
  (5) a base-level verdict halts further picks of THAT app only;
  (6) each same-app slot gets its OWN worktree path and its own AppConfig copy
      (_make_git mutates app.workdir — sharing would point two builders at one tree);
  (7) the reaper's canonical set covers slot worktrees (never eats an idle slot);
  (8) stop event: no new picks after it fires;
  (9) EU-272: concurrent slots attribute stdout to their own app via the ContextVar.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

sdk = types.ModuleType("claude_agent_sdk")
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", sdk)

sys.path.insert(0, ".")

from orchestrator import cockpit_state, loop  # noqa: E402
from orchestrator.config import Config, AppConfig  # noqa: E402
from orchestrator.contracts import Ticket, Outcome, TicketReport  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


_tmp = Path(tempfile.mkdtemp())


def mkcfg(n=2) -> Config:
    c = Config(apps=[], audit_path=str(_tmp / "audit.jsonl"))
    c.max_concurrent_builders = n
    c.use_worktree = False        # no real git in this harness
    c.max_cost_usd = 0
    return c


APP_A = AppConfig(name="alpha", repo_path=str(_tmp / "a"), base_branch="dev",
                  protected_branch="main", backlog_backend="none")
APP_B = AppConfig(name="beta", repo_path=str(_tmp / "b"), base_branch="dev",
                  protected_branch="main", backlog_backend="none")


def T(tid, desc="", app="alpha"):
    return Ticket(id=tid, key=tid, summary=tid, description=desc, app=app, ephemeral=True)


class _Audit:
    def __init__(self): self.events = []
    def record(self, event, **k): self.events.append((event, k))


# stub the per-ticket work: records concurrency + attribution, yields to interleave
trace = {"active": 0, "peak": 0, "order": [], "tee": []}


async def _fake_process(ticket, app, cfg, git, backlog, audit, budget, stop_event=None):
    trace["active"] += 1
    trace["peak"] = max(trace["peak"], trace["active"])
    trace["order"].append(("start", ticket.id))
    trace["tee"].append((ticket.id, cockpit_state._RUN_APP.get()))
    await asyncio.sleep(0.05)
    trace["active"] -= 1
    trace["order"].append(("end", ticket.id))
    return TicketReport(ticket.id, Outcome.MERGED, 1, 0.0, app.name, notes="")


_orig_inner = loop._process_ticket_inner
_orig_note = loop.run_logger.write_note_log
_orig_mkgit = loop._make_git
loop._process_ticket_inner = _fake_process
loop.run_logger.write_note_log = lambda *a, **k: None
loop._make_git = lambda cfg, app, slot=0: SimpleNamespace(ensure_clean=lambda: None)

try:
    # (2)+(4)+(9) two tickets, two slots → overlap, full reports, per-slot attribution
    trace.update(active=0, peak=0, order=[], tee=[])
    reports = asyncio.run(loop._run_inner(
        mkcfg(2), [(APP_A, T("A-1")), (APP_B, T("B-1", app="beta"))], _Audit()))
    ok("(2) N=2 overlaps ticket processing (peak in-flight == 2)", trace["peak"] == 2,
       f"peak={trace['peak']}")
    ok("(4) every ticket reported exactly once",
       sorted(r.ticket_id for r in reports) == ["A-1", "B-1"], str(reports))
    ok("(9) each slot's stdout context attributes to ITS app (EU-272)",
       dict(trace["tee"]) == {"A-1": "alpha", "B-1": "beta"}, str(trace["tee"]))

    # (3) split siblings never overlap
    trace.update(active=0, peak=0, order=[], tee=[])
    sib = "— Auto-split from AUTO-999 by the Scrum Master (too heavy to land as one)."
    reports = asyncio.run(loop._run_inner(
        mkcfg(2), [(APP_A, T("A-2", desc=sib)), (APP_A, T("A-3", desc=sib))], _Audit()))
    ok("(3) split siblings are mutexed (peak in-flight == 1)", trace["peak"] == 1,
       f"peak={trace['peak']} order={trace['order']}")
    ok("(3b) both siblings still ran to completion", len(reports) == 2, str(reports))

    # (5) a base-level verdict halts only that app's FUTURE picks. Under concurrency an
    # already-in-flight same-app ticket finishes (it may repeat the verdict once) — the halt
    # contract is about not STARTING more work on a broken base. Deterministic ordering: worker 2
    # is held busy on the slow B-2 while A-4 fast-fails, so A-5's pick happens after the halt.
    async def _base_halt_process(ticket, app, cfg, git, backlog, audit, budget, stop_event=None):
        if ticket.id == "A-4":
            await asyncio.sleep(0.01)
            return TicketReport(ticket.id, Outcome.ESCALATED, 1, 0.0, app.name,
                                notes="red base — gate fails on the clean base tree")
        await asyncio.sleep(0.08)
        return TicketReport(ticket.id, Outcome.MERGED, 1, 0.0, app.name, notes="")
    loop._process_ticket_inner = _base_halt_process
    au = _Audit()
    reports = asyncio.run(loop._run_inner(
        mkcfg(1 + 1), [(APP_A, T("A-4")), (APP_B, T("B-2", app="beta")), (APP_A, T("A-5"))], au))
    by_id = {r.ticket_id: r for r in reports}
    ok("(5) after a base-level verdict the app's NEXT pick is skipped",
       by_id["A-5"].outcome == Outcome.SKIPPED, str(by_id.get("A-5")))
    ok("(5b) the OTHER app keeps building", by_id["B-2"].outcome == Outcome.MERGED)
    loop._process_ticket_inner = _fake_process

    # (8) stop event: no new picks
    import threading
    stop = threading.Event(); stop.set()
    reports = asyncio.run(loop._run_inner(
        mkcfg(2), [(APP_A, T("A-6"))], _Audit(), stop_event=stop))
    ok("(8) a set stop event yields no picks", reports == [], str(reports))

    # (1) default OFF routes to the serial body
    src = Path("orchestrator/loop.py").read_text()
    ok("(1) max_concurrent_builders<=1 routes to the untouched serial drain",
       "return await _run_inner_serial(cfg, worklist, audit, stop_event, stop_between_tickets)" in src
       and "async def _run_inner_serial" in src)
    ok("(1b) the knob defaults to 1 (concurrency is opt-in)",
       Config(apps=[]).max_concurrent_builders == 1)

    # (6) slot worktrees + per-slot AppConfig copies
    ok("(6) slot paths are distinct per slot",
       loop._worktree_path(APP_A, mkcfg(2), 0).endswith("/alpha")
       and loop._worktree_path(APP_A, mkcfg(2), 1).endswith("/alpha-s1"))
    ok("(6b) the concurrent path copies the AppConfig before _make_git mutates workdir",
       "app_slot = copy.copy(app)" in src)

    # (7) the reaper protects slot worktrees
    gsrc = Path("orchestrator/git_ops.py").read_text()
    ok("(7) reap_stale_worktrees canonicalizes every slot path",
       "for _slot in range(_slots):" in gsrc and "_worktree_path(a, cfg, _slot)" in gsrc)
    # (7b) 2026-07-17 live incident: Elite-Unit-s1 was eaten within hours — a reap invoked with a
    # cfg LACKING the knob (bare harness in a worktree; defaults = 1 slot) saw the idle slot as
    # non-canonical (detached-at-base + flock-free = "merged and dead"). Slots must be protected
    # BY NAME PATTERN, independent of the caller's cfg knob value.
    ok("(7b) slot worktrees are pattern-protected regardless of the cfg's knob",
       're.fullmatch(re.escape(getattr(a, "name", "")) + r"-s\\d+", _base)' in gsrc,
       "a defaults-cfg reap would eat idle slots (the -s1 incident, 2026-07-17)")

finally:
    loop._process_ticket_inner = _orig_inner
    loop.run_logger.write_note_log = _orig_note
    loop._make_git = _orig_mkgit

print(f"\n{checks}/{checks} passed")
