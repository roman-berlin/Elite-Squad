"""EU-597: scrub remaining Group room references — doc-regression guard."""

import os
import re
import subprocess
import unittest


# ------------------------------------------------------------------ #
#   We treat QA_MANUAL.md as a source-of-truth spec for this pass.    #
#   Active (present-tense / imperative) Group-room instructions       #
#   must NOT exist after EU-541's removal landed.                     #
# ------------------------------------------------------------------ #

_CLASSIFY_RE = re.compile(r"group\s*room", re.IGNORECASE)
_PASS_TENSE_MARKER = re.compile(
    r"(?:was\s+removed|superseded|removed\s+\d{4}-|no\s+longer\s+exists)"
)


class TestEu597GroupRoomScrub(unittest.TestCase):
    """Assert QA_MANUAL.md carries no active Group-room instruction."""

    _MANUAL_PATH = "QA_MANUAL.md"

    def _read_lines(self):
        with open(self._MANUAL_PATH, encoding="utf-8") as f:
            return f.readlines()

    # -- 1. No heading-level Group-room section -----------------------

    def test_no_group_room_heading(self):
        """QA_MANUAL.md must not contain a '## N. Group room' heading."""
        lines = self._read_lines()
        for i, raw in enumerate(lines, 1):
            if re.match(
                r"^\s*##\s+\d*\.*\s*group\s*room\b", raw, re.IGNORECASE
            ):
                self.fail(
                    f"Found active Group-room heading at {self._MANUAL_PATH}:{i}: "
                    f"{raw.rstrip()}\nRemove it or collapse to a past-tense note."
                )

    # -- 2. No standalone 'group' token in checklist rows -------------

    def test_no_group_in_checklist_row(self):
        """The QA checklist row must not contain standalone 'group' as an area."""
        lines = self._read_lines()
        for i, raw in enumerate(lines, 1):
            stripped = raw.strip()
            if not (stripped.startswith("|") and "|" in stripped[1:]):
                continue
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            for cell in cells:
                if not cell:
                    continue
                if re.search(r"(?<!\w)group(?!\w)", cell, re.IGNORECASE) and not _PASS_TENSE_MARKER.search(
                    cell
                ):
                    self.fail(
                        f"Found standalone 'group' token in checklist cell at "
                        f"{self._MANUAL_PATH}:{i}: '{cell}'. "
                        f"Rewrite to reflect current state (CTO chat, consult, etc.)."
                    )

    # -- 3. All grep hits are past-tense / historical -----------------

    def test_all_group_room_hits_are_pass_tense_or_historical(self):
        """Every 'group room' mention outside this test must be past-tense / historical."""
        result = subprocess.run(
            [
                "grep", "-rni", "group room",
                "--include=*.md", "--include=*.py", ".",
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return  # no hits — good

        # Basenames of files where "group room" mentions are always historical
        # or assert a removal — never active feature descriptions.
        EXCLUDE_BASES = frozenset({
            "ROADMAP.md",
            "Development_Status.md",
            os.path.basename(__file__),  # this test's own docstrings/comments
            "eu594_group_entry_points_test.py",  # asserts Group room was removed
            "eu608_group_chat_removed_test.py",  # asserts Group room routes gone
            "eu534_rebrand_test.py",              # references removed card
        })

        bad = []
        for line in result.stdout.splitlines():
            parts = line.split(":", 1)
            if len(parts) < 2:
                continue
            filepath = parts[0]
            # Strip leading ./ for basename matching
            clean = filepath.lstrip("./")
            base = os.path.basename(clean)
            if base in EXCLUDE_BASES:
                continue
            text = parts[1].strip()
            if _PASS_TENSE_MARKER.search(text):
                continue
            bad.append(line)

        self.assertFalse(
            bad,
            "Found 'group room' mentions that look like active feature refs:\n"
            + "\n".join(bad),
        )

    # -- 4. No imperative + group-room combo on any line ---------------

    def test_qa_manual_no_active_group_room_instruction(self):
        """Assert there is no imperative instruction about a Group room tab."""
        lines = self._read_lines()
        for i, raw in enumerate(lines, 1):
            stripped = raw.strip().lower()
            if any(m in stripped for m in ("was removed", "superseded", "no longer exists")):
                continue
            if stripped.startswith("_(") and "was removed" in stripped:
                continue
            imperatives = (
                "open", "click", "go to", "navigate to", "check",
                "verify", "test", "expect.*group", "visit", "enter",
            )
            for kw in imperatives:
                pat = re.compile(kw, re.IGNORECASE)
                if pat.search(stripped) and _CLASSIFY_RE.search(stripped):
                    self.fail(
                        f"Active Group-room instruction at {self._MANUAL_PATH}:{i}: "
                        f"{raw.rstrip()}"
                    )


if __name__ == "__main__":
    # run standalone with non-zero exit on failure
    unittest.main(exit=True)
