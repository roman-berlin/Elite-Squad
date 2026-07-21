"""EU-405 — a mechanical restart guard + a self-update restart that fires WITHOUT a running drain.

Two P1s from the 2026-07-21 production audit (ops-deploy + crash-recovery):

  (a) Nothing mechanical stops `launchctl kickstart` from killing a live build — the kickstart-during-
      run safety was a memory-only human rule. It killed AUTO-177 mid-build on 2026-07-19.

  (b) EU-387's self-update restart was consumed ONLY at a drain cycle boundary (autopilot.py's
      `_maybe_self_restart`). A self-repo land from a MANUAL cockpit run (no drain armed) announces
      "restarting automatically once idle" and then never restarts — the stale-process class that ran
      old code 11h.

This harness pins:

  §1  in_flight_builds — the cross-process audit-tail signal `./general deploy` refuses against.
      Same terminal-event truth as _dangling_in_progress, but answers "is a build running RIGHT NOW"
      (not "is a ticket In Progress on the board"). since_s bounds staleness so a killed-and-stranded
      ticket_start can't pin the host to old code forever.
  §2  _self_restart_tick — the testable core of the serve-level consumer. Calls _maybe_self_restart
      when active_run_count()==0; skips when busy (never restarts a live build). This is the watcher
      that closes the manual-run gap.
  §3  ensure_self_restart_watcher — the singleton starter (two calls → one thread, like ensure_poll_loop).
  §4  loop.py land-site notify tells the TRUTH: promises an automatic restart only when the knob is on
      (now actually fulfilled by §2 even for manual runs); says RESTART BY HAND when it is off.
  §5  `./general deploy` exists, refuses on an in-flight signature, and `--force` is the documented
      break-glass; the refusal names ./general deploy as THE way and kickstart as break-glass.
  §6  /api/health exposes active_run_count (the authoritative live signal the wrapper probes), and
      serve() starts the watcher.
  §7  docs: DEPLOYMENT.md names ./general deploy as THE way and kickstart as break-glass only.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from unittest.mock import patch

# Stub the Agent SDK the way every other harness does (no network, no real model).
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _D
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


def mkcfg() -> Config:
    return Config(apps=[], audit_path=str(_tmp / "audit.jsonl"))


def _ts(seconds_ago: float) -> str:
    """An audit-shaped local-time stamp `seconds_ago` before now."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - seconds_ago))


def _row(event, tid="EU-1", app="eu", seconds_ago=0.0):
    return {"event": event, "ticket_id": tid, "app": app, "ts": _ts(seconds_ago)}


# ─── §1  in_flight_builds ──────────────────────────────────────────────────────
ok("§1 in_flight_builds exists", hasattr(autopilot, "in_flight_builds"))

# (a) nothing in the audit → nothing in flight
ok("§1a empty audit → no builds in flight",
   autopilot.in_flight_builds(mkcfg(), audit_rows=[]) == [])

# (b) a ticket_start with a terminal AFTER it → closed, not in flight
rows = [_row("ticket_start", seconds_ago=600), _row("merged", seconds_ago=300)]
ok("§1b terminal after start → not in flight",
   autopilot.in_flight_builds(mkcfg(), audit_rows=rows) == [],
   str(autopilot.in_flight_builds(mkcfg(), audit_rows=rows)))

# (c) a recent ticket_start with NO terminal → in flight
rows = [_row("ticket_start", tid="EU-405", app="eu", seconds_ago=120)]
res = autopilot.in_flight_builds(mkcfg(), audit_rows=rows)
ok("§1c recent open start → in flight",
   len(res) == 1 and res[0]["ticket_id"] == "EU-405", str(res))

# (d) a STALE open start (older than the default 2h window) → not in flight
rows = [_row("ticket_start", tid="EU-OLD", seconds_ago=3 * 3600)]
ok("§1d stale open start (>2h) does not block — killed-and-stranded class",
   autopilot.in_flight_builds(mkcfg(), audit_rows=rows) == [],
   str(autopilot.in_flight_builds(mkcfg(), audit_rows=rows)))

# (e) since_s=None is strict: even the stale start counts
ok("§1e since_s=None is strict (any open start counts)",
   len(autopilot.in_flight_builds(mkcfg(), audit_rows=rows, since_s=None)) == 1)

# (f) terminal BEFORE the start, then the start stays open → in flight (last start wins)
rows = [_row("merged", seconds_ago=600), _row("ticket_start", tid="EU-9", seconds_ago=120)]
res = autopilot.in_flight_builds(mkcfg(), audit_rows=rows)
ok("§1f the LAST start is what counts (older terminal doesn't close it)",
   len(res) == 1 and res[0]["ticket_id"] == "EU-9", str(res))

# (g) concurrent: two open starts → both in flight (N=2 drain)
rows = [_row("ticket_start", tid="EU-A", seconds_ago=120),
        _row("ticket_start", tid="EU-B", seconds_ago=60)]
res = autopilot.in_flight_builds(mkcfg(), audit_rows=rows)
ok("§1g two concurrent open starts → both in flight",
   sorted(r["ticket_id"] for r in res) == ["EU-A", "EU-B"], str(res))


# ─── §2  _self_restart_tick (the testable watcher core) ─────────────────────────
ok("§2 _self_restart_tick exists", hasattr(autopilot, "_self_restart_tick"))

flag = autopilot._self_restart_flag(mkcfg())
flag.parent.mkdir(parents=True, exist_ok=True)
flag.write_text('{"ticket":"EU-1","ts":' + str(time.time()) + "}", encoding="utf-8")

# idle (active_run_count==0) + flag set → calls _maybe_self_restart
called = []
with patch("orchestrator.cockpit_state.active_run_count", lambda: 0), \
     patch.object(autopilot, "_maybe_self_restart", lambda c, a: called.append(1)):
    fired = autopilot._self_restart_tick(mkcfg(), None)
ok("§2a idle → tick fires _maybe_self_restart", fired is True and called == [1])

# busy (active_run_count>0) → does NOT call _maybe_self_restart (never a mid-build kill)
called.clear()
with patch("orchestrator.cockpit_state.active_run_count", lambda: 1), \
     patch.object(autopilot, "_maybe_self_restart", lambda c, a: called.append(1)):
    fired = autopilot._self_restart_tick(mkcfg(), None)
ok("§2b busy → tick skips _maybe_self_restart (never kills a live build)",
   fired is False and called == [], str((fired, called)))
flag.unlink(missing_ok=True)


# ─── §3  ensure_self_restart_watcher singleton ──────────────────────────────────
ok("§3 ensure_self_restart_watcher exists",
   hasattr(autopilot, "ensure_self_restart_watcher"))

# Reset any module-global thread state left by a prior harness.
autopilot._self_restart_thread = None
stop = threading.Event()
t1 = autopilot.ensure_self_restart_watcher(mkcfg(), None, interval_s=0.01, stop_event=stop)
ok("§3a watcher starts a daemon thread", t1 is not None and t1.is_alive())
t2 = autopilot.ensure_self_restart_watcher(mkcfg(), None, interval_s=0.01, stop_event=stop)
ok("§3b a second call reuses the SAME thread (singleton, no stack-up)", t2 is t1)
stop.set()
t1.join(timeout=2.0)
ok("§3c the watcher honours its stop_event and exits", not t1.is_alive())
autopilot._self_restart_thread = None


# ─── §4  loop.py land-site notify tells the truth ───────────────────────────────
lsrc = Path("orchestrator/loop.py").read_text(encoding="utf-8")
ok("§4a notify branches on the self_update_auto_restart knob",
   'getattr(cfg, "self_update_auto_restart", True)' in lsrc)
ok("§4b knob-off branch names the honest by-hand restart (./general deploy)",
   "self_update_auto_restart is OFF" in lsrc and "./general deploy" in lsrc)


# ─── §5  ./general deploy wrapper ───────────────────────────────────────────────
msrc = Path("orchestrator/main.py").read_text(encoding="utf-8")
ok("§5a the deploy subcommand is wired", 'args.command == "deploy"' in msrc)
ok("§5b the wrapper consults the in-flight signal", "in_flight_builds" in msrc)
ok("§5c the wrapper has a --force break-glass", "add_argument(\"--force\"" in msrc)
# The refusal message must name ./general deploy as THE way and kickstart as break-glass.
ok("§5d the refusal names kickstart as break-glass", "kickstart" in msrc and "break-glass" in msrc)


# ─── §6  /api/health exposes active_run_count + serve starts the watcher ─────────
ssrc = Path("orchestrator/server.py").read_text(encoding="utf-8")
ok("§6a /api/health exposes active_run_count (the live probe signal)",
   "active_run_count" in ssrc and "/api/health" in ssrc)
ok("§6b serve() starts the serve-level self-restart watcher",
   "ensure_self_restart_watcher" in ssrc)


# ─── §7  docs ───────────────────────────────────────────────────────────────────
def _doc_text() -> str:
    for cand in ("DEPLOYMENT.md", "README.md"):
        p = Path(cand)
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


dsrc = _doc_text()
ok("§7 docs name ./general deploy as THE way to restart", "./general deploy" in dsrc)
ok("§7b docs call raw kickstart break-glass only", "break-glass" in dsrc)

print(f"\n{checks}/{checks} passed")
