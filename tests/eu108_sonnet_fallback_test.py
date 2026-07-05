#!/usr/bin/env python3
"""EU-108: Sonnet-cap fallback to Opus tests.

Tests that:
1. Sonnet-cap fallback state tracking works correctly
2. Model selection returns Opus when fallback is active
3. Config knob enables/disables the fallback
4. Fallback auto-clears after reset time
5. Telegram notification is sent once on activation
6. Simulated Sonnet-limit → Opus retry → fallback activation
7. All-models limit still pauses (no false fallback)
"""

from __future__ import annotations

import sys
import time
import types

# Stub the Agent SDK before any orchestrator import — the run_agent fallback check imports
# orchestrator.agent, and CI does not install claude_agent_sdk (it crashed on every CI run).
# ClaudeAgentOptions must be a REAL kwargs-holder: the fallback check reads options.model.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
class _Options:
    def __init__(s, **kw): s.__dict__.update(kw)
sdk.ClaudeAgentOptions = _Options
sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

# Add parent directory to path for imports
sys.path.insert(0, ".")

# Import from the orchestrator package
from orchestrator.models import (
    sonnet_fallback_active,
    activate_sonnet_fallback,
    reset_sonnet_fallback,
    fallback_reset_time_str,
    _get_next_friday_0900_utc,
    sonnet_fallback_notification_sent,
    mark_sonnet_fallback_notified,
    reset_sonnet_fallback_notification,
    SONNET,
    OPUS,
)


class MockConfig:
    """Mock Config object for testing."""

    def __init__(self, opus_fallback_on_sonnet_cap=True):
        self.opus_fallback_on_sonnet_cap = opus_fallback_on_sonnet_cap
        self.builder_model = OPUS
        self.auto_model = True


def test_sonnet_fallback_activation():
    """activate_sonnet_fallback sets the fallback state with correct expiration."""
    reset_at = _get_next_friday_0900_utc()
    now = time.time()

    # Ensure reset time is in the future
    assert reset_at > now, "Reset time should be in the future"

    # Activate fallback
    activate_sonnet_fallback(reset_at)

    # Check it's active
    cfg = MockConfig()
    assert sonnet_fallback_active(cfg) is True, "Fallback should be active after activation"

    # Check reset time string is not empty
    reset_str = fallback_reset_time_str()
    assert reset_str, "Reset time string should not be empty"
    assert "UTC" in reset_str, "Reset time string should include UTC"

    print(f"  ✓ Fallback activated, resets at: {reset_str}")


def test_sonnet_fallback_auto_clears():
    """Fallback auto-clears after the reset time passes."""
    # Activate with a reset time in the past
    past_reset = time.time() - 3600  # 1 hour ago
    activate_sonnet_fallback(past_reset)

    cfg = MockConfig()

    # Should not be active (past reset time)
    assert sonnet_fallback_active(cfg) is False, "Fallback should auto-clear after reset time"

    print("  ✓ Fallback auto-clears after reset time")


def test_sonnet_fallback_config_disabled():
    """Fallback can be disabled via config knob."""
    # Activate fallback
    reset_at = _get_next_friday_0900_utc()
    activate_sonnet_fallback(reset_at)

    # With config disabled — should clear the fallback state
    cfg_disabled = MockConfig(opus_fallback_on_sonnet_cap=False)
    assert sonnet_fallback_active(cfg_disabled) is False, \
        "Fallback should be cleared when config disabled"

    # Re-activate for the enabled check
    activate_sonnet_fallback(reset_at)

    # With config enabled — should remain active
    cfg_enabled = MockConfig(opus_fallback_on_sonnet_cap=True)
    assert sonnet_fallback_active(cfg_enabled) is True, \
        "Fallback should be active when config enabled"

    print("  ✓ Config knob controls fallback activation")


def test_sonnet_fallback_reset():
    """reset_sonnet_fallback clears all fallback state."""
    # Activate fallback
    reset_at = _get_next_friday_0900_utc()
    activate_sonnet_fallback(reset_at)
    mark_sonnet_fallback_notified()

    # Verify it's active
    cfg = MockConfig()
    assert sonnet_fallback_active(cfg) is True, "Should be active before reset"

    # Reset
    reset_sonnet_fallback()

    # Verify cleared
    assert sonnet_fallback_active(cfg) is False, "Should be inactive after reset"
    assert sonnet_fallback_notification_sent() is False, "Notification flag should be cleared"

    print("  ✓ reset_sonnet_fallback clears all state")


def test_notification_sent_flag():
    """Notification sent flag works correctly."""
    # Initially not sent
    assert sonnet_fallback_notification_sent() is False, "Should start as not sent"

    # Mark as sent
    mark_sonnet_fallback_notified()
    assert sonnet_fallback_notification_sent() is True, "Should be marked as sent"

    # Reset flag
    reset_sonnet_fallback_notification()
    assert sonnet_fallback_notification_sent() is False, "Should be cleared after reset"

    print("  ✓ Notification sent flag tracks correctly")


def test_friday_reset_calculation():
    """Friday 09:00 UTC calculation returns a valid future timestamp."""
    reset_at = _get_next_friday_0900_utc()
    now = time.time()

    # Should be in the future (within 8 days max)
    assert reset_at > now, "Reset time should be in the future"
    assert reset_at < now + (8 * 86400), "Reset time should be within 8 days"

    print("  ✓ Friday reset time is calculated correctly")


def test_model_selection_with_fallback():
    """for_builder returns Opus when fallback is active."""
    from orchestrator.models import for_builder

    # Mock ticket
    ticket = {"id": "TEST-1", "description": "Test ticket"}

    cfg = MockConfig()

    # Without fallback, should return Sonnet (auto_model ON, low effort)
    model, reason = for_builder(cfg, ticket, "low")
    assert "sonnet" in model.lower(), f"Should return Sonnet without fallback, got: {model}"

    # Activate fallback
    reset_at = _get_next_friday_0900_utc()
    activate_sonnet_fallback(reset_at)

    # With fallback, should return Opus
    model, reason = for_builder(cfg, ticket, "low")
    assert "opus" in model.lower(), f"Should return Opus with fallback active, got: {model}"
    assert "fallback" in reason.lower(), f"Reason should mention fallback, got: {reason}"

    print("  ✓ Model selection respects fallback state")

    # Cleanup
    reset_sonnet_fallback()


def test_config_disabled_fallback_no_effect():
    """When config disabled, fallback doesn't affect model selection."""
    from orchestrator.models import for_builder

    # Mock ticket
    ticket = {"id": "TEST-1", "description": "Test ticket"}

    # Config with fallback disabled
    cfg = MockConfig(opus_fallback_on_sonnet_cap=False)

    # Activate fallback (simulating it was set earlier)
    reset_at = _get_next_friday_0900_utc()
    activate_sonnet_fallback(reset_at)

    # Should return Sonnet (fallback ignored due to config)
    model, reason = for_builder(cfg, ticket, "low")
    assert "sonnet" in model.lower(), \
        f"Should return Sonnet when fallback disabled in config, got: {model}"

    print("  ✓ Config disabled prevents fallback effect")

    # Cleanup
    reset_sonnet_fallback()


def test_reset_time_string_format():
    """fallback_reset_time_str returns a human-readable time."""
    # Activate fallback
    reset_at = _get_next_friday_0900_utc()
    activate_sonnet_fallback(reset_at)

    reset_str = fallback_reset_time_str()

    # Should contain expected elements
    assert reset_str, "Reset string should not be empty"
    assert "UTC" in reset_str, "Should include UTC"
    assert any(day in reset_str for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]), \
        "Should include day of week"

    print(f"  ✓ Reset time format: {reset_str}")

    # Cleanup
    reset_sonnet_fallback()


def test_run_agent_fallback_sonnet_limit():
    """run_agent_with_fallback retries with Opus on Sonnet plan-limit and activates fallback."""
    import asyncio
    from orchestrator.agent import run_agent_with_fallback, AgentRun
    from claude_agent_sdk import ClaudeAgentOptions

    # Track which models were called
    calls = []

    # Mock run_agent to simulate Sonnet 429 then Opus success
    async def mock_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
        model = getattr(options, "model", "")
        calls.append(model)

        # First call (Sonnet) - simulate plan-limit error
        if "sonnet" in model.lower():
            return AgentRun(
                text="",
                final="",
                cost_usd=0.0,
                num_turns=0,
                is_error=True,
                input_tokens=0,
                output_tokens=0,
                is_plan_limit=True  # Sonnet hit a limit
            )

        # Second call (Opus) - success
        return AgentRun(
            text="Success",
            final="Success",
            cost_usd=0.5,
            num_turns=1,
            is_error=False,
            input_tokens=1000,
            output_tokens=500,
            is_plan_limit=False
        )

    # Patch run_agent
    import orchestrator.agent as agent_module
    original_run_agent = agent_module.run_agent
    agent_module.run_agent = mock_run_agent

    try:
        # Reset fallback state
        reset_sonnet_fallback()
        reset_sonnet_fallback_notification()

        # Create options for Sonnet
        options = ClaudeAgentOptions(
            model=SONNET,
            system_prompt="Test",
        )

        # Mock config with fallback enabled
        cfg = MockConfig()

        # Run the test
        result = asyncio.run(
            run_agent_with_fallback("test prompt", options, tag="test", cfg=cfg)
        )

        # Should have called both Sonnet and Opus
        assert len(calls) == 2, f"Expected 2 calls (Sonnet then Opus), got {len(calls)}: {calls}"
        assert "sonnet" in calls[0].lower(), f"First call should be Sonnet, got {calls[0]}"
        assert "opus" in calls[1].lower(), f"Second call should be Opus, got {calls[1]}"

        # Result should be the successful Opus call
        assert result.is_error is False, "Result should be successful (Opus)"
        assert result.final == "Success", "Should return Opus result"
        assert result.is_plan_limit is False, "Opus succeeded, so no plan limit"

        # Fallback should be activated
        assert sonnet_fallback_active(cfg) is True, "Fallback should be active after Sonnet limit → Opus success"

        print("  ✓ Sonnet limit → Opus retry → fallback activated")
    finally:
        # Restore original
        agent_module.run_agent = original_run_agent
        reset_sonnet_fallback()


def test_run_agent_fallback_all_models_cap():
    """run_agent_with_fallback pauses when both Sonnet and Opus hit limits (All-models cap)."""
    import asyncio
    from orchestrator.agent import run_agent_with_fallback, AgentRun
    from claude_agent_sdk import ClaudeAgentOptions

    # Track which models were called
    calls = []

    # Mock run_agent to simulate both Sonnet and Opus hitting plan limits
    async def mock_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
        model = getattr(options, "model", "")
        calls.append(model)

        # Both Sonnet and Opus hit plan limits
        return AgentRun(
            text="",
            final="",
            cost_usd=0.0,
            num_turns=0,
            is_error=True,
            input_tokens=0,
            output_tokens=0,
            is_plan_limit=True  # Hit a limit
        )

    # Patch run_agent
    import orchestrator.agent as agent_module
    original_run_agent = agent_module.run_agent
    agent_module.run_agent = mock_run_agent

    try:
        # Reset fallback state
        reset_sonnet_fallback()
        reset_sonnet_fallback_notification()

        # Create options for Sonnet
        options = ClaudeAgentOptions(
            model=SONNET,
            system_prompt="Test",
        )

        # Mock config with fallback enabled
        cfg = MockConfig()

        # Run the test
        result = asyncio.run(
            run_agent_with_fallback("test prompt", options, tag="test", cfg=cfg)
        )

        # Should have called both Sonnet and Opus
        assert len(calls) == 2, f"Expected 2 calls (Sonnet then Opus), got {len(calls)}: {calls}"
        assert "sonnet" in calls[0].lower(), f"First call should be Sonnet, got {calls[0]}"
        assert "opus" in calls[1].lower(), f"Second call should be Opus, got {calls[1]}"

        # Result should be the original Sonnet error (All-models cap)
        assert result.is_error is True, "Result should be error (All-models cap)"
        assert result.is_plan_limit is True, "Should indicate plan limit"

        # Fallback should NOT be activated (both models hit the same cap)
        assert sonnet_fallback_active(cfg) is False, "Fallback should NOT activate when both models limit (All-models cap)"

        print("  ✓ Both Sonnet and Opus limit → no fallback (All-models cap)")
    finally:
        # Restore original
        agent_module.run_agent = original_run_agent
        reset_sonnet_fallback()


def run_all():
    """Run all EU-108 tests."""
    print("\n🧪 EU-108: Sonnet-cap fallback tests\n")

    tests = [
        test_sonnet_fallback_activation,
        test_sonnet_fallback_auto_clears,
        test_sonnet_fallback_config_disabled,
        test_sonnet_fallback_reset,
        test_notification_sent_flag,
        test_friday_reset_calculation,
        test_model_selection_with_fallback,
        test_config_disabled_fallback_no_effect,
        test_reset_time_string_format,
        test_run_agent_fallback_sonnet_limit,
        test_run_agent_fallback_all_models_cap,
    ]

    for test in tests:
        try:
            # Reset state before each test
            reset_sonnet_fallback()
            reset_sonnet_fallback_notification()

            test()
        except AssertionError as e:
            print(f"  ✗ {test.__name__}: {e}")
            return False
        except Exception as e:
            print(f"  ✗ {test.__name__}: unexpected error: {e}")
            return False

    print("\n✓ All EU-108 tests passed\n")
    return True


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
