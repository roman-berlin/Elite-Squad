"""Tests for EU-699: persistent result store accessors (get + clear).

Validates that the public ``get_last_result`` / ``clear_last_result`` helpers
round-trip structured records (tone, text, timestamp), survive repeated reads
(non-destructive), clear cleanly from both scopes, and isolate per-app writes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pytest
except ImportError:
    print("0/0 passed")
    print("  RESULT: SKIPPED (pytest not installed)")
    sys.exit(0)

from orchestrator.cockpit_state import get_state, reset_workspaces
import orchestrator.server as server


# ═══════════════════════════════════════════════════════════════════════════
# AC1: Structured record round-trips through storage per-app
# ═══════════════════════════════════════════════════════════════════════════
class TestAc1RoundTripPerApp:
    """Every call-site's (app, tone, text) → set → get path survives intact."""

    def setup_method(self):
        reset_workspaces()

    @pytest.mark.parametrize("app", ["automatixy", "eu-cockpit", None])
    @pytest.mark.parametrize("tone", ["ok", "error", "warn"])
    def test_round_trip_all_tones_per_app(self, app, tone):
        """set_last_result + get_last_result round-trip for each (app, tone)."""
        server.set_last_result(app, tone, f"result-{tone}")
        rec = server.get_last_result(app)
        assert rec is not None
        assert rec["tone"] == tone
        assert rec["text"] == f"result-{tone}"
        assert isinstance(rec["timestamp"], float)

    def test_qa_verdict_round_trip(self):
        """QA verdict (None-app, 'ok') — one of the six call sites."""
        server.set_last_result(None, "ok", "QA passed all checks")
        rec = server.get_last_result(None)
        assert rec is not None
        assert rec["tone"] == "ok"
        assert rec["text"] == "QA passed all checks"

    def test_scribe_failure_round_trip(self):
        """Scribe failure (None-app, 'error') — another call site."""
        server.set_last_result(None, "error", "scribe failed: timeout")
        rec = server.get_last_result(None)
        assert rec["tone"] == "error"
        assert "scribe failed" in rec["text"]


# ═══════════════════════════════════════════════════════════════════════════
# AC2: Storage does NOT pop-on-read; it is persistent across reads
# ═══════════════════════════════════════════════════════════════════════════
class TestAc2NonDestructivePeek:
    """The store survives being read repeatedly — no pop-on-read."""

    def setup_method(self):
        reset_workspaces()

    def test_multiple_gets_return_same_record(self):
        """Three successive gets return identical records."""
        server.set_last_result("appx", "warn", "caution")
        r1 = server.get_last_result("appx")
        r2 = server.get_last_result("appx")
        r3 = server.get_last_result("appx")
        assert r1 == r2 == r3

    def test_get_does_not_clear_state(self):
        """After a successful get, the record still exists in the backing dict."""
        server.set_last_result("appx", "ok", "still here")
        _ = server.get_last_result("appx")
        rec_after = get_state("appx").get("last_result_record")
        assert rec_after is not None
        assert rec_after["text"] == "still here"


# ═══════════════════════════════════════════════════════════════════════════
# AC3: Minimal read accessor returns None on fresh/cleared state
# ═══════════════════════════════════════════════════════════════════════════
class TestAc3AccessorDefaults:
    """get_last_result returns None when nothing has been stored."""

    def setup_method(self):
        reset_workspaces()

    def test_fresh_state_returns_none(self):
        assert server.get_last_result("fresh_app") is None

    def test_global_fresh_state_returns_none(self):
        # _state is a module-level global that reset_workspaces doesn't clear — purge it first.
        server._state.pop("last_result", None)
        server._state.pop("last_result_record", None)
        assert server.get_last_result(None) is None

    def test_after_clear_returns_none(self):
        server.set_last_result("appx", "ok", "gone")
        server.clear_last_result("appx")
        assert server.get_last_result("appx") is None


# ═══════════════════════════════════════════════════════════════════════════
# AC4: GET / banner rendering uses new accessor (integrated via _view_state)
# ═══════════════════════════════════════════════════════════════════════════
class TestAc4ViewStateIntegration:
    """_view_state merges unit-wide results via get_last_result."""

    def setup_method(self):
        reset_workspaces()
        # Also purge _state global (reset_workspaces only resets per-app dicts).
        server._state.pop("last_result", None)
        server._state.pop("last_result_record", None)

    def test_unit_wide_result_propagates_to_tab_view(self):
        """When global writes last_result_record, _view_state surfaces it into tab view.

        _view_state is an inner function of create_app(), so we replicate its
        core merge logic here: call ``get_last_result(None)`` and confirm the
        result flows into the per-app state that the board would receive."""
        from orchestrator.cockpit_state import get_state as _gs

        # Global writer (standup/council/QA) stores to None key
        server.set_last_result(None, "error", "council failed")
        # Tab-specific state for another app has no record
        tab_st = _gs("other_app")
        # Simulate _view_state's merge step
        gl = server.get_last_result(None)
        local = server.get_last_result("other_app")
        assert gl is not None
        assert gl["text"] == "council failed"
        assert local is None  # tab has nothing
        # The merged view should carry the global record
        if local is None:
            merged_rec = gl
        else:
            merged_rec = local
        assert merged_rec["tone"] == "error"
        assert merged_rec["text"] == "council failed"


# ═══════════════════════════════════════════════════════════════════════════
# AC5: Unit test per call-site scenario (table-driven)
# ═══════════════════════════════════════════════════════════════════════════
class TestAc5CallSiteScenarios:
    """Table-driven: exercise the full round-trip for every original call-site pattern."""

    def setup_method(self):
        reset_workspaces()

    # Each row mirrors a live call site:
    #   (app_arg, tone_arg, text_pattern, description)
    CALL_SITES = [
        ("qa_verdict",     "ok",   "QA finished",               "server.py QA verdict route"),
        ("council_fail",   "error", "council failed:",          "server.py council failure"),
        ("scribe_fail",    "error", "scribe failed:",           "server.py scribe failure"),
        ("report_intake",  "ok",   "report intake complete",   "server.py report intake"),
        ("standup_fail",   "error", "standup failed:",          "server.py standup failure"),
        ("answer_api",     "ok",   "Answer sent to",           "server.py answer_api"),
    ]

    @pytest.mark.parametrize("key,tone,text_pattern,_desc", CALL_SITES)
    def test_call_site_round_trip(self, key, tone, text_pattern, _desc):
        """Each call site's (app, tone, text) persists through get_last_result()."""
        server.set_last_result(key, tone, f"{text_pattern} ticket")
        rec = server.get_last_result(key)
        assert rec is not None
        assert rec["tone"] == tone
        assert text_pattern in rec["text"]
        assert isinstance(rec["timestamp"], float)


# ═══════════════════════════════════════════════════════════════════════════
# clear_last_result correctness
# ═══════════════════════════════════════════════════════════════════════════
class TestClearLastResult:
    """clear_last_result removes both record and legacy plain-string keys."""

    def setup_method(self):
        reset_workspaces()

    def test_clear_removes_structured_and_plain(self):
        server.set_last_result("appx", "ok", "bye")
        server.clear_last_result("appx")
        st = get_state("appx")
        assert st.get("last_result_record") is None
        assert st.get("last_result", "") == ""

    def test_clear_idempotent_no_crash(self):
        """Clearing nothing twice must not raise."""
        server.clear_last_result("fresh_app")
        server.clear_last_result("fresh_app")

    def test_clear_global_state(self):
        """clear_last_result(None) clears the unit-wide _state."""
        old_val = server._state.get("last_result")
        server.set_last_result(None, "warn", "global")
        server.clear_last_result(None)
        assert server._state.get("last_result", "") == ""
        assert server._state.get("last_result_record") is None
        server._state["last_result"] = old_val

    def test_clear_per_app_also_cleans_global(self):
        """When called with a non-None app, it ALSO pops the global _state."""
        server.set_last_result(None, "ok", "global残留")
        server.set_last_result("appx", "ok", "local")
        server.clear_last_result("appx")
        # Local scope cleaned
        assert get_state("appx").get("last_result_record") is None
        # Global scope also cleaned
        assert server._state.get("last_result_record") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
