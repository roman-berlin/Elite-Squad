"""Tests for provider detection (EU-123)."""
import os
import sys
import unittest

# Stub the SDK before importing orchestrator modules
sdk = type(sys)("claude_agent_sdk")
sys.modules["claude_agent_sdk"] = sdk
sys.modules["claude_agent_sdk"].query = lambda *args, **kwargs: iter([])

sys.path.insert(0, ".")
from orchestrator import provider


class TestProviderDetection(unittest.TestCase):
    """Test provider detection logic."""

    def test_anthropic_provider_default(self):
        """When ANTHROPIC_BASE_URL is not set or doesn't contain z.ai, provider is Anthropic."""
        # Clear the environment variable
        original = os.environ.get("ANTHROPIC_BASE_URL")
        if "ANTHROPIC_BASE_URL" in os.environ:
            del os.environ["ANTHROPIC_BASE_URL"]

        try:
            prv, ver = provider.get_provider_info("claude-opus-4-8")
            assert prv == "Anthropic", f"Expected Anthropic, got {prv}"
            assert ver == "claude-opus-4-8", f"Expected claude-opus-4-8, got {ver}"
        finally:
            # Restore original value
            if original is not None:
                os.environ["ANTHROPIC_BASE_URL"] = original

    def test_glm_provider_detection(self):
        """When ANTHROPIC_BASE_URL contains z.ai, provider is GLM."""
        original = os.environ.get("ANTHROPIC_BASE_URL", "")
        os.environ["ANTHROPIC_BASE_URL"] = "https://api.z.ai/v1"

        try:
            prv, ver = provider.get_provider_info("glm-4")
            assert prv == "GLM", f"Expected GLM, got {prv}"
            assert ver == "glm-4", f"Expected glm-4, got {ver}"
        finally:
            os.environ["ANTHROPIC_BASE_URL"] = original

    def test_model_normalization_opus(self):
        """Opus model names are normalized correctly."""
        prv, ver = provider.get_provider_info("claude-opus-4-8")
        assert ver == "claude-opus-4-8"

    def test_model_normalization_sonnet(self):
        """Sonnet model names are normalized correctly."""
        prv, ver = provider.get_provider_info("claude-sonnet-4-6")
        assert ver == "claude-sonnet-4-6"

    def test_model_normalization_haiku(self):
        """Haiku model names strip the date suffix."""
        prv, ver = provider.get_provider_info("claude-haiku-4-5-20251001")
        assert ver == "claude-haiku-4-5", f"Expected claude-haiku-4-5, got {ver}"

    def test_empty_model(self):
        """Empty model string returns empty model_version."""
        original = os.environ.get("ANTHROPIC_BASE_URL")
        if "ANTHROPIC_BASE_URL" in os.environ:
            del os.environ["ANTHROPIC_BASE_URL"]

        try:
            prv, ver = provider.get_provider_info("")
            assert prv == "Anthropic", "Default provider should be Anthropic"
            assert ver == "", "Model version should be empty for empty input"
        finally:
            if original is not None:
                os.environ["ANTHROPIC_BASE_URL"] = original

    def test_glm_model_variants(self):
        """GLM model variants are preserved correctly."""
        os.environ["ANTHROPIC_BASE_URL"] = "https://api.z.ai/v1"
        try:
            prv, ver = provider.get_provider_info("glm-4-plus")
            assert prv == "GLM"
            assert ver == "glm-4-plus"
        finally:
            del os.environ["ANTHROPIC_BASE_URL"]


if __name__ == "__main__":
    unittest.main()
