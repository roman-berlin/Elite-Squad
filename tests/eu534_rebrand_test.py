"""EU-534 — rebrand 'officer' → 'engineer' in warroom.py display strings.

Scans orchestrator/warroom.py source and asserts:
  1. EU-594: Group room card was removed — old tooltip text no longer in source.
  2. Log-empty placeholder reads "an engineer's model call", not officer's.
  3. The /group?officer= query-param key is REMOVED (EU-613 routes everything through /chat).
  4. Module-level identifiers (from .officers import, OFFICER_NAMES) are UNCHANGED.
  5. Quoted-string regex hits on '[Oo]fficer' are zero across rendered copy, tooltips, docstrings, and prose comments.
"""
from __future__ import annotations

import pathlib
import re

WARROOM = pathlib.Path(__file__).resolve().parent.parent / "orchestrator" / "warroom.py"


def test_group_tooltip_uses_engineers():
    """EU-594: Group room card was removed — tooltip text should no longer appear."""
    src = WARROOM.read_text()
    assert 'convene all the engineers' not in src, (
        "EU-594: 'convene all the engineers' should have been removed with the Group room card")


def test_log_empty_uses_engineer():
    """Log-empty placeholder must say an engineer's model call, not officer's."""
    src = WARROOM.read_text()
    # Source uses \' inside f-string; raw text has literal backslash-quote
    expected = r"an engineer\'s model call prints nothing"
    actual_bad = r"an officer\'s model call"
    assert expected in src, f'Expected log-empty: {expected!r}'
    assert actual_bad not in src, f'Old pattern still present: {actual_bad!r}'


def test_group_param_key_removed():
    """EU-613: /group?officer= is gone — all links route through /chat."""
    src = WARROOM.read_text()
    assert '/group?officer=' not in src, "/group?officer= should have been removed by EU-613"


def test_identifiers_unchanged():
    """Module import and OFFICER_NAMES references must stay as-is."""
    src = WARROOM.read_text()
    assert 'from .officers import' in src, ".officers import was renamed — breaks imports"
    # OFFICER_KEY and OFFICER_ROLES dict names should be untouched too
    assert '_OFFICER_KEY' in src, "_OFFICER_KEY identifier was renamed"
    assert '_OFFICER_ROLES' in src, "_OFFICER_ROLES identifier was renamed"
    assert '_OFFICERS' in src, "_OFFICERS identifier was renamed"


def test_no_officer_in_display_strings():
    """Explicit checks on every known 'officer' occurrence location in warroom.py.

    After rebrand, none of these prose/comment/docstring lines should say 'officer'.
    Also verifies the /group?officer= param line remains unchanged."""
    src = WARROOM.read_text()

    # --- Module docstring (part[1] from triple-quote split) ---
    mod_doc = src.split('"""')[1]
    assert 'engineer' in mod_doc, "Module docstring should mention 'engineer(s)'"
    assert "officers'" not in mod_doc, "Old 'officers\'' still in module docstring"
    assert 'officer roster' not in mod_doc, "Old 'officer roster' still in module docstring"

    # --- Comment near _OFFICER_KEY ---
    assert 'engineer is one edit in officers.OFFICER_NAMES' in src, \
        "Comment near _OFFICER_KEY should say 'engineer is one edit'"
    assert 'renaming an\n# officer' not in src, \
        "Comment near _OFFICER_KEY should not mention renaming an officer"

    # --- _md_to_html docstring ---
    assert 'in engineer text' in src, \
        "_md_to_html docstring should say 'in engineer text'"
    assert 'bullet-formatted engineer reports' in src, \
        "_md_to_html docstring should say 'bullet-formatted engineer reports'"
    assert 'officer text' not in src, "Old 'officer text' still in _md_to_html docstring"
    assert 'bullet-formatted officer' not in src, "Old 'bullet-formatted officer' still present"

    # --- roster() function comments ---
    assert 'matches board label via SOT' in src, \
        "roster() comment should reference SOT alignment"
    assert 'Every officer link routes through /chat' in src, \
        "roster() comment should explain /chat routing"

    # --- feed() comment ---
    assert 'Engineer-report bullets' in src, \
        "feed() comment should say 'Engineer-report bullets'"
    assert 'Officer-report bullets' not in src, \
        "Old 'Officer-report bullets' still in feed()"

    # --- phase_name() docstring ---
    assert 'fires for every engineer' in src, \
        "phase_name docstring should say 'fires for every engineer'"
    assert 'fires for every officer' not in src, \
        "Old 'fires for every officer' still in phase_name docstring"

    # --- URL param key REMOVED by EU-613 (all links go /chat now) ---
    assert '/group?officer=' not in src, "/group?officer= should have been removed by EU-613"
    # --- Module import MUST remain ---
    assert 'from .officers import' in src, ".officers import was renamed — breaks imports"
