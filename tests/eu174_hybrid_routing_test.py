#!/usr/bin/env python3
"""EU-174: Hybrid LLM Routing tests.

Tests that:
1. Task classification correctly routes to local vs cloud tiers
2. Routing tier can be determined from environment variables
3. Agent integration works for both local and cloud routing
4. Routing is disabled by default (backward compatibility)
5. Local routing uses correct Ollama endpoint configuration
6. Cloud routing uses default Anthropic/GLM endpoint configuration
"""

from __future__ import annotations

import sys
import os
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, ".")

# Mock the Claude Agent SDK BEFORE any imports
types = __import__("types")
sdk = types.ModuleType("claude_agent_sdk")


class ClaudeAgentOptions:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _MockMessage:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class AssistantMessage(_MockMessage):
    pass


class ResultMessage(_MockMessage):
    pass


class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, name, input):
        self.name = name
        self.input = input


sdk.ClaudeAgentOptions = ClaudeAgentOptions
sdk.AssistantMessage = AssistantMessage
sdk.ResultMessage = ResultMessage
sdk.TextBlock = TextBlock
sdk.ToolUseBlock = ToolUseBlock


def _mock_query(prompt, options, **kwargs):
    """Mock query function that simulates tier-based routing."""

    async def _gen():
        # Return a successful result
        yield AssistantMessage(content=[TextBlock(text="Mock agent response")])
        yield ResultMessage(
            result="Mock agent response",
            total_cost_usd=0.1,
            num_turns=1,
            is_error=False,
            usage={"input_tokens": 1000, "output_tokens": 500}
        )
    return _gen()


sdk.query = _mock_query
sys.modules["claude_agent_sdk"] = sdk

from orchestrator import routing, agent
from orchestrator.routing import RoutingTier


def test_task_classification_tier2_keywords():
    """Tasks with complex architectural keywords route to Tier 2 (cloud)."""
    tier2_tasks = [
        "Implement multi-tenant database isolation",
        "Deep architectural refactor of authentication system",
        "Investigate production performance issue",
        "Review and implement Row Level Security policies",
        "Complex debugging of tenant separation logic",
    ]

    for task in tier2_tasks:
        tier = routing.classify_task(ticket_description=task)
        assert tier == RoutingTier.CLOUD, \
            f"Task '{task}' should route to CLOUD, got {tier}"

    print("  ✓ Tier 2 keywords correctly route to cloud")


def test_task_classification_tier1_keywords():
    """Tasks with routine keywords route to Tier 1 (local)."""
    tier1_tasks = [
        "Parse JSON response from API",
        "Add unit test for user utility",
        "Generate boilerplate for new component",
        "Fix typo in documentation",
        "Update configuration file format",
    ]

    for task in tier1_tasks:
        tier = routing.classify_task(ticket_description=task)
        assert tier == RoutingTier.LOCAL, \
            f"Task '{task}' should route to LOCAL, got {tier}"

    print("  ✓ Tier 1 keywords correctly route to local")


def test_task_classification_by_size():
    """Large/XL tasks route to cloud regardless of keywords."""
    large_tasks = [
        ("Some task", "", "", "L"),
        ("Another task", "", "", "XL"),
    ]

    for task_desc, task_type, effort, size in large_tasks:
        tier = routing.classify_task(
            ticket_description=task_desc,
            task_type=task_type,
            effort=effort,
            size=size,
        )
        assert tier == RoutingTier.CLOUD, \
            f"Size {size} task should route to CLOUD, got {tier}"

    print("  ✓ Large/XL tasks correctly route to cloud")


def test_task_classification_by_effort():
    """High/max effort tasks route to cloud."""
    high_effort_tasks = [
        ("Some task", "", "high", ""),
        ("Another task", "", "max", ""),
        ("Complex task", "", "xhigh", ""),
    ]

    for task_desc, task_type, effort, size in high_effort_tasks:
        tier = routing.classify_task(
            ticket_description=task_desc,
            task_type=task_type,
            effort=effort,
            size=size,
        )
        assert tier == RoutingTier.CLOUD, \
            f"Effort {effort} task should route to CLOUD, got {tier}"

    print("  ✓ High/max effort tasks correctly route to cloud")


def test_task_classification_defaults():
    """Small/medium tasks without keywords default to local."""
    default_tasks = [
        ("Simple fix", "", "low", "S"),
        ("Medium task", "", "medium", "M"),
        ("Another task", "", "", ""),
    ]

    for task_desc, task_type, effort, size in default_tasks:
        tier = routing.classify_task(
            ticket_description=task_desc,
            task_type=task_type,
            effort=effort,
            size=size,
        )
        assert tier == RoutingTier.LOCAL, \
            f"Default task should route to LOCAL, got {tier}"

    print("  ✓ Default tasks correctly route to local")


def test_base_url_for_tier():
    """Base URL retrieval works for both tiers."""
    # Save original
    original_anthropic = os.environ.get("ANTHROPIC_BASE_URL")
    original_ollama = os.environ.get("OLLAMA_BASE_URL")

    try:
        os.environ["ANTHROPIC_BASE_URL"] = "https://api.anthropic.com"
        os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434"

        cloud_url = routing.get_base_url_for_tier(RoutingTier.CLOUD)
        local_url = routing.get_base_url_for_tier(RoutingTier.LOCAL)

        assert cloud_url == "https://api.anthropic.com", \
            f"Cloud URL should be Anthropic endpoint, got {cloud_url}"
        assert local_url == "http://localhost:11434", \
            f"Local URL should be Ollama endpoint, got {local_url}"

        print("  ✓ Base URLs retrieved correctly for both tiers")
    finally:
        # Restore
        if original_anthropic:
            os.environ["ANTHROPIC_BASE_URL"] = original_anthropic
        elif "ANTHROPIC_BASE_URL" in os.environ:
            del os.environ["ANTHROPIC_BASE_URL"]

        if original_ollama:
            os.environ["OLLAMA_BASE_URL"] = original_ollama
        elif "OLLAMA_BASE_URL" in os.environ:
            del os.environ["OLLAMA_BASE_URL"]


def test_api_key_for_tier():
    """API key retrieval works for both tiers."""
    # Save original
    original_anthropic = os.environ.get("ANTHROPIC_API_KEY")
    original_ollama = os.environ.get("OLLAMA_API_KEY")

    try:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        os.environ["OLLAMA_API_KEY"] = "ollama-test-key"

        cloud_key = routing.get_api_key_for_tier(RoutingTier.CLOUD)
        local_key = routing.get_api_key_for_tier(RoutingTier.LOCAL)

        assert cloud_key == "sk-ant-test", \
            f"Cloud key should be Anthropic key, got {cloud_key}"
        assert local_key == "ollama-test-key", \
            f"Local key should be Ollama key, got {local_key}"

        print("  ✓ API keys retrieved correctly for both tiers")
    finally:
        # Restore
        if original_anthropic:
            os.environ["ANTHROPIC_API_KEY"] = original_anthropic
        elif "ANTHROPIC_API_KEY" in os.environ:
            del os.environ["ANTHROPIC_API_KEY"]

        if original_ollama:
            os.environ["OLLAMA_API_KEY"] = original_ollama
        elif "OLLAMA_API_KEY" in os.environ:
            del os.environ["OLLAMA_API_KEY"]


def test_model_for_tier():
    """Model retrieval works for both tiers."""
    # Save original
    original_anthropic_model = os.environ.get("ANTHROPIC_MODEL")
    original_ollama_model = os.environ.get("OLLAMA_MODEL")

    try:
        os.environ["ANTHROPIC_MODEL"] = "glm-5.2"
        os.environ["OLLAMA_MODEL"] = "glm-4.7-flash:q4_K_M"

        cloud_model = routing.get_model_for_tier(RoutingTier.CLOUD)
        local_model = routing.get_model_for_tier(RoutingTier.LOCAL)

        assert cloud_model == "glm-5.2", \
            f"Cloud model should be glm-5.2, got {cloud_model}"
        assert local_model == "glm-4.7-flash:q4_K_M", \
            f"Local model should be glm-4.7-flash:q4_K_M, got {local_model}"

        print("  ✓ Models retrieved correctly for both tiers")
    finally:
        # Restore
        if original_anthropic_model:
            os.environ["ANTHROPIC_MODEL"] = original_anthropic_model
        elif "ANTHROPIC_MODEL" in os.environ:
            del os.environ["ANTHROPIC_MODEL"]

        if original_ollama_model:
            os.environ["OLLAMA_MODEL"] = original_ollama_model
        elif "OLLAMA_MODEL" in os.environ:
            del os.environ["OLLAMA_MODEL"]


def test_routing_disabled_by_default():
    """Routing is disabled by default for backward compatibility."""
    assert routing.is_routing_enabled() is False, \
        "Routing should be disabled by default"

    print("  ✓ Routing is disabled by default")


def test_routing_enabled_from_env():
    """Routing can be enabled via environment variable."""
    # Save original
    original = os.environ.get("ROUTING_ENABLED")

    try:
        os.environ["ROUTING_ENABLED"] = "true"
        assert routing.is_routing_enabled() is True, \
            "Routing should be enabled when ROUTING_ENABLED=true"

        os.environ["ROUTING_ENABLED"] = "1"
        assert routing.is_routing_enabled() is True, \
            "Routing should be enabled when ROUTING_ENABLED=1"

        print("  ✓ Routing can be enabled via environment")
    finally:
        # Restore
        if original:
            os.environ["ROUTING_ENABLED"] = original
        elif "ROUTING_ENABLED" in os.environ:
            del os.environ["ROUTING_ENABLED"]


def test_routing_tier_from_env():
    """Routing tier can be configured via environment variable."""
    # Save original
    original = os.environ.get("ROUTING_TIER")

    try:
        os.environ["ROUTING_TIER"] = "local"
        tier = routing.routing_tier_from_env()
        assert tier == RoutingTier.LOCAL, \
            f"ROUTING_TIER=local should return LOCAL tier, got {tier}"

        os.environ["ROUTING_TIER"] = "cloud"
        tier = routing.routing_tier_from_env()
        assert tier == RoutingTier.CLOUD, \
            f"ROUTING_TIER=cloud should return CLOUD tier, got {tier}"

        # Default is cloud
        del os.environ["ROUTING_TIER"]
        tier = routing.routing_tier_from_env()
        assert tier == RoutingTier.CLOUD, \
            f"Default ROUTING_TIER should be CLOUD, got {tier}"

        print("  ✓ Routing tier can be configured via environment")
    finally:
        # Restore
        if original:
            os.environ["ROUTING_TIER"] = original
        elif "ROUTING_TIER" in os.environ:
            del os.environ["ROUTING_TIER"]


def test_should_route_to_local():
    """Convenience function correctly identifies local-routing tasks."""
    # Save original
    original_enabled = os.environ.get("ROUTING_ENABLED")
    os.environ["ROUTING_ENABLED"] = "true"

    try:
        # Tier 1 task
        should_local = routing.should_route_to_local(
            ticket_description="Parse JSON data",
            task_type="build",
            effort="low",
            size="S",
        )
        assert should_local is True, \
            "Tier 1 task should route to local"

        # Tier 2 task
        should_local = routing.should_route_to_local(
            ticket_description="Multi-tenant architecture refactor",
            task_type="build",
            effort="high",
            size="L",
        )
        assert should_local is False, \
            "Tier 2 task should NOT route to local"

        print("  ✓ should_route_to_local convenience function works")
    finally:
        # Restore
        if original_enabled:
            os.environ["ROUTING_ENABLED"] = original_enabled
        elif "ROUTING_ENABLED" in os.environ:
            del os.environ["ROUTING_ENABLED"]


def test_agent_integration_local_routing():
    """Agent integration with local routing doesn't break."""
    import asyncio

    # Save original
    original_base_url = os.environ.get("ANTHROPIC_BASE_URL")
    original_api_key = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434"
    os.environ["OLLAMA_API_KEY"] = ""
    os.environ["OLLAMA_MODEL"] = "glm-4.7-flash:q4_K_M"

    try:
        options = ClaudeAgentOptions(
            model="glm-4.7-flash:q4_K_M",
            system_prompt="Test prompt",
        )

        async def test_run():
            run = await agent.run_agent(
                "Test prompt",
                options,
                tag="test",
                routing_tier="local"
            )
            assert run.is_error is False, \
                "Agent run with local routing should succeed"
            return run

        result = asyncio.run(test_run())
        assert result.provider == "Anthropic", \
            f"Provider should be detected, got {result.provider}"

        print("  ✓ Agent integration with local routing works")
    finally:
        # Restore
        if original_base_url:
            os.environ["ANTHROPIC_BASE_URL"] = original_base_url
        elif "ANTHROPIC_BASE_URL" in os.environ:
            del os.environ["ANTHROPIC_BASE_URL"]

        if original_api_key:
            os.environ["ANTHROPIC_API_KEY"] = original_api_key
        elif "ANTHROPIC_API_KEY" in os.environ:
            del os.environ["ANTHROPIC_API_KEY"]


def test_agent_integration_cloud_routing():
    """Agent integration with cloud routing uses default behavior."""
    import asyncio

    # Save original
    original_base_url = os.environ.get("ANTHROPIC_BASE_URL")
    original_api_key = os.environ.get("ANTHROPIC_API_KEY")

    try:
        os.environ["ANTHROPIC_BASE_URL"] = "https://api.anthropic.com"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"

        options = ClaudeAgentOptions(
            model="claude-sonnet-4-6",
            system_prompt="Test prompt",
        )

        async def test_run():
            run = await agent.run_agent(
                "Test prompt",
                options,
                tag="test",
                routing_tier="cloud"
            )
            assert run.is_error is False, \
                "Agent run with cloud routing should succeed"
            return run

        result = asyncio.run(test_run())
        assert result.provider == "Anthropic", \
            f"Provider should be Anthropic, got {result.provider}"

        print("  ✓ Agent integration with cloud routing works")
    finally:
        # Restore
        if original_base_url:
            os.environ["ANTHROPIC_BASE_URL"] = original_base_url
        elif "ANTHROPIC_BASE_URL" in os.environ:
            del os.environ["ANTHROPIC_BASE_URL"]

        if original_api_key:
            os.environ["ANTHROPIC_API_KEY"] = original_api_key
        elif "ANTHROPIC_API_KEY" in os.environ:
            del os.environ["ANTHROPIC_API_KEY"]


def test_agent_no_routing_backward_compatibility():
    """Agent without routing_tier parameter maintains backward compatibility."""
    import asyncio

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        system_prompt="Test prompt",
    )

    async def test_run():
        run = await agent.run_agent(
            "Test prompt",
            options,
            tag="test",
        )
        assert run.is_error is False, \
            "Agent run without routing should succeed (backward compatibility)"
        return run

    result = asyncio.run(test_run())
    # Provider detection works regardless of routing (may be Anthropic or GLM depending on env)
    assert result.provider in ("Anthropic", "GLM"), \
        f"Provider should be detected (Anthropic or GLM), got {result.provider}"

    print("  ✓ Agent without routing maintains backward compatibility")


def run_all():
    """Run all EU-174 tests."""
    print("\n🧪 EU-174: Hybrid LLM Routing tests\n")

    tests = [
        test_task_classification_tier2_keywords,
        test_task_classification_tier1_keywords,
        test_task_classification_by_size,
        test_task_classification_by_effort,
        test_task_classification_defaults,
        test_base_url_for_tier,
        test_api_key_for_tier,
        test_model_for_tier,
        test_routing_disabled_by_default,
        test_routing_enabled_from_env,
        test_routing_tier_from_env,
        test_should_route_to_local,
        test_agent_integration_local_routing,
        test_agent_integration_cloud_routing,
        test_agent_no_routing_backward_compatibility,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: unexpected error: {e}")
            failed += 1

    print("\n--------------------------------------------------")
    print(f"  {passed}/{len(tests)} passed")
    print("  RESULT:", "ALL GREEN" if failed == 0 else f"{failed} FAIL")
    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
