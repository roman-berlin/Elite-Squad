#!/usr/bin/env python3
"""EU-202: GLM/z.ai quota limit detection and bidirectional backend switching tests.

Tests that:
1. agent._classify_plan_limit() recognizes GLM/z.ai error patterns (quota, credit, balance, etc.)
2. cockpit_views._plan_limit_banner() offers Continue-on for BOTH Opus→GLM AND GLM→Opus
3. The banner correctly labels which backend hit the limit
"""
from __future__ import annotations

import sys
import types

# Stub the Agent SDK (imported transitively) so import never needs a real model.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import agent, backends, cockpit_views

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ============ _classify_plan_limit recognizes GLM/z.ai errors ============ #
def test_classify_plan_limit_glm_errors():
    """Test that _classify_plan_limit recognizes GLM/z.ai quota error patterns.

    GLM/z.ai may return errors with different message formats than Anthropic's
    "usage limit" / "plan limit" patterns. This test verifies that GLM-specific
    patterns (quota, credit, balance, billing, insufficient) are also detected.
    """
    glm_error_patterns = [
        "quota exceeded",           # Generic quota exhausted
        "quota",                   # Just "quota" in error
        "credit",                  # Credit/billing related
        "balance",                 # Account balance issues
        "billing",                 # Billing problems
        "insufficient",            # Insufficient funds/quota
        "InsufficientBalance",     # API-style balance error
        "quota credit low",        # Combined quota + credit message
    ]

    for err in glm_error_patterns:
        kind = agent._classify_plan_limit(err)
        assert kind == "cap", f"GLM error '{err}' should classify as 'cap', got '{kind}'"

    print("  ✓ GLM/z.ai quota errors are classified as 'cap' for graceful pause")


def test_classify_plan_limit_transient_glm_errors():
    """Test that transient GLM rate limits are classified separately."""
    # GLM might also return transient 429s that aren't quota exhaustion
    transient_patterns = [
        "rate limit",
        "rate_limit",
        "too many requests",
        "overloaded",
        "429",
    ]

    for err in transient_patterns:
        kind = agent._classify_plan_limit(err)
        assert kind == "transient", f"Transient error '{err}' should classify as 'transient', got '{kind}'"

    print("  ✓ Transient GLM rate limits are classified as 'transient' (not cap)")


def test_classify_plan_limit_non_glm_errors():
    """Test that non-plan-limit errors return empty string."""
    non_limit_errors = [
        "internal server error",
        "timeout",
        "connection refused",
        "invalid request",
        "authentication failed",
    ]

    for err in non_limit_errors:
        kind = agent._classify_plan_limit(err)
        assert kind == "", f"Non-limit error '{err}' should classify as '', got '{kind}'"

    print("  ✓ Non-plan-limit errors return empty classification")


# ============ _plan_limit_banner bidirectional switching ============ #
def test_plan_limit_banner_opus_to_glm():
    """Test that the banner offers Continue-on-GLM when Opus hits a limit."""
    # Simulate active backend is NATIVE (Opus) and GLM is available
    had_token = sys.modules.get("orchestrator.backends")._glm_token() if hasattr(sys.modules.get("orchestrator.backends") or type(""), "_glm_token") else None

    import os
    os.environ["GLM_AUTH_TOKEN"] = "test-zai-key"

    # Mock backend_pref.active to return NATIVE
    import orchestrator.backend_pref as _bp
    orig_active = _bp.active
    _bp.active = lambda cfg=None: backends.NATIVE

    state = {
        "plan_limit_hit": True,
        "plan_limit_reset_at": 1_780_000_000,  # Non-zero to skip live usage fetch
        "last_run": {"app": "automatixy", "tickets": ["AUTO-99"]}
    }

    html = cockpit_views._plan_limit_banner(state, cfg=object())

    # Should show the banner
    assert "plan limit reached" in html.lower(), "Banner should show plan limit warning"
    # Should identify Claude as the limited backend
    assert "Claude" in html, "Banner should name Claude as the limited backend"
    # Should offer Continue-on-GLM
    assert "/api/continue-on-alternate" in html, "Banner should offer Continue-on-alternate"
    assert "Continue on GLM" in html or "Continue on GLM" in html, "Button should offer GLM"
    assert "AUTO-99" in html, "Continue button should name the paused ticket"

    # Restore
    _bp.active = orig_active
    if had_token is None:
        os.environ.pop("GLM_AUTH_TOKEN", None)

    print("  ✓ Opus limit → banner offers Continue-on-GLM with ticket name")


def test_plan_limit_banner_glm_to_opus():
    """Test that the banner offers Continue-on-Opus when GLM hits a limit."""
    # Simulate active backend is GLM and Opus is available
    import os
    os.environ["GLM_AUTH_TOKEN"] = "test-zai-key"

    # Mock backend_pref.active to return GLM
    import orchestrator.backend_pref as _bp
    orig_active = _bp.active
    _bp.active = lambda cfg=None: backends.GLM

    state = {
        "plan_limit_hit": True,
        "plan_limit_reset_at": 1_780_000_000,
        "last_run": {"app": "automatixy", "tickets": ["EU-202"]}
    }

    html = cockpit_views._plan_limit_banner(state, cfg=object())

    # Should show the banner
    assert "plan limit reached" in html.lower(), "Banner should show plan limit warning"
    # Should identify GLM as the limited backend
    assert "GLM" in html, "Banner should name GLM as the limited backend"
    # Should offer Continue-on-Opus (since alternates() returns NATIVE when current is GLM)
    assert "/api/continue-on-alternate" in html, "Banner should offer Continue-on-alternate"
    assert "Continue on Claude" in html, "Button should offer Claude (Opus)"
    assert "EU-202" in html, "Continue button should name the paused ticket"

    # Restore
    _bp.active = orig_active
    os.environ.pop("GLM_AUTH_TOKEN", None)

    print("  ✓ GLM limit → banner offers Continue-on-Opus with ticket name")


def test_plan_limit_banner_no_alternate():
    """Test that the banner shows warning but NO Continue button when no alternate is available."""
    # Remove GLM token so no alternate is available
    import os
    had_token = os.environ.get("GLM_AUTH_TOKEN")
    os.environ.pop("GLM_AUTH_TOKEN", None)

    import orchestrator.backend_pref as _bp
    orig_active = _bp.active
    _bp.active = lambda cfg=None: backends.NATIVE

    state = {
        "plan_limit_hit": True,
        "plan_limit_reset_at": 1_780_000_000,
        "last_run": {"app": "automatixy", "tickets": ["AUTO-99"]}
    }

    html = cockpit_views._plan_limit_banner(state, cfg=object())

    # Should still warn
    assert "plan limit reached" in html.lower(), "Banner should still warn when no alternate"
    # Should NOT offer Continue button
    assert "/api/continue-on-alternate" not in html, "Should NOT offer Continue when no alternate"

    # Restore
    _bp.active = orig_active
    if had_token is not None:
        os.environ["GLM_AUTH_TOKEN"] = had_token

    print("  ✓ No alternate available → banner warns but offers no Continue button")


def test_plan_limit_banner_no_hit():
    """Test that no banner shows when plan limit is not hit."""
    state = {
        "plan_limit_hit": False,  # Not hit
        "plan_limit_reset_at": None,
    }

    html = cockpit_views._plan_limit_banner(state, cfg=object())

    assert html == "", "Should return empty string when limit not hit"

    print("  ✓ No banner when plan limit is not hit")


def main():
    print("=" * 70)
    print("EU-202: GLM/z.ai quota limit detection + bidirectional switching")
    print("=" * 70)

    print("\n--- GLM error pattern classification ---")
    test_classify_plan_limit_glm_errors()
    test_classify_plan_limit_transient_glm_errors()
    test_classify_plan_limit_non_glm_errors()

    print("\n--- Bidirectional plan-limit banner ---")
    test_plan_limit_banner_opus_to_glm()
    test_plan_limit_banner_glm_to_opus()
    test_plan_limit_banner_no_alternate()
    test_plan_limit_banner_no_hit()

    print("\n" + "=" * 70)
    print("RESULT: ALL GREEN")
    print("=" * 70)


if __name__ == "__main__":
    main()
