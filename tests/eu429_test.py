#!/usr/bin/env python3
"""EU-429 — guard the lock-step between usage._CAP_REFUSAL_MARKERS and agent._CAP_PATTERNS.

EU-407 introduced ``orchestrator/usage.py``'s ``_CAP_REFUSAL_MARKERS`` as a verbatim copy of
``orchestrator/agent.py``'s ``_CAP_PATTERNS``, held in agreement only by a code comment. The two
MUST agree on what a provider "cap" looks like:

  * ``agent._classify_plan_limit`` reads ``_CAP_PATTERNS`` at CALL TIME to classify an SDK error as a
    settled provider cap ("usage limit", "quota exceeded", a GLM/z.ai billing cap, …) vs. a transient
    per-minute 429/529 overload (``_TRANSIENT_PATTERNS``).
  * ``usage.is_cap_refusal`` reads ``_CAP_REFUSAL_MARKERS`` LATER, over a builder report's notes, so
    ``autopilot._tally_errored`` can re-recognise a cap and grant it the NO-STRIKE exemption (mirrors
    the infra/login exemption) — so a usage storm can never Blocked-park a healthy ticket.

If a future edit updates one tuple and not the other, the call-time classifier and the tally-time
re-recogniser disagree: a cap the agent saw is no longer recognised at tally time, the exemption
silently drops, and a cap storm can Blocked-park a healthy ticket again — the exact regression EU-407
fixed. This harness makes the agreement structural: it imports both tuples and asserts set-equality,
so any drift fails the gate.

Mutation check (proves the assertion is not vacuous): with the tuples currently in lock-step the
headline equality passes — so we also simulate the exact regression this guard exists to catch
(a marker added to ``_CAP_PATTERNS`` but not ``_CAP_REFUSAL_MARKERS``) and confirm the SAME
set-comparison flips to unequal. A non-empty sanity check rules out the degenerate empty==empty pass.
"""
import sys
import types

# Stub the Agent SDK (imported transitively by orchestrator.agent) so the import needs no real model —
# the eu210/eu202 precedent. The tuples under test are plain module constants; no SDK behaviour is read.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import agent, usage

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ── 1. THE INVARIANT: both tuples must agree on what a provider "cap" looks like ───────────────────
# set-equality (per the ticket spec): order and any accidental duplicate within a tuple don't matter,
# only the SET of cap signatures each side recognises. A failure prints both sets so the drift is
# visible in the run log without a separate diff.
chk("_CAP_REFUSAL_MARKERS == _CAP_PATTERNS (lock-step, EU-429)",
    set(usage._CAP_REFUSAL_MARKERS) == set(agent._CAP_PATTERNS),
    f"usage={sorted(set(usage._CAP_REFUSAL_MARKERS))} != agent={sorted(set(agent._CAP_PATTERNS))}")

# Ruling out the degenerate case keeps the headline honest: two EMPTY tuples would pass
# set() == set() vacuously. Both sides carry the real EU-202/EU-220 cap vocabulary (>0 markers).
chk("both tuples are non-empty (no vacuous empty==empty agreement)",
    len(usage._CAP_REFUSAL_MARKERS) > 0 and len(agent._CAP_PATTERNS) > 0,
    f"len(usage)={len(usage._CAP_REFUSAL_MARKERS)} len(agent)={len(agent._CAP_PATTERNS)}")


# ── 2. MUTATION CHECK — the assertion has teeth: a one-sided drift IS caught ───────────────────────
# Simulate the exact regression this guard exists to prevent — a future edit appends a marker to
# agent._CAP_PATTERNS but forgets usage._CAP_REFUSAL_MARKERS — and confirm the SAME set-comparison the
# headline uses now reports the sides as UNEQUAL. If this stayed equal under perturbation, the headline
# would be vacuous (passing regardless of the code). Restored in the finally so the live module is clean.
_orig_patterns = agent._CAP_PATTERNS
try:
    agent._CAP_PATTERNS = (*agent._CAP_PATTERNS, "eu429-synthetic-cap-marker")
    chk("mutation: one-sided drift IS detected (the equality assertion is not vacuous)",
        set(usage._CAP_REFUSAL_MARKERS) != set(agent._CAP_PATTERNS),
        f"usage={sorted(set(usage._CAP_REFUSAL_MARKERS))} == drifted agent={sorted(set(agent._CAP_PATTERNS))}")
finally:
    agent._CAP_PATTERNS = _orig_patterns


print("\n============ EU-429 CAP-MARKER LOCK-STEP GUARD QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
