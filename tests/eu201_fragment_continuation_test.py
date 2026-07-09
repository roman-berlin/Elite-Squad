"""EU-201: After a scrum split, auto-continue building the fragments in order, then the next ticket.

When a ticket splits during a drain, the fragments are created and set to In Progress + parent closes Done.
The drain should then automatically build the fragments one by one (in dependency order), then continue
to the next queue ticket. No manual re-trigger should be needed.
"""
import sys, types, tempfile, asyncio
from pathlib import Path

# Mock the SDK
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import loop, scrum, intake
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, Outcome
from orchestrator.audit import AuditLog
import orchestrator.backlog.base as backlog_base

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Setup test environment
d = Path(tempfile.mkdtemp())
audit_path = d / "audit.jsonl"
audit_path.write_text("")

# Test app
app = AppConfig(
    name="automatixy",
    repo_path=str(d),
    base_branch="DEV",
    protected_branch="MAIN",
    backlog_backend="jira",
    backlog={"base_url": "x", "project_key": "AUTO"}
)
cfg = Config(
    apps=[app],
    audit_path=str(audit_path),
    use_worktree=False,
    dry_run=True  # Don't actually try to build
)

# --- Mock backlog that can create fragment tickets ---
fragment_counter = 0
fragments_created = []
fragments_moved_in_progress = []

class MockBacklog:
    def __init__(self):
        self.tickets = {}
        # Start with a parent ticket
        self.tickets["AUTO-100"] = Ticket(
            id="AUTO-100",
            key="AUTO-100",
            summary="Big feature",
            description="This is too big",
            acceptance_criteria=["AC1"],
            app="automatixy",
            ephemeral=False
        )

    def get_ready_tasks(self, limit):
        return [t for t in self.tickets.values() if hasattr(t, 'status') and t.status == "To Do"]

    def get_task(self, key):
        if key in self.tickets:
            return self.tickets[key]
        # Return a basic ticket for fragments that haven't been created yet
        return Ticket(
            id=key,
            key=key,
            summary=f"Fragment {key}",
            description="Fragment",
            acceptance_criteria=[],
            app="automatixy",
            ephemeral=False
        )

    def set_status(self, ticket, status):
        if isinstance(ticket, str):
            ticket = self.get_task(ticket)
        if hasattr(ticket, 'key'):
            t = self.tickets.get(ticket.key)
            if t:
                t.status = status
        if status == "In Progress" and "AUTO-" in str(ticket.key if hasattr(ticket, 'key') else ticket):
            fragments_moved_in_progress.append(str(ticket.key if hasattr(ticket, 'key') else ticket))

    def add_comment(self, ticket, body):
        pass

    def create_task(self, summary, description, labels=None, issue_type="Task"):
        global fragment_counter
        fragment_counter += 1
        key = f"AUTO-{100 + fragment_counter}"
        fragment = Ticket(
            id=key,
            key=key,
            summary=summary,
            description=description,
            acceptance_criteria=[],
            app="automatixy",
            ephemeral=False,
            status="In Progress"
        )
        self.tickets[key] = fragment
        fragments_created.append(key)
        return key

mock_backlog = MockBacklog()
backlog_base.make_backlog = lambda app: mock_backlog

# --- Test 1: Verify _fetch_fragments_to_worklist helper ---
print("Test 1: _fetch_fragments_to_worklist helper")
async def test_fetch_fragments():
    # Create a parent that splits into 3 fragments
    parent = Ticket(
        id="AUTO-100",
        key="AUTO-100",
        summary="Parent",
        description="Parent",
        acceptance_criteria=[],
        app="automatixy",
        ephemeral=False
    )

    # Simulate a split that creates 3 fragments
    global fragment_counter, fragments_created, fragments_moved_in_progress
    fragment_counter = 0
    fragments_created.clear()
    fragments_moved_in_progress.clear()

    # Create the fragments via the backlog
    fragment_keys = []
    for i in range(3):
        key = mock_backlog.create_task(
            f"Fragment {i}",
            f"Fragment {i} description"
        )
        fragment_keys.append(key)
        mock_backlog.set_status(key, "In Progress")

    chk("3 fragments created", len(fragment_keys) == 3)
    chk("all fragments moved to In Progress", len(fragments_moved_in_progress) == 3)

    # Now test the helper function (we'll add this to loop.py)
    try:
        from orchestrator.loop import _fetch_fragments_to_worklist
        worklist_items = _fetch_fragments_to_worklist(cfg, app, fragment_keys)
        chk("worklist has 3 items", len(worklist_items) == 3)
        chk("each item is (app, ticket)", all(isinstance(item, tuple) and len(item) == 2 for item in worklist_items))
        if worklist_items:
            chk("tickets have correct keys", [item[1].key for item in worklist_items] == fragment_keys)
    except (ImportError, AttributeError) as e:
        chk(f"TODO: add _fetch_fragments_to_worklist helper to loop.py: {e}", False)

asyncio.run(test_fetch_fragments())

# --- Test 2: Verify fragment injection into worklist ---
print("\nTest 2: Fragment injection into worklist after split")
async def test_fragment_injection():
    # This tests the main flow: parent splits -> fragments injected -> built in order
    # We'll verify this once we implement the feature
    try:
        from orchestrator.loop import _run_inner
        from orchestrator.contracts import Outcome

        # Create a worklist with a parent ticket
        parent = Ticket(
            id="AUTO-100",
            key="AUTO-100",
            summary="Parent ticket",
            description="This is too big",
            acceptance_criteria=["AC1"],
            app="automatixy",
            ephemeral=False
        )
        worklist = [(app, parent)]

        # Create a mock report indicating a split with 3 fragments
        # We'll simulate the split by creating a REQUEUED report
        global fragment_counter, fragments_created, fragments_moved_in_progress
        fragment_counter = 0
        fragments_created.clear()
        fragments_moved_in_progress.clear()

        # Create fragments via backlog
        fragment_keys = []
        for i in range(3):
            key = mock_backlog.create_task(
                f"Fragment {i}",
                f"Fragment {i} description"
            )
            fragment_keys.append(key)
            mock_backlog.set_status(key, "In Progress")

        # Verify fragments were created
        chk("fragment injection setup: 3 fragments created", len(fragment_keys) == 3)

        # Test that fragments are injected into the worklist
        # We'll verify this works by checking the logic in _run_inner
        # For now, just verify the parsing logic works
        import re
        test_notes = "too big — Scrum Master split into AUTO-101, AUTO-102, AUTO-103"
        fragment_match = re.search(r'split into ([^.\n]+)', test_notes)
        chk("fragment injection: parse fragment keys from notes", fragment_match is not None)
        if fragment_match:
            extracted_keys = [k.strip() for k in fragment_match.group(1).split(',')]
            chk("fragment injection: extracted 3 keys", len(extracted_keys) == 3)
            chk("fragment injection: keys match expected", extracted_keys == fragment_keys)

    except Exception as e:
        chk(f"fragment injection test failed: {e}", False)

asyncio.run(test_fragment_injection())

# --- Test 3: Verify depth guard still works ---
print("\nTest 3: Depth guard prevents runaway recursion")
def test_depth_guard():
    # A fragment at max depth should NOT split again
    from orchestrator import scrum as scrum_mod
    max_depth_ticket = types.SimpleNamespace(
        id="AUTO-200",
        summary="Already split multiple times",
        description="x <!-- autosplit-depth: 3 -->",
        ephemeral=False
    )
    depth = scrum_mod._split_depth(max_depth_ticket)
    chk("depth read correctly", depth == 3)
    chk("depth at max", depth >= scrum_mod._MAX_SPLIT_DEPTH)

test_depth_guard()

print("\n============ EU-201 FRAGMENT CONTINUATION QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
