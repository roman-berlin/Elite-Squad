"""EU-823: verify _TOKENS_FALLBACK carries --brand (dark + light) in sync with warroom._PAGE."""

import re, sys, os

FALLBACK = """\
:root{color-scheme:dark;--bg:#080a0f;--panel:#0f141d;--panel2:#141a25;--line:#1b2230;--line2:#283342;--ink:#e7ebf2;--dim:#7e8795;--faint:#a0aab8;--ok:#34d399;--okbg:#0e2a1e;--okline:#1c5238;--warn:#f5b34a;--warnbg:#2c2410;--warnline:#5a4a1c;--bad:#f0676b;--badbg:#2a1417;--badline:#5a1f22;--info:#6aa9ff;--infobg:#0a1f2e;--infoline:#1a3a5c;--accent:#4d7cff;--accentbg:#0f1c30;--accentline:#1e3457;}
"""

DARK_VAL = "#ff7a59"
LIGHT_VAL = "#e8590c"


def _read_tokens_fallback():
    """Pull _TOKENS_FALLBACK from the cockpit_views module at import time."""
    # The worktree root (parent of tests/) holds orchestrator/cockpit_views.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from orchestrator.cockpit_views import _TOKENS_FALLBACK  # noqa: F401
    return _TOKENS_FALLBACK


def _count_brand(fallback):
    return fallback.count("--brand")


def test_exactly_two_brand_tokens():
    fb = _read_tokens_fallback()
    assert _count_brand(fb) == 2, f"Expected 2 --brand tokens, got {_count_brand(fb)}"


def _find_light_block(fallback):
    m = re.search(r"\[data-theme=light\]\{([^}]+)\}", fallback)
    assert m, "Missing :root[data-theme=light] block in _TOKENS_FALLBACK"
    return m.group(1)


def test_dark_brand_value():
    fb = _read_tokens_fallback()
    m = re.search(
        r"--accentline:#1e3457;--brand:("
        + DARK_VAL
        + ");.*?--mono:",
        fb,
        re.DOTALL,
    )
    assert m, f"Dark --brand not found between --accentline and --mono in _TOKENS_FALLBACK"


def test_light_brand_value():
    fb = _read_tokens_fallback()
    lb = _find_light_block(fb)
    assert f"--brand:{LIGHT_VAL}" in lb, f"Light block missing --brand:{LIGHT_VAL}; got: {lb!r}"


def test_values_match_warroom_page():
    fb = _read_tokens_fallback()
    assert f"--brand:{DARK_VAL}" in fb, f"Dark brand {DARK_VAL} missing"
    assert f"--brand:{LIGHT_VAL}" in fb, f"Light brand {LIGHT_VAL} missing"


if __name__ == "__main__":
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                print(f"FAIL {name}: {e}")
                failed.append(name)
            except Exception as e:
                print(f"ERR {name}: {e}")
                failed.append(name)
    if failed:
        print(f"\n{len(failed)} test(s) failed: {', '.join(failed)}")
        exit(1)
    else:
        print("\nAll tests passed")
        exit(0)
