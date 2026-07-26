"""EU-635 — concurrent _worker(): a real per-ticket log handle that actually RECEIVES the lines.

Verifies that each picked ticket's per-ticket log file is opened via open_run_log
(registered in _log_handles under the (app.name, slot) tuple), not written once via
write_note_log; that the handle is closed when process_ticket returns or raises (no leaked
handles across tickets sharing a slot); and — the routing half of the PM decision — that the
_RUN_LOG_KEY ContextVar set by _worker routes each slot's captured stdout into ITS ticket's
file: a line written under a co-scheduled slot lands in that slot's file, never another
slot's, while UI attribution (_LOG / recent_log) stays on the bare app name (EU-104/272
unchanged). When open_run_log itself raises, the ticket still runs (exception swallowed)
and close_run_log is NOT called for that ticket.
"""
from __future__ import annotations

import asyncio
import datetime
import sys
import tempfile
import types
import uuid
from pathlib import Path
from types import SimpleNamespace

sdk = types.ModuleType("claude_agent_sdk")
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", sdk)

sys.path.insert(0, ".")

from orchestrator import cockpit_state, loop, run_logger  # noqa: E402
from orchestrator.config import Config, AppConfig  # noqa: E402
from orchestrator.contracts import Outcome, TicketReport  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


_tmp = Path(tempfile.mkdtemp())

# ---------------------------------------------------------------------------
# Recorder helpers -----------------------------------------------------------
# ---------------------------------------------------------------------------

class Recorder:
    """Records calls and arguments."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.args_list: list[list] = []
        self.kwargs_list: list[dict] = []

    def record(self, *args, **kwargs):
        self.calls.append(True)
        self.args_list.append(list(args))
        self.kwargs_list.append(dict(kwargs))

    @property
    def count(self):
        return len(self.calls)


# ---------------------------------------------------------------------------
# Config + apps ------------------------------------------------------------
# ---------------------------------------------------------------------------

def mkcfg(n=2) -> Config:
    c = Config(apps=[], audit_path=str(_tmp / "audit.jsonl"))
    c.max_concurrent_builders = n
    c.use_worktree = False  # no real git in this harness
    c.max_cost_usd = 0
    c.concurrent_min_free_gb = 0  # disable the memory-floor guard so slot>0 never sleeps in tests
    return c


APP_A = AppConfig(name="alpha", repo_path=str(_tmp / "a"), base_branch="dev",
                  protected_branch="main", backlog_backend="none")


def T(tid, desc="", app="alpha"):
    from orchestrator.contracts import Ticket
    return Ticket(id=tid, key=tid, summary=tid, description=desc, app=app, ephemeral=True)


class _Audit:
    def __init__(self):
        self.events = []

    def record(self, event, **k):
        self.events.append((event, k))


# ---------------------------------------------------------------------------
# Fake process_ticket_inner that records which tickets ran ------------------
# ---------------------------------------------------------------------------

process_order: list[str] = []


async def _fake_process_ok(ticket, app, cfg, git, backlog, audit, budget, stop_event=None):
    process_order.append(ticket.id)
    await asyncio.sleep(0.02)
    return TicketReport(ticket.id, Outcome.MERGED, 1, 0.0, app.name, notes="")


async def _fake_process_raise(ticket, app, cfg, git, backlog, audit, budget, stop_event=None):
    process_order.append(ticket.id + "_raise")
    raise RuntimeError(f"simulated failure for {ticket.id}")


# ---------------------------------------------------------------------------
# TEST 1 — open_run_log called, write_note_log never called ---------------
# ---------------------------------------------------------------------------

print("\n=== TEST 1: open_run_log called, write_note_log zero ===")

note_recorder = Recorder()
open_recorder = Recorder()
close_recorder = Recorder()


def _stub_open(cfg, app_name, ticket_key, *, run_key=None):
    open_recorder.record(cfg, app_name, ticket_key, run_key=run_key)


def _stub_close(*args, **kwargs):
    close_recorder.record(*args, **kwargs)


_orig_process = loop._process_ticket_inner
_orig_note = loop.run_logger.write_note_log
_orig_open = getattr(loop.run_logger, "open_run_log", None)
_orig_close = getattr(loop.run_logger, "close_run_log", None)
_orig_mkgit = loop._make_git

loop._process_ticket_inner = _fake_process_ok
loop.run_logger.write_note_log = note_recorder.record
loop.run_logger.open_run_log = _stub_open
loop.run_logger.close_run_log = _stub_close
loop._make_git = lambda cfg, app, slot=0: SimpleNamespace(ensure_clean=lambda: None)

try:
    reports = asyncio.run(loop._run_inner(
        mkcfg(2), [(APP_A, T("T-1")), (APP_A, T("T-2"))], _Audit()))

    ok("write_note_log called ZERO times from _worker path",
       note_recorder.count == 0,
       f"expected 0 got {note_recorder.count}")

    ok("open_run_log called exactly ONCE per ticket (2)",
       open_recorder.count == 2,
       f"expected 2 got {open_recorder.count}")

    ok("close_run_log called exactly ONCE per ticket (2)",
       close_recorder.count == 2,
       f"expected 2 got {close_recorder.count}")

    ok("open_run_log args order: (cfg, app.name, ticket.id)",
       open_recorder.args_list[0][1] == "alpha" and open_recorder.args_list[0][2] == "T-1",
       f"{open_recorder.args_list}")

    ok("open_run_log run_key == (app.name, slot) tuple present",
       isinstance(open_recorder.kwargs_list[0].get("run_key"), tuple)
       and len(open_recorder.kwargs_list[0]["run_key"]) == 2,
       f"{open_recorder.kwargs_list[0]}")

except Exception as exc:
    print(f"  ✗ TEST 1 raised: {exc}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# TEST 2 — balanced open/close across 2 sequential picks by SAME slot ----
# ---------------------------------------------------------------------------

print("\n=== TEST 2: balanced open/close across 2 tickets (same slot) ===")

open_recorder.calls.clear()
open_recorder.args_list.clear()
open_recorder.kwargs_list.clear()
close_recorder.calls.clear()
close_recorder.args_list.clear()
close_recorder.kwargs_list.clear()

# With N=2 slots and 2 tickets both for alpha, the first pick gets slot 0,
# the second pick may get slot 0 or 1 depending on scheduling; we verify
# balance regardless.
process_order.clear()
reports = asyncio.run(loop._run_inner(
    mkcfg(2), [(APP_A, T("S-1")), (APP_A, T("S-2"))], _Audit()))

ok("open counts == close counts (no leaked handles)",
   open_recorder.count == close_recorder.count,
   f"open={open_recorder.count}, close={close_recorder.count}")

ok("both tickets ran to completion report",
   len(reports) == 2,
   f"got {len(reports)}")

# ---------------------------------------------------------------------------
# TEST 3 — when open_run_log RAISES, ticket still runs, close NOT called ---
# ---------------------------------------------------------------------------

print("\n=== TEST 3: open_run_log raises → ticket still runs, no close ===")

open_recorder.calls.clear()
open_recorder.args_list.clear()
open_recorder.kwargs_list.clear()
close_recorder.calls.clear()
close_recorder.args_list.clear()
close_recorder.kwargs_list.clear()


def _stub_open_raises(cfg, app_name, ticket_key, *, run_key=None):
    if ticket_key == "F-1":
        raise FileNotFoundError("simulated log dir missing")
    open_recorder.record(cfg, app_name, ticket_key, run_key=run_key)


loop.run_logger.open_run_log = _stub_open_raises

process_order.clear()
reports = asyncio.run(loop._run_inner(
    mkcfg(2), [(APP_A, T("F-1")), (APP_A, T("F-2"))], _Audit()))

by_id = {r.ticket_id: r for r in reports}
ok("even though open failed, F-1 still produced a report",
   "F-1" in by_id, str(by_id))
ok("F-2 still opened its log successfully",
   any(isinstance(kw.get("run_key"), tuple) for kw in open_recorder.kwargs_list),
   f"open_kw={open_recorder.kwargs_list}")

ok("F-1 has ZERO close calls (handle was never opened)",
   not any(kw.get("run_key", (None,))[0] == "F-1" for kw in close_recorder.kwargs_list),
   f"close_args={close_recorder.args_list}, close_kw={close_recorder.kwargs_list}")

ok("exactly ONE close call overall (only F-2's handle was ever opened)",
   close_recorder.count == 1,
   f"got {close_recorder.count}")

# ---------------------------------------------------------------------------
# RESTORE --------------------------------------------------------------------
# ---------------------------------------------------------------------------

loop._process_ticket_inner = _orig_process
loop.run_logger.write_note_log = _orig_note
if _orig_open is not None:
    loop.run_logger.open_run_log = _orig_open
if _orig_close is not None:
    loop.run_logger.close_run_log = _orig_close
loop._make_git = _orig_mkgit

# ---------------------------------------------------------------------------
# TEST 4 — routing seam: a co-scheduled slot's line lands in ITS file ------
# ---------------------------------------------------------------------------

print("\n=== TEST 4: _Tee routes lines via _RUN_LOG_KEY to the slot's own file ===")

# Two SAME-APP slot handles open at the same time — the exact clobber scenario the tuple
# key exists for. Real open_run_log / close_run_log against the tmp log root from here on.
rl_cfg = mkcfg(2)
path0 = run_logger.open_run_log(rl_cfg, "alpha", "LK-1", run_key=("alpha", 0))
path1 = run_logger.open_run_log(rl_cfg, "alpha", "LK-2", run_key=("alpha", 1))

ok("both slot handles registered under their distinct tuple keys",
   ("alpha", 0) in run_logger._log_handles and ("alpha", 1) in run_logger._log_handles,
   str(list(run_logger._log_handles)))

tee = cockpit_state._Tee(sys.stdout)
tok_app = cockpit_state.set_run_app("alpha")   # process_ticket binds this — UI attribution
try:
    tok0 = cockpit_state.set_run_log_key(("alpha", 0))
    try:
        tee.write("seam-line slot0 LK-1\n")
    finally:
        cockpit_state.reset_run_log_key(tok0)
    tok1 = cockpit_state.set_run_log_key(("alpha", 1))
    try:
        tee.write("seam-line slot1 LK-2\n")
    finally:
        cockpit_state.reset_run_log_key(tok1)
finally:
    cockpit_state.reset_run_app(tok_app)

run_logger.close_run_log(run_key=("alpha", 0))
run_logger.close_run_log(run_key=("alpha", 1))

content0 = path0.read_text(encoding="utf-8")
content1 = path1.read_text(encoding="utf-8")

ok("slot 0's file carries slot 0's line", "seam-line slot0 LK-1" in content0, content0)
ok("slot 0's file has NONE of slot 1's line (no cross-routing)",
   "seam-line slot1 LK-2" not in content0, content0)
ok("slot 1's file carries slot 1's line", "seam-line slot1 LK-2" in content1, content1)
ok("slot 1's file has NONE of slot 0's line (no clobber)",
   "seam-line slot0 LK-1" not in content1, content1)

feed = cockpit_state.recent_log(app="alpha")
ok("UI live-feed still attributes BOTH lines to the bare app name (EU-104/272 unchanged)",
   any("seam-line slot0 LK-1" in ln for ln in feed)
   and any("seam-line slot1 LK-2" in ln for ln in feed),
   str(feed[-4:]))

ok("reset left _RUN_LOG_KEY unset (serial path keeps its app_key fallback)",
   cockpit_state._RUN_LOG_KEY.get() is None)

# ---------------------------------------------------------------------------
# TEST 5 — end-to-end: concurrent _worker routes each ticket's stdout ------
# ---------------------------------------------------------------------------

print("\n=== TEST 5: end-to-end concurrent _worker — each ticket's lines in ITS file ===")

APP_B = AppConfig(name="beta", repo_path=str(_tmp / "b"), base_branch="dev",
                  protected_branch="main", backlog_backend="none")


async def _fake_process_print(ticket, app, cfg, git, backlog, audit, budget, stop_event=None):
    # Two prints bracketing an await: the slot's ContextVar must survive the await, and both
    # lines must land in this ticket's file. Different apps → no footprint conflict → the two
    # tickets are genuinely co-scheduled, one per slot.
    print(f"e2e-line {ticket.id} from {app.name}", flush=True)
    await asyncio.sleep(0.05)
    print(f"e2e-done {ticket.id}", flush=True)
    return TicketReport(ticket.id, Outcome.MERGED, 1, 0.0, app.name, notes="")


loop._process_ticket_inner = _fake_process_print
loop._make_git = lambda cfg, app, slot=0: SimpleNamespace(ensure_clean=lambda: None)

_real_stdout = sys.stdout
sys.stdout = cockpit_state._Tee(_real_stdout)   # the production seam: stdout → _Tee
try:
    reports = asyncio.run(loop._run_inner(
        mkcfg(2), [(APP_A, T("E-1")), (APP_B, T("E-2", app="beta"))], _Audit()))
finally:
    sys.stdout = _real_stdout
    loop._process_ticket_inner = _orig_process
    loop._make_git = _orig_mkgit

ok("both co-scheduled tickets reported", len(reports) == 2, f"got {len(reports)}")

_date = datetime.datetime.now().strftime("%Y-%m-%d")
e1_files = list((_tmp / "logs" / "alpha" / _date).glob("E-1-*.log"))
e2_files = list((_tmp / "logs" / "beta" / _date).glob("E-2-*.log"))
ok("E-1 got exactly one per-ticket log file (opened, not noted)",
   len(e1_files) == 1, str(e1_files))
ok("E-2 got exactly one per-ticket log file (opened, not noted)",
   len(e2_files) == 1, str(e2_files))

c1 = e1_files[0].read_text(encoding="utf-8") if e1_files else ""
c2 = e2_files[0].read_text(encoding="utf-8") if e2_files else ""
ok("E-1's file carries BOTH of E-1's lines (ContextVar survives the await)",
   "e2e-line E-1 from alpha" in c1 and "e2e-done E-1" in c1, c1)
ok("E-1's file has NONE of E-2's output (slots don't cross-route)", "E-2" not in c1, c1)
ok("E-2's file carries BOTH of E-2's lines",
   "e2e-line E-2 from beta" in c2 and "e2e-done E-2" in c2, c2)
ok("E-2's file has NONE of E-1's output", "E-1" not in c2, c2)

ok("no log handles leaked after the drain (slot reuse stays clean)",
   not run_logger._log_handles, str(run_logger._log_handles))

print(f"\n{checks}/{checks} passed")
