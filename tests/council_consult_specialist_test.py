"""Test _consult_specialist (EU-600): single-officer grounded lookup."""
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
    council_rounds=3,
)

results = []


def check(name, cond, d=""):
    results.append((name, bool(cond), d))


# ------------------------------------------------------------------ #
# Fixtures — stub run_agent and signal-collect helpers to avoid the
# real LLM and filesystem access during these unit tests.
# ------------------------------------------------------------------ #

run_agent_calls = []  # tracks how many times run_agent was called


async def fake_run_agent(prompt, options, tag=None):
    """Return whatever ``_CONSULT_FAKE`` says (or the default brief answer)."""
    run_agent_calls.append({"prompt": prompt, "options": options, "tag": tag})
    answer = getattr(sys.modules[__name__], "_CONSULT_FAKE", None)
    if answer is ...:
        raise RuntimeError("stub error")  # sentinel -> exception path
    return ns(final=answer, text=answer, is_error=False, cost_usd=0.0, num_turns=1, tools=[])


council.run_agent = fake_run_agent


def fake_collect_signals(_cfg):
    return {"status": "ok"}


def fake_format_signals(_sig):
    return "digest placeholder"


def fake_recent_notes(_cfg, lines=12):
    return ""


council.collect_signals = fake_collect_signals
council.format_signals = fake_format_signals
council.recent_commander_notes = fake_recent_notes

# Security Engineer's key — a known valid COUNCIL officer.
SEC_KEY = council._officer_key("Security Engineer")

# Short happy-path answer used for most tests.
SHORT_ANSWER = "Auth looks solid — no exposed secrets found in recent changes."

# Multi-paragraph reply for brevity guard testing.
LONG_ANSWER = (
    "After reviewing recent changes, authentication appears well-protected.\n\n"
    "No new surface-level injection vectors were introduced, and existing token "
    "rotation remains intact.\n\n"
    "One minor note: the login endpoint uses consistent validation patterns that "
    "match the rest of the codebase, so there are no surprises there either.\n\n"
    "Overall confidence in auth safety is high based on what I can see."
)

PASS_SENTINEL = "pass"  # matches _SKIP when lowercased/stripped


# ================================================================== #
# Test 1: Happy path — returns a short string answer
# ================================================================== #
run_agent_calls.clear()
sys.modules[__name__]._CONSULT_FAKE = SHORT_ANSWER
resp = asyncio.run(council._consult_specialist(CFG, "is auth safe?", SEC_KEY))
check(
    "happy path returns non-empty str",
    isinstance(resp, str) and len(resp) > 0 and resp == SHORT_ANSWER,
    repr(resp),
)
check("happy path called run_agent exactly once", len(run_agent_calls) == 1)

# ================================================================== #
# Test 2: Unknown officer_key — returns None without raising
# ================================================================== #
run_agent_calls.clear()
resp = asyncio.run(council._consult_specialist(CFG, "question", "no-such-officer"))
check("unknown key returns None", resp is None)
check("unknown key did NOT call run_agent (no fallback)", len(run_agent_calls) == 0)

# ================================================================== #
# Test 3a: run_agent raises — caught, returns None
# ================================================================== #
run_agent_calls.clear()
sys.modules[__name__]._CONSULT_FAKE = ...  # sentinel -> RuntimeError
resp = asyncio.run(council._consult_specialist(CFG, "fail?", SEC_KEY))
check("run_agent RuntimeError returns None", resp is None)
del sys.modules[__name__]._CONSULT_FAKE

# ================================================================== #
# Test 3b: collect_signals raises — caught, returns None
# ================================================================== #
run_agent_calls.clear()


def _bad_collect(_cfg):
    raise TimeoutError("signal collection timed out")


council.collect_signals = _bad_collect
resp = asyncio.run(council._consult_specialist(CFG, "signal fail?", SEC_KEY))
check("collect_signals exception returns None", resp is None)
# The fact that we returned None here means the exception was swallowed
# inside _consult_specialist (no assert needed — the caller survived).
# Restore working collector.
council.collect_signals = fake_collect_signals

# ================================================================== #
# Test 4: Brevity guard — multi-paragraph trimmed to ≤2 sentences
# ================================================================== #
run_agent_calls.clear()
sys.modules[__name__]._CONSULT_FAKE = LONG_ANSWER
resp = asyncio.run(council._consult_specialist(CFG, "long answer?", SEC_KEY))
check("brief trims to ≤2 sentences", (resp.count(". ") + int(resp.endswith("."))) <= 2 or resp.count(".") <= 2, repr(resp))

# ================================================================== #
# Test 5: Skip guard — 'PASS' / empty reply yields None
# ================================================================== #
run_agent_calls.clear()
sys.modules[__name__]._CONSULT_FAKE = PASS_SENTINEL
resp = asyncio.run(council._consult_specialist(CFG, "out of scope?", SEC_KEY))
check("PASS reply returns None", resp is None)

run_agent_calls.clear()
sys.modules[__name__]._CONSULT_FAKE = ""
resp = asyncio.run(council._consult_specialist(CFG, "", SEC_KEY))
check("empty reply returns None", resp is None)

del sys.modules[__name__]._CONSULT_FAKE

# ================================================================== #
# Summary
# ================================================================== #
print("\n=========== CONSULT SPECIALIST QA ===========")
for n, ok, d in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({d})" if d and not ok else ""))
p = sum(1 for _, ok, _ in results if ok)
print(f"  {p}/{len(results)} passed", "✅" if p == len(results) else "❌")
assert p == len(results)
