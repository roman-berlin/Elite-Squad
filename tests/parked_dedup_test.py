"""EU-56(b) — the immediate-park set is a SINGLE source of truth.

`PARKED` (the outcomes that park a ticket immediately — no auto-retry) must be defined ONCE, in
contracts.py, and imported wherever it's needed. This harness guards the de-dup so the drift can't come
back: events.py used to carry its OWN divergent `_PARKED` (it wrongly included ERRORED) that was dead
code but a trap, and autopilot re-declared the literal instead of importing it. Assert there is now one
definition, one value, that autopilot re-exports the very same object, and that ERRORED is NOT in it
(ERRORED is retried before parking — see autopilot._MAX_TICKET_ERRORS).
"""
import sys
import types

# Importing autopilot pulls in the Agent SDK + requests transitively — stub them (no network/models).
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import autopilot, contracts
from orchestrator.contracts import Outcome

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- one canonical definition, and it lives in contracts.py ---
chk("contracts defines the canonical PARKED", hasattr(contracts, "PARKED"))
chk("PARKED is exactly (ESCALATED, PR_OPENED)", contracts.PARKED == (Outcome.ESCALATED, Outcome.PR_OPENED))
chk("ERRORED is NOT in PARKED (it's retried, not parked immediately)", Outcome.ERRORED not in contracts.PARKED)

# --- autopilot re-exports the SAME object; it does not re-declare its own copy ---
chk("autopilot exposes PARKED", hasattr(autopilot, "PARKED"))
chk("autopilot.PARKED IS the contracts constant (single source, not a copy)",
    autopilot.PARKED is contracts.PARKED)

# --- the dead, divergent duplicate in events.py is gone for good — and so is the module
#     itself (Phase-2 §2, 2026-07-06: the autonomy layer was deleted; on-demand ceremonies only).
import importlib.util
chk("events.py stays deleted (its divergent _PARKED can never return)",
    importlib.util.find_spec("orchestrator.events") is None)

print("\n============ PARKED SINGLE-SOURCE QA (EU-56b) ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
