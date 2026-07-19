"""EU-301 — scrum.split decomposes an oversized ticket into an EPIC + To-Do children (+ verify child).

Roman 2026-07-14: when a big ticket is split, the pieces should go under an Epic as children built
one at a time, and the last piece should be a verify-and-close child carrying the whole feature's
acceptance. scrum.split previously filed FLAT siblings all marked In Progress at once (board noise,
no grouping, WIP>1, the AUTO-135..143 stranded pile).

Pins (recon + backlog stubbed — no LLM, no live Jira):
  (1) an Epic is created (issue_type="Epic") from the parent;
  (2) every work child is created linked to the Epic via parent=<epic key>;
  (3) a final "Verify & close" child is appended, carrying the PARENT's acceptance criteria;
  (4) children are NOT pre-marked In Progress (they start To Do; the drain promotes on ticket_start);
  (5) the parent is closed (Done) with a decomposed-into-Epic comment;
  (6) graceful fallback: if Epic creation fails (no Epic type), children are filed FLAT (parent=None)
      and the split still succeeds — never lost.
"""
from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import patch

sdk = types.ModuleType("claude_agent_sdk")
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", sdk)
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers={}, update=lambda *a, **k: None)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import scrum  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


REPORT = """Here is the decomposition:
=== TICKET ===
TITLE: Piece one
Do the first part.
=== TICKET ===
TITLE: Piece two
Do the second part.
"""


class FakeBacklog:
    def __init__(self, epic_raises=False):
        self.epic_raises = epic_raises
        self.created = []      # (summary, issue_type, parent, labels)
        self.statuses = []     # (key, status)
        self.comments = []

    def create_task(self, summary, description, labels=None, issue_type="Task", priority=None, parent=None):
        if issue_type == "Epic" and self.epic_raises:
            raise RuntimeError("board has no Epic issue type")
        key = ("EPIC-1" if issue_type == "Epic" else f"CH-{len([c for c in self.created if c[1] != 'Epic']) + 1}")
        self.created.append((summary, issue_type, parent, list(labels or [])))
        return key

    def set_status(self, ticket, status):
        self.statuses.append((getattr(ticket, "key", getattr(ticket, "id", ticket)), status))

    def add_comment(self, ticket, body):
        self.comments.append(body)


def parent_ticket():
    return types.SimpleNamespace(id="EU-999", key="EU-999", summary="Big feature",
                                 description="Build the whole thing.",
                                 acceptance_criteria=["It works end to end", "No regressions"],
                                 ephemeral=False)


class _Cfg:
    reviewer_model = "claude-opus-4-8"
    discussion_model = "claude-sonnet-5"
    auto_model = False

    def app(self, name):
        return types.SimpleNamespace(name=name, repo_path=".", base_branch="dev")


async def _run(fake):
    async def _fake_officer(*a, **k):
        return REPORT

    with patch("orchestrator.backlog.base.make_backlog", lambda app: fake), \
         patch("orchestrator.recon.run_officer", _fake_officer), \
         patch("orchestrator.models.for_officer", lambda *a, **k: ("claude-sonnet-5", "test")):
        return await scrum.split(_Cfg(), "Elite-Unit", parent_ticket(), reason="too big")


# --- Epic path ---
fake = FakeBacklog(epic_raises=False)
res = asyncio.run(_run(fake))
epics = [c for c in fake.created if c[1] == "Epic"]
child = [c for c in fake.created if c[1] != "Epic"]

ok("(1) an Epic is created from the parent", len(epics) == 1 and epics[0][0] == "Big feature",
   f"epics={epics}")
ok("(2) every child is linked to the Epic via parent", child and all(c[2] == "EPIC-1" for c in child),
   f"children parents={[c[2] for c in child]}")
ok("(3) a Verify & close child is appended carrying the parent AC",
   any("Verify & close" in c[0] for c in child), f"child titles={[c[0] for c in child]}")
# the 2 work pieces + 1 verify child
ok("(3b) children = work pieces + exactly one verify child", len(child) == 3, f"n children={len(child)}")
ok("(4) children are NOT pre-marked In Progress",
   not any(s[1] == "In Progress" for s in fake.statuses), f"statuses={fake.statuses}")
ok("(5) parent closed Done with a decomposed-into-Epic comment",
   ("EU-999", "Done") in fake.statuses and any("EPIC-1" in c for c in fake.comments),
   f"statuses={fake.statuses} comments={fake.comments}")
ok("(5b) split reports ok=True + the epic key", res.get("ok") is True and res.get("epic") == "EPIC-1",
   f"res={ {k: res.get(k) for k in ('ok', 'epic', 'keys', 'error')} }")

# --- graceful fallback: no Epic type ---
fake2 = FakeBacklog(epic_raises=True)
res2 = asyncio.run(_run(fake2))
child2 = [c for c in fake2.created if c[1] != "Epic"]
ok("(6) Epic creation failure → flat children (parent=None), split still succeeds",
   res2.get("ok") is True and child2 and all(c[2] is None for c in child2) and res2.get("epic") is None,
   f"res2={ {k: res2.get(k) for k in ('ok', 'epic', 'keys')} } parents={[c[2] for c in child2]}")

print(f"\n{checks}/{checks} passed")
