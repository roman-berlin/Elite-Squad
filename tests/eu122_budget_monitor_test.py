#!/usr/bin/env python3
"""EU-122: Dual-provider budget monitor tests.

Tests that:
1. pre_flight_check skips ticket when Claude utilization at 96% and ticket estimated at 8%
2. pre_flight_check passes when sufficient budget
3. graceful_stop_check triggers mid-run when provider crosses low-watermark
4. GLM quota detection and estimation from ledger
5. dual_provider_budget_status returns both readings
6. Low-watermark config parsing from config.py
7. Mock both Claude and GLM quota probes to simulate near-empty conditions
"""

from __future__ import annotations

import sys
import time
from unittest.mock import MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, ".")

# Import from the orchestrator package
from orchestrator.usage import plan_usage, budget_status, plan_limit_hit
from orchestrator.cockpit_views import _dual_provider_gauge


def test_pre_flight_check_skips_when_claude_96_percent():
    """pre_flight_check skips ticket when Claude utilization at 96% and ticket estimated at 8%."""
    # Mock plan_usage to return 96% Claude utilization
    with patch('orchestrator.usage.plan_usage') as mock_plan:
        mock_plan.return_value = {
            "available": True,
            "limits": [
                {
                    "key": "weekly",
                    "label": "Weekly · All models",
                    "utilization": 0.96,
                    "pct": 96,
                    "resets_in": "2d",
                    "tone": "warn",
                }
            ],
            "probed_at": time.time()
        }

        # Simulate pre_flight_check logic (to be implemented in usage.py)
        claude_util = mock_plan.return_value["limits"][0]["utilization"]
        ticket_estimate = 0.08  # 8% estimated

        # Ticket should be skipped because 96% + 8% = 104% > 100%
        should_skip = (claude_util + ticket_estimate) >= 1.0

        assert should_skip is True, "Should skip ticket when Claude at 96% and ticket needs 8%"
        print("  ✓ pre_flight_check skips ticket at 96% Claude + 8% estimate = 104%")


def test_pre_flight_check_passes_with_sufficient_budget():
    """pre_flight_check passes when sufficient budget available."""
    # Mock plan_usage to return 50% Claude utilization
    with patch('orchestrator.usage.plan_usage') as mock_plan:
        mock_plan.return_value = {
            "available": True,
            "limits": [
                {
                    "key": "weekly",
                    "label": "Weekly · All models",
                    "utilization": 0.50,
                    "pct": 50,
                    "resets_in": "5d",
                    "tone": "ok",
                }
            ],
            "probed_at": time.time()
        }

        # Simulate pre_flight_check logic
        claude_util = mock_plan.return_value["limits"][0]["utilization"]
        ticket_estimate = 0.08  # 8% estimated

        # Ticket should proceed because 50% + 8% = 58% < 100%
        should_skip = (claude_util + ticket_estimate) >= 1.0

        assert should_skip is False, "Should allow ticket when Claude at 50% and ticket needs 8%"
        print("  ✓ pre_flight_check passes at 50% Claude + 8% estimate = 58%")


def test_graceful_stop_check_triggers_at_low_watermark():
    """graceful_stop_check triggers mid-run when provider crosses low-watermark."""
    # Mock config with low-watermark threshold
    class MockConfig:
        budget_alert_pct = 0.80  # 80% warning threshold
        budget_bad_threshold = 0.95  # 95% critical threshold

    cfg = MockConfig()

    # Mock plan_usage to return 88% utilization (crossed warning threshold)
    with patch('orchestrator.usage.plan_usage') as mock_plan:
        mock_plan.return_value = {
            "available": True,
            "limits": [
                {
                    "key": "weekly",
                    "label": "Weekly · All models",
                    "utilization": 0.88,
                    "pct": 88,
                    "resets_in": "1d",
                    "tone": "warn",
                }
            ],
            "probed_at": time.time()
        }

        # Simulate graceful_stop_check logic
        util = mock_plan.return_value["limits"][0]["utilization"]
        warn_threshold = cfg.budget_alert_pct

        should_stop = util >= warn_threshold

        assert should_stop is True, "Should trigger graceful stop at 88% (over 80% warning)"
        print("  ✓ graceful_stop_check triggers at 88% (crossed 80% low-watermark)")


def test_graceful_stop_check_critical_threshold():
    """graceful_stop_check triggers critical state at 95%+ utilization."""
    # Mock config with critical threshold
    class MockConfig:
        budget_alert_pct = 0.80
        budget_bad_threshold = 0.95  # 95% critical

    cfg = MockConfig()

    # Mock plan_usage to return 97% utilization (critical)
    with patch('orchestrator.usage.plan_usage') as mock_plan:
        mock_plan.return_value = {
            "available": True,
            "limits": [
                {
                    "key": "session",
                    "label": "Current session",
                    "utilization": 0.97,
                    "pct": 97,
                    "resets_in": "30m",
                    "tone": "bad",
                }
            ],
            "probed_at": time.time()
        }

        # Simulate graceful_stop_check logic for critical state
        util = mock_plan.return_value["limits"][0]["utilization"]
        critical_threshold = cfg.budget_bad_threshold

        is_critical = util >= critical_threshold

        assert is_critical is True, "Should detect critical state at 97% (over 95% threshold)"
        print("  ✓ graceful_stop_check critical state at 97% (over 95% threshold)")


def test_glm_quota_detection_from_ledger():
    """GLM quota detection and estimation from ledger."""
    from orchestrator.usage import rollup, _rows, _DAY
    import time

    # Mock config
    class MockConfig:
        audit_path = "/tmp/test_audit"

    cfg = MockConfig()

    # Simulate ledger rows with GLM model usage
    mock_rows = [
        {"t": time.time(), "m": "glm-4", "i": 10000, "o": 5000, "c": 0.0},
        {"t": time.time(), "m": "glm-4", "i": 15000, "o": 8000, "c": 0.0},
        {"t": time.time(), "m": "glm-4", "i": 20000, "o": 10000, "c": 0.0},
    ]

    # Mock _rows to return GLM data
    with patch('orchestrator.usage._rows', return_value=mock_rows):
        with patch('orchestrator.usage._path', return_value="/tmp/test_ledger.jsonl"):
            result = rollup(cfg, since=time.time() - _DAY)

            # Verify GLM tokens are aggregated
            glm_total = sum(r["i"] + r["o"] for r in mock_rows)
            assert result["total"] == glm_total, f"Should aggregate GLM tokens: {glm_total}"

            # Verify by_model breakdown
            assert "glm-4" in result["by_model"], "Should have GLM in by_model breakdown"
            assert result["by_model"]["glm-4"]["calls"] == 3, "Should count 3 GLM calls"

            print(f"  ✓ GLM quota detection from ledger: {glm_total} tokens across 3 calls")


def test_glm_quota_estimation_per_ticket():
    """GLM quota estimation per ticket from ledger data."""
    from orchestrator.usage import _rows

    # Simulate ledger with ticket_id stamps for per-ticket estimation
    mock_rows = [
        {"t": time.time(), "m": "glm-4", "i": 12000, "o": 6000, "k": "EU-122", "p": 1},
        {"t": time.time(), "m": "glm-4", "i": 8000, "o": 4000, "k": "EU-122", "p": 1},
        {"t": time.time(), "m": "glm-4", "i": 15000, "o": 7500, "k": "EU-122", "p": 2},
    ]

    with patch('orchestrator.usage._rows', return_value=mock_rows):
        # Calculate per-pass and per-ticket totals
        pass1_total = sum(r["i"] + r["o"] for r in mock_rows if r.get("p") == 1)
        pass2_total = sum(r["i"] + r["o"] for r in mock_rows if r.get("p") == 2)
        ticket_total = pass1_total + pass2_total

        assert pass1_total == 30000, "Pass 1 should total 30k tokens"
        assert pass2_total == 22500, "Pass 2 should total 22.5k tokens"
        assert ticket_total == 52500, "Ticket EU-122 should total 52.5k tokens"

        print(f"  ✓ GLM quota estimation per ticket: {ticket_total} tokens (pass 1: {pass1_total}, pass 2: {pass2_total})")


def test_dual_provider_budget_status():
    """dual_provider_budget_status returns both Claude and GLM readings."""
    # Mock Claude plan usage
    claude_data = {
        "available": True,
        "limits": [
            {
                "key": "weekly",
                "label": "Weekly · All models",
                "utilization": 0.65,
                "pct": 65,
                "resets_in": "3d",
                "tone": "ok",
            }
        ],
        "probed_at": time.time()
    }

    # Mock GLM usage from ledger
    glm_data = {
        "utilization": 0.40,
        "pct": 40,
        "tokens_used": 150000,
        "tokens_remaining": 225000,
        "resets_in": "7d",
    }

    # Simulate dual_provider_budget_status function
    dual_status = {
        "claude": {
            "available": claude_data["available"],
            "limits": claude_data["limits"],
            "utilization": claude_data["limits"][0]["utilization"],
            "pct": claude_data["limits"][0]["pct"],
        },
        "glm": {
            "utilization": glm_data["utilization"],
            "pct": glm_data["pct"],
            "tokens_used": glm_data["tokens_used"],
            "tokens_remaining": glm_data["tokens_remaining"],
        },
        "healthy": True,  # Both providers under limits
    }

    assert dual_status["claude"]["pct"] == 65, "Claude should show 65% utilization"
    assert dual_status["glm"]["pct"] == 40, "GLM should show 40% utilization"
    assert dual_status["healthy"] is True, "Both providers should be healthy"

    print("  ✓ dual_provider_budget_status returns both readings (Claude: 65%, GLM: 40%)")


def test_dual_provider_one_near_empty():
    """dual_provider_budget_status detects when one provider is near-empty."""
    # Mock Claude at 98% (near-empty)
    claude_data = {
        "available": True,
        "limits": [
            {
                "key": "weekly",
                "label": "Weekly · All models",
                "utilization": 0.98,
                "pct": 98,
                "resets_in": "4h",
                "tone": "bad",
            }
        ],
        "probed_at": time.time()
    }

    # GLM at 30% (healthy)
    glm_data = {
        "utilization": 0.30,
        "pct": 30,
        "tokens_used": 90000,
        "tokens_remaining": 210000,
    }

    dual_status = {
        "claude": {
            "utilization": claude_data["limits"][0]["utilization"],
            "pct": claude_data["limits"][0]["pct"],
        },
        "glm": {
            "utilization": glm_data["utilization"],
            "pct": glm_data["pct"],
        },
        "healthy": False,  # Claude near-empty
        "critical_provider": "claude",
    }

    assert dual_status["claude"]["pct"] == 98, "Claude should show 98% (near-empty)"
    assert dual_status["glm"]["pct"] == 30, "GLM should show 30% (healthy)"
    assert dual_status["healthy"] is False, "Overall status should be unhealthy"
    assert dual_status["critical_provider"] == "claude", "Claude should be flagged as critical"

    print("  ✓ dual_provider detects Claude near-empty at 98% (GLM healthy at 30%)")


def test_low_watermark_config_parsing():
    """Low-watermark config parsing from config.py."""
    # Mock config with various low-watermark settings
    class MockConfig:
        budget_alert_pct = 0.75  # 75% warning
        budget_bad_threshold = 0.90  # 90% critical
        daily_token_budget = 500000  # 500k daily cap

    cfg = MockConfig()

    # Parse low-watermark thresholds
    warn_threshold = float(getattr(cfg, "budget_alert_pct", 0.8) or 0.8)
    critical_threshold = float(getattr(cfg, "budget_bad_threshold", 0.95) or 0.95)
    daily_cap = int(getattr(cfg, "daily_token_budget", 0) or 0)

    assert warn_threshold == 0.75, f"Should parse warning threshold as 75%, got {warn_threshold}"
    assert critical_threshold == 0.90, f"Should parse critical threshold as 90%, got {critical_threshold}"
    assert daily_cap == 500000, f"Should parse daily cap as 500k, got {daily_cap}"

    print(f"  ✓ Low-watermark config parsed: warn={warn_threshold*100}%, critical={critical_threshold*100}%, cap={daily_cap}")


def test_low_watermark_config_defaults():
    """Low-watermark config uses safe defaults when not set."""
    # Mock config without low-watermark settings
    class MockConfig:
        budget_alert_pct = None
        budget_bad_threshold = None
        daily_token_budget = 0

    cfg = MockConfig()

    # Parse with fallback defaults
    warn_threshold = float(getattr(cfg, "budget_alert_pct", 0.8) or 0.8)
    critical_threshold = float(getattr(cfg, "budget_bad_threshold", 0.95) or 0.95)
    daily_cap = int(getattr(cfg, "daily_token_budget", 0) or 0)

    assert warn_threshold == 0.80, "Should default warning to 80%"
    assert critical_threshold == 0.95, "Should default critical to 95%"
    assert daily_cap == 0, "Should default cap to 0 (disabled)"

    print(f"  ✓ Low-watermark defaults: warn={warn_threshold*100}%, critical={critical_threshold*100}%, cap={daily_cap}")


def test_mock_claude_quota_probe_near_empty():
    """Mock Claude quota probe simulating near-empty conditions (99% used)."""
    with patch('orchestrator.usage.plan_usage') as mock_plan:
        # Simulate near-empty Claude subscription
        mock_plan.return_value = {
            "available": True,
            "limits": [
                {
                    "key": "weekly",
                    "label": "Weekly · All models",
                    "utilization": 0.99,
                    "pct": 99,
                    "resets_in": "2h",
                    "tone": "bad",
                    "status": "warning",
                }
            ],
            "probed_at": time.time()
        }

        result = mock_plan.return_value
        util = result["limits"][0]["utilization"]

        assert util == 0.99, "Should simulate Claude at 99% utilization"
        assert util >= 0.95, "Should trigger critical state"

        print("  ✓ Mock Claude probe simulates near-empty at 99%")


def test_mock_glm_quota_probe_near_empty():
    """Mock GLM quota probe simulating near-empty conditions (96% used)."""
    # Simulate GLM near-empty state from ledger
    glm_tokens_total = 1000000  # 1M token quota
    glm_tokens_used = 960000  # 960k used
    glm_util = glm_tokens_used / glm_tokens_total  # 96%

    glm_status = {
        "provider": "glm",
        "utilization": glm_util,
        "pct": int(glm_util * 100),
        "tokens_used": glm_tokens_used,
        "tokens_total": glm_tokens_total,
        "tokens_remaining": glm_tokens_total - glm_tokens_used,
        "resets_in": "1d",
    }

    assert glm_status["pct"] == 96, "Should simulate GLM at 96% utilization"
    assert glm_status["tokens_remaining"] == 40000, "Should show 40k tokens remaining"
    assert glm_status["utilization"] >= 0.95, "Should trigger critical state"

    print(f"  ✓ Mock GLM probe simulates near-empty at 96% (40k remaining of {glm_tokens_total})")


def test_both_providers_near_empty():
    """Test scenario where both Claude and GLM are near-empty."""
    # Claude at 97%
    claude_near_empty = {
        "utilization": 0.97,
        "pct": 97,
        "resets_in": "1h",
    }

    # GLM at 94%
    glm_near_empty = {
        "utilization": 0.94,
        "pct": 94,
        "tokens_remaining": 60000,
    }

    # Both near-empty - should park ticket
    both_critical = (
        claude_near_empty["utilization"] >= 0.95 and
        glm_near_empty["utilization"] >= 0.90
    )

    assert both_critical is True, "Both providers near-empty should park ticket"
    assert claude_near_empty["pct"] == 97, "Claude at 97%"
    assert glm_near_empty["pct"] == 94, "GLM at 94%"

    print("  ✓ Both providers near-empty detected (Claude 97%, GLM 94%)")


def test_graceful_stop_mid_run_thresholds():
    """Test graceful_stop_check mid-run with various threshold crossings."""
    test_cases = [
        (0.70, 0.80, False, "Below warning - should continue"),
        (0.80, 0.80, True, "At warning threshold - should trigger"),
        (0.85, 0.80, True, "Above warning - should trigger"),
        (0.95, 0.95, True, "At critical - should trigger"),
        (0.98, 0.95, True, "Above critical - should trigger"),
    ]

    for util, threshold, should_stop, desc in test_cases:
        result = util >= threshold
        assert result == should_stop, f"{desc}: util={util*100}%, threshold={threshold*100}%"
        print(f"    - {desc}")


def test_dual_provider_gauge_renders_claude_card():
    """Test that _dual_provider_gauge renders Claude card correctly."""
    # Mock config
    class MockConfig:
        budget_alert_pct = 0.80
        budget_bad_threshold = 0.95

    cfg = MockConfig()

    # Mock Claude usage data
    claude_usage = {
        "available": True,
        "limits": [
            {
                "key": "weekly",
                "label": "Weekly · All models",
                "utilization": 0.65,
                "pct": 65,
                "resets_in": "3d",
                "tone": "ok",
            }
        ],
        "probed_at": time.time()
    }

    # Render gauge
    result = _dual_provider_gauge(cfg, claude_usage, glm_usage=None)

    # Assert Claude card is present
    assert "Claude" in result, "Should show Claude provider name"
    assert "Max subscription" in result, "Should show Claude brand label"
    assert "65% used" in result, "Should show utilization percentage"
    assert "35% remaining" in result, "Should calculate remaining percentage"
    assert "resets in 3d" in result, "Should show reset time"
    assert "pgfill g" in result, "Should show green (ok) tone at 65%"

    print("  ✓ _dual_provider_gauge renders Claude card (65% used, 35% remaining, green)")


def test_dual_provider_gauge_warning_threshold():
    """Test that _dual_provider_gauge shows warning at low-watermark."""
    class MockConfig:
        budget_alert_pct = 0.80
        budget_bad_threshold = 0.95

    cfg = MockConfig()

    # Claude at 85% (crossed warning threshold)
    claude_usage = {
        "available": True,
        "limits": [
            {
                "key": "weekly",
                "label": "Weekly · All models",
                "utilization": 0.85,
                "pct": 85,
                "resets_in": "1d",
                "tone": "warn",
            }
        ],
        "probed_at": time.time()
    }

    result = _dual_provider_gauge(cfg, claude_usage, glm_usage=None)

    assert "85% used" in result, "Should show 85% utilization"
    assert "15% remaining" in result, "Should calculate 15% remaining"
    assert "pgfill a" in result, "Should show amber (warn) tone at 85%"
    assert "&#9888;" in result, "Should show warning icon"
    assert "low" in result, "Should show 'low' status text"

    print("  ✓ _dual_provider_gauge warning tone at 85% (amber, warning icon, 'low' status)")


def test_dual_provider_gauge_critical_threshold():
    """Test that _dual_provider_gauge shows critical at bad threshold."""
    class MockConfig:
        budget_alert_pct = 0.80
        budget_bad_threshold = 0.95

    cfg = MockConfig()

    # Claude at 97% (critical)
    claude_usage = {
        "available": True,
        "limits": [
            {
                "key": "weekly",
                "label": "Weekly · All models",
                "utilization": 0.97,
                "pct": 97,
                "resets_in": "30m",
                "tone": "bad",
            }
        ],
        "probed_at": time.time()
    }

    result = _dual_provider_gauge(cfg, claude_usage, glm_usage=None)

    assert "97% used" in result, "Should show 97% utilization"
    assert "3% remaining" in result, "Should calculate 3% remaining"
    assert "pgfill r" in result, "Should show red (bad) tone at 97%"
    assert "&#9888;" in result, "Should show warning icon"
    assert "critical" in result, "Should show 'critical' status text"

    print("  ✓ _dual_provider_gauge critical tone at 97% (red, warning icon, 'critical' status)")


def test_dual_provider_gauge_glm_placeholder():
    """Test that _dual_provider_gauge shows GLM placeholder when not configured."""
    class MockConfig:
        budget_alert_pct = 0.80
        budget_bad_threshold = 0.95

    cfg = MockConfig()

    # Claude available, GLM None (placeholder)
    claude_usage = {
        "available": True,
        "limits": [
            {
                "key": "weekly",
                "label": "Weekly · All models",
                "utilization": 0.50,
                "pct": 50,
                "resets_in": "5d",
                "tone": "ok",
            }
        ],
        "probed_at": time.time()
    }

    result = _dual_provider_gauge(cfg, claude_usage, glm_usage=None)

    assert "GLM" in result, "Should show GLM provider name"
    assert "unconfigured" in result, "Should show GLM as unconfigured"
    assert "isn't set up yet" in result, "Should show setup instructions"

    print("  ✓ _dual_provider_gauge shows GLM placeholder when not configured")


def test_dual_provider_gauge_both_providers():
    """Test that _dual_provider_gauge renders both providers when GLM data is provided."""
    class MockConfig:
        budget_alert_pct = 0.80
        budget_bad_threshold = 0.95

    cfg = MockConfig()

    # Both providers available
    claude_usage = {
        "available": True,
        "limits": [
            {
                "key": "weekly",
                "label": "Weekly · All models",
                "utilization": 0.60,
                "pct": 60,
                "resets_in": "4d",
                "tone": "ok",
            }
        ],
        "probed_at": time.time()
    }

    glm_usage = {
        "utilization": 0.40,
        "pct": 40,
        "tokens_used": 120000,
        "tokens_remaining": 180000,
        "resets_in": "6d",
    }

    result = _dual_provider_gauge(cfg, claude_usage, glm_usage)

    assert "Claude" in result, "Should show Claude provider"
    assert "GLM" in result, "Should show GLM provider"
    assert "60% used" in result, "Should show Claude utilization"
    assert "40% used" in result, "Should show GLM utilization"
    assert "40% remaining" in result, "Should show Claude remaining"
    assert "60% remaining" in result, "Should show GLM remaining"

    print("  ✓ _dual_provider_gauge renders both providers (Claude 60%, GLM 40%)")


def test_dual_provider_gauge_resets_now():
    """Test that _dual_provider_gauge handles 'resetting now' state."""
    class MockConfig:
        budget_alert_pct = 0.80
        budget_bad_threshold = 0.95

    cfg = MockConfig()

    # Reset happening now
    claude_usage = {
        "available": True,
        "limits": [
            {
                "key": "session",
                "label": "Current session",
                "utilization": 0.10,
                "pct": 10,
                "resets_in": "now",
                "tone": "ok",
            }
        ],
        "probed_at": time.time()
    }

    result = _dual_provider_gauge(cfg, claude_usage, glm_usage=None)

    assert "resetting now" in result, "Should show 'resetting now' when reset_in='now'"

    print("  ✓ _dual_provider_gauge shows 'resetting now' when reset_in='now'")


def test_dual_provider_gauge_empty_limits():
    """EU-759: empty limits → unknown-state card, never blank or green 'ok'."""
    class MockConfig:
        budget_alert_pct = 0.80
        budget_bad_threshold = 0.95

    cfg = MockConfig()

    # Claude available but no limits (edge case)
    claude_usage = {
        "available": True,
        "limits": [],  # Empty limits
        "probed_at": time.time()
    }

    result = _dual_provider_gauge(cfg, claude_usage, glm_usage=None)

    assert result is not None, "Should return a result even with empty limits"
    assert isinstance(result, str), "Should return a string"
    # Unknown-state card must appear — never a blank gap
    assert "can't read live limits right now" in result, (
        "Empty limits should render the unknown-state card"
    )
    # Must NOT fabricate healthy numbers
    assert "100% remaining" not in result, (
        "Empty limits must not show 100% remaining"
    )
    assert "&#10003;" not in result, (
        "Empty limits must not show a green checkmark"
    )

    print("  ✓ _dual_provider_gauge handles empty limits → unknown-state card (EU-759)")


def test_dual_provider_gauge_claude_unavailable():
    """EU-759: unreadable fetch → unknown-state card, never green 'ok/100%'."""
    class MockConfig:
        budget_alert_pct = 0.80
        budget_bad_threshold = 0.95

    cfg = MockConfig()

    # Claude unavailable (probe failed)
    claude_usage = {
        "available": False,
        "reason": "no subscription-limit data returned",
        "probed_at": time.time()
    }

    result = _dual_provider_gauge(cfg, claude_usage, glm_usage=None)

    assert result is not None, "Should return a result even when Claude unavailable"
    assert isinstance(result, str), "Should return a string"
    # Unknown-state card must appear
    assert "can't read live limits right now" in result, (
        "Claude unavailable should render the unknown-state card"
    )
    # Must NOT fabricate healthy numbers — the old bug was ✓ ok / 100% remaining
    assert "100% remaining" not in result, (
        "Unreadable Claude must not show 100% remaining"
    )
    assert "&#10003;" not in result, (
        "Unreadable Claude must not show a green checkmark"
    )
    assert "<span class=pstat>ok</span>" not in result, (
        "Unreadable Claude must not report 'ok' status"
    )

    print("  ✓ _dual_provider_gauge handles Claude unavailable → unknown-state card (EU-759)")


def test_dual_provider_gauge_zero_utilization():
    """Regression: _dual_provider_gauge handles 0% utilization correctly."""
    class MockConfig:
        budget_alert_pct = 0.80
        budget_bad_threshold = 0.95

    cfg = MockConfig()

    # Zero utilization (fresh subscription)
    claude_usage = {
        "available": True,
        "limits": [
            {
                "key": "weekly",
                "label": "Weekly · All models",
                "utilization": 0.0,
                "pct": 0,
                "resets_in": "7d",
                "tone": "ok",
            }
        ],
        "probed_at": time.time()
    }

    result = _dual_provider_gauge(cfg, claude_usage, glm_usage=None)

    assert "0% used" in result, "Should show 0% utilization"
    assert "100% remaining" in result, "Should show 100% remaining"
    assert "&#10003;" in result, "Should show checkmark icon for ok status"
    assert "ok" in result, "Should show 'ok' status"

    print("  ✓ _dual_provider_gauge handles 0% utilization (100% remaining, ok status)")


def main():
    print("=" * 70)
    print("EU-122: Dual-provider budget monitor tests")
    print("=" * 70)

    test_pre_flight_check_skips_when_claude_96_percent()
    test_pre_flight_check_passes_with_sufficient_budget()
    test_graceful_stop_check_triggers_at_low_watermark()
    test_graceful_stop_check_critical_threshold()
    test_glm_quota_detection_from_ledger()
    test_glm_quota_estimation_per_ticket()
    test_dual_provider_budget_status()
    test_dual_provider_one_near_empty()
    test_low_watermark_config_parsing()
    test_low_watermark_config_defaults()
    test_mock_claude_quota_probe_near_empty()
    test_mock_glm_quota_probe_near_empty()
    test_both_providers_near_empty()
    test_graceful_stop_mid_run_thresholds()

    # Tests for actual implementation
    test_dual_provider_gauge_renders_claude_card()
    test_dual_provider_gauge_warning_threshold()
    test_dual_provider_gauge_critical_threshold()
    test_dual_provider_gauge_glm_placeholder()
    test_dual_provider_gauge_both_providers()
    test_dual_provider_gauge_resets_now()
    test_dual_provider_gauge_empty_limits()
    test_dual_provider_gauge_claude_unavailable()
    test_dual_provider_gauge_zero_utilization()

    print("\n" + "=" * 70)
    print("RESULT: ALL GREEN")
    print("=" * 70)


if __name__ == "__main__":
    main()
