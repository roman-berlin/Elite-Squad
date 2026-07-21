"""2026-07-21: the concurrent drain's per-ticket file must POINT somewhere.

Live cost of the old stub: EU-403 was actively building (writing scripts/watchdog.sh, iterating
its test suite) while its per-ticket log held only "consult the drain log" with no path. Reading
that file made an healthy run look stalled for 30 minutes.

Pins: the note carries the resolved drain-stream path plus a runnable `tail -f`; an unresolvable
stdout degrades to the generic hint (never a wrong path); the original note text survives.
"""
import sys
import tempfile
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import run_logger
from orchestrator.config import AppConfig, Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev",
                             protected_branch="main", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), log_folder=str(tmp / "logs"))
NOTE = "[EU-380] concurrent drain (slot 0, 2 builders) — per-ticket stdout is in the shared stream."

_orig = run_logger.drain_log_path

# (1) resolvable stdout → the note carries the path AND a runnable command
run_logger.drain_log_path = lambda: "/Users/x/Library/Logs/General/cockpit.log"
try:
    p = run_logger.write_note_log(cfg, "Elite-Unit", "EU-403", NOTE)
    body = p.read_text(encoding="utf-8")
finally:
    run_logger.drain_log_path = _orig
chk("(1a) the original note survives", "[EU-380] concurrent drain" in body)
chk("(1b) the resolved path is written",
    "/Users/x/Library/Logs/General/cockpit.log" in body, body[-120:])
chk("(1c) it is a runnable command, not prose",
    "tail -f /Users/x/Library/Logs/General/cockpit.log" in body, body[-120:])

# (2) unresolvable stdout → generic hint, never a wrong path
run_logger.drain_log_path = lambda: None
try:
    p2 = run_logger.write_note_log(cfg, "Elite-Unit", "EU-404", NOTE)
    body2 = p2.read_text(encoding="utf-8")
finally:
    run_logger.drain_log_path = _orig
chk("(2a) degrades to the generic hint", "StandardOutPath" in body2, body2[-140:])
chk("(2b) never invents a tail command with no path", "tail -f None" not in body2)

# (3) the real resolver returns a usable answer or an honest None (never a tty/pipe)
real = run_logger.drain_log_path()
chk("(3) resolver returns an absolute file path or None",
    real is None or (real.startswith("/") and not real.startswith("/dev/tty")), str(real))

print("\n========== DRAIN-LOG POINTER QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
