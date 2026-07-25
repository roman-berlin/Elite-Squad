"""EU-516 + EU-481 children: _strip_quoted_context pure helper.

Tests for orchestrator.reviewer._strip_quoted_context(text) — a small text-transform that
removes markdown quoted-context spans (triple-backtick fenced blocks, inline single-backtick
spans, and blockquote lines), replacing each removed span with a single space so surrounding
words don't merge. Pure function with no module-level state.

Fail-first convention: every assert is guarded so the harness exits non-zero on failure.
run_all.py auto-discovers via its ``tests/*_test.py`` glob.
"""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from orchestrator.reviewer import _strip_quoted_context


def test_import_callable():
    """AC 1: _strip_quoted_context is importable from orchestrator.reviewer and callable."""
    assert callable(_strip_quoted_context), "_strip_quoted_context must be callable"
    print("✓ AC1: importable and callable")


def test_inline_backtick_removed():
    """AC 2: backtick-quoted span is removed; surrounding prose survives with whitespace."""
    result = _strip_quoted_context('the summary says `tests still failing` as an example')
    assert 'still failing' not in result, \
        "inline backtick span should be stripped"
    assert 'the summary says' in result, "leading prose must survive"
    assert 'as an example' in result, "trailing prose must survive"
    # Ensure words didn't merge: 'says' and 'as' separated by whitespace
    import re
    assert re.search(r'\w+\s+\w+', result), \
        "surrounding words should be separated by whitespace, not merged"
    print("✓ AC2: inline backtick span removed, prose intact")


def test_triple_backtick_fence_removed():
    """AC 3: language-tagged fenced block removed entirely (body gone)."""
    inp = 'before\n```python\ntests are failing here\n```\nafter'
    result = _strip_quoted_context(inp)
    assert 'before' in result, "pre-fence text must survive"
    assert 'after' in result, "post-fence text must survive"
    assert 'failing' not in result, "fenced-body prose must be gone"
    print("✓ AC3: triple-backtick fence removed")


def test_blockquote_line_removed():
    """AC 4: blockquote lines removed (plain and indented)."""
    result = _strip_quoted_context('ok line\n> the test suite is still red\nnext line')
    assert 'ok line' in result, "non-blockquote line must survive"
    assert 'next line' in result, "non-blockquote line must survive"
    assert 'still red' not in result, "blockquote body must be gone"

    # Indented blockquote too
    result2 = _strip_quoted_context('start\n  > also quoted\nend')
    assert 'start' in result2 and 'end' in result2, "adjacent lines must survive"
    assert 'also quoted' not in result2, "indented blockquote must be stripped"
    print("✓ AC4: blockquote lines removed")


def test_plain_prose_unchanged():
    """AC 5: plain unquoted text returned unchanged."""
    original = 'Fixed the flaky path handling and added a regression test.'
    result = _strip_quoted_context(original)
    for word in original.split():
        assert word in result, f"word '{word}' must survive in plain prose"
    print("✓ AC5: plain prose unchanged")


def test_empty_none_input():
    """AC 6: empty string returns ''; None returns '' without raising."""
    assert _strip_quoted_context('') == '', "empty string → ''"
    assert _strip_quoted_context(None) == '', "None → ''"
    print("✓ AC6: empty/None guard works")


if __name__ == "__main__":
    tests = [
        test_import_callable,
        test_inline_backtick_removed,
        test_triple_backtick_fence_removed,
        test_blockquote_line_removed,
        test_plain_prose_unchanged,
        test_empty_none_input,
    ]
    passed = total = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"✗ {t.__doc__.splitlines()[0]}: {e}")
            total += 1
            break
    else:
        total = len(tests)
    print(f"\n{passed}/{total} passed")
    if passed != total:
        sys.exit(1)
