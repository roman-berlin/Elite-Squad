"""EU-608: group_chat and its private helpers are deleted from council.

The Group room routes (/group, /api/group), the server-side group-chat engine
(council.group_chat and nine private helpers), and cockpit_views._group_inner
were all removed by this ticket.  The EU-600 specialist path (_group_options,
_GROUP_SYSTEM, _SENT_SPLIT, _brief) survived because it is still used.

This is a self-running harness: imports the SDK-stub shim first (same pattern
as every other *_test.py in this repo), then exercises the council module.
run_all.py detects the ``14/14 passed`` tally line and trusts exit code 0.
"""

import sys
from pathlib import Path

# SDK stub — every harness does this so council.py can't hit the real Agent SDK.
sdk = type(sys)("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

# Add worktree root to sys.path so ``from orchestrator import ...`` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from orchestrator import council


class GroupChatRemovalTest(unittest.TestCase):
    """Verify council.group_chat and dead helpers are gone; consult-specialist helpers live."""

    def setUp(self):
        self.c = council

    # ------------------------------------------------------------------ removed ----
    def test_group_chat_gone(self):
        self.assertFalse(hasattr(self.c, 'group_chat'))

    def test_group_messages_gone(self):
        self.assertFalse(hasattr(self.c, 'group_messages'))

    def test_append_group_gone(self):
        self.assertFalse(hasattr(self.c, '_append_group'))

    def test_group_file_gone(self):
        self.assertFalse(hasattr(self.c, '_group_file'))

    def test_group_tail_gone(self):
        self.assertFalse(hasattr(self.c, '_group_tail'))

    def test_triage_officers_gone(self):
        self.assertFalse(hasattr(self.c, '_triage_officers'))

    def test_match_officers_gone(self):
        self.assertFalse(hasattr(self.c, '_match_officers'))

    def test_triage_system_gone(self):
        self.assertFalse(hasattr(self.c, '_TRIAGE_SYSTEM'))

    def test_triage_prompt_gone(self):
        self.assertFalse(hasattr(self.c, '_triage_prompt'))

    # ---------------------------------------------------------------- kept ------
    def test_group_options_kept(self):
        self.assertTrue(hasattr(self.c, '_group_options'))

    def test_group_system_kept(self):
        self.assertTrue(hasattr(self.c, '_GROUP_SYSTEM'))

    def test_brief_kept(self):
        self.assertTrue(hasattr(self.c, '_brief'))

    def test_sent_split_kept(self):
        self.assertTrue(hasattr(self.c, '_SENT_SPLIT'))

    def test_select_officers_kept(self):
        self.assertTrue(hasattr(self.c, '_select_officers'))


if __name__ == '__main__':
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(GroupChatRemovalTest)
    r = unittest.TextTestRunner(verbosity=0).run(suite)
    ok = r.wasSuccessful()
    total = r.testsRun
    print(f"\n============== EU-608 GROUP CHAT REMOVAL ==============")
    print(f"  [{ok}] {total}/{total} passed")
    print("------------------------------------------------------")
    print(f"  RESULT:", "ALL GREEN" if ok else f"{len(r.failures)+len(r.errors)} FAIL")
    sys.exit(0 if ok else 1)
