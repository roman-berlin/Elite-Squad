"""Tests for EU-653: set_last_result() structured result helper.

Validates tone validation, dual-key storage (plain string + record),
back-compat with existing plain-string readers, and invalid-tone isolation.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pytest
except ImportError:
    print("0/0 passed")
    print("  RESULT: SKIPPED (pytest not installed)")
    sys.exit(0)

from orchestrator.cockpit_state import get_state, reset_workspaces
from orchestrator.cockpit_views import _result_banner
import orchestrator.server as server


class TestSetLastResultValidTones:
    """Test that valid tones store correctly in both keys."""

    def setup_method(self):
        reset_workspaces()

    def test_ok_stores_both_keys(self):
        """server.set_last_result('appx', 'ok', 'done') stores plain text AND record."""
        before_ts = time.time()
        server.set_last_result("appx", "ok", "done")
        st = get_state("appx")

        # Plain-string back-compat: still a plain string
        assert st["last_result"] == "done"

        # Structured record
        rec = st["last_result_record"]
        assert isinstance(rec, dict)
        assert rec["tone"] == "ok"
        assert rec["text"] == "done"
        assert isinstance(rec["timestamp"], float)
        assert before_ts <= rec["timestamp"] <= time.time() + 1

    def test_error_stores_tone(self):
        """Tone 'error' round-trips into the record."""
        server.set_last_result("appx", "error", "boom")
        rec = get_state("appx")["last_result_record"]
        assert rec["tone"] == "error"
        assert rec["text"] == "boom"
        assert isinstance(rec["timestamp"], float)

    def test_warn_stores_tone(self):
        """Tone 'warn' round-trips into the record."""
        server.set_last_result("appx", "warn", "caution")
        rec = get_state("appx")["last_result_record"]
        assert rec["tone"] == "warn"
        assert rec["text"] == "caution"
        assert isinstance(rec["timestamp"], float)

    def test_successive_sets_update_all_keys(self):
        """Calling set_last_result multiple times updates both keys each time."""
        server.set_last_result("appx", "ok", "first")
        assert get_state("appx")["last_result"] == "first"
        assert get_state("appx")["last_result_record"]["tone"] == "ok"

        server.set_last_result("appx", "error", "second")
        assert get_state("appx")["last_result"] == "second"
        assert get_state("appx")["last_result_record"]["tone"] == "error"

        server.set_last_result("appx", "warn", "third")
        assert get_state("appx")["last_result"] == "third"
        assert get_state("appx")["last_result_record"]["tone"] == "warn"


class TestSetLastResultInvalidTone:
    """Test that invalid tones raise ValueError and leave state untouched."""

    def setup_method(self):
        reset_workspaces()

    def test_invalid_tone_raises_value_error(self):
        """Tone outside {ok, error, warn} raises ValueError."""
        with pytest.raises(ValueError, match="invalid last_result tone"):
            server.set_last_result("appx", "info", "x")

    def test_empty_tone_raises(self):
        """Empty string tone raises ValueError."""
        with pytest.raises(ValueError):
            server.set_last_result("appx", "", "x")

    def test_uppercase_tone_raises(self):
        """Uppercase 'OK' is distinct from 'ok' and raises."""
        with pytest.raises(ValueError):
            server.set_last_result("appx", "OK", "x")

    def test_invalid_tone_leaves_state_unchanged(self):
        """An invalid call leaves state entirely unmodified — no partial write."""
        # Seed a value
        server.set_last_result("appx", "ok", "original")
        prior_plain = get_state("appx").get("last_result")
        prior_rec = get_state("appx").get("last_result_record")

        # Attempt invalid tone
        try:
            server.set_last_result("appx", "info", "bad")
        except ValueError:
            pass

        # State must be identical to before
        assert get_state("appx")["last_result"] == prior_plain
        assert get_state("appx")["last_result_record"] is prior_rec

    def test_default_key_none_writes_to_legacy_state(self):
        """set_last_result(None, 'warn', 'x') writes to the default _state."""
        old_state_val = server._state.get("last_result")
        old_rec_val = server._state.get("last_result_record")

        server.set_last_result(None, "warn", "default key test")

        # Should have written to the shared _state
        assert server._state["last_result"] == "default key test"
        assert server._state["last_result_record"]["tone"] == "warn"
        assert server._state["last_result_record"]["text"] == "default key test"

        # Clean up
        server._state["last_result"] = old_state_val
        server._state["last_result_record"] = old_rec_val


class TestBackCompatReaders:
    """Test that existing plain-string readers survive unchanged."""

    def setup_method(self):
        reset_workspaces()

    def test_result_banner_pops_plain_string(self):
        """_result_banner strips/pops last_result as plain text — no crash."""
        server.set_last_result(None, "error", "boom")
        state = server._state  # use same object as _result_banner reads

        banner = _result_banner(state)

        # Banner should contain the text and the key should be popped
        assert "boom" in banner
        assert state.get("last_result", "") == ""  # popped

    def test_result_banner_with_ok_tone(self):
        """A 'ok' tone's plain text renders in the banner and pops cleanly."""
        server.set_last_result(None, "ok", "all clear")
        state = server._state

        banner = _result_banner(state)
        assert "all clear" in banner
        assert state.get("last_result", "") == ""

    def test_get_without_raise(self):
        """.get('last_result', '') returns the plain text (no .strip() needed because we store raw)."""
        server.set_last_result("appx", "ok", "hello world")
        st = get_state("appx")
        val = st.get("last_result", "")
        assert val == "hello world"

    def test_record_available_on_fresh_state(self):
        """Fresh states (never called through set_last_result) have None for the record key."""
        reset_workspaces()
        st = get_state("fresh_app")
        assert st.get("last_result_record") is None
        assert st.get("last_result", "") == ""
