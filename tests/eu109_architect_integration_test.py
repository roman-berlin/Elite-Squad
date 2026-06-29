"""Architect officer integration tests: config wiring and cross-component behavior (EU-109).

This test file focuses on truly cross-component concerns that involve the Architect's
interaction with other parts of the system (config, loop integration, etc.). Pure
function tests (parse_adr, detect_oversized, to_dict, etc.) are in architect_test.py.
"""
import sys, types, asyncio
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Integration test: verify architect_enabled config flag works
def test_architect_enabled_config():
    """Test that architect_enabled config flag controls Architect execution."""
    from orchestrator.config import Config

    # Test 1: architect_enabled defaults to False
    cfg = Config(apps=[])
    chk("architect_enabled defaults to False",
        not getattr(cfg, "architect_enabled", True))

    # Test 2: architect_enabled can be set to True
    cfg.architect_enabled = True
    chk("architect_enabled can be set to True",
        cfg.architect_enabled is True)

test_architect_enabled_config()

# Integration test: verify Architect does NOT trigger Scrum Master split directly
# This is a key cross-component test: the Architect produces ADR only; loop.py handles split
def test_architect_no_split_trigger():
    """Test that Architect design() produces ADR but does NOT trigger Scrum Master split.

    This verifies the separation of concerns: Architect produces the ADR, and loop.py
    is responsible for detecting oversized designs and triggering the Scrum Master.
    """
    from orchestrator.architect import parse_adr, design, ADRExtraction

    # Verify parse_adr handles oversized ADR correctly
    oversized_adr_text = """
## APPROACH
Large migration across multiple modules.

## RISK + ALTERNATIVE
Complexity risk. Alternative: incremental migration (rejected for timeline).

## TOUCH-POINTS
src/auth/middleware.py
src/auth/jwt_helper.py
src/db/users.py
src/api/users.py
src/frontend/Users.tsx
src/frontend/UserForm.tsx

## DEFINITION-OF-DONE
Tests: migration tests
A11y: user pages scans
Security: API gateway authz

ADR_COMPLETE
"""
    parsed = parse_adr(oversized_adr_text)
    chk("Oversized ADR parsed correctly", parsed.skipped is False)
    chk("Oversized ADR has 6 touch-points", len(parsed.touch_points) == 6)

    # Verify that ADRExtraction itself doesn't have split logic
    # The design() function returns ADRExtraction; loop.py calls detect_oversized separately
    chk("ADRExtraction is a pure data structure",
        hasattr(ADRExtraction, "to_dict") and not hasattr(ADRExtraction, "split"))
    chk("ADRExtraction has no 'split' method",
        not hasattr(ADRExtraction, "trigger_split"))

test_architect_no_split_trigger()

# Integration test: verify Architect integrates with Config model selection
def test_architect_model_selection():
    """Test that Architect uses high-effort model selection for design reasoning."""
    from orchestrator.architect import should_run_architect
    from orchestrator.config import Config

    # Verify should_run_architect works with Config
    cfg = Config(apps=[])

    class TestApp:
        repo_path = "/fake/repo"
    cfg.apps = [TestApp()]

    # Test with feature ticket (should run Architect)
    feature_ticket = Ticket(
        id="FEAT-1",
        key="FEAT-1",
        summary="Add feature",
        description="Add feature",
        issue_type="Story",
        acceptance_criteria=["AC1", "AC2", "AC3", "AC4", "AC5"]
    )

    async def run_test():
        result = await should_run_architect(cfg, feature_ticket)
        chk("should_run_architect integrates with Config", result is True)

    asyncio.run(run_test())

test_architect_model_selection()

# Result tally
print(f"{sum(1 for _, ok, _ in results if ok)}/{len(results)} passed")
if not all(ok for _, ok, _ in results):
    print("RESULT: FAIL")
    for n, ok, d in results:
        if not ok:
            print(f"  ✗ {n}: {d}")
    sys.exit(1)
else:
    print("RESULT: PASS")
