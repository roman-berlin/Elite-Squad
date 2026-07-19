"""PID-file holder refcount QA — the 2026-07-15 "badge lies mid-run" class.

Incident: a cockpit-started per-app drain (pid 40582, Elite-Unit) was live building EU-321 when
/tmp/general-autopilot.pid vanished with NO autopilot_stop in the audit trail — daemon_running(),
the single source of truth for the cockpit ON/OFF badge (EU-73) and the daemon_is_external()
start guards, read False during a live run. Root cause: the EU-321 build's own gate ran this
repo's test suite, and three harnesses executed the REAL autopilot() against the REAL machine-
global PID file — each run overwrote the live drain's pid with the suite's pid, then deleted the
file in its finally (the contents==getpid() ownership check passes for the process that just
wrote it). The same deletion class exists INSIDE one cockpit process: every per-app drain thread
shares one process-wide PID file and the same getpid(), so the first drain to exit deleted the
file while a sibling drain still ran.

Three pins, no network, no real SDK:

  (1) UNIT — _write_pid()/_remove_pid() are holder-refcounted: with two holders registered, the
      first _remove_pid() must KEEP the file; only the last holder's exit unlinks it. Unpaired
      removes never drive the count negative, and a file owned by a FOREIGN process is never
      deleted (the cross-process ownership check survives the refcount).

  (2) INTEGRATION — two overlapping REAL per-app autopilot() drains in ONE process (each in its
      own thread, the cockpit's exact Start pattern): the first drain to stand down must NOT
      delete the PID file nor flip daemon_running() False while the second is still live; the
      second's exit removes it.

  (3) REPO GUARD — every tests/*.py that invokes the real autopilot() coroutine must redirect
      _PID_FILE off /tmp first (assignment or patch.object), so no future harness can hijack a
      live daemon's PID file again.
"""
import asyncio
import os
import re
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

# ── minimal SDK / requests stubs so the orchestrator can import without real deps ──
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers={}, update=lambda *a, **k: None)
sys.modules.setdefault("requests", req)
sys.path.insert(0, ".")

from orchestrator import autopilot
from orchestrator.config import Config

tmp = Path(tempfile.mkdtemp())
# NEVER touch the machine-global /tmp/general-autopilot.pid — that hijack IS the bug under test.
autopilot._PID_FILE = tmp / "general-autopilot.pid"

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), str(d)))

_PID = str(os.getpid())

# EU-368 turned the PID file from a bare int into a {"pid", "start"} identity record (a recycled pid
# made a dead daemon read as alive). These pins are about WHOSE pid the file records, not how it's
# spelled, so they read the pid back through the record parser instead of string-matching the bytes.
def _recorded_pid():
    rec = autopilot._read_pid_record()
    return str(rec[0]) if rec else None


# =============================================================================
# (1) UNIT — holder refcount semantics on _write_pid()/_remove_pid()
# =============================================================================

# Two holders: the first remove must keep the file (THE regression pin), the last removes it.
autopilot._write_pid()
chk("unit: first _write_pid() creates the file", autopilot._PID_FILE.exists())
chk("unit: file records THIS pid", _recorded_pid() == _PID,
    autopilot._PID_FILE.read_text() if autopilot._PID_FILE.exists() else "<missing>")
autopilot._write_pid()      # sibling drain in the same process registers a second hold
autopilot._remove_pid()     # first drain exits
chk("unit: first _remove_pid() with a sibling hold KEEPS the file",
    autopilot._PID_FILE.exists())
chk("unit: daemon_running() still True after the first exit", autopilot.daemon_running())
autopilot._remove_pid()     # last drain exits
chk("unit: last _remove_pid() unlinks the file", not autopilot._PID_FILE.exists())
chk("unit: daemon_running() False once the last holder exits", not autopilot.daemon_running())

# Unpaired removes must not drive the count negative (a later write/remove pair still balances).
autopilot._remove_pid()     # unpaired — nothing held, must be a harmless no-op
autopilot._write_pid()
autopilot._remove_pid()
chk("unit: an unpaired _remove_pid() doesn't corrupt the refcount (next pair still balances)",
    not autopilot._PID_FILE.exists())

# Cross-process ownership survives the refcount: a file a FOREIGN process has since overwritten
# is never deleted, even by the last in-process holder's exit (pid 1 = launchd, alive, not ours).
autopilot._write_pid()
autopilot._PID_FILE.write_text("1")
autopilot._remove_pid()
chk("unit: last holder's exit never deletes a FOREIGN process's file",
    autopilot._PID_FILE.exists() and autopilot._PID_FILE.read_text().strip() == "1")
autopilot._PID_FILE.unlink(missing_ok=True)


# =============================================================================
# (2) INTEGRATION — two overlapping REAL per-app drains in ONE process
# =============================================================================

# Offline-safe stubs (the eu232 set): empty queue, no Telegram, no budget gates, fast sleeps.
autopilot.usage.budget_status = lambda c: {"over": False, "alert": False, "used": 0, "cap": 1, "pct": 0.0}
autopilot.usage.plan_limit_hit = lambda c: {"hit": False, "over_limits": []}
autopilot.usage.graceful_stop_check = lambda c: {"should_stop": False}
autopilot.usage.pre_flight_check = lambda c: {"should_skip": False}
autopilot.notify.configured = lambda: False
autopilot.notify.send = lambda *a, **k: None
autopilot._sleep = lambda seconds, stop_event=None: (stop_event.wait(0.02) if stop_event
                                                     else time.sleep(0.02))

in_loop = {"appA": threading.Event(), "appB": threading.Event()}
def _from_drain(cfg, app, n):
    if app in in_loop:
        in_loop[app].set()   # this drain is past setup (past _write_pid) and cycling
    return []
autopilot.intake.from_drain = _from_drain

def _drain(app: str, ev: threading.Event) -> threading.Thread:
    """Start a per-app drain exactly like the cockpit does: autopilot() in a background thread."""
    cfg = Config(apps=[], audit_path=str(tmp / f"{app}.jsonl"))
    t = threading.Thread(
        target=lambda: asyncio.run(autopilot.autopilot(cfg, app_name=app, once=False,
                                                       interval=1, stop_event=ev)),
        daemon=True, name=f"drain-{app}")
    t.start()
    return t

evA, evB = threading.Event(), threading.Event()
tA = _drain("appA", evA)
chk("integration: drain A reaches its loop", in_loop["appA"].wait(timeout=15))
chk("integration: PID file exists while A runs", autopilot._PID_FILE.exists())
tB = _drain("appB", evB)
chk("integration: drain B reaches its loop", in_loop["appB"].wait(timeout=15))

evA.set()                    # stop the FIRST drain while the second is still live
tA.join(timeout=15)
chk("integration: drain A stood down", not tA.is_alive())
chk("integration: A's exit did NOT delete the PID file while B still runs  ← the 2026-07-15 pin",
    autopilot._PID_FILE.exists(),
    "file vanished — first-to-exit deleted the shared PID file")
chk("integration: daemon_running() still True while B runs", autopilot.daemon_running())
chk("integration: file still records THIS process",
    autopilot._PID_FILE.exists() and _recorded_pid() == _PID)

evB.set()                    # now stop the LAST drain
tB.join(timeout=15)
chk("integration: drain B stood down", not tB.is_alive())
chk("integration: last drain's exit removes the PID file", not autopilot._PID_FILE.exists())
chk("integration: daemon_running() False once no drain is live", not autopilot.daemon_running())


# =============================================================================
# (3) REPO GUARD — no harness may run the real autopilot() against the real /tmp PID file
# =============================================================================

# An actual coroutine invocation (`X.autopilot(cfg, ...` — with arguments), not a bare mention in
# a comment line or a string like "autopilot.autopilot()". Any harness that runs the real
# coroutine writes and then deletes _PID_FILE, so it MUST point it somewhere disposable first.
_CALL = re.compile(r"\.autopilot\(\s*[^)\s]")
_leakers = []
for f in sorted(Path(__file__).resolve().parent.glob("*.py")):
    text = f.read_text(encoding="utf-8", errors="replace")
    calls = any(_CALL.search(ln) for ln in text.splitlines()
                if not ln.lstrip().startswith("#"))
    if calls and "_PID_FILE" not in text:
        _leakers.append(f.name)
chk("repo guard: every harness that runs the real autopilot() redirects _PID_FILE off /tmp "
    "(add `autopilot._PID_FILE = <tmpdir> / 'general-autopilot.pid'` before the first run)",
    not _leakers, ", ".join(_leakers))


# =============================================================================
# Summary
# =============================================================================

print("\n=========== AUTOPILOT PID-FILE REFCOUNT QA ===========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
