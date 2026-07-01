"""Architect officer integration tests: config wiring and cross-component behavior (EU-109).

This test file focuses on truly cross-component concerns that involve the Architect's
interaction with other parts of the system (config, loop integration, etc.). Pure
function tests (parse_adr, detect_oversized, to_dict, etc.) are in architect_test.py.
"""
import sys, types, asyncio
from unittest.mock import AsyncMock, patch

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator.contracts import Ticket
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Integration test: verify architect_enabled config flag works
def test_architect_enabled_config():
    """Test that architect_enabled config flag controls Architect execution."""
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
    from orchestrator.architect import ADRExtraction

    # Verify that ADRExtraction itself doesn't have split/split-triggering logic
    chk("ADRExtraction has no 'split' method", not hasattr(ADRExtraction, "split"))
    chk("ADRExtraction has no 'trigger_split' method", not hasattr(ADRExtraction, "trigger_split"))

test_architect_no_split_trigger()

# Integration test: verify Architect integrates with Config model selection and runs correctly
def test_architect_model_selection_and_run():
    """Test that Architect uses configured models and correctly threads config."""
    from orchestrator.architect import should_run_architect, design
    from orchestrator.config import Config, AppConfig

    app_cfg = AppConfig(name="test-app", repo_path="/fake/repo")
    cfg = Config(apps=[app_cfg])
    cfg.auto_model = False
    cfg.reviewer_model = "test-custom-reviewer-model"

    feature_ticket = Ticket(
        id="FEAT-1",
        key="FEAT-1",
        summary="Add feature",
        description="Add feature description",
        issue_type="Story",
        acceptance_criteria=["AC1", "AC2", "AC3", "AC4", "AC5"],
        app="test-app"
    )

    async def run_test():
        # Verify should_run_architect integrates with Config
        result = await should_run_architect(cfg, feature_ticket)
        chk("should_run_architect integrates with Config", result is True)

        # Mock run_officer to verify how design() invokes it
        with patch("orchestrator.recon.run_officer", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "SKIP_ADR"
            
            adrex = await design(cfg, feature_ticket, repo_context="test-ctx", gated=True)
            
            chk("design returns ADRExtraction", adrex.skipped is True)
            chk("run_officer called once", mock_run.call_count == 1)
            
            # Verify passed arguments to run_officer
            call_kwargs = mock_run.call_args[1]
            chk("run_officer gets correct officer", call_kwargs.get("officer") == "architect")
            chk("run_officer gets correct cfg", call_kwargs.get("cfg") is cfg)
            chk("run_officer gets correct cwd", call_kwargs.get("cwd") == "/fake/repo")
            chk("run_officer uses reviewer model as ceiling", call_kwargs.get("model") == "test-custom-reviewer-model")
            chk("run_officer gets high effort", call_kwargs.get("effort") == "high")

    asyncio.run(run_test())

test_architect_model_selection_and_run()

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
