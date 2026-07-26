"""Test _needs_specialist (EU-601): cheap specialist triage — one-call classifier."""
import asyncio
import sys
import types

# Stub the Claude Agent SDK so importing council is network-free (council_test pattern).
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _D

sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import council

ns = types.SimpleNamespace
CFG = ns(
    builder_model="m",
    reviewer_model="m",
    discussion_model="m",
    smalltalk_model="m",
    council_rounds=3,
)

results = []


def check(name, cond, d=""):
    results.append((name, bool(cond), d))


# ------------------------------------------------------------------ #
# Fixtures — stub run_agent and signal-collect helpers.
# ------------------------------------------------------------------ #

run_agent_calls = []


async def fake_run_agent(prompt, options, tag=None):
    """Return whatever ``_NEEDS_SPECIALIST_FAKE`` says (or raise on sentinel)."""
    run_agent_calls.append({"prompt": prompt, "options": options, "tag": tag})
    answer = getattr(sys.modules[__name__], "_NEEDS_SPECIALIST_FAKE", None)
    if answer is ...:
        raise RuntimeError("stub error")  # sentinel -> exception path
    return ns(final=answer, text=answer, is_error=False, cost_usd=0.0, num_turns=1, tools=[])


council.run_agent = fake_run_agent


# ================================================================== #
# Test 1: Security-shaped question → provost (Security Engineer)
# ================================================================== #
run_agent_calls.clear()
sys.modules[__name__]._NEEDS_SPECIALIST_FAKE = "Security Engineer"
resp = asyncio.run(council._needs_specialist(CFG, "is there a secret leaked in the repo?"))
check(
    "security question → provost",
    resp == "provost",
    repr(resp),
)
check("security call hit run_agent", len(run_agent_calls) == 1)


# ================================================================== #
# Test 2: Build/gate question → field_engineer (Dev Team Lead)
# ================================================================== #
run_agent_calls.clear()
sys.modules[__name__]._NEEDS_SPECIALIST_FAKE = "Dev Team Lead"
resp = asyncio.run(council._needs_specialist(CFG, "why does the gate fail on this build?"))
check(
    "build/gate question → field_engineer",
    resp == "field_engineer",
    repr(resp),
)


# ================================================================== #
# Test 3a: 'NONE' reply → None (small talk)
# ================================================================== #
run_agent_calls.clear()
sys.modules[__name__]._NEEDS_SPECIALIST_FAKE = "NONE"
resp = asyncio.run(council._needs_specialist(CFG, "good morning"))
check(
    "NONE reply → None",
    resp is None,
    repr(resp),
)


# ================================================================== #
# Test 3b: Empty reply → None
# ================================================================== #
run_agent_calls.clear()
sys.modules[__name__]._NEEDS_SPECIALIST_FAKE = ""
resp = asyncio.run(council._needs_specialist(CFG, "hey"))
check(
    "empty reply → None",
    resp is None,
    repr(resp),
)


# ================================================================== #
# Test 4: run_agent raises → None (never propagate)
# ================================================================== #
run_agent_calls.clear()
sys.modules[__name__]._NEEDS_SPECIALIST_FAKE = ...  # sentinel → RuntimeError
resp = asyncio.run(council._needs_specialist(CFG, "boom?"))
check("exception returns None", resp is None)
del sys.modules[__name__]._NEEDS_SPECIALIST_FAKE


# ================================================================== #
# Test 5: Retired officers are NEVER returned
# Each retired display name maps to a retired internal key which must
# be rejected by the _LIVE_KEYS guard.
# ================================================================== #
RETIREES = {
    "Test Engineer": "test_engineer",
    "Engineering Coach": "drillmaster",
    "DevOps": "devops",
    "Architect": "architect",
    "Technical Writer": "scribe",
}

for rank_display, expected_key in RETIREES.items():
    run_agent_calls.clear()
    sys.modules[__name__]._NEEDS_SPECIALIST_FAKE = rank_display
    resp = asyncio.run(council._needs_specialist(CFG, f"question for {rank_display}?"))
    check(
        f"retired '{rank_display}' → None",
        resp is None,
        f"got {repr(resp)} instead of None (expected key {expected_key!r} blocked by _LIVE_KEYS)",
    )

del sys.modules[__name__]._NEEDS_SPECIALIST_FAKE


# ================================================================== #
# Summary
# ================================================================== #
print("\n=========== NEEDS SPECIALIST QA (EU-601) ===========")
for n, ok, d in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({d})" if d and not ok else ""))
p = sum(1 for _, ok, _ in results if ok)
print(f"  {p}/{len(results)} passed", "✅" if p == len(results) else "❌")
assert p == len(results)
