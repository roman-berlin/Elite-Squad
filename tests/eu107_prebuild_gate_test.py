"""EU-107: Pre-build gate integration tests."""
import sys, types, asyncio
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import gate, senior_pm, loop
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket
from orchestrator.audit import AuditLog
from unittest.mock import AsyncMock, MagicMock, patch
import tempfile
import os

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Test configuration
cfg = Config(
    apps=[AppConfig(
        name="automatixy",
        repo_path=tempfile.mkdtemp(),
        base_branch="DEV",
        protected_branch="MAIN",
        backlog_backend="none",
        backlog={"project_key": "AUTO"}
    )],
    audit_path="/tmp/x.jsonl",
    use_worktree=True,
    worktree_dir=tempfile.mkdtemp(),
    prebuild_gate_enabled=True,  # Enable gate for testing
    auto_mode=True  # Enable auto_mode for testing (close actions execute)
)

audit = AuditLog(cfg.audit_path)

# --- is_worktree_locked function ---
# Test with no lock file
wt_path = tempfile.mkdtemp()
chk("is_worktree_locked returns False when no lock file", not loop.is_worktree_locked(wt_path))

# Test with lock file but not locked
lock_file = os.path.join(wt_path + ".lock")
with open(lock_file, "w") as f:
    f.write("1234\n")
chk("is_worktree_locked returns False when lock file exists but not locked", not loop.is_worktree_locked(wt_path))

# Test with locked worktree
import fcntl
lock_fd = os.open(lock_file, os.O_RDWR)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
try:
    chk("is_worktree_locked returns True when worktree is locked", loop.is_worktree_locked(wt_path))
finally:
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)

# --- prebuild_gate function ---
# Create test tickets
answer_ticket = Ticket(
    id="AUTO-100",
    key="AUTO-100",
    summary="Where is the config file?",
    description="What's the path to the config?",
    app="automatixy"
)

close_ticket = Ticket(
    id="AUTO-101",
    key="AUTO-101",
    summary="Duplicate ticket",
    description="This is a duplicate",
    app="automatixy"
)

continue_ticket = Ticket(
    id="AUTO-102",
    key="AUTO-102",
    summary="Add new feature",
    description="Implement a new feature",
    app="automatixy"
)

# Mock the Senior PM triage function
async def mock_triage(cfg, ticket, **kwargs):
    if "Where" in ticket.summary:
        return senior_pm.SeniorPMAudit(
            verdict="ANSWER",
            ticket_id=ticket.id,
            answer="Check the config directory",
            raw="Answer: Check the config directory\nSENIOR_PM VERDICT: ANSWER"
        )
    elif "duplicate" in ticket.summary.lower():
        return senior_pm.SeniorPMAudit(
            verdict="CLOSE",
            ticket_id=ticket.id,
            close_reason="Duplicate of AUTO-42",
            raw="Duplicate of AUTO-42\nSENIOR_PM VERDICT: CLOSE"
        )
    else:
        return senior_pm.SeniorPMAudit(
            verdict="CONTINUE",
            ticket_id=ticket.id,
            raw="Needs implementation\nSENIOR_PM VERDICT: CONTINUE"
        )

# Mock backlog functions
mock_backlog = MagicMock()
mock_backlog.add_comment = MagicMock()
mock_backlog.set_status = MagicMock()
mock_backlog.create_task = MagicMock(return_value="AUTO-103")

with patch('orchestrator.senior_pm.triage_async', side_effect=mock_triage):
    with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
        worklist = [(cfg.apps[0], answer_ticket)]
        filtered = asyncio.run(gate.prebuild_gate(cfg, worklist, audit))
        chk("prebuild_gate filters ANSWER ticket", len(filtered) == 0)
        chk("prebuild_gate closes ANSWER ticket", mock_backlog.set_status.call_count == 1)

# Test CLOSE ticket
mock_backlog.reset_mock()
with patch('orchestrator.senior_pm.triage_async', side_effect=mock_triage):
    with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
        worklist = [(cfg.apps[0], close_ticket)]
        filtered = asyncio.run(gate.prebuild_gate(cfg, worklist, audit))
        chk("prebuild_gate filters CLOSE ticket", len(filtered) == 0)
        chk("prebuild_gate closes CLOSE ticket", mock_backlog.set_status.call_count == 1)

# Test CONTINUE ticket
mock_backlog.reset_mock()
with patch('orchestrator.senior_pm.triage_async', side_effect=mock_triage):
    with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
        worklist = [(cfg.apps[0], continue_ticket)]
        filtered = asyncio.run(gate.prebuild_gate(cfg, worklist, audit))
        chk("prebuild_gate passes CONTINUE ticket through", len(filtered) == 1)
        chk("prebuild_gate doesn't close CONTINUE ticket", mock_backlog.set_status.call_count == 0)

# Test worktree locked ticket
mock_backlog.reset_mock()
locked_path = tempfile.mkdtemp()
# Create a locked worktree
lock_file = os.path.join(locked_path + ".lock")
with open(lock_file, "w") as f:
    f.write("1234\n")
lock_fd = os.open(lock_file, os.O_RDWR)
fcntl.flock(lock_fd, fcntl.LOCK_EX)

try:
    with patch('orchestrator.loop._worktree_path', return_value=locked_path):
        worklist = [(cfg.apps[0], continue_ticket)]
        filtered = asyncio.run(gate.prebuild_gate(cfg, worklist, audit))
        chk("prebuild_gate skips locked worktree tickets", len(filtered) == 0)
finally:
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)

# --- REFILE ticket test ---
refile_ticket = Ticket(
    id="AUTO-104",
    key="AUTO-104",
    summary="Vague request",
    description="Make it better",
    app="automatixy"
)

async def mock_refile_triage(cfg, ticket, **kwargs):
    return senior_pm.SeniorPMAudit(
        verdict="REFILE",
        ticket_id=ticket.id,
        refile_targets=[{
            "title": "Specific improvement",
            "type": "Story",
            "body": "Add specific details",
            "project": "AUTO"
        }],
        raw="Needs reformulation\n```json\n[{\"title\":\"Specific improvement\"}]\n```\nSENIOR_PM VERDICT: REFILE"
    )

mock_backlog.reset_mock()
with patch('orchestrator.senior_pm.triage_async', side_effect=mock_refile_triage):
    with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
        worklist = [(cfg.apps[0], refile_ticket)]
        filtered = asyncio.run(gate.prebuild_gate(cfg, worklist, audit))
        chk("prebuild_gate filters REFILE ticket", len(filtered) == 0)
        chk("prebuild_gate creates new tickets for REFILE", mock_backlog.create_task.call_count == 1)
        chk("prebuild_gate closes original REFILE ticket", mock_backlog.set_status.call_count == 1)

# --- Mixed tickets test ---
mock_backlog.reset_mock()
mixed_worklist = [
    (cfg.apps[0], answer_ticket),
    (cfg.apps[0], close_ticket),
    (cfg.apps[0], continue_ticket)
]

with patch('orchestrator.senior_pm.triage_async', side_effect=mock_triage):
    with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
        filtered = asyncio.run(gate.prebuild_gate(cfg, mixed_worklist, audit))
        chk("prebuild_gate filters mixed worklist correctly", len(filtered) == 1)
        chk("prebuild_gate only continues non-answerable tickets", filtered[0][1].id == "AUTO-102")

# --- EU-134: Conservative override for tickets with acceptance criteria ---
# A ticket with AC should ALWAYS CONTINUE, even if Senior PM says ANSWER/CLOSE/REFILE
feature_with_ac = Ticket(
    id="AUTO-105",
    key="AUTO-105",
    summary="Add new feature",
    description="Implement a new feature",
    acceptance_criteria=["AC1: Feature works", "AC2: Tests pass"],
    app="automatixy"
)

async def mock_answer_with_ac(cfg, ticket, **kwargs):
    # Senior PM says ANSWER, but ticket has AC
    return senior_pm.SeniorPMAudit(
        verdict="ANSWER",
        ticket_id=ticket.id,
        answer="This is a feature request",
        raw="Answer: This is a feature request\nSENIOR_PM VERDICT: ANSWER"
    )

mock_backlog.reset_mock()
with patch('orchestrator.senior_pm.triage_async', side_effect=mock_answer_with_ac):
    with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
        worklist = [(cfg.apps[0], feature_with_ac)]
        filtered = asyncio.run(gate.prebuild_gate(cfg, worklist, audit))
        chk("prebuild_gate overrides ANSWER to CONTINUE for ticket with AC", len(filtered) == 1)
        chk("prebuild_gate doesn't close ticket with AC", mock_backlog.set_status.call_count == 0)

# --- EU-134: Conservative override for [Feature] tagged tickets ---
feature_ticket = Ticket(
    id="AUTO-106",
    key="AUTO-106",
    summary="Feature request",
    description="Add a feature",
    labels=["Feature"],
    app="automatixy"
)

async def mock_close_feature(cfg, ticket, **kwargs):
    # Senior PM says CLOSE, but ticket has [Feature] label
    return senior_pm.SeniorPMAudit(
        verdict="CLOSE",
        ticket_id=ticket.id,
        close_reason="Feature is deprecated",
        raw="Feature deprecated\nSENIOR_PM VERDICT: CLOSE"
    )

mock_backlog.reset_mock()
with patch('orchestrator.senior_pm.triage_async', side_effect=mock_close_feature):
    with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
        worklist = [(cfg.apps[0], feature_ticket)]
        filtered = asyncio.run(gate.prebuild_gate(cfg, worklist, audit))
        chk("prebuild_gate overrides CLOSE to CONTINUE for [Feature] ticket", len(filtered) == 1)
        chk("prebuild_gate doesn't close [Feature] ticket", mock_backlog.set_status.call_count == 0)

# --- EU-134: Conservative override for [Bug] tagged tickets ---
bug_ticket = Ticket(
    id="AUTO-107",
    key="AUTO-107",
    summary="Bug report",
    description="Something is broken",
    labels=["Bug"],
    app="automatixy"
)

async def mock_refile_bug(cfg, ticket, **kwargs):
    # Senior PM says REFILE, but ticket has [Bug] label
    return senior_pm.SeniorPMAudit(
        verdict="REFILE",
        ticket_id=ticket.id,
        refile_targets=[{"title": "Better bug", "type": "Bug", "body": "Details", "project": "AUTO"}],
        raw="Needs reformulation\nSENIOR_PM VERDICT: REFILE"
    )

mock_backlog.reset_mock()
with patch('orchestrator.senior_pm.triage_async', side_effect=mock_refile_bug):
    with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
        worklist = [(cfg.apps[0], bug_ticket)]
        filtered = asyncio.run(gate.prebuild_gate(cfg, worklist, audit))
        chk("prebuild_gate overrides REFILE to CONTINUE for [Bug] ticket", len(filtered) == 1)
        chk("prebuild_gate doesn't refile [Bug] ticket", mock_backlog.create_task.call_count == 0)

# --- EU-134 Iteration 3: auto_mode off behavior ---
# When auto_mode is off, ANSWER/CLOSE/REFILE verdicts should set status to "Needs you"
# instead of auto-closing the ticket.

cfg_auto_off = Config(
    apps=[AppConfig(
        name="automatixy",
        repo_path=tempfile.mkdtemp(),
        base_branch="DEV",
        protected_branch="MAIN",
        backlog_backend="none",
        backlog={"project_key": "AUTO"}
    )],
    audit_path="/tmp/x.jsonl",
    use_worktree=True,
    worktree_dir=tempfile.mkdtemp(),
    prebuild_gate_enabled=True,
    auto_mode=False  # auto_mode is OFF
)

# Test ANSWER verdict with auto_mode off -> Needs you
mock_backlog.reset_mock()
with patch('orchestrator.senior_pm.triage_async', side_effect=mock_triage):
    with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
        worklist = [(cfg_auto_off.apps[0], answer_ticket)]
        filtered = asyncio.run(gate.prebuild_gate(cfg_auto_off, worklist, audit))
        chk("prebuild_gate with auto_mode off: ANSWER ticket filtered", len(filtered) == 0)
        chk("prebuild_gate with auto_mode off: ANSWER sets Needs you", mock_backlog.set_status.call_count == 1)
        if mock_backlog.set_status.call_count > 0:
            call_args = mock_backlog.set_status.call_args[0]
            status = call_args[1] if len(call_args) > 1 else None
            chk("prebuild_gate with auto_mode off: ANSWER uses 'Needs you' status", status == "Needs you")

# Test CLOSE verdict with auto_mode off -> Needs you
mock_backlog.reset_mock()
with patch('orchestrator.senior_pm.triage_async', side_effect=mock_triage):
    with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
        worklist = [(cfg_auto_off.apps[0], close_ticket)]
        filtered = asyncio.run(gate.prebuild_gate(cfg_auto_off, worklist, audit))
        chk("prebuild_gate with auto_mode off: CLOSE ticket filtered", len(filtered) == 0)
        chk("prebuild_gate with auto_mode off: CLOSE sets Needs you", mock_backlog.set_status.call_count == 1)
        if mock_backlog.set_status.call_count > 0:
            call_args = mock_backlog.set_status.call_args[0]
            status = call_args[1] if len(call_args) > 1 else None
            chk("prebuild_gate with auto_mode off: CLOSE uses 'Needs you' status", status == "Needs you")

# Test REFILE verdict with auto_mode off -> Needs you (no new tickets created)
mock_backlog.reset_mock()
with patch('orchestrator.senior_pm.triage_async', side_effect=mock_refile_triage):
    with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
        worklist = [(cfg_auto_off.apps[0], refile_ticket)]
        filtered = asyncio.run(gate.prebuild_gate(cfg_auto_off, worklist, audit))
        chk("prebuild_gate with auto_mode off: REFILE ticket filtered", len(filtered) == 0)
        chk("prebuild_gate with auto_mode off: REFILE sets Needs you", mock_backlog.set_status.call_count == 1)
        chk("prebuild_gate with auto_mode off: REFILE doesn't create new tickets", mock_backlog.create_task.call_count == 0)
        if mock_backlog.set_status.call_count > 0:
            call_args = mock_backlog.set_status.call_args[0]
            status = call_args[1] if len(call_args) > 1 else None
            chk("prebuild_gate with auto_mode off: REFILE uses 'Needs you' status", status == "Needs you")

print("\n================ EU-107 PRE-BUILD GATE TESTS ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
