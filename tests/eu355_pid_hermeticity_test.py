"""EU-355 — the suite must isolate autopilot's PID file so no harness leaks live-drain state.

Live incident 2026-07-16 10:26: the EU drain's base gate went red on the clean dev tip
(red_base_block EU-218, 30-min drain hold). Cause: a harness read cockpit_state.get_autopilot_
status(), which ORs in daemon_is_external() → the machine-global /tmp/general-autopilot.pid. A LIVE
drain held that PID, so the test process saw "autopilot ON" and its "Resume button present" check
failed → false red base → drain halt. The reverse leak also exists: a harness running the real
autopilot() OVERWRITES then DELETES the live daemon's PID file. Both were previously papered over by
per-harness `_PID_FILE` pins that each new test had to remember (eu203 forgot).

The class-level fix (this ticket): autopilot._PID_FILE honours GENERAL_PID_FILE, and run_all.py
sets it to a per-run temp path in every harness's env — one central seam, no house rule to forget.

Pins:
  (1) autopilot._PID_FILE honours GENERAL_PID_FILE (env override, not hardcoded /tmp);
  (2) run_all.py injects GENERAL_PID_FILE into the child env, pointing OFF /tmp;
  (3) mutation guard: with the env var set, importing autopilot in a child does NOT resolve the
      machine-global /tmp path.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


ROOT = Path(__file__).resolve().parent.parent

# (1) source pin: _PID_FILE reads GENERAL_PID_FILE
ap_src = (ROOT / "orchestrator" / "autopilot.py").read_text()
ok("(1) autopilot._PID_FILE honours GENERAL_PID_FILE",
   'os.environ.get("GENERAL_PID_FILE")' in ap_src,
   "_PID_FILE is still hardcoded to /tmp — a live drain can red the base gate")

# (2) run_all injects the isolated path into the child env
ra_src = (ROOT / "tests" / "run_all.py").read_text()
ok("(2) run_all.py sets GENERAL_PID_FILE in the harness child env",
   '_CHILD_ENV["GENERAL_PID_FILE"]' in ra_src,
   "harnesses still inherit the machine-global PID file")
ok("(2b) the injected path is a temp path, not /tmp/general-autopilot.pid",
   "tempfile.mkdtemp" in ra_src and "general-autopilot.pid" not in ra_src.split("GENERAL_PID_FILE")[1][:120],
   "run_all points harnesses back at the machine-global file")

# (3) behavioral: a child process with GENERAL_PID_FILE set resolves _PID_FILE to that path
sandbox = Path(tempfile.mkdtemp()) / "iso.pid"
env = dict(os.environ)
env["GENERAL_PID_FILE"] = str(sandbox)
env["GENERAL_AUTH_PROBE"] = "0"
out = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0,'.'); from orchestrator import autopilot as a; print(a._PID_FILE)"],
    cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=30)
resolved = (out.stdout or "").strip()
ok("(3) child autopilot._PID_FILE resolves to the isolated path, not /tmp",
   resolved == str(sandbox),
   f"resolved={resolved!r} stderr={out.stderr[-200:]!r}")

print(f"\n{checks}/{checks} passed")
