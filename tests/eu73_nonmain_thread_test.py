"""EU-73 regression — autopilot() must start cleanly on a NON-main thread.

The cockpit Start button runs autopilot() inside a background worker thread (server.py's ``_bg``:
``asyncio.run(autopilot(...))``). On ANY non-main thread ``signal.signal()`` raises
``ValueError: signal only works in main thread of the main interpreter`` — so the SIGTERM
registration (and its restore) MUST be guarded to the main thread. The rejected iteration registered
it unconditionally, crashing every cockpit-started autopilot before its loop even began.

This harness drives autopilot()'s startup exactly the way server._bg does, and pins down:
  * no exception (especially no ValueError) escapes the worker thread, and
  * no orphaned PID file is left behind — _write_pid()/_remove_pid() bracket the run in a
    try/finally, so even a guarded/failed startup can't strand /tmp/general-autopilot.pid pointing
    at a live process (which would pin daemon_running() True forever and freeze the cockpit badge ON).
"""
import asyncio
import sys
import tempfile
import threading
import types
from pathlib import Path
from unittest.mock import patch

# --- stub the heavy imports the orchestrator pulls in (no Agent SDK / network in tests) ---
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D  # type: ignore[attr-defined]
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(  # type: ignore[attr-defined]
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

import orchestrator.autopilot as ap_mod  # noqa: E402 — after sys.path / stubs
from orchestrator.config import AppConfig, Config  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# Never touch real Telegram or the real /tmp PID file.
ap_mod.notify.configured = lambda: False
ap_mod.notify.send = lambda *a, **k: None

d = Path(tempfile.mkdtemp())
(d / "audit.jsonl").write_text("")
tmp_pid = d / "general-autopilot.pid"
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)

# Mirror server.py's autopilot _bg(): asyncio.run(autopilot(...)) on a background worker thread. A
# pre-set stop Event makes the loop break on its very first iteration, so startup runs to completion
# fast while still exercising the exact signal-registration path that used to crash off the main thread.
captured: dict = {"on_main": None, "error": None, "finished": False}


def _bg():
    captured["on_main"] = threading.current_thread() is threading.main_thread()
    ev = threading.Event()
    ev.set()
    try:
        asyncio.run(ap_mod.autopilot(cfg, "automatixy", once=False, stop_event=ev))
        captured["finished"] = True
    except BaseException as exc:  # noqa: BLE001 — capture EVERYTHING, incl. a signal.signal ValueError
        captured["error"] = repr(exc)


with patch.object(ap_mod, "_PID_FILE", tmp_pid):
    t = threading.Thread(target=_bg)
    t.start()
    t.join(10)
    alive_after = t.is_alive()
    pid_orphaned = tmp_pid.exists()
    # daemon_running() reads the same (patched) _PID_FILE — after a clean exit it must read False.
    daemon_after = ap_mod.daemon_running()

audit_txt = (d / "audit.jsonl").read_text()

chk("harness drove autopilot() on a NON-main thread (mirrors server._bg)",
    captured["on_main"] is False, captured["on_main"])
chk("autopilot() startup raised NO exception on a non-main thread (no signal.signal ValueError)",
    captured["error"] is None, captured["error"])
chk("the worker thread finished — autopilot() returned, no hang",
    (not alive_after) and captured["finished"], f"alive={alive_after} finished={captured['finished']}")
chk("autopilot() actually ran its body (audit recorded autopilot_start + autopilot_stop)",
    "autopilot_start" in audit_txt and "autopilot_stop" in audit_txt, audit_txt[:200])
chk("no orphaned PID file remains after exit (try/finally PID cleanup)",
    not pid_orphaned, f"{tmp_pid} exists={pid_orphaned}")
chk("daemon_running() reads False after the loop exits (PID file removed)", not daemon_after)

print("\n========= EU-73 non-main-thread autopilot startup =========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
