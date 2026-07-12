#!/usr/bin/env python3
"""EU-220: end-to-end harness for EU-201's fragment auto-injection wiring.

EU-201 added `while`-loop wiring in `loop._run_inner` that, on a REQUEUED ticket whose notes
say "split into X-1, X-2", splices the fragment tickets into the live worklist right after the
parent (`worklist[i:i] = fragment_items`) and records a `fragment_injection` audit event. Until
now only the helpers (`_fetch_fragments_to_worklist`, the notes regex, manual list-splice math)
had tests — see `tests/eu201_integration_test.py`. Nobody drove the actual `_run_inner` while-loop,
so the reviewer flagged the wiring itself as unverified at land time.

This harness drives `loop._run_inner` for real, stubbing only the boundary the loop can't run
without (git, the backlog, and `process_ticket` itself — the officers/gate/land pipeline is out of
scope here; EU-201's own wiring is what's under test). It asserts:
  1. the fragments build BEFORE the next queued ticket, in split order (ticket_start order);
  2. a `fragment_injection` audit event fires with the correct `fragment_keys`;
  3. injection also fires when the parent is the ONLY ticket in the worklist (a 1-ticket run).
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path

# Stub the Agent SDK (imported transitively) so import never needs a real model.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import loop
from orchestrator.audit import AuditLog
from orchestrator.config import AppConfig, Config
from orchestrator.contracts import Outcome, Ticket, TicketReport
import orchestrator.backlog.base as backlog_base

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


class _FakeGit:
    """Stands in for git_ops.Git — _run_inner only needs `.ensure_clean()` before it hands off
    to process_ticket (which is stubbed below, so no real checkout/branch work ever happens)."""
    def ensure_clean(self) -> None:
        return None


def _mk_ticket(key: str, app: str, ephemeral: bool = True) -> Ticket:
    return Ticket(id=key, key=key, summary=f"Ticket {key}", description="d",
                 acceptance_criteria=["AC1"], app=app, ephemeral=ephemeral)


def _read_audit_events(audit_path: Path, event: str) -> list[dict]:
    rows = [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == event]


def _make_backlog_stub(fragment_ids: dict[str, Ticket]):
    """A minimal BacklogAdapter whose get_task() resolves fragment keys to Ticket objects —
    exactly what `_fetch_fragments_to_worklist` (loop.py) needs to turn split keys into
    worklist items."""
    class _Stub:
        def get_ready_tasks(self, limit):
            return []
        def get_task(self, key):
            return fragment_ids[key]
        def set_status(self, ticket, status):
            return None
        def add_comment(self, ticket, body):
            return None
    return _Stub()


async def _drive(cfg, app, worklist, audit, build_order, splits):
    """Run `loop._run_inner` with `loop.process_ticket` and `loop._make_git` stubbed.

    `splits`: {ticket_id: [fragment_key, ...]} — tickets that should REQUEUE with a
    "split into ..." note listing those fragment keys. Every ticket_id NOT in `splits`
    resolves as MERGED.
    """
    orig_process_ticket = loop.process_ticket
    orig_make_git = loop._make_git

    async def stub_process_ticket(ticket, app, cfg, git, backlog, audit, budget, stop_event=None):
        build_order.append(ticket.id)
        audit.record("ticket_start", ticket_id=ticket.id, app=app.name)
        if ticket.id in splits:
            keys = ", ".join(splits[ticket.id])
            return TicketReport(ticket.id, Outcome.REQUEUED, 1, 0.0, app.name,
                                notes=f"too big — Scrum Master split into {keys}")
        return TicketReport(ticket.id, Outcome.MERGED, 1, 0.0, app.name)

    loop.process_ticket = stub_process_ticket
    loop._make_git = lambda cfg, app: _FakeGit()
    try:
        return await loop._run_inner(cfg, worklist, audit, stop_event=None)
    finally:
        loop.process_ticket = orig_process_ticket
        loop._make_git = orig_make_git


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------------- #
# Test 1: fragments build BEFORE the next queued ticket, in split order
# ------------------------------------------------------------------------- #
d1 = Path(tempfile.mkdtemp())
audit_path_1 = d1 / "audit.jsonl"
audit_path_1.write_text("")

app1 = AppConfig(name="automatixy", repo_path=str(d1), base_branch="DEV", protected_branch="MAIN",
                 backlog_backend="jira", backlog={"base_url": "x", "project_key": "AUTO"})
cfg1 = Config(apps=[app1], audit_path=str(audit_path_1), use_worktree=False, dry_run=True,
             max_iterations=1)

parent = _mk_ticket("AUTO-100", "automatixy")
next_ticket = _mk_ticket("AUTO-200", "automatixy")
frag1 = _mk_ticket("AUTO-101", "automatixy")
frag2 = _mk_ticket("AUTO-102", "automatixy")

orig_make_backlog_1 = backlog_base.make_backlog
backlog_base.make_backlog = lambda app: _make_backlog_stub({"AUTO-101": frag1, "AUTO-102": frag2})
try:
    build_order_1: list[str] = []
    audit1 = AuditLog(audit_path_1)
    worklist_1 = [(app1, parent), (app1, next_ticket)]
    reports_1 = _run(_drive(cfg1, app1, worklist_1, audit1, build_order_1,
                           splits={"AUTO-100": ["AUTO-101", "AUTO-102"]}))

    chk("multi-ticket: build order is parent, fragments, then next ticket",
       build_order_1 == ["AUTO-100", "AUTO-101", "AUTO-102", "AUTO-200"],
       f"got {build_order_1}")
    chk("multi-ticket: fragments build before the next queue ticket",
       build_order_1.index("AUTO-102") < build_order_1.index("AUTO-200"))
    chk("multi-ticket: all 4 tickets produced a report",
       len(reports_1) == 4, f"got {len(reports_1)} reports")

    injections_1 = _read_audit_events(audit_path_1, "fragment_injection")
    chk("multi-ticket: exactly one fragment_injection audit event", len(injections_1) == 1,
       f"got {len(injections_1)}")
    if injections_1:
        evt = injections_1[0]
        chk("multi-ticket: fragment_injection names the parent ticket",
           evt.get("ticket_id") == "AUTO-100", f"got {evt.get('ticket_id')}")
        chk("multi-ticket: fragment_injection carries the correct fragment_keys",
           evt.get("fragment_keys") == ["AUTO-101", "AUTO-102"], f"got {evt.get('fragment_keys')}")
        chk("multi-ticket: fragment_injection carries the correct fragment_count",
           evt.get("fragment_count") == 2, f"got {evt.get('fragment_count')}")
finally:
    backlog_base.make_backlog = orig_make_backlog_1


# ------------------------------------------------------------------------- #
# Test 2: injection also fires on a 1-ticket worklist (parent-only, no next ticket)
# ------------------------------------------------------------------------- #
d2 = Path(tempfile.mkdtemp())
audit_path_2 = d2 / "audit.jsonl"
audit_path_2.write_text("")

app2 = AppConfig(name="automatixy", repo_path=str(d2), base_branch="DEV", protected_branch="MAIN",
                 backlog_backend="jira", backlog={"base_url": "x", "project_key": "AUTO"})
cfg2 = Config(apps=[app2], audit_path=str(audit_path_2), use_worktree=False, dry_run=True,
             max_iterations=1)

solo_parent = _mk_ticket("AUTO-300", "automatixy")
frag3 = _mk_ticket("AUTO-301", "automatixy")

orig_make_backlog_2 = backlog_base.make_backlog
backlog_base.make_backlog = lambda app: _make_backlog_stub({"AUTO-301": frag3})
try:
    build_order_2: list[str] = []
    audit2 = AuditLog(audit_path_2)
    worklist_2 = [(app2, solo_parent)]   # ONLY the parent — no other queued ticket
    reports_2 = _run(_drive(cfg2, app2, worklist_2, audit2, build_order_2,
                           splits={"AUTO-300": ["AUTO-301"]}))

    chk("1-ticket run: the fragment still builds after the solo parent",
       build_order_2 == ["AUTO-300", "AUTO-301"], f"got {build_order_2}")
    chk("1-ticket run: both the parent and the fragment produced a report",
       len(reports_2) == 2, f"got {len(reports_2)} reports")

    injections_2 = _read_audit_events(audit_path_2, "fragment_injection")
    chk("1-ticket run: fragment_injection still fires with a 1-ticket worklist",
       len(injections_2) == 1, f"got {len(injections_2)}")
    if injections_2:
        chk("1-ticket run: fragment_injection carries the correct fragment_keys",
           injections_2[0].get("fragment_keys") == ["AUTO-301"], f"got {injections_2[0].get('fragment_keys')}")
finally:
    backlog_base.make_backlog = orig_make_backlog_2


print("\n============ EU-220 / EU-201 _run_inner e2e TEST ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
