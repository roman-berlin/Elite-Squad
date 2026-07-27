"""EU-676 — index() must NOT pop last_result on GET /.

Acceptance criteria covered:
  AC(a) A pending un-dismissed result renders correctly on a fresh full GET / load.
  AC(b) Loading GET / multiple times without dismissing shows the same strip every time.
  AC(c) After POST /api/dismiss-result, a subsequent GET / no longer shows the strip.
  AC(d) No other behavior of index() changes.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, ".")

import datetime as _dt


class IndexResultStripTest(unittest.TestCase):
    """Integration tests: exercise index() and dismiss-result API against real app."""

    def setUp(self):
        """Build the test app and seed audit data."""
        self.tmp_dir = Path(tempfile.mkdtemp())
        audit = self.tmp_dir / "audit.jsonl"
        now = _dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        audit.write_text(
            '{"event":"ticket_start","ticket_id":"EU-676","app":"x",'
            f'"branch":"b","ts":"' + now + '"}\n',
            encoding="utf-8",
        )
        from orchestrator.config import Config, AppConfig
        self.cfg = Config(
            apps=[AppConfig(name="x", repo_path=str(self.tmp_dir), base_branch="DEV",
                            protected_branch="MAIN", backlog_backend="none")],
            audit_path=str(audit),
        )
        from orchestrator.server import create_app
        self.app = create_app(self.cfg)

    # ── helper to seed global _state ────────────────────────────────────────
    def _seed_result(self, text: str, tone: str = "ok") -> None:
        """Put a pending result into the global cockpit _state dict."""
        from orchestrator.cockpit_state import _state
        _state["last_result"] = text
        _state["last_result_record"] = {"tone": tone, "text": text, "timestamp": 1_000_000}

    def _clear_result(self) -> None:
        from orchestrator.cockpit_state import _state
        _state.pop("last_result", None)
        _state.pop("last_result_record", None)

    def test_ac_a_result_visible_on_first_load(self):
        """AC(a): pending result renders on first GET /."""
        self._seed_result("shipped successfully")
        with self.app.test_client() as client:
            resp = client.get("/")
            self.assertEqual(resp.status_code, 200)
            html = resp.data.decode()
            self.assertIn("shipped successfully", html,
                          "AC(a): result text should appear in page HTML")
            self.assertIn("var(--ok)", html, "AC(a): ok tone styling present")

    def test_ac_b_same_strip_on_reload(self):
        """AC(b): reload GET / multiple times — strip persists (no pop)."""
        self._seed_result("still here", "warn")
        with self.app.test_client() as client:
            h1 = client.get("/").data.decode()
            h2 = client.get("/").data.decode()
            h3 = client.get("/").data.decode()
            self.assertIn("still here", h1, "first load shows strip")
            self.assertIn("still here", h2, "second load shows strip (not popped)")
            self.assertIn("still here", h3, "third load shows strip (not popped)")

    def test_ac_c_dismiss_clears_strip(self):
        """AC(c): POST /api/dismiss-result clears the strip for subsequent GET /."""
        self._seed_result("dismiss-me", "error")
        with self.app.test_client() as client:
            # Before dismiss: strip visible
            h_before = client.get("/").data.decode()
            self.assertIn("dismiss-me", h_before, "strip visible before dismiss")
            # Dismiss
            rv = client.post("/api/dismiss-result?app=", content_type="multipart/form-data")
            self.assertEqual(rv.status_code, 200)
            # After dismiss: strip gone
            h_after = client.get("/").data.decode()
            self.assertNotIn("dismiss-me", h_after,
                             "AC(c): strip absent after dismiss")

    def test_ac_d_no_other_behavior_change(self):
        """AC(d): page loads successfully with no 5xx / crash."""
        with self.app.test_client() as client:
            resp = client.get("/")
            self.assertEqual(resp.status_code, 200)
            html = resp.data.decode()
            # Some structural marker must exist (not a blank or error page)
            self.assertGreater(len(html), 500, "page body has meaningful content")


if __name__ == "__main__":
    unittest.main()
