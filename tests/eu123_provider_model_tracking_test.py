"""EU-123: Provider + model tracking in audit logs and live feed tests.

Tests that the real provider + model per officer is shown in the live feed + audit,
ensuring that when the unit is routed to GLM via the Z.ai endpoint, the logs correctly
show GLM instead of Claude model names.
"""
import sys, types, tempfile, json, os
from pathlib import Path

# Mock the Claude Agent SDK BEFORE any imports
sdk = types.ModuleType("claude_agent_sdk")

class ClaudeAgentOptions:
    def __init__(self, **kw): self.__dict__.update(kw)

class _MockMessage:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

class AssistantMessage(_MockMessage):
    pass

class ResultMessage(_MockMessage):
    pass

class TextBlock:
    def __init__(self, text): self.text = text

class ToolUseBlock:
    def __init__(self, name, input): self.name = name; self.input = input

sdk.ClaudeAgentOptions = ClaudeAgentOptions
sdk.AssistantMessage = AssistantMessage
sdk.ResultMessage = ResultMessage
sdk.TextBlock = TextBlock
sdk.ToolUseBlock = ToolUseBlock

def _mock_query(prompt, options, **kwargs):
    """Mock query function that returns provider-specific results."""
    model = getattr(options, "model", "claude-sonnet-4-6")
    # Determine provider based on model
    if "z.ai" in os.environ.get("ANTHROPIC_BASE_URL", "").lower():
        provider = "GLM"
        model_version = "glm-4"
    else:
        provider = "Anthropic"
        if "opus" in model.lower():
            model_version = "claude-opus-4-8"
        elif "sonnet" in model.lower():
            model_version = "claude-sonnet-4-6"
        else:
            model_version = "claude-haiku-4-5"

    # Return a mock result with provider info
    async def _gen():
        yield AssistantMessage(content=[TextBlock(text="Test result")])
        yield ResultMessage(
            result="Test result",
            total_cost_usd=0.1,
            num_turns=1,
            is_error=False,
            usage={"input_tokens": 1000, "output_tokens": 500}
        )
    return _gen()

sdk.query = _mock_query
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import agent, provider
from orchestrator.contracts import BuildResult, ReviewResult, TestEngineerResult

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Test 1: AgentRun captures provider from Anthropic endpoint
os.environ.pop("ANTHROPIC_BASE_URL", None)
async def _test_anthropic_provider():
    opts = ClaudeAgentOptions(model="claude-opus-4-8", system_prompt="test")
    run = await agent.run_agent("test prompt", opts, tag="builder")
    chk("AgentRun captures Anthropic provider",
        run.provider == "Anthropic",
        f"got provider={run.provider}")
    chk("AgentRun captures Anthropic model version",
        run.model_version == "claude-opus-4-8",
        f"got model_version={run.model_version}")

import asyncio
asyncio.run(_test_anthropic_provider())

# Test 2: AgentRun captures provider from GLM endpoint
os.environ["ANTHROPIC_BASE_URL"] = "https://api.z.ai/v1"
async def _test_glm_provider():
    opts = ClaudeAgentOptions(model="claude-sonnet-4-6", system_prompt="test")
    run = await agent.run_agent("test prompt", opts, tag="builder")
    chk("AgentRun captures GLM provider from z.ai endpoint",
        run.provider == "GLM",
        f"got provider={run.provider}")
    chk("AgentRun captures model version from GLM endpoint",
        run.model_version == "glm-4",
        f"got model_version={run.model_version}")

asyncio.run(_test_glm_provider())
del os.environ["ANTHROPIC_BASE_URL"]

# Test 3: BuildResult includes provider and model fields
build_result = BuildResult(
    ok=True,
    summary="Test build",
    cost_usd=0.1,
    num_turns=1,
    raw="Test",
    tools=["Read", "Write"],
    input_tokens=1000,
    output_tokens=500,
    provider="Anthropic",
    model_version="claude-opus-4-8"
)
chk("BuildResult has provider field",
    hasattr(build_result, "provider") and build_result.provider == "Anthropic",
    f"provider={build_result.provider}")
chk("BuildResult has model_version field",
    hasattr(build_result, "model_version") and build_result.model_version == "claude-opus-4-8",
    f"model_version={build_result.model_version}")

# Test 4: TestEngineerResult includes provider and model fields
te_result = TestEngineerResult(
    ok=True,
    coverage="Coverage: 95%",
    summary="Tests added",
    cost_usd=0.05,
    num_turns=1,
    raw="Test",
    tools=["Read", "Write"],
    input_tokens=500,
    output_tokens=200,
    provider="GLM",
    model_version="glm-4"
)
chk("TestEngineerResult has provider field",
    hasattr(te_result, "provider") and te_result.provider == "GLM",
    f"provider={te_result.provider}")
chk("TestEngineerResult has model_version field",
    hasattr(te_result, "model_version") and te_result.model_version == "glm-4",
    f"model_version={te_result.model_version}")

# Test 5: ReviewResult includes provider and model fields
from orchestrator.contracts import Verdict
review_result = ReviewResult(
    verdict=Verdict.PASS,
    spec_met=True,
    summary="Review passed",
    cost_usd=0.05,
    raw="Test",
    input_tokens=500,
    output_tokens=200,
    provider="Anthropic",
    model_version="claude-sonnet-4-6"
)
chk("ReviewResult has provider field",
    hasattr(review_result, "provider") and review_result.provider == "Anthropic",
    f"provider={review_result.provider}")
chk("ReviewResult has model_version field",
    hasattr(review_result, "model_version") and review_result.model_version == "claude-sonnet-4-6",
    f"model_version={review_result.model_version}")

# Test 6: Provider detection from environment variable
def _test_provider_detection():
    # Test Anthropic (no z.ai in URL)
    os.environ.pop("ANTHROPIC_BASE_URL", None)
    prv, ver = provider.get_provider_info("claude-opus-4-8")
    chk("Provider detection defaults to Anthropic when no URL set",
        prv == "Anthropic",
        f"got {prv}")
    chk("Model version is normalized for Anthropic",
        ver == "claude-opus-4-8",
        f"got {ver}")

    # Test GLM detection (z.ai in URL)
    os.environ["ANTHROPIC_BASE_URL"] = "https://api.z.ai/v1"
    prv, ver = provider.get_provider_info("claude-sonnet-4-6")
    chk("Provider detection detects GLM from z.ai endpoint",
        prv == "GLM",
        f"got {prv}")
    chk("Model version is normalized for GLM",
        ver == "glm-4",
        f"got {ver}")
    del os.environ["ANTHROPIC_BASE_URL"]

_test_provider_detection()

# Test 7: Provider detection with case-insensitive URL matching
os.environ["ANTHROPIC_BASE_URL"] = "https://API.Z.AI/v1"
prv, ver = provider.get_provider_info("claude-opus-4-8")
chk("Provider detection is case-insensitive for z.ai",
    prv == "GLM",
    f"got {prv} from https://API.Z.AI/v1")
del os.environ["ANTHROPIC_BASE_URL"]

# Test 8: Model normalization strips date suffix from Haiku
os.environ.pop("ANTHROPIC_BASE_URL", None)
prv, ver = provider.get_provider_info("claude-haiku-4-5-20251001")
chk("Model normalization strips Haiku date suffix",
    ver == "claude-haiku-4-5",
    f"got {ver}")

# Test 9: Empty model string returns empty model_version
prv, ver = provider.get_provider_info("")
chk("Empty model string returns empty model_version",
    ver == "",
    f"got {ver}")

print("\n=============== EU-123 PROVIDER+MODEL TRACKING QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
