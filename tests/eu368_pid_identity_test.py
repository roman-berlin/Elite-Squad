"""EU-368 — PID liveness needs an IDENTITY check, and the CLI must not clobber a live daemon.

From the 2026-07-16 total audit. Two holes, one root cause: the PID file recorded a bare integer,
so "is the daemon alive?" could only ever be `os.kill(pid, 0)` — a question about *some* process,
not about *our* process.

  (a) RECYCLED PID — after a crash the file outlives the daemon. The OS hands that pid to an
      unrelated process; kill(0) then succeeds and daemon_running() reports a daemon that died
      hours ago. autostart refuses to relaunch (EU-224) and the cockpit badge (EU-73) shows ON.
  (b) SECOND DAEMON — the cockpit guards Start with daemon_is_external() (server.py:605/665/689)
      but `general autopilot <app>` had no such guard, and autopilot() calls _write_pid()
      unconditionally: the CLI overwrote a live launchd daemon's record and two daemons then ran
      against one PID file — the EU-355 hermeticity incident class, in production.

Pins:
  1. The record carries an identity: _write_pid() writes {"pid", "start"} and _read_pid_record()
     round-trips it.
  2. RECYCLED PID — a record naming a LIVE pid with a foreign start-time reads as NOT running.
     This is the pin that fails on the pre-fix code (bare kill(0) says True).
  3. A genuine self-written record still reads as running (the fix must not break the badge).
  4. LEGACY bare-int records degrade to today's kill(0) behaviour, never to a false "alive"
     for a dead pid — an old-format daemon mid-upgrade must not read as gone (that would open
     the very second-daemon hole (b) closes).
  5. _remove_pid() / _pid_file_holds_our_pid() / _alert_unclean_restart() all speak the new
     format, and still never delete a FOREIGN process's record.
  6. SECOND DAEMON — `general autopilot <app>` refuses (rc=2, autopilot never invoked) while a
     live foreign daemon holds the file, and --force overrides.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

# ── minimal SDK / requests stubs so the orchestrator can import without real deps ──
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)
req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules.setdefault("requests", req)
sys.path.insert(0, ".")

from orchestrator import autopilot

tmp = Path(tempfile.mkdtemp())
# NEVER touch the machine-global /tmp/general-autopilot.pid (autopilot_pid_refcount_test's guard).
autopilot._PID_FILE = tmp / "general-autopilot.pid"

results: list[tuple[str, bool, str]] = []
def chk(n, c, d=""):
    results.append((n, bool(c), str(d)))

_PID = os.getpid()


def _clear():
    autopilot._PID_FILE.unlink(missing_ok=True)
    autopilot._pid_holders = 0


# ============================================================================================== #
# 1. The record carries an identity
# ============================================================================================== #
print("\n=== 1: the PID record stores pid + process start-time ===")
_clear()
autopilot._write_pid()
raw = autopilot._PID_FILE.read_text()
try:
    doc = json.loads(raw)
except Exception as exc:  # noqa: BLE001
    doc = {"_parse_error": str(exc)}
if not isinstance(doc, dict):
    doc = {"_not_a_record": doc}     # pre-fix: a bare int — report a clean FAIL, don't crash
chk("record is JSON with a 'pid' field", doc.get("pid") == _PID, raw)
chk("record carries a 'start' identity for that pid", bool(doc.get("start")), raw)
rec = getattr(autopilot, "_read_pid_record", lambda: None)()
chk("_read_pid_record() round-trips (pid, start)",
    rec is not None and rec[0] == _PID and rec[1] == doc.get("start"), str(rec))


# ============================================================================================== #
# 3. A genuine self-written record still reads as running (badge must not regress)
# ============================================================================================== #
print("\n=== 3: a genuine record still reads as running ===")
chk("daemon_running() True for our own freshly-written record", autopilot.daemon_running(),
    autopilot._PID_FILE.read_text())
chk("_pid_file_holds_our_pid() True for our own record", autopilot._pid_file_holds_our_pid())
chk("daemon_is_external() False for our own record", not autopilot.daemon_is_external())
autopilot._remove_pid()
chk("_remove_pid() unlinks our own record", not autopilot._PID_FILE.exists())


# ============================================================================================== #
# 2. RECYCLED PID — the headline pin. Fails on pre-fix code, for the right reason.
# ============================================================================================== #
print("\n=== 2: a recycled PID (live pid, foreign identity) reads as NOT running ===")
_clear()
autopilot._write_pid()
chk("control: our own genuine record reads as running", autopilot.daemon_running())

# THE pin, expressed so it can only pass for the right reason. The record is whatever _write_pid
# produces; we then make the identity probe disagree with it — which is precisely what the OS does
# after a crash when it hands the dead daemon's pid to an unrelated process. Pre-fix, daemon_running()
# never consults the identity at all, so it answers True here and this FAILS (as it must).
# create=True so this reads as a clean FAIL on a build where _proc_start doesn't exist yet.
with patch.object(autopilot, "_proc_start", lambda pid: "Thu Jan  1 00:00:00 1970", create=True):
    chk("daemon_running() False when the live pid's identity != the recorded one  "
        "← THE recycled-PID pin", not autopilot.daemon_running(),
        "kill(0) alone reported a long-dead daemon as alive because an unrelated process "
        "had recycled its pid")
    chk("daemon_is_external() False too (nothing is actually running)",
        not autopilot.daemon_is_external())
_clear()

# A record whose pid is genuinely dead stays dead (no regression on the plain case).
autopilot._PID_FILE.write_text(json.dumps({"pid": 999999, "start": "Thu Jan  1 00:00:00 1970"}))
chk("daemon_running() False for a dead pid", not autopilot.daemon_running())


# ============================================================================================== #
# 4. LEGACY bare-int records
# ============================================================================================== #
print("\n=== 4: legacy bare-int records degrade to kill(0), never to a false 'gone' ===")
_clear()
autopilot._PID_FILE.write_text(str(_PID))     # old format, live pid
chk("legacy record naming a LIVE pid still reads as running (an old-format daemon mid-upgrade "
    "must not read as gone — that would let a second daemon start)", autopilot.daemon_running(),
    "legacy handling regressed to 'not running'")
_clear()
autopilot._PID_FILE.write_text("999999")   # old format, dead pid
chk("legacy record naming a DEAD pid reads as not running", not autopilot.daemon_running())
_clear()
autopilot._PID_FILE.write_text("garbage")
chk("garbled record reads as not running", not autopilot.daemon_running())
_clear()
chk("missing record reads as not running", not autopilot.daemon_running())


# ============================================================================================== #
# 5. The other three readers speak the new format; a FOREIGN record is never deleted
# ============================================================================================== #
print("\n=== 5: _remove_pid / _pid_file_holds_our_pid / _alert_unclean_restart on the new format ===")
# A REAL live foreign process — our own child, so kill(pid, 0) reaches it (pid 1 would raise EPERM
# on darwin and read as dead, which is why this can't be faked with launchd).
_child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
try:
    _clear()
    autopilot._write_pid()
    _cstart = getattr(autopilot, "_proc_start", lambda p: None)(_child.pid)
    autopilot._PID_FILE.write_text(json.dumps({"pid": _child.pid, "start": _cstart}))
    autopilot._remove_pid()
    chk("last holder's exit never deletes a LIVE FOREIGN daemon's record",
        autopilot._PID_FILE.exists(),
        "the cross-process ownership check was lost in the format change")
    chk("_pid_file_holds_our_pid() False for a foreign record",
        not autopilot._pid_file_holds_our_pid())
    chk("daemon_is_external() True for a live, identified foreign daemon",
        autopilot.daemon_is_external())
    # And the recycled-pid case for that same foreign record: right pid, wrong identity -> gone.
    autopilot._PID_FILE.write_text(json.dumps({"pid": _child.pid, "start": "Thu Jan  1 00:00:00 1970"}))
    chk("a foreign record with a mismatched identity reads as not running",
        not autopilot.daemon_running(), autopilot._PID_FILE.read_text())
finally:
    _child.kill()
    _child.wait(timeout=10)

# _alert_unclean_restart: a leftover record whose process is gone must fire exactly once.
_clear()
autopilot._PID_FILE.write_text(json.dumps({"pid": 999999, "start": "Thu Jan  1 00:00:00 1970"}))
_fired: list[tuple] = []
_audit = types.SimpleNamespace(record=lambda ev, **kw: _fired.append((ev, kw)))
with patch.object(autopilot.notify, "send", lambda *a, **k: None):
    _rv = autopilot._alert_unclean_restart(_audit)
chk("_alert_unclean_restart() fires for a dead new-format record", _rv and len(_fired) == 1, str(_fired))
chk("_alert_unclean_restart() reports the stale PID, not the raw JSON",
    _fired and str(_fired[0][1].get("stale_pid")) == "999999", str(_fired))

_clear()
autopilot._write_pid()
_fired.clear()
with patch.object(autopilot.notify, "send", lambda *a, **k: None):
    _rv2 = autopilot._alert_unclean_restart(_audit)
chk("_alert_unclean_restart() stays silent for OUR OWN live record", not _rv2 and not _fired, str(_fired))
autopilot._remove_pid()


# ============================================================================================== #
# 6. SECOND DAEMON — the CLI refuses while a live foreign daemon holds the file
# ============================================================================================== #
print("\n=== 6: `general autopilot <app>` refuses to clobber a live foreign daemon ===")
from orchestrator.main import _main, build_parser  # noqa: E402  (after stubs)

def _parse(argv):
    """argparse exits the process on an unknown flag — turn that into a clean FAIL."""
    try:
        return build_parser().parse_args(argv)
    except SystemExit:
        return None

_args = _parse(["autopilot", "smoke"])
chk("CLI: autopilot has a --force flag, default False", getattr(_args, "force", None) is False,
    f"force={getattr(_args, 'force', 'MISSING')!r}")
_forced = _parse(["autopilot", "smoke", "--force"])
chk("CLI: --force parses", getattr(_forced, "force", None) is True,
    "argparse rejected --force (flag not registered)")

subprocess.run(["git", "init", "-q", str(tmp)], check=True)   # AppConfig validates repo_path is a git repo
cfgp = tmp / "config.yaml"
cfgp.write_text(f'audit_path: "{tmp}/audit.jsonl"\n'
                'apps:\n'
                '  - name: smoke\n'
                f'    repo_path: "{tmp}"\n'
                '    base_branch: "dev"\n'
                '    backlog_backend: "none"\n')

_calls: list[tuple] = []
async def _fake_autopilot(cfg, app_name=None, **kw):
    _calls.append((app_name, kw))
    return None

def _run_cli(argv, external=True):
    _calls.clear()
    with patch.object(autopilot, "autopilot", _fake_autopilot), \
         patch.object(autopilot, "daemon_is_external", lambda: external):
        try:
            return asyncio.run(_main(["--config", str(cfgp), *argv]))
        except SystemExit as e:      # argparse rejects an unknown flag by exiting the process
            return e.code

rc = _run_cli(["autopilot", "smoke"])
chk("CLI: refuses with a non-zero exit while a live foreign daemon holds the PID file  "
    "← THE second-daemon pin", rc == 2, f"rc={rc}")
chk("CLI: the real autopilot() is never invoked (so _write_pid can't clobber the daemon's record)",
    not _calls, str(_calls))

rc_f = _run_cli(["autopilot", "smoke", "--force"])
chk("CLI: --force overrides the guard and starts the drain", rc_f == 0 and len(_calls) == 1,
    f"rc={rc_f} calls={_calls}")

# No foreign daemon -> unchanged behaviour (the guard must not block the normal path).
rc_n = _run_cli(["autopilot", "smoke"], external=False)
chk("CLI: with no foreign daemon the drain starts as before", rc_n == 0 and len(_calls) == 1,
    f"rc={rc_n} calls={_calls}")


# ============================================================================================== #
print("\n================ EU-368 PID IDENTITY QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
