"""EU-253 — per-ticket file logging is wired into EVERY drain (cockpit AND automode), not just the
two manual cockpit endpoints — AND the log handle is registered under the SAME key the live stdout
Tee writes to, so automode lands actually land lines on disk (iteration-1 keyed on app.name while a
unit-wide drain's Tee keys on None → empty files; this is the iteration-2 fix).

Pre-fix: ``run_logger.open_run_log`` was only bracketed around the whole run in server.py's
``/api/run-tickets`` / ``/api/run`` handlers. Automode (``autopilot.autopilot()`` -> ``run_loop`` ->
``loop._run_inner``) never opened a log, so every automode land wrote zero on-disk transcript — and
an external/CLI daemon (no cockpit process) never even installed the ``_Tee`` that feeds
``run_logger.write_line``, so a log opened there would have stayed empty anyway.

The fix moves the open/close bracket to be PER-TICKET inside ``loop._run_inner``'s worklist loop (the
single seam every drain funnels through), registers the handle under the ACTIVE run key that
``cockpit_state._Tee.write`` looks up (``active_runs()[0]`` when one run is active, else ``None``)
while keeping the on-disk path derived from the ticket's app, and installs the ``_Tee`` idempotently
at the top of ``autopilot.autopilot()`` for the external/CLI path. Tests:

  1. Unit-wide drain (``claim_run(None)`` — the automode all-apps default): a stubbed one-ticket cycle
     creates the dated per-ticket file under the ticket's APP path, and that file EXISTS AND CONTAINS
     the Tee-captured stdout line (registered under the None key the Tee actually wrote to), then the
     None-keyed handle is closed at cycle end.
  2. Per-app drain (``claim_run(<app>)``): the effective key follows the single active run, so the
     line still lands and the handle closes.
  3. Multi-ticket unit-wide drain: each ticket gets its OWN dated file; a ticket whose processing
     raises still closes its handle and its output lands in ITS OWN file (not lost / not mixed).
  4. Concurrent multi-app drains (``len(active_runs()) > 1``): the Tee collapses attribution to the
     shared None key, so a dedicated per-ticket handle can't be isolated — the loop must NOT silently
     emit an empty per-ticket file. Instead it drops a dated, NON-EMPTY note file explaining the gap,
     and leaves no leaked handle.
  5. External/CLI path: autopilot() installs ``_Tee`` on ``sys.stdout`` idempotently (no double-wrap)
     and captured lines reach ``run_logger.write_line``.
  6. Structural: server.py no longer brackets the whole run; loop.py carries the per-ticket bracket
     keyed on the active run; autopilot.py installs the Tee behind an isinstance guard.

Offline — the SDK is stubbed; no network, no real models.
"""
import asyncio
import datetime
import sys
import tempfile
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)
sys.path.insert(0, ".")

import orchestrator.loop as loop
from orchestrator import autopilot as _autopilot
from orchestrator import cockpit_state, run_logger
from orchestrator.config import AppConfig, Config
from orchestrator.contracts import Outcome, Ticket, TicketReport

results = []
def chk(name, cond, detail=""):
    results.append((name, bool(cond), str(detail)))


class _Audit:
    def record(self, *a, **k):
        pass


class DummyGit:
    def ensure_clean(self):
        pass


def _make_cfg(tmp: Path, app_name: str = "testapp") -> tuple[Config, AppConfig]:
    app = AppConfig(name=app_name, repo_path=str(tmp), base_branch="dev",
                    protected_branch="main", backlog_backend="none")
    cfg = Config(apps=[app], audit_path=str(tmp / "events.jsonl"), log_folder="logs/")
    return cfg, app


def _install_tee():
    """Install cockpit_state._Tee on sys.stdout for the duration of the test section."""
    orig = sys.stdout
    sys.stdout = cockpit_state._Tee(orig)
    return orig


def _restore(orig):
    sys.stdout = orig


def _log_files(tmp: Path, app_name: str):
    root = tmp / "logs" / app_name
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.log"))


def _stub_ok_process():
    async def _fake(ticket, app, cfg, git, backlog, audit, budget, stop_event=None):
        print(f"[{ticket.id}] doing the work")
        return TicketReport(ticket.id, Outcome.MERGED, 0, 0.0, app.name)
    return _fake


# ---------------------------------------------------------------------------
# 1. Unit-wide drain (claim_run(None)): the automode all-apps default. The handle must be registered
#    under None (what the Tee writes to) even though the file lives under the ticket's app path — so
#    the file EXISTS AND CONTAINS the captured line, not merely "a file was created".
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cfg, app = _make_cfg(tmp)
    cockpit_state.reset_run_state()

    loop._notify = lambda cfg, t: None
    loop._make_git = lambda cfg, app: DummyGit()
    loop.process_ticket = _stub_ok_process()

    ticket = Ticket(id="AUTO-901", key="AUTO-901", summary="s", description="d",
                    ephemeral=True, app=app.name)

    _orig_stdout = _install_tee()
    try:
        cockpit_state.claim_run(None)   # unit-wide / all-apps drain — the key the Tee will use
        try:
            reports = asyncio.run(loop.run(cfg, [(app, ticket)], _Audit()))
        finally:
            cockpit_state.release_run(None)
    finally:
        _restore(_orig_stdout)

    chk("unit-wide: MERGED report", bool(reports) and reports[0].outcome == Outcome.MERGED,
        reports[0].outcome if reports else "no report")

    files = _log_files(tmp, app.name)
    chk("unit-wide: exactly one dated log file under the app path", len(files) == 1,
        [str(f) for f in files])
    if files:
        chk("unit-wide: filename starts with ticket id", files[0].name.startswith("AUTO-901-"))
        chk("unit-wide: dated day-folder",
            files[0].parent.name == datetime.date.today().isoformat())
        content = files[0].read_text(encoding="utf-8")
        # The teeth: iteration-1 keyed the handle on app.name, so the None-keyed Tee line never
        # landed and this file was EMPTY. The fix keys on the active run (None) so the line is here.
        chk("unit-wide: file EXISTS and CONTAINS the Tee-captured line (not just created)",
            "[AUTO-901] doing the work" in content, repr(content))

    with run_logger._lock:
        chk("unit-wide: None-keyed handle closed after the cycle",
            None not in run_logger._log_handles)

    cockpit_state.reset_run_state()


# ---------------------------------------------------------------------------
# 2. Per-app drain (claim_run(<app>)): the effective key follows the single active run's concrete
#    name, so the line lands and the handle closes under that name.
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cfg, app = _make_cfg(tmp)
    cockpit_state.reset_run_state()

    loop._notify = lambda cfg, t: None
    loop._make_git = lambda cfg, app: DummyGit()
    loop.process_ticket = _stub_ok_process()

    ticket = Ticket(id="AUTO-950", key="AUTO-950", summary="s", description="d",
                    ephemeral=True, app=app.name)

    _orig_stdout = _install_tee()
    try:
        cockpit_state.claim_run(app.name)   # per-app drain — active_runs()==[app.name]
        try:
            reports = asyncio.run(loop.run(cfg, [(app, ticket)], _Audit()))
        finally:
            cockpit_state.release_run(app.name)
    finally:
        _restore(_orig_stdout)

    files = _log_files(tmp, app.name)
    chk("per-app: one dated log file", len(files) == 1, [str(f) for f in files])
    if files:
        content = files[0].read_text(encoding="utf-8")
        chk("per-app: file CONTAINS the Tee-captured line", "[AUTO-950] doing the work" in content,
            repr(content))

    with run_logger._lock:
        chk("per-app: handle closed under the app key", app.name not in run_logger._log_handles)

    cockpit_state.reset_run_state()


# ---------------------------------------------------------------------------
# 3. Multi-ticket unit-wide drain: each ticket gets its OWN dated file; a ticket whose processing
#    raises still closes its handle and its output lands in ITS OWN file, not the other ticket's.
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cfg, app = _make_cfg(tmp)
    cockpit_state.reset_run_state()

    loop._notify = lambda cfg, t: None
    loop._make_git = lambda cfg, app: DummyGit()

    async def _fake_process_multi(ticket, app, cfg, git, backlog, audit, budget, stop_event=None):
        if ticket.id == "AUTO-902":
            print(f"[{ticket.id}] built fine")
            return TicketReport(ticket.id, Outcome.MERGED, 0, 0.0, app.name)
        print(f"[{ticket.id}] simulated builder failure output")
        raise RuntimeError("boom — builder crashed")
    loop.process_ticket = _fake_process_multi

    t1 = Ticket(id="AUTO-902", key="AUTO-902", summary="s", description="d",
                ephemeral=True, app=app.name)
    t2 = Ticket(id="AUTO-903", key="AUTO-903", summary="s", description="d",
                ephemeral=True, app=app.name)

    _orig_stdout = _install_tee()
    try:
        cockpit_state.claim_run(None)
        try:
            reports = asyncio.run(loop.run(cfg, [(app, t1), (app, t2)], _Audit()))
        finally:
            cockpit_state.release_run(None)
    finally:
        _restore(_orig_stdout)

    chk("multi-ticket: two reports returned", len(reports) == 2, len(reports))
    chk("multi-ticket: 1st MERGED", reports[0].outcome == Outcome.MERGED if reports else False)
    chk("multi-ticket: 2nd ERRORED (exception caught, not propagated)",
        reports[1].outcome == Outcome.ERRORED if len(reports) > 1 else False,
        reports[1].outcome if len(reports) > 1 else "n/a")

    files = _log_files(tmp, app.name)
    chk("multi-ticket: two separate log files created", len(files) == 2, [str(f) for f in files])
    f1 = next((f for f in files if f.name.startswith("AUTO-902-")), None)
    f2 = next((f for f in files if f.name.startswith("AUTO-903-")), None)
    chk("multi-ticket: AUTO-902's own file exists", f1 is not None)
    chk("multi-ticket: AUTO-903's own file exists", f2 is not None)
    if f1 and f2:
        c1, c2 = f1.read_text(encoding="utf-8"), f2.read_text(encoding="utf-8")
        chk("multi-ticket: AUTO-902's line is in AUTO-902's file", "[AUTO-902] built fine" in c1, c1)
        chk("multi-ticket: AUTO-902's file does NOT contain AUTO-903's line", "AUTO-903" not in c1, c1)
        chk("multi-ticket: AUTO-903's failure output flushed to ITS OWN file (raised but still logged)",
            "[AUTO-903] simulated builder failure output" in c2, c2)
        chk("multi-ticket: AUTO-903's file does NOT contain AUTO-902's line", "AUTO-902" not in c2, c2)

    with run_logger._lock:
        chk("multi-ticket: no leftover open handle after the cycle (both closed)",
            None not in run_logger._log_handles)

    cockpit_state.reset_run_state()


# ---------------------------------------------------------------------------
# 4. Concurrent multi-app drains: two runs active at once means the Tee attributes lines to the shared
#    None key, so a dedicated per-ticket handle can't be isolated. The loop must NOT silently emit an
#    empty per-ticket file — it drops a dated, NON-EMPTY note file explaining the collapse, and leaks
#    no handle.
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cfg, app = _make_cfg(tmp, app_name="alpha")
    cockpit_state.reset_run_state()

    loop._notify = lambda cfg, t: None
    loop._make_git = lambda cfg, app: DummyGit()
    loop.process_ticket = _stub_ok_process()

    ticket = Ticket(id="AUTO-960", key="AUTO-960", summary="s", description="d",
                    ephemeral=True, app=app.name)

    _orig_stdout = _install_tee()
    try:
        # TWO drains in flight simultaneously → active_runs() has length 2 → ambiguous attribution.
        cockpit_state.claim_run("alpha")
        cockpit_state.claim_run("beta")
        chk("concurrent: two runs active (attribution collapses to None)",
            len(cockpit_state.active_runs()) == 2, cockpit_state.active_runs())
        try:
            reports = asyncio.run(loop.run(cfg, [(app, ticket)], _Audit()))
        finally:
            cockpit_state.release_run("alpha")
            cockpit_state.release_run("beta")
    finally:
        _restore(_orig_stdout)

    files = _log_files(tmp, "alpha")
    chk("concurrent: a dated per-ticket file still exists (not skipped)", len(files) == 1,
        [str(f) for f in files])
    if files:
        content = files[0].read_text(encoding="utf-8")
        chk("concurrent: the file is NOT silently empty", content.strip() != "", repr(content))
        chk("concurrent: the file explains the ambiguity (EU-253 note)",
            "EU-253" in content and "AUTO-960" in content, repr(content))

    # No handle may leak under None (we wrote a standalone note, we did not register a handle).
    with run_logger._lock:
        chk("concurrent: no handle registered/leaked under None",
            None not in run_logger._log_handles)

    cockpit_state.reset_run_state()


# ---------------------------------------------------------------------------
# 5. External/CLI path: autopilot() installs _Tee on sys.stdout idempotently, and captured lines
#    reach run_logger.write_line.
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cockpit_state.reset_run_state()
    _orig_stdout = sys.stdout
    _orig_pid = _autopilot._PID_FILE
    _autopilot._PID_FILE = tmp / "ap.pid"
    _orig_write_line = run_logger.write_line
    _write_calls = []
    def _recording_write_line(app_key, line):
        _write_calls.append((app_key, line))
    run_logger.write_line = _recording_write_line
    try:
        cfg_ap = Config(apps=[], audit_path=str(tmp / "events.jsonl"))
        asyncio.run(_autopilot.autopilot(cfg_ap, once=True))
        chk("external path: sys.stdout wrapped in _Tee after autopilot()",
            isinstance(sys.stdout, cockpit_state._Tee))
        _after_first = sys.stdout

        asyncio.run(_autopilot.autopilot(cfg_ap, once=True))
        chk("external path: second call does NOT double-wrap (same Tee instance)",
            sys.stdout is _after_first)
        chk("external path: inner ._real is not itself a _Tee (no nesting)",
            not isinstance(getattr(sys.stdout, "_real", None), cockpit_state._Tee))

        chk("external path: captured stdout lines reached run_logger.write_line",
            len(_write_calls) > 0, len(_write_calls))
    finally:
        run_logger.write_line = _orig_write_line
        _autopilot._PID_FILE = _orig_pid
        sys.stdout = _orig_stdout
        cockpit_state.reset_run_state()


# ---------------------------------------------------------------------------
# 6. Structural: server.py's /api/run and /api/run-tickets no longer bracket the WHOLE run;
#    loop.py's per-ticket bracket keyed on the active run is the only one left; autopilot installs
#    the Tee behind an isinstance guard.
# ---------------------------------------------------------------------------
_server_src = Path("orchestrator/server.py").read_text(encoding="utf-8")
chk("server.py: no more open_run_log calls (whole-run bracket removed)",
    "open_run_log(" not in _server_src)
chk("server.py: no more close_run_log calls (whole-run bracket removed)",
    "close_run_log(" not in _server_src)

_loop_src = Path("orchestrator/loop.py").read_text(encoding="utf-8")
chk("loop.py: open_run_log is called inside _run_inner keyed on the active run",
    "run_logger.open_run_log(cfg, app.name, ticket.id, run_key=_log_key)" in _loop_src)
chk("loop.py: close_run_log uses the same run_key (per-ticket teardown)",
    "run_logger.close_run_log(run_key=_log_key)" in _loop_src)
chk("loop.py: effective key mirrors the Tee (active_runs()[0] when one run, else None)",
    "_active[0] if len(_active) == 1 else None" in _loop_src)
chk("loop.py: concurrent-drain ambiguity handled via a note file (no silent empty log)",
    "write_note_log" in _loop_src)

_autopilot_src = Path("orchestrator/autopilot.py").read_text(encoding="utf-8")
chk("autopilot.py: installs _Tee idempotently (isinstance guard)",
    "isinstance(sys.stdout, cockpit_state._Tee)" in _autopilot_src)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
print("\n================= EU-253 automode run-log QA =================")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    mark = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{mark}] {name}{extra}")
print("-----------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
