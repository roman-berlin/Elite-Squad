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
8. Error classification splits transient rate limits from genuine cap exhaustion
9. A transient 429 retries Sonnet with backoff and does NOT arm the weekly fallback
10. A failed (non-plan-limit) Opus probe does NOT arm the weekly fallback
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

    # Track which models were called + the full options object per call (2026-07-05 audit §6
    # defect 1: the Opus probe's rebuilt options silently dropped cwd/hooks/disallowed_tools —
    # capturing the object lets the fidelity assertions below pin the copy-based clone).
    calls = []
    captured_options = []

    # Mock run_agent to simulate Sonnet 429 then Opus success
    async def mock_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
        model = getattr(options, "model", "")
        calls.append(model)
        captured_options.append(options)

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
                is_plan_limit=True,       # Sonnet hit a limit
                plan_limit_kind="cap",    # cap-classified — only this kind may probe/arm
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

    # EU-139 gate incident (2026-07-05): this path fires the one-shot "Sonnet weekly cap" Telegram
    # alert, and with only the SDK stubbed the harness sent a REAL message to the ops chat when the
    # gate ran the suite inside the credential-loaded orchestrator. Stub notify.send so this harness
    # can never page the Commander — and capture the message so the alert behaviour stays pinned.
    from orchestrator import notify as notify_module
    sent: list[str] = []
    original_send = notify_module.send
    notify_module.send = lambda text, chat_id=None: (sent.append(text), True)[1]

    try:
        # Reset fallback state
        reset_sonnet_fallback()
        reset_sonnet_fallback_notification()

        # Create options for Sonnet — with every safety-relevant field set, so the fidelity
        # assertions below can prove the Opus probe preserves them (worktree cwd, guard hooks,
        # tool denylist — the exact fields the pre-fix clone dropped).
        guard_hooks = {"PreToolUse": ["guard-denylist"]}
        options = ClaudeAgentOptions(
            model=SONNET,
            system_prompt="Test",
            cwd="/work/some-ticket-worktree",
            permission_mode="bypassPermissions",
            allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            disallowed_tools=["NotebookEdit"],
            hooks=guard_hooks,
            setting_sources=[],
            max_turns=40,
            effort="high",
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

        # The one-shot alert must go through the (stubbed) notify.send, never a real channel
        assert any("Sonnet weekly cap" in m for m in sent), f"alert should be captured by the stub, got {sent}"

        # ── Option fidelity on the Opus probe (2026-07-05 audit §6 defect 1) ──
        # The probe's options must carry EVERY field of the original — cwd (worktree isolation),
        # hooks (guard denylist) and disallowed_tools were silently dropped by the old rebuild,
        # so the probe ran unguarded in the process CWD under bypassPermissions.
        opus_opts = captured_options[1]
        assert opus_opts is not options, "Opus probe must run on a copy, not mutate the original"
        assert getattr(opus_opts, "cwd", None) == "/work/some-ticket-worktree", \
            f"probe dropped cwd: {getattr(opus_opts, 'cwd', None)!r}"
        assert getattr(opus_opts, "hooks", None) == guard_hooks, \
            f"probe dropped the guard hooks: {getattr(opus_opts, 'hooks', None)!r}"
        assert getattr(opus_opts, "disallowed_tools", None) == ["NotebookEdit"], \
            f"probe dropped disallowed_tools: {getattr(opus_opts, 'disallowed_tools', None)!r}"
        assert getattr(opus_opts, "permission_mode", None) == "bypassPermissions"
        assert getattr(opus_opts, "allowed_tools", None) == ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
        assert getattr(opus_opts, "setting_sources", None) == []
        assert getattr(opus_opts, "max_turns", None) == 40
        assert getattr(opus_opts, "effort", None) == "high"
        assert getattr(opus_opts, "system_prompt", None) == "Test"
        # The original options must be untouched (still Sonnet) — the copy owns the model swap.
        assert getattr(options, "model", None) == SONNET, \
            f"original options mutated by the probe: {getattr(options, 'model', None)!r}"

        print("  ✓ Sonnet limit → Opus retry → fallback activated (alert captured; probe options faithful)")
    finally:
        # Restore original
        agent_module.run_agent = original_run_agent
        notify_module.send = original_send
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
            is_plan_limit=True,       # Hit a limit
            plan_limit_kind="cap",    # cap-classified — only this kind may probe/arm
        )

    # Patch run_agent
    import orchestrator.agent as agent_module
    original_run_agent = agent_module.run_agent
    agent_module.run_agent = mock_run_agent

    # EU-139 gate incident guard (see test_run_agent_fallback_sonnet_limit): never let this harness
    # reach the real notify.send. This path must not notify at all — assert that too.
    from orchestrator import notify as notify_module
    sent: list[str] = []
    original_send = notify_module.send
    notify_module.send = lambda text, chat_id=None: (sent.append(text), True)[1]

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

        # No fallback → no alert either
        assert not sent, f"All-models cap must not notify, got {sent}"

        print("  ✓ Both Sonnet and Opus limit → no fallback (All-models cap)")
    finally:
        # Restore original
        agent_module.run_agent = original_run_agent
        notify_module.send = original_send
        reset_sonnet_fallback()


def test_plan_limit_error_classification():
    """_classify_plan_limit splits transient rate limits from genuine cap exhaustion."""
    from orchestrator.agent import _classify_plan_limit

    # Genuine quota exhaustion — the message names the cap → eligible to probe/arm
    cap_errors = [
        "Claude usage limit reached|1751702400",
        "429 {'type': 'error', 'error': {'message': 'Weekly limit exceeded'}}",
        "Plan limit exceeded for this billing period",
        "usage-limit",
    ]
    for err in cap_errors:
        assert _classify_plan_limit(err) == "cap", f"Should classify as cap: {err}"

    # Transient blips — per-minute 429 / 529 overload → retry, never arm
    transient_errors = [
        "429 rate_limit_error: This request would exceed your per-minute rate limit",
        "429 Too Many Requests",
        "529 overloaded_error: The API is temporarily overloaded",
    ]
    for err in transient_errors:
        assert _classify_plan_limit(err) == "transient", f"Should classify as transient: {err}"

    # Non-limit errors classify as neither
    assert _classify_plan_limit("401 authentication_error: invalid x-api-key") == ""
    assert _classify_plan_limit("connection reset by peer") == ""

    print("  ✓ Error classification splits cap vs transient correctly")


def test_transient_429_does_not_arm_fallback():
    """A transient per-minute 429 retries Sonnet once — no Opus probe, no weekly arming."""
    import asyncio
    from orchestrator.agent import run_agent_with_fallback, AgentRun
    from claude_agent_sdk import ClaudeAgentOptions

    calls = []

    # Mock run_agent: first Sonnet call hits a transient 429, the retry succeeds
    async def mock_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
        model = getattr(options, "model", "")
        calls.append(model)

        if len(calls) == 1:
            return AgentRun(
                text="",
                final="",
                cost_usd=0.0,
                num_turns=0,
                is_error=True,
                input_tokens=0,
                output_tokens=0,
                is_plan_limit=True,
                plan_limit_kind="transient",  # per-minute 429, NOT a cap
            )

        return AgentRun(
            text="Success",
            final="Success",
            cost_usd=0.1,
            num_turns=1,
            is_error=False,
            input_tokens=500,
            output_tokens=200,
            is_plan_limit=False,
        )

    import orchestrator.agent as agent_module
    original_run_agent = agent_module.run_agent
    original_backoff = agent_module._TRANSIENT_RETRY_BACKOFF_S
    agent_module.run_agent = mock_run_agent
    agent_module._TRANSIENT_RETRY_BACKOFF_S = 0.0  # no sleeping in tests

    try:
        reset_sonnet_fallback()
        reset_sonnet_fallback_notification()

        options = ClaudeAgentOptions(model=SONNET, system_prompt="Test")
        cfg = MockConfig()

        result = asyncio.run(
            run_agent_with_fallback("test prompt", options, tag="test", cfg=cfg)
        )

        # Both calls stay on Sonnet — a transient blip must not trigger the Opus probe
        assert len(calls) == 2, f"Expected 2 calls (Sonnet, Sonnet retry), got {len(calls)}: {calls}"
        assert all("sonnet" in c.lower() for c in calls), f"All calls should be Sonnet, got {calls}"

        assert result.is_error is False, "Retry should succeed"
        assert result.final == "Success", "Should return the retry result"

        # The critical pin: the WEEKLY fallback stays disarmed
        assert sonnet_fallback_active(cfg) is False, \
            "Transient 429 must NOT arm the weekly Opus fallback"

        print("  ✓ Transient 429 → Sonnet retry, weekly fallback stays disarmed")
    finally:
        agent_module.run_agent = original_run_agent
        agent_module._TRANSIENT_RETRY_BACKOFF_S = original_backoff
        reset_sonnet_fallback()


def test_failed_opus_probe_does_not_arm_fallback():
    """An Opus probe that fails (auth/network, not plan-limit) must not arm the fallback."""
    import asyncio
    from orchestrator.agent import run_agent_with_fallback, AgentRun
    from claude_agent_sdk import ClaudeAgentOptions

    calls = []

    # Mock run_agent: Sonnet hits a genuine cap, but the Opus probe errors out (e.g. auth)
    async def mock_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
        model = getattr(options, "model", "")
        calls.append(model)

        if "sonnet" in model.lower():
            return AgentRun(
                text="",
                final="",
                cost_usd=0.0,
                num_turns=0,
                is_error=True,
                input_tokens=0,
                output_tokens=0,
                is_plan_limit=True,
                plan_limit_kind="cap",
            )

        # Opus probe: failed, but NOT a plan limit (auth error, network error, …)
        return AgentRun(
            text="",
            final="",
            cost_usd=0.0,
            num_turns=0,
            is_error=True,
            input_tokens=0,
            output_tokens=0,
            is_plan_limit=False,
        )

    import orchestrator.agent as agent_module
    original_run_agent = agent_module.run_agent
    agent_module.run_agent = mock_run_agent

    try:
        reset_sonnet_fallback()
        reset_sonnet_fallback_notification()

        options = ClaudeAgentOptions(model=SONNET, system_prompt="Test")
        cfg = MockConfig()

        result = asyncio.run(
            run_agent_with_fallback("test prompt", options, tag="test", cfg=cfg)
        )

        assert len(calls) == 2, f"Expected 2 calls (Sonnet then Opus), got {len(calls)}: {calls}"
        assert "opus" in calls[1].lower(), f"Second call should be the Opus probe, got {calls[1]}"

        # A broken probe proves nothing — surface the original Sonnet plan-limit error
        assert result.is_error is True, "Should return an error result"
        assert result.is_plan_limit is True, \
            "Should surface the Sonnet plan-limit error (for EU-82 pause handling)"

        # The critical pin: no arming off a failed probe
        assert sonnet_fallback_active(cfg) is False, \
            "Failed (non-plan-limit) Opus probe must NOT arm the weekly fallback"

        print("  ✓ Failed Opus probe → no arming, Sonnet error surfaced")
    finally:
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
        test_plan_limit_error_classification,
        test_transient_429_does_not_arm_fallback,
        test_failed_opus_probe_does_not_arm_fallback,
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
