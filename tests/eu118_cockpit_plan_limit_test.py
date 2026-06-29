"""Tests for EU-118 plan-limit cockpit integration — state tracking, banner rendering, Telegram alerts."""
import os
import sys
import time
from unittest.mock import MagicMock, patch

# Add repo root to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from orchestrator.cockpit_state import (
    get_state,
    is_plan_limit_hit,
    plan_limit_reset_at,
    reset_workspaces,
    set_plan_limit_hit,
)
from orchestrator.cockpit_views import _plan_limit_banner
from orchestrator.notify import plan_limit_alert, reset_plan_limit_alert


class TestPlanLimitState:
    """Test plan-limit state tracking in cockpit_state.py."""

    def setup_method(self):
        """Reset state before each test."""
        reset_workspaces()

    def test_set_plan_limit_hit(self):
        """Setting plan-limit hit stores boolean and reset timestamp."""
        reset_ts = time.time() + 3600  # 1 hour from now
        set_plan_limit_hit("testapp", hit=True, reset_at=reset_ts)

        assert is_plan_limit_hit("testapp")
        assert plan_limit_reset_at("testapp") == reset_ts

    def test_set_plan_limit_clear(self):
        """Clearing plan-limit removes the hit state."""
        set_plan_limit_hit("testapp", hit=True, reset_at=time.time() + 3600)
        assert is_plan_limit_hit("testapp")

        set_plan_limit_hit("testapp", hit=False, reset_at=None)
        assert not is_plan_limit_hit("testapp")
        assert plan_limit_reset_at("testapp") is None

    def test_plan_limit_per_app(self):
        """Plan-limit state is per-app (isolated)."""
        set_plan_limit_hit("app1", hit=True, reset_at=time.time() + 3600)
        set_plan_limit_hit("app2", hit=False, reset_at=None)

        assert is_plan_limit_hit("app1")
        assert not is_plan_limit_hit("app2")

    def test_plan_limit_default_state(self):
        """Default state has no plan-limit hit."""
        assert not is_plan_limit_hit(None)
        assert not is_plan_limit_hit("nonexistent_app")
        assert plan_limit_reset_at(None) is None


class TestPlanLimitBanner:
    """Test plan-limit banner rendering in cockpit_views.py."""

    def setup_method(self):
        """Reset state before each test."""
        reset_workspaces()

    def test_banner_not_shown_when_no_limit(self):
        """Banner returns empty string when plan-limit not hit."""
        state = get_state("testapp")
        banner = _plan_limit_banner(state)

        assert banner == ""

    def test_banner_shown_when_limit_hit(self):
        """Banner renders when plan-limit is hit."""
        state = get_state("testapp")
        state["plan_limit_hit"] = True

        banner = _plan_limit_banner(state)

        # Check for warning emoji (HTML entity or literal) and key text
        assert ("&#9888;" in banner or "⚠" in banner or "⛔" in banner)
        assert "plan limit reached" in banner.lower()
        assert "implementation paused" in banner.lower()

    def test_banner_with_reset_time(self):
        """Banner includes reset timestamp when available."""
        state = get_state("testapp")
        state["plan_limit_hit"] = True
        state["plan_limit_reset_at"] = 1719700000  # Fixed timestamp

        banner = _plan_limit_banner(state)

        # Should include formatted time (day of week, date, time)
        assert "resets at" in banner.lower()

    def test_banner_persists_until_cleared(self):
        """Banner persists on multiple calls until limit is cleared."""
        state = get_state("testapp")
        state["plan_limit_hit"] = True

        banner1 = _plan_limit_banner(state)
        banner2 = _plan_limit_banner(state)

        # Both should render the banner (not one-shot like result_banner)
        assert ("&#9888;" in banner1 or "⚠" in banner1 or "⛔" in banner1)
        assert ("&#9888;" in banner2 or "⚠" in banner2 or "⛔" in banner2)


class TestPlanLimitTelegramAlert:
    """Test plan-limit Telegram alert in notify.py."""

    def setup_method(self):
        """Reset alert state before each test."""
        reset_plan_limit_alert()

    def test_alert_sends_once_per_session(self):
        """Alert sends only once per session even if called multiple times."""
        over_limits = [
            {"label": "Weekly Opus tokens", "utilization": 1.0, "resets_at": time.time() + 86400}
        ]
        reset_times = ["Mon Jun 30 14:30 UTC"]

        with patch("orchestrator.notify.send") as mock_send:
            mock_send.return_value = True

            # First call should send
            sent1 = plan_limit_alert(over_limits, reset_times)
            assert sent1
            assert mock_send.call_count == 1

            # Second call should NOT send (already sent this session)
            sent2 = plan_limit_alert(over_limits, reset_times)
            assert not sent2  # Returns False (not sent)
            assert mock_send.call_count == 1  # No additional call

    def test_alert_content_formatting(self):
        """Alert formats message with limit names and reset times."""
        over_limits = [
            {"label": "Weekly Opus tokens", "utilization": 1.0},
            {"label": "Session Sonnet", "utilization": 1.0}
        ]
        reset_times = ["Mon Jun 30 14:30 UTC", "Tue Jul 1 00:00 UTC"]

        with patch("orchestrator.notify.send") as mock_send:
            mock_send.return_value = True
            plan_limit_alert(over_limits, reset_times)

            # Check the call arguments
            call_args = mock_send.call_args
            message = call_args[0][0]  # First positional argument

            # Should be HTML formatted
            assert "<b>" in message
            assert "⛔" in message
            assert "Weekly Opus tokens" in message
            assert "Session Sonnet" in message
            assert "Mon Jun 30 14:30 UTC" in message

    def test_alert_returns_false_if_not_configured(self):
        """Alert returns False when Telegram is not configured."""
        with patch("orchestrator.notify.configured") as mock_configured:
            mock_configured.return_value = False

            sent = plan_limit_alert([], [])
            assert not sent

    def test_alert_reset_allows_resend(self):
        """After resetting, alert can be sent again."""
        over_limits = [{"label": "Weekly Opus tokens", "utilization": 1.0}]
        reset_times = ["Mon Jun 30 14:30 UTC"]

        with patch("orchestrator.notify.send") as mock_send:
            mock_send.return_value = True

            # Send first alert
            plan_limit_alert(over_limits, reset_times)
            assert mock_send.call_count == 1

            # Reset alert flag
            reset_plan_limit_alert()

            # Send second alert (should go through)
            plan_limit_alert(over_limits, reset_times)
            assert mock_send.call_count == 2

    def test_alert_handles_empty_limit_names(self):
        """Alert handles case where limits have no labels."""
        over_limits = [
            {"key": "unknown_limit", "utilization": 1.0}
        ]
        reset_times = ["unknown time"]

        with patch("orchestrator.notify.send") as mock_send:
            mock_send.return_value = True
            plan_limit_alert(over_limits, reset_times)

            message = mock_send.call_args[0][0]
            assert "unknown" in message


class TestPlanLimitIntegration:
    """Integration tests for plan-limit state across components."""

    def setup_method(self):
        """Reset state before each test."""
        reset_workspaces()
        reset_plan_limit_alert()

    def test_full_flow_hit_to_clear(self):
        """Test full flow: limit hit → alert → banner → clear."""
        # 1. Hit the limit
        reset_ts = time.time() + 3600
        set_plan_limit_hit("testapp", hit=True, reset_at=reset_ts)

        # 2. State should reflect hit
        assert is_plan_limit_hit("testapp")
        assert plan_limit_reset_at("testapp") == reset_ts

        # 3. Banner should show
        state = get_state("testapp")
        banner = _plan_limit_banner(state)
        assert ("&#9888;" in banner or "⚠" in banner or "⛔" in banner)

        # 4. Clear the limit
        set_plan_limit_hit("testapp", hit=False, reset_at=None)

        # 5. State should reflect cleared
        assert not is_plan_limit_hit("testapp")
        assert plan_limit_reset_at("testapp") is None

        # 6. Banner should not show
        state = get_state("testapp")
        banner = _plan_limit_banner(state)
        assert banner == ""

    def test_per_app_isolation(self):
        """Plan-limit state is isolated per app."""
        set_plan_limit_hit("app1", hit=True, reset_at=time.time() + 3600)
        set_plan_limit_hit("app2", hit=False, reset_at=None)

        # App1 should show banner
        state1 = get_state("app1")
        banner1 = _plan_limit_banner(state1)
        assert ("&#9888;" in banner1 or "⚠" in banner1 or "⛔" in banner1)

        # App2 should not show banner
        state2 = get_state("app2")
        banner2 = _plan_limit_banner(state2)
        assert banner2 == ""


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
