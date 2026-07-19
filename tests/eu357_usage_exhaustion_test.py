"""EU-357 — the drain must detect 5-hour-window exhaustion instead of churning the backlog.

Incident 2026-07-16 12:20–12:59 (live, Telegram flood in Roman's screenshot): the 5h Max window
ran out; instead of pausing once, the drain fired 41 ticket_start events in 38 min — every ticket
died in ~20s (ticket_start → sonnet_fallback reason=transient → errored), each re-picked 2–3× — and
NOT ONE plan_limit_pause fired. Two compounding defects:
  (1) usage.plan_limit_hit fails OPEN — when the usage probe is blind (unavailable), over_limits is
      empty so hit=False, indistinguishable from "verified under limit";
  (2) no circuit breaker — the refusals were classified "transient", so neither the plan-limit
      pause nor the infra/auth holds ever armed, and the drain walked the whole backlog to no effect.

Pins:
  (1) plan_limit_hit reports blind=True when plan_usage is unavailable (was silently hit=False);
  (1b) it still reports blind=False + hit=False on a genuine under-limit reading;
  (2) _is_barren_cycle is True only when EVERY report ERRORED and none is infra/auth/base-explained;
  (2b) an explained (infra) all-errored cycle is NOT barren (offline-hold owns it);
  (2c) a cycle with any landed/parked outcome is NOT barren;
  (3) the breaker arms a real hold: source pins for _MAX_BARREN_CYCLES, the cooldown, and the
      usage_exhaustion_hold audit event + top-of-loop hold gate.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch

sdk = types.ModuleType("claude_agent_sdk")
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", sdk)
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers={}, update=lambda *a, **k: None)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import autopilot, usage  # noqa: E402
from orchestrator.contracts import Outcome, TicketReport  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


class _Cfg:
    pass


# (1) blind probe → blind=True (was the fail-open hit=False)
with patch.object(usage, "plan_usage", return_value={"available": False, "reason": "probe refused"}):
    r = usage.plan_limit_hit(_Cfg(), force=True)
ok("(1) plan_limit_hit reports blind=True when usage data is unavailable",
   r.get("blind") is True and r.get("hit") is False,
   f"got {r} — a blind probe still looks like 'plenty of headroom'")

# (1b) genuine under-limit reading → not blind, not hit
with patch.object(usage, "plan_usage",
                  return_value={"available": True, "limits": [{"utilization": 0.4}]}):
    r = usage.plan_limit_hit(_Cfg(), force=True)
ok("(1b) a real under-limit reading is blind=False, hit=False",
   r.get("blind") is False and r.get("hit") is False, f"got {r}")


def rep(tid, outcome=Outcome.ERRORED):
    return TicketReport(tid, outcome, 0, 0.0, "EU")


# (2) all-errored, none explained → barren
reps = [rep("EU-1"), rep("EU-2"), rep("EU-3")]
ok("(2) all-errored + none explained → barren cycle", autopilot._is_barren_cycle(reps, set()) is True)

# (2b) all-errored but infra-explained → NOT barren (offline-hold owns it)
ok("(2b) explained (infra) all-errored cycle is NOT barren",
   autopilot._is_barren_cycle(reps, {"EU-2"}) is False,
   "the offline/auth/base holds must own explained failures, not the usage breaker")

# (2c) any non-errored outcome → NOT barren
mixed = [rep("EU-1"), rep("EU-2", Outcome.MERGED)]
ok("(2c) a cycle with a landed outcome is NOT barren",
   autopilot._is_barren_cycle(mixed, set()) is False)

# (2d) empty cycle → NOT barren
ok("(2d) empty report set is NOT barren", autopilot._is_barren_cycle([], set()) is False)

# (3) source pins for the breaker's hold machinery
src = Path("orchestrator/autopilot.py").read_text()
ok("(3) _MAX_BARREN_CYCLES threshold defined", "_MAX_BARREN_CYCLES = 3" in src)
ok("(3b) a cooldown is defined", "_USAGE_HOLD_COOLDOWN_S" in src)
ok("(3c) the breaker records a usage_exhaustion_hold audit event",
   'audit.record("usage_exhaustion_hold"' in src)
ok("(3d) a top-of-loop usage-hold gate exists (holds new work during cooldown)",
   "time.time() < usage_hold_until" in src)

print(f"\n{checks}/{checks} passed")
