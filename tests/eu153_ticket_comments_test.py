"""Tests for EU-153: LLM Ticket Commenter feature."""
from __future__ import annotations

import sys
from unittest.mock import Mock, patch, MagicMock

# pytest is OPTIONAL — the repo's runner (tests/run_all.py) and CI execute harnesses as plain
# scripts and do not install pytest; the bare import crashed every CI run. A tiny shim keeps the
# @pytest.mark.integration decorator working when pytest is absent.
try:
    import pytest
except ImportError:  # CI / bare venv
    import types as _types
    pytest = _types.SimpleNamespace(
        mark=_types.SimpleNamespace(integration=lambda f: f),
        main=None,
    )

# Allow running as standalone script or via pytest
sys.path.insert(0, ".")
from orchestrator.jira_adapter import TicketCommenter, GateComment


class MockConfig:
    """Mock Config object for testing."""
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.no_comments = False


class MockTicket:
    """Mock Ticket object for testing."""
    def __init__(self, key="TEST-123"):
        self.key = key
        self.id = key
        self.ephemeral = False


class MockBacklog:
    """Mock BacklogAdapter for testing."""
    def __init__(self):
        self.comments = []

    def add_comment(self, ticket, body):
        self.comments.append({"ticket": ticket.key, "body": body})


def test_gate_comment_dataclass():
    """Test GateComment dataclass structure."""
    comment = GateComment(
        gate="Build",
        status="FAILED",
        summary="Type error in src/main.py",
        timestamp="2026-07-01T12:00:00"
    )
    assert comment.gate == "Build"
    assert comment.status == "FAILED"
    assert comment.summary == "Type error in src/main.py"
    assert comment.timestamp == "2026-07-01T12:00:00"


def test_ticket_commenter_initialization():
    """Test TicketCommenter initialization."""
    cfg = MockConfig(dry_run=False)
    commenter = TicketCommenter(cfg, dry_run=False, no_comment=False)

    assert commenter.cfg is cfg
    assert commenter.dry_run is False
    assert commenter.no_comment is False
    assert commenter.comment_counts == {}
    assert commenter.MAX_COMMENTS_PER_CYCLE == 5


def test_no_comment_flag():
    """Test that no_comment flag prevents posting."""
    cfg = MockConfig()
    commenter = TicketCommenter(cfg, dry_run=False, no_comment=True)

    # Should not post when no_comment is True
    assert not commenter._should_post_comment("TEST-123")


def test_rate_limiting():
    """Test rate limiting of comments per ticket."""
    cfg = MockConfig()
    commenter = TicketCommenter(cfg, dry_run=False, no_comment=False)

    # First 5 comments should be allowed
    for i in range(5):
        assert commenter._should_post_comment("TEST-123")
        commenter._increment_counter("TEST-123")

    # 6th comment should be blocked
    assert not commenter._should_post_comment("TEST-123")


def test_rate_limiting_per_ticket():
    """Test that rate limiting is per-ticket."""
    cfg = MockConfig()
    commenter = TicketCommenter(cfg, dry_run=False, no_comment=False)

    # Exhaust limit for TEST-123
    for i in range(5):
        commenter._increment_counter("TEST-123")

    # TEST-456 should still be allowed
    assert commenter._should_post_comment("TEST-456")


def test_cycle_reset():
    """Test resetting comment counter for a new cycle."""
    cfg = MockConfig()
    commenter = TicketCommenter(cfg, dry_run=False, no_comment=False)

    # Add some comments
    for i in range(3):
        commenter._increment_counter("TEST-123")

    assert commenter.comment_counts["TEST-123"] == 3

    # Reset the cycle
    commenter.reset_cycle("TEST-123")

    # Should now be 0
    assert commenter.comment_counts["TEST-123"] == 0


def test_format_comment():
    """Test comment formatting with emojis."""
    cfg = MockConfig()
    commenter = TicketCommenter(cfg, dry_run=False, no_comment=False)

    # Test different statuses
    cases = [
        ("Build", "PASSED", "✅ Build: passed successfully"),
        ("Review", "FAILED", "❌ Review: Type error in main.py"),
        ("Security", "BLOCKED", "🛑 Security: SQL injection vulnerability"),
        ("PM", "ESCALATED", "🔥 PM: Needs decision on scope"),
        ("Gate", "RETRY", "🔄 Gate: Retrying with fixes"),
        ("Unknown", "OTHER", "• Unknown: Other status"),
    ]

    for gate, status, expected_prefix in cases:
        comment = GateComment(
            gate=gate,
            status=status,
            summary="test summary",
            timestamp="2026-07-01T12:00:00"
        )
        formatted = commenter.format_comment(comment)
        assert formatted.startswith(expected_prefix.split(":")[0])


def test_summarize_trivial_pass():
    """Test that trivial passes don't call LLM."""
    cfg = MockConfig()
    commenter = TicketCommenter(cfg, dry_run=False, no_comment=False)

    # Trivial pass should not call LLM
    with patch.object(commenter, '_call_haiku') as mock_llm:
        comment = commenter.summarize_gate_event(
            "Build",
            "PASSED",
            "Build passed successfully",
            "TEST-123"
        )
        # LLM should not be called for trivial passes
        mock_llm.assert_not_called()
        assert comment is not None
        assert comment.status == "PASSED"


def test_summarize_with_llm():
    """Test LLM-based summarization."""
    cfg = MockConfig()
    commenter = TicketCommenter(cfg, dry_run=False, no_comment=False)

    # Mock LLM response
    with patch.object(commenter, '_call_haiku', return_value="Type error: missing return annotation"):
        comment = commenter.summarize_gate_event(
            "Review",
            "FAILED",
            "Type error in src/main.py:45: missing return annotation for function process_data",
            "TEST-123"
        )
        assert comment is not None
        assert comment.gate == "Review"
        assert comment.status == "FAILED"
        assert "Type error" in comment.summary


def test_summarize_llm_failure_fallback():
    """Test fallback when LLM fails."""
    cfg = MockConfig()
    commenter = TicketCommenter(cfg, dry_run=False, no_comment=False)

    # Mock LLM failure
    with patch.object(commenter, '_call_haiku', return_value=None):
        comment = commenter.summarize_gate_event(
            "Security",
            "BLOCKED",
            "SQL injection in user query",
            "TEST-123"
        )
        # Should fallback to simple template
        assert comment is not None
        assert comment.gate == "Security"
        assert comment.status == "BLOCKED"
        assert "SQL injection" in comment.summary


def test_post_comment_dry_run():
    """Test posting comment in dry-run mode."""
    cfg = MockConfig(dry_run=True)
    commenter = TicketCommenter(cfg, dry_run=True, no_comment=False)
    backlog = MockBacklog()

    comment = GateComment(
        gate="Build",
        status="FAILED",
        summary="Type error",
        timestamp="2026-07-01T12:00:00"
    )

    result = commenter.post_comment(backlog, "TEST-123", comment)

    # Should return True (success) but not actually post
    assert result is True
    assert len(backlog.comments) == 0


def test_post_comment_live():
    """Test posting comment in live mode."""
    cfg = MockConfig(dry_run=False)
    commenter = TicketCommenter(cfg, dry_run=False, no_comment=False)
    backlog = MockBacklog()

    comment = GateComment(
        gate="Build",
        status="FAILED",
        summary="Type error",
        timestamp="2026-07-01T12:00:00"
    )

    result = commenter.post_comment(backlog, "TEST-123", comment)

    # Should post the comment
    assert result is True
    assert len(backlog.comments) == 1
    assert backlog.comments[0]["ticket"] == "TEST-123"
    assert "❌ Build: Type error" in backlog.comments[0]["body"]


def test_post_comment_respects_rate_limit():
    """Test that posting respects rate limits."""
    cfg = MockConfig()
    commenter = TicketCommenter(cfg, dry_run=False, no_comment=False)
    backlog = MockBacklog()

    comment = GateComment(
        gate="Build",
        status="FAILED",
        summary="Test error",
        timestamp="2026-07-01T12:00:00"
    )

    # Exhaust rate limit
    for i in range(5):
        commenter._increment_counter("TEST-123")

    # Should not post due to rate limit
    result = commenter.post_comment(backlog, "TEST-123", comment)
    assert result is False
    assert len(backlog.comments) == 0


def test_llm_summary_truncation():
    """Test that LLM summaries are truncated to 80 chars."""
    cfg = MockConfig()
    commenter = TicketCommenter(cfg, dry_run=False, no_comment=False)

    # Mock LLM returning a long summary
    long_summary = "This is a very long summary that exceeds the maximum length of 80 characters and should be truncated"
    with patch.object(commenter, '_call_haiku', return_value=long_summary):
        comment = commenter.summarize_gate_event(
            "Review",
            "FAILED",
            "Some error details",
            "TEST-123"
        )
        assert comment is not None
        assert len(comment.summary) <= 80


def test_llm_summary_prefix_removal():
    """Test that common LLM prefixes are removed."""
    cfg = MockConfig()
    commenter = TicketCommenter(cfg, dry_run=False, no_comment=False)

    # Mock LLM returning summary with prefix
    with patch.object(commenter, '_call_haiku', return_value="Summary: Type error in main.py"):
        comment = commenter.summarize_gate_event(
            "Review",
            "FAILED",
            "Some error details",
            "TEST-123"
        )
        assert comment is not None
        assert not comment.summary.startswith("Summary:")


def test_post_comment_error_handling():
    """Test error handling when posting fails."""
    cfg = MockConfig()
    commenter = TicketCommenter(cfg, dry_run=False, no_comment=False)

    # Mock backlog that raises exception
    backlog = Mock()
    backlog.add_comment.side_effect = Exception("Jira API error")

    comment = GateComment(
        gate="Build",
        status="FAILED",
        summary="Test error",
        timestamp="2026-07-01T12:00:00"
    )

    # Should handle error gracefully
    result = commenter.post_comment(backlog, "TEST-123", comment)
    assert result is False


def test_ephemeral_ticket_skipped():
    """Test that ephemeral tickets are handled in the loop (not in commenter)."""
    cfg = MockConfig()
    commenter = TicketCommenter(cfg, dry_run=False, no_comment=False)
    backlog = MockBacklog()

    # The commenter itself doesn't check ephemeral status - that's done in the loop
    # This test just verifies the structure is correct
    comment = GateComment(
        gate="Build",
        status="FAILED",
        summary="Test error",
        timestamp="2026-07-01T12:00:00"
    )

    # Comment should be postable (ephemeral check is done by caller)
    result = commenter.post_comment(backlog, "EPHEMERAL-1", comment)
    assert result is True
    assert len(backlog.comments) == 1


@pytest.mark.integration
def test_end_to_end_comment_flow():
    """Integration test for full comment flow."""
    cfg = MockConfig(dry_run=True)  # Use dry-run for safety
    commenter = TicketCommenter(cfg, dry_run=True, no_comment=False)
    backlog = MockBacklog()

    # Simulate a gate failure
    comment = commenter.summarize_gate_event(
        "Review",
        "FAILED",
        "Type error in src/main.py:45: missing return annotation",
        "TEST-123"
    )

    assert comment is not None
    assert comment.gate == "Review"
    assert comment.status == "FAILED"

    # Post the comment
    result = commenter.post_comment(backlog, "TEST-123", comment)
    assert result is True

    # In dry-run, no actual comment should be in backlog
    assert len(backlog.comments) == 0


if __name__ == "__main__":
    # Repo-standard standalone runner (run_all.py executes harnesses as plain scripts; pytest is
    # optional). Runs every test_* function, prints the k/n tally, exits non-zero on any failure.
    import traceback
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  [PASS] {name}")
        except Exception:  # noqa: BLE001
            print(f"  [FAIL] {name}")
            traceback.print_exc()
    print("-" * 50)
    print(f"  {passed}/{len(tests)} passed")
    print("  RESULT:", "ALL GREEN" if passed == len(tests) else f"{len(tests) - passed} FAIL")
    sys.exit(0 if passed == len(tests) else 1)
