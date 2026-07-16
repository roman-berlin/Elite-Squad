"""EU-199: /api/health surfaces the active model backend (Opus vs GLM).

Fail-first tests for each acceptance criterion.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

# This harness calls health.summary() for REAL — keep the auth-liveness probe (a real `claude -p`
# round-trip) off even when run standalone, outside run_all.py's stripped child env.
os.environ["GENERAL_AUTH_PROBE"] = "0"

# Mock the SDK module so health.checks doesn't fail
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import health, backend_pref
from orchestrator.backends import NATIVE, GLM
from orchestrator.config import AppConfig, Config


def test_health_summary_includes_backend_opus():
    """Criterion 1: health.summary() includes 'backend' field set to 'Opus (Claude)' when opus is active."""
    # Create a minimal valid config
    ROOT = tempfile.mkdtemp()
    (Path(ROOT) / ".git").mkdir()

    app = AppConfig(
        name="test-app",
        repo_path=ROOT,
        base_branch="dev",
        workdir=ROOT,
        gate_commands=[],
        backlog_backend="none"
    )
    cfg = Config(apps=[app], audit_path="/tmp/test.jsonl", use_worktree=False)

    # Mock backend_pref.active to return opus
    _original_active = backend_pref.active

    def mock_active_opus(cfg=None):
        return NATIVE

    backend_pref.active = mock_active_opus

    try:
        summary = health.summary(cfg)

        # CRITICAL: This should FAIL before we implement the feature
        assert "backend" in summary, "summary must include 'backend' field"
        assert summary["backend"] == "Opus (Claude)", f"expected 'Opus (Claude)', got {summary['backend']!r}"
    finally:
        backend_pref.active = _original_active


def test_health_summary_includes_backend_glm():
    """Criterion 2: health.summary() includes 'backend' field set to 'GLM (Z.ai)' when glm is active."""
    ROOT = tempfile.mkdtemp()
    (Path(ROOT) / ".git").mkdir()

    app = AppConfig(
        name="test-app",
        repo_path=ROOT,
        base_branch="dev",
        workdir=ROOT,
        gate_commands=[],
        backlog_backend="none"
    )
    cfg = Config(apps=[app], audit_path="/tmp/test.jsonl", use_worktree=False)

    _original_active = backend_pref.active

    def mock_active_glm(cfg=None):
        return GLM

    backend_pref.active = mock_active_glm

    try:
        summary = health.summary(cfg)

        # CRITICAL: This should FAIL before we implement the feature
        assert "backend" in summary, "summary must include 'backend' field"
        assert summary["backend"] == "GLM (Z.ai)", f"expected 'GLM (Z.ai)', got {summary['backend']!r}"
    finally:
        backend_pref.active = _original_active


def test_health_backend_matches_sticky_pref():
    """Criterion 3: The 'backend' field matches the sticky preference from backend_pref.active(cfg)
    regardless of config.yaml model_backend default.

    This tests that backend_pref.active() is consulted (not just the config default).
    """
    ROOT = tempfile.mkdtemp()
    (Path(ROOT) / ".git").mkdir()

    app = AppConfig(
        name="test-app",
        repo_path=ROOT,
        base_branch="dev",
        workdir=ROOT,
        gate_commands=[],
        backlog_backend="none"
    )
    # Create config with opus as default
    cfg = Config(apps=[app], audit_path="/tmp/test.jsonl", use_worktree=False,
                 builder_model="claude-opus-4-8", reviewer_model="claude-opus-4-8",
                 model_backend="opus")

    _original_active = backend_pref.active

    # But sticky pref says glm
    def mock_active_glm(cfg=None):
        return GLM  # Sticky pref overrides config

    backend_pref.active = mock_active_glm

    try:
        summary = health.summary(cfg)

        # Should show GLM because backend_pref.active() says glm
        assert "backend" in summary, "summary must include 'backend' field"
        assert summary["backend"] == "GLM (Z.ai)", (
            f"expected 'GLM (Z.ai)' from sticky pref, got {summary['backend']!r}"
        )
    finally:
        backend_pref.active = _original_active


def test_backend_label_mapping():
    """Test that backend IDs map to correct display labels."""
    # Document what the mapping should be:
    # NATIVE (opus) → "Opus (Claude)"
    # GLM (glm) → "GLM (Z.ai)"
    assert NATIVE == "opus", f"Expected NATIVE to be 'opus', got {NATIVE!r}"
    assert GLM == "glm", f"Expected GLM to be 'glm', got {GLM!r}"


def test_doctor_cli_surfaces_backend():
    """Criterion 4: ./general doctor CLI output surfaces the backend information.

    The doctor CLI (orchestrator/main.py:46) uses health.summary(), so if summary()
    includes the backend field, the doctor will surface it. This test verifies
    the integration by actually calling the _doctor function and checking its output.
    """
    import io
    import yaml
    from orchestrator.main import _doctor

    ROOT = tempfile.mkdtemp()
    (Path(ROOT) / ".git").mkdir()

    app = AppConfig(
        name="test-app",
        repo_path=ROOT,
        base_branch="dev",
        workdir=ROOT,
        gate_commands=[],
        backlog_backend="none"
    )
    cfg = Config(apps=[app], audit_path="/tmp/test.jsonl", use_worktree=False)

    # Write a config file for _doctor to load
    config_data = {
        "apps": [{
            "name": "test-app",
            "repo_path": ROOT,
            "base_branch": "dev",
            "workdir": ROOT,
            "gate_commands": [],
            "backlog_backend": "none"
        }],
        "audit_path": "/tmp/test.jsonl",
        "use_worktree": False
    }
    config_file = Path(ROOT) / "config.yaml"
    config_file.write_text(yaml.dump(config_data), encoding="utf-8")

    _original_active = backend_pref.active

    def mock_active_opus(cfg=None):
        return NATIVE

    backend_pref.active = mock_active_opus

    try:
        # Capture stdout to verify the CLI actually prints the backend
        captured_output = io.StringIO()
        import sys
        old_stdout = sys.stdout
        sys.stdout = captured_output

        # Call the actual _doctor function
        result = _doctor(str(config_file))

        sys.stdout = old_stdout
        output = captured_output.getvalue()

        # The doctor function returns 1 when there are health problems, which is expected
        # in a test environment. We only care that it prints the backend.
        # Verify the backend is actually printed in the CLI output
        assert "backend" in output.lower(), "CLI output must mention 'backend'"
        assert "Opus (Claude)" in output, "CLI output must show 'Opus (Claude)'"
        assert "models: builder" in output, "CLI output must show the models line with backend"
        assert "backend Opus (Claude)" in output, "CLI output must include 'backend Opus (Claude)'"

        # Verify the backend is actually printed in the CLI output
        assert "backend" in output.lower(), "CLI output must mention 'backend'"
        assert "Opus (Claude)" in output, "CLI output must show 'Opus (Claude)'"
        assert "models: builder" in output, "CLI output must show the models line with backend"
    finally:
        backend_pref.active = _original_active


# Criterion 5 (python3 tests/run_all.py green) will be validated by running the full suite

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
