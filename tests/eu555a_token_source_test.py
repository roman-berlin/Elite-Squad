"""EU-791: Token source byte-identity tests.

Confirms that the token CSS no longer uses a drifted fallback or regex scrape;
instead it wires directly into warroom.THEME_TOKENS_CSS verbatim.
"""
import unittest


class TokenSourceTest(unittest.TestCase):
    def setUp(self):
        from orchestrator.cockpit_views import _token_css, _THEME_BOOT
        from orchestrator.warroom import _PAGE, THEME_TOKENS_CSS
        self.token_css = _token_css()
        self.page = _PAGE
        self.tokens = THEME_TOKENS_CSS
        self.boot = _THEME_BOOT

    def test_theme_tokens_in_page(self):
        # The constant must appear verbatim in the rendered War Room page HTML.
        self.assertIn(self.tokens, self.page)

    def test_token_css_byte_identity(self):
        # _token_css() must be exactly "<style>" + THEME_TOKENS_CSS + "</style>" + _THEME_BOOT.
        expected = "<style>" + self.tokens + "</style>" + self.boot
        self.assertEqual(self.token_css, expected)


if __name__ == "__main__":
    unittest.main()
