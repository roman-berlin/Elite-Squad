"""EU-533 — rebrand 'officer' → 'engineer' in cockpit_views.py display strings.

Scans orchestrator/cockpit_views.py source and asserts:
  1. ZERO occurrences of 'officer'/'Officer' remain in any string literal or docstring.
  2. The chat empty-state reads 'When an engineer needs a decision'.
  3. The group empty-state reads 'the 1–2 relevant engineers'.
  4. The Roster nav link title is 'Engineers &amp; duties — the full unit roster'.
  5. The _working / _actbtn / _actbar docstrings use 'engineer task' / 'engineer-action'.
"""
from __future__ import annotations

import pathlib
import re

COCKPIT = pathlib.Path(__file__).resolve().parent.parent / "orchestrator" / "cockpit_views.py"


def test_no_officer_in_source():
    """Every old 'officer'/'Officer' occurrence must be gone — zero hits for [Oo]fficer."""
    src = COCKPIT.read_text()
    hits = re.findall(r'[Oo]fficer', src)
    assert not hits, f"Found {len(hits)} remaining 'officer'/[Oo]fficer occurrences: {hits}"


def test_chat_empty_state_uses_engineer():
    """Chat empty-state must say 'When an engineer needs a decision', not officer."""
    src = COCKPIT.read_text()
    assert 'When an engineer needs a decision' in src, (
        "Expected chat empty-state with 'When an engineer needs a decision'")
    assert 'When an officer needs a decision' not in src, (
        "Old 'When an officer needs a decision' still present")


def test_group_empty_state_uses_engineers():
    """Group-chat empty-state must say 'the 1–2 relevant engineers', not officers."""
    src = COCKPIT.read_text()
    assert 'the 1–2 relevant engineers' in src, (
        "Expected group empty-state with 'the 1–2 relevant engineers'")
    assert 'the 1–2 relevant officers' not in src, (
        "Old 'the 1–2 relevant officers' still present")


def test_roster_nav_title_updated():
    """Roster nav link title must be 'Engineers &amp; duties — the full unit roster'."""
    src = COCKPIT.read_text()
    assert 'title="Engineers &amp; duties — the full unit roster"' in src, (
        "Expected roster title with 'Engineers &amp; duties'")
    assert 'title="Officers &amp; duties' not in src, (
        "Old 'Officers &amp; duties' title still present")


def test_docstrings_use_engineer_task():
    """_working docstring says 'engineer task', not 'officer task'."""
    src = COCKPIT.read_text()
    assert 'engineer task' in src, "Expected '_working' docstring to mention 'engineer task'"
    assert 'officer task' not in src, "Old 'officer task' still in docstring"


def test_docstrings_use_engineer_action():
    """_actbtn / _actbar docstrings say 'engineer-action', not 'officer-action'."""
    src = COCKPIT.read_text()
    assert 'engineer-action' in src, "Expected _act* docstrings to mention 'engineer-action'"
    assert 'officer-action' not in src, "Old 'officer-action' still in docstring"
