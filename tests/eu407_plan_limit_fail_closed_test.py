"""EU-407 — plan-limit fails CLOSED when the probe is blind; cap refusals never strike a ticket.

Production audit 2026-07-21 (failure-paths + resources-cost P1s); extends EU-357. Two compounding
defects left a healthy, top-Rank ticket Blocked-parked for a PROVIDER outage:

  (a) ``usage.plan_limit_hit`` surfaces ``blind=True`` when the usage probe can't see (the 5h Max
      window exhausted → the throwaway probe itself is refused → ``over_limits`` empty → ``hit=False``).
      EU-357 added the flag with ZERO consumers, so a capped provider looked healthy and the drain
      kept churning 3 full cycles before the barren-cycle breaker paused.
  (b) ``autopilot._tally_errored`` charged a per-ticket error strike for a cap-classified refusal
      (provider outage text in the report notes). With ``_MAX_TICKET_ERRORS == _MAX_BARREN_CYCLES
      == 3``, the strike threshold parked the head-of-queue ticket on the EXACT cycle the breaker
      fired — a healthy ticket moved to Blocked for a provider problem.

Acceptance:
  AC1  blind probe + a cap-classified refusal this cycle ⇒ treat as limit-hit (fail closed):
       secondary fallback or pause, never error-strike the ticket.
  AC2  refusals classified as provider-cap add NO per-ticket strike (mirror infra/login).
  AC3  harness pins: blind+refusal pauses; a storm of cap refusals parks ZERO tickets.

This harness pins the pure seams (the classifier, the tally exemption, the cache-seed helper) and
source-pins the autopilot wiring that turns them into a hold. No network, no real models.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch

# Stub the Agent SDK + requests so importing orchestrator.* needs no network/binary (run_all style).
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


def report(tid, outcome, notes):
    return TicketReport(tid, outcome, 0, 0.0, "EU", notes=notes)


CAP_MSG = "Error: usage limit reached — your Claude Max weekly limit has been exceeded"
TRANSIENT_MSG = "Error 429: rate limit reached (too many requests) — overloaded"
TURN_LIMIT_MSG = "maximum number of turns reached for this pass"
REAL_BUG_MSG = "TypeError: cannot read property 'x' of undefined"


# ================================================================================================ #
# AC2 / AC3: the classifier + the per-ticket strike exemption
# ================================================================================================ #
print("\n=== AC2/AC3: cap refusal is a no-strike class (mirrors infra/login) ===")

is_cap_refusal = getattr(usage, "is_cap_refusal", None)
ok("usage.is_cap_refusal exists (the classifier the audit names)",
   callable(is_cap_refusal), "is_cap_refusal missing — provider-cap refusals can't be recognised")

ok("is_cap_refusal(usage-limit text) → True", is_cap_refusal(CAP_MSG), repr(CAP_MSG))
ok("is_cap_refusal(None/empty) → False", is_cap_refusal("") is False and is_cap_refusal(None) is False)
# A transient per-minute 429/529 overload is retry territory, NOT a provider cap — must NOT match,
# else the breaker loses its backstop signal (the storm that exhausts the 5h window reads transient).
ok("is_cap_refusal(transient rate-limit text) → False (cap only, not transient)",
   is_cap_refusal(TRANSIENT_MSG) is False, repr(TRANSIENT_MSG))
ok("is_cap_refusal(turn-limit text) → False (ticket-attributable, EU-248 owns it)",
   is_cap_refusal(TURN_LIMIT_MSG) is False, repr(TURN_LIMIT_MSG))
ok("is_cap_refusal(a real ticket bug) → False", is_cap_refusal(REAL_BUG_MSG) is False)

# _tally_errored must treat a cap refusal EXACTLY like infra/login: no strike, no counter touch.
counts = {}
reps = [report("EU-1", Outcome.ERRORED, CAP_MSG),
        report("EU-2", Outcome.ERRORED, CAP_MSG),
        report("AUTO-9", Outcome.ERRORED, REAL_BUG_MSG)]
_park, _errored, _retrying, _infra, _changed = autopilot._tally_errored(reps, counts)
ok("AC2: cap-refusal ERRORED reports leave error_counts UNTOUCHED (no strike)",
   "EU-1" not in counts and "EU-2" not in counts, f"counts={counts}")
ok("AC2: cap-refusal reports never enter park_now (a cap is not a ticket defect)",
   "EU-1" not in _park and "EU-2" not in _park, f"park_now={_park}")
ok("AC2: a real-bug ERRORED report DOES still strike (no regression)",
   counts.get("AUTO-9") == 1, f"counts={counts}")

# AC3: a STORM of cap refusals across many cycles parks ZERO tickets and accrues ZERO strikes.
storm = {}
parked_total: set[str] = set()
for cycle in range(6):  # 2x the barren-cycle / strike threshold of 3
    reps = [report(f"EU-{i}", Outcome.ERRORED, CAP_MSG) for i in range(1, 5)]
    p, _e, _r, _i, _c = autopilot._tally_errored(reps, storm)
    parked_total |= set(p)
ok("AC3: a storm of cap refusals accrues ZERO per-ticket strikes",
   storm == {}, f"counts after 6 cap-refusal cycles = {storm}")
ok("AC3: a storm of cap refusals parks ZERO tickets",
   parked_total == set(), f"parked = {parked_total}")

# Fail-first control: with is_cap_refusal stubbed away, the SAME cap report WOULD strike —
# proves _tally_errored actually consults usage.is_cap_refusal, not its own dead branch.
control = {}
_orig = usage.is_cap_refusal
usage.is_cap_refusal = lambda text: False
try:
    _p, _e, _r, _i, _c = autopilot._tally_errored([report("EU-CTL", Outcome.ERRORED, CAP_MSG)], control)
finally:
    usage.is_cap_refusal = _orig
ok("control: with is_cap_refusal stubbed False, the cap report WOULD strike "
   "(proves _tally_errored delegates to usage.is_cap_refusal)",
   control.get("EU-CTL") == 1, f"control counts={control}")


# ================================================================================================ #
# AC1: a blind probe + a cap refusal ⇒ fail CLOSED (the EU-357 blind flag finally consumed)
# ================================================================================================ #
print("\n=== AC1: blind probe + cap refusal ⇒ treat as a limit hit (fail closed) ===")

# The blind flag still surfaces (EU-357 already pinned this — re-pin so the consumer contract is explicit).
with patch.object(usage, "plan_usage", return_value={"available": False, "reason": "probe refused"}):
    blind_check = usage.plan_limit_hit(_Cfg(), force=True)
ok("blind probe still reports blind=True / hit=False (EU-357 flag intact)",
   blind_check.get("blind") is True and blind_check.get("hit") is False, f"got {blind_check}")

mark = getattr(usage, "mark_blind_cap_hit", None)
ok("usage.mark_blind_cap_hit exists (the cache-seed that fails the blind window closed)",
   callable(mark), "mark_blind_cap_hit missing — blind+cap can't be promoted to a hit")

# mark_blind_cap_hit seeds the plan-limit cache so a subsequent plan_limit_hit reads hit=True —
# which is what makes autopilot's existing secondary-fallback-or-pause block (and resolve_for_run,
# and cockpit_state) all treat the blind window as a real cap. Drive it through the PUBLIC read so
# the contract is exactly what the caller sees.
usage.plan_limit_reset_cache()
with patch.object(usage, "plan_usage", return_value={"available": False, "reason": "blind"}):
    pre = usage.plan_limit_hit(_Cfg(), force=True)              # blind, hit=False (fails open today)
ok("before mark_blind_cap_hit a blind window is hit=False (the fail-open the fix removes)",
   pre.get("hit") is False and pre.get("blind") is True)
mark()                                                          # promote blind+cap → a hit
post = usage.plan_limit_hit(_Cfg())                             # cached read now sees the seeded hit
ok("after mark_blind_cap_hit the blind window reads hit=True (fail CLOSED)",
   post.get("hit") is True, f"post-mark plan_limit_hit = {post}")
ok("the seeded hit carries over_limits so the existing pause/alert plumbing works unchanged",
   isinstance(post.get("over_limits"), list) and len(post.get("over_limits", [])) >= 1,
   f"over_limits = {post.get('over_limits')}")
usage.plan_limit_reset_cache()

# Source-pins: autopilot must (1) track cap refusals across a cycle and (2) call mark_blind_cap_hit
# under blind + cap_refusal, so the existing plan-limit pause/secondary block fires closed.
src = Path("orchestrator/autopilot.py").read_text()
ok("autopilot consults usage.is_cap_refusal in the strike tally (AC2 wired in)",
   "usage.is_cap_refusal" in src)
ok("autopilot seeds the cache on blind + cap evidence (AC1 wired in)",
   "mark_blind_cap_hit" in src, "mark_blind_cap_hit is never called — blind+cap stays fail-open")
ok("autopilot reads the blind flag (the EU-357 flag now has a consumer)",
   '.get("blind")' in src or '["blind"]' in src,
   "no consumer of plan_check['blind'] — the fail-open the ticket is about")

print(f"\n{checks}/{checks} passed")
