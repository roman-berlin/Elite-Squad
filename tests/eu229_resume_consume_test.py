"""EU-229 follow-up — Jira-answer resume must CONSUME the answer it detected (2026-07-16).

Live incident: a single new Jira comment on EU-335 (which carried a stale pending
decision with answer_baseline "") made `autopilot._resumable_answered` re-detect the
SAME comment every drain cycle — "▶️ Resuming (answered on Jira): EU-335" spammed
Telegram every ~2 minutes (10:45→10:56+) until the pending record was hand-deleted.
Root cause: EU-61's original blocked-only resume was self-limiting (the ticket left
the blocked set after one resume); EU-229 widened the scan to ALL pending decisions
but never moved the consume step — nothing ever advanced `answer_baseline`.

Companion clobber bug, same incident: the resume block wrote back a `blocked` set
loaded at the TOP of the cycle, ~2 minutes of Jira scanning earlier — resurrecting
EU-218 minutes after the Commander's /unblock removed it (blocked_tickets.json
mtime 10:56:30 == the resume cycle's save). The park path lower in the loop already
re-reads right before its write-back for exactly this reason; the resume site must too.

Pins:
  (1) first scan returns the newly-answered ticket (control);
  (2) an IDENTICAL answer does not resume again — the scan consumed it (this is the
      fail-first behavioral red: pre-fix, the same comment resumed every call);
  (3) the consumed answer is persisted as the new answer_baseline in the store;
  (4) a genuinely NEW (different) comment resumes the ticket again;
  (5) source pin: the resume block re-reads load_blocked() right before save_blocked
      instead of writing back the stale top-of-cycle snapshot (mutation-verified:
      reverting to `blocked -= set(resumed)` + save of the stale set turns this RED).
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

# ── minimal SDK / requests stubs so the orchestrator imports without real deps ──
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers={}, update=lambda *a, **k: None)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import autopilot, decisions  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402
from orchestrator.contracts import Ticket  # noqa: E402

_TMP = Path(tempfile.mkdtemp())
_CFG = Config(
    apps=[
        AppConfig(name="testapp", repo_path=str(_TMP), base_branch="dev",
                  protected_branch="main", backlog_backend="jira"),
    ],
    audit_path=str(_TMP / "a.jsonl"),
    use_worktree=False,
)

checks = 0


def ok(name: str, cond, detail: str = "") -> None:
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


class FakeBacklog:
    """Just enough of the adapter surface for _resumable_answered."""

    def __init__(self):
        self.answer = "do X — approved"

    def get_task(self, key):
        return Ticket(id=key, key=key, summary="s", description="d")

    def latest_answer(self, ticket):
        return self.answer


fake = FakeBacklog()

# Seed one pending decision the way the live store held EU-335: baseline "" (any
# human comment counts as new).
decisions._save(_CFG, [{
    "id": "EU-999", "app": "testapp", "question": "q?", "summary": "s",
    "description": "", "acceptance": [], "ephemeral": False, "ts": 0.0,
    "_question_fp": "deadbeef", "answer_baseline": "",
}])

with patch("orchestrator.backlog.base.make_backlog", return_value=fake):
    first = autopilot._resumable_answered(_CFG, "testapp", set())
    ok("(1) new Jira answer resumes the ticket", "EU-999" in first,
       f"got {sorted(first)}")

    second = autopilot._resumable_answered(_CFG, "testapp", set())
    ok("(2) the SAME answer does not resume again (consumed)", "EU-999" not in second,
       f"got {sorted(second)} — the scan re-detected an already-consumed comment; "
       f"this is the '▶️ Resuming' Telegram spam loop")

    stored = {d.get("id"): d for d in decisions.load(_CFG)}
    ok("(3) consumed answer persisted as the new baseline",
       stored.get("EU-999", {}).get("answer_baseline") == fake.answer,
       f"baseline={stored.get('EU-999', {}).get('answer_baseline')!r}")

    fake.answer = "second thought — do Y instead"
    third = autopilot._resumable_answered(_CFG, "testapp", set())
    ok("(4) a genuinely NEW comment resumes again", "EU-999" in third,
       f"got {sorted(third)}")

# (5) the stale-blocked clobber: the resume block must re-read the on-disk parked set
# right before its write-back (the park path lower in the loop already does — same rule).
src = (Path(__file__).resolve().parent.parent / "orchestrator" / "autopilot.py").read_text()
resume_at = src.find("Resuming (answered on Jira)")
ok("(5-pre) the resume log anchor still exists in autopilot.py", resume_at != -1,
   "the anchor string was reworded — without this guard the window silently widened to the "
   "whole file and check (5) could false-pass")
window = src[max(0, resume_at - 600):resume_at]
ok("(5) resume write-back re-reads load_blocked() instead of saving the stale snapshot",
   "load_blocked(cfg) - set(resumed)" in window,
   "the resume block writes back a blocked set loaded before the (minutes-long) Jira "
   "scan — an /unblock landing mid-scan gets clobbered (EU-218, 2026-07-16 10:56)")

print(f"\n{checks}/{checks} passed")
