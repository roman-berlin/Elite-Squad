"""EU-613 — officer-roster board links route through /chat, never /group.

Assertions:
  1. Every roster row's href == "/chat" (even non-general officers like pm, builder).
  2. No generated-href line in warroom.py contains '/group'.
  3. _GROUP_NAME is absent from warroom.py module-level.
"""
from __future__ import annotations

import pathlib
import sys
from unittest.mock import patch, PropertyMock, MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import orchestrator.warroom as warroom


def test_every_roster_href_is_chat():
    """EU-613: every officer-roster row resolves to /chat, verified end-to-end."""
    # Patch _scan and _last_council so roster() doesn't touch real files.
    with patch.object(warroom, "_scan", return_value={"last": {}, "count": {}, "merged_by_day": {}}), \
         patch.object(warroom, "_last_council", return_value=None):
        cfg = MagicMock(audit_path="/tmp/fake")
        rows = warroom.roster(cfg, [], False)       # not active, no tasks
        assert rows, "roster() returned empty — something is wrong"
        for i, row in enumerate(rows):
            assert row["href"] == "/chat", (
                f"Row {i} ({row['name']}) has href={row['href']!r}, expected '/chat'"
            )


def test_no_group_href_in_source():
    """EU-613: warroom.py generates no href pointing at /group."""
    src = pathlib.Path(__file__).resolve().parent.parent / "orchestrator" / "warroom.py"
    text = src.read_text()
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if 'href' in stripped and '/group' in stripped:
            raise AssertionError(
                f"Line {lineno} still emits a /group href: {stripped}"
            )


def test_group_name_symbol_removed():
    """EU-613: _GROUP_NAME is gone — no dead symbol left behind."""
    assert not hasattr(warroom, "_GROUP_NAME"), (
        "_GROUP_NAME still exists on the warroom module"
    )
