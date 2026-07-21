"""EU-387 (closes the EU-224/EU-384 epic) — self-land triggers an idle-gated graceful restart.

The unit self-updates but the resident process keeps executing OLD code (no hot-reload) — the
2026-07-09 incident ran pre-EU-201 code for 11h and stranded fragments. Detection landed in
b369130 (self_update_pending_restart + notify); EU-385/386 landed boot auto-resume + dirty
forensics. This is the last piece: the land site FLAGS (it is mid-run by definition — its
worklist may hold more tickets), and the drain's CYCLE BOUNDARY — the one place "no run in
flight for ANY app" is knowable — performs the clean exit (75, EX_TEMPFAIL: respawn-triggering
but forensically distinct from a crash). The keepalive respawns on the new sha; EU-385 re-arms.

Pins:
  (1) flag_self_update writes the pending file; loop._land calls it at the self-repo detection
      point (source pin — driving a full land needs a live repo);
  (2) idle + clean + knob-on → _maybe_self_restart audits self_restart_exit, removes the PID,
      and calls the exit hook with SELF_RESTART_EXIT_CODE (os._exit patched);
  (3) BUSY (any active run) → defers: no exit, flag SURVIVES for the next cycle;
  (4) knob off → notify-only: no exit, flag cleared (no ping-pong);
  (5) dirty tree → REFUSES: no exit, flag cleared, self_restart_refused audited (EU-386 rule —
      a respawn on a dirty tree silently activates un-gated WIP);
  (6) boot consume is one-shot: consume returns the payload + audits self_restart_completed,
      second consume returns None; serve boot wires it (source pin);
  (7) no flag → no-op (the every-cycle call costs one stat).
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

sdk = types.ModuleType("claude_agent_sdk")
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", sdk)

sys.path.insert(0, ".")

from orchestrator import autopilot  # noqa: E402
from orchestrator.config import Config  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


_tmp = Path(tempfile.mkdtemp())


def mkcfg(knob=True) -> Config:
    c = Config(apps=[], audit_path=str(_tmp / "audit.jsonl"))
    c.self_update_auto_restart = knob
    return c


class _Audit:
    def __init__(self): self.events = []
    def record(self, event, **k): self.events.append((event, k))


exits = []
pids_removed = []
notes = []


def drive(cfg, *, active=0, dirty=False):
    """Run _maybe_self_restart with every side effect captured."""
    exits.clear(); pids_removed.clear(); notes.clear()
    au = _Audit()
    with patch.object(autopilot.os, "_exit", lambda code: exits.append(code)), \
         patch.object(autopilot, "_remove_pid", lambda: pids_removed.append(1)), \
         patch.object(autopilot.notify, "send", lambda m: notes.append(m)), \
         patch.object(autopilot, "tree_forensics", lambda: (dirty, ["orchestrator/x.py"] if dirty else [])), \
         patch.object(autopilot, "respawn_blocking_paths",
                      lambda: ["orchestrator/x.py"] if dirty else []), \
         patch("orchestrator.cockpit_state.active_run_count", lambda: active):
        autopilot._maybe_self_restart(cfg, au)
    return au


ok("autopilot exposes flag/consume/maybe helpers",
   all(hasattr(autopilot, n) for n in
       ("flag_self_update", "consume_self_restart_flag", "_maybe_self_restart",
        "SELF_RESTART_EXIT_CODE")))

# (7) no flag → no-op
cfg = mkcfg()
au = drive(cfg)
ok("(7) without a flag the cycle hook is a no-op", exits == [] and au.events == [])

# (1) flag written with payload
autopilot.flag_self_update(cfg, "EU-999", sha="abc123")
flag = autopilot._self_restart_flag(cfg)
ok("(1) flag_self_update writes the pending file",
   flag.exists() and json.loads(flag.read_text())["ticket"] == "EU-999")

# (3) busy → defer, flag survives
au = drive(cfg, active=1)
ok("(3) a run in flight defers the exit (never mid-build)", exits == [], str(exits))
ok("(3b) the flag survives for the next cycle", flag.exists())

# (2) idle + clean → graceful exit
au = drive(cfg, active=0)
ok("(2) idle + clean + knob-on exits with the distinctive code",
   exits == [autopilot.SELF_RESTART_EXIT_CODE], str(exits))
ok("(2b) the PID file is removed before the exit", pids_removed == [1])
ok("(2c) self_restart_exit is audited",
   any(e == "self_restart_exit" for e, _ in au.events), str(au.events))

# (4) knob off → notify-only, flag cleared
autopilot.flag_self_update(cfg, "EU-999")
cfg_off = mkcfg(knob=False)
au = drive(cfg_off, active=0)
ok("(4) knob off → no exit (today's behaviour)", exits == [])
ok("(4b) the flag is cleared so it can't ping-pong", not flag.exists())

# (5) dirty tree → refuse, clear, audit
autopilot.flag_self_update(cfg, "EU-999")
au = drive(cfg, active=0, dirty=True)
ok("(5) a dirty tree refuses the restart (EU-386 rule)", exits == [])
ok("(5b) refusal is audited + flag cleared",
   any(e == "self_restart_refused" for e, _ in au.events) and not flag.exists(),
   str(au.events))
ok("(5c) the refusal is loud (Telegram)", any("REFUSED" in n for n in notes), str(notes))

# (6) boot consume is one-shot
autopilot.flag_self_update(cfg, "EU-999", sha="abc123")
au = _Audit()
data = autopilot.consume_self_restart_flag(cfg, au)
ok("(6) consume returns the payload + audits completion",
   data and data.get("ticket") == "EU-999"
   and any(e == "self_restart_completed" for e, _ in au.events))
ok("(6b) a second consume is a no-op (one-shot)",
   autopilot.consume_self_restart_flag(cfg, _Audit()) is None)

# (8) 2026-07-19 stabilization: a failed flag write returns False + audits — the land site must
# never announce "restarting automatically" for a flag that didn't persist (the announce-lie).
au8 = _Audit()
with patch.object(autopilot, "_self_restart_flag",
                  lambda cfg: Path("/nonexistent-dir-xyz/flag.json")):
    ok("(8) flag write failure returns False",
       autopilot.flag_self_update(cfg, "EU-999", audit=au8) is False)
ok("(8b) the failure is audited (self_restart_flag_write_failed)",
   any(e == "self_restart_flag_write_failed" for e, _ in au8.events), str(au8.events))
ok("(8c) a successful write returns True", autopilot.flag_self_update(cfg, "EU-999") is True)
autopilot._self_restart_flag(cfg).unlink()

# (9) 2026-07-19 stabilization: a flag whose ts predates THIS process's boot has already been
# honoured — it survives only when boot's consume couldn't unlink it. Exiting on it again would
# crash-loop; it must be ignored, cleared, and audited instead.
flag.write_text(json.dumps({"ticket": "EU-999", "ts": autopilot._PROCESS_START_TS - 3600}),
                encoding="utf-8")
au = drive(cfg, active=0)
ok("(9) a stale (pre-boot) flag never exits — crash-loop capped", exits == [], str(exits))
ok("(9b) the stale flag is cleared + audited",
   not flag.exists() and any(e == "self_restart_stale_flag" for e, _ in au.events),
   str(au.events))

# source pins: the land site flags; the drain cycle checks; serve boot consumes
lsrc = Path("orchestrator/loop.py").read_text()
ok("(1b) loop._land flags at the self-repo detection point",
   "_ap.flag_self_update(cfg, ticket.id" in lsrc)
asrc = Path("orchestrator/autopilot.py").read_text()
ok("(2d) the drain cycle boundary calls _maybe_self_restart",
   "_maybe_self_restart(cfg, audit)" in asrc)
msrc = Path("orchestrator/main.py").read_text()
ok("(6c) serve boot consumes the flag", "consume_self_restart_flag" in msrc)

print(f"\n{checks}/{checks} passed")
