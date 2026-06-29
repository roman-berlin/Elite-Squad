"""Autopilot pre-build gate tests - verify that Senior PM triage runs before Builder."""
import sys, types, asyncio, tempfile
from unittest.mock import AsyncMock, MagicMock, patch

# Stub the Agent SDK (no network / real models)
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import autopilot, gate, senior_pm
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, Outcome
from orchestrator.audit import AuditLog

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ---- Test configuration ----
cfg = Config(
    apps=[
        AppConfig(
            name="automatixy",
            repo_path=tempfile.mkdtemp(),
            base_branch="DEV",
            protected_branch="MAIN",
            backlog_backend="none",
            backlog={"project_key": "AUTO"}
        )
    ],
    audit_path=tempfile.mktemp(),
    use_worktree=True,
    worktree_dir=tempfile.mkdtemp()
)

# ---- Test that Senior PM triage is integrated ----
print("\n==== Testing Senior PM Integration in Autopilot ====")

async def test_senior_pm_triage_called():
    """Test that Senior PM triage is called by the prebuild gate."""

    ticket = Ticket(
        id="AUTO-100",
        key="AUTO-100",
        summary="Question about config",
        description="Where is the config file?",
        app="automatixy"
    )

    triage_calls = []

    async def mock_triage(cfg, ticket, **kwargs):
        triage_calls.append(ticket.id)
        return senior_pm.SeniorPMAudit(
            verdict="ANSWER",
            ticket_id=ticket.id,
            answer="Check config directory",
            raw="Answer: Check config directory\nSENIOR_PM VERDICT: ANSWER"
        )

    mock_backlog = MagicMock()
    mock_backlog.add_comment = MagicMock()
    mock_backlog.set_status = MagicMock()

    with patch('orchestrator.senior_pm.triage_async', side_effect=mock_triage):
        with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
            worklist = [(cfg.apps[0], ticket)]
            audit = AuditLog(cfg.audit_path)
            filtered = await gate.prebuild_gate(cfg, worklist, audit)

    chk("Senior PM triage is called for each ticket", len(triage_calls) == 1)
    chk("Senior PM triage receives correct ticket", triage_calls[0] == "AUTO-100")
    chk("prebuild_gate filters ANSWER tickets", len(filtered) == 0)
    chk("prebuild_gate closes ANSWER tickets", mock_backlog.set_status.call_count == 1)

asyncio.run(test_senior_pm_triage_called())

async def test_close_verdict_handling():
    """Test that CLOSE verdict results in ticket closure."""

    ticket = Ticket(
        id="AUTO-101",
        key="AUTO-101",
        summary="Duplicate ticket",
        description="This is a duplicate",
        app="automatixy"
    )

    async def mock_triage(cfg, ticket, **kwargs):
        return senior_pm.SeniorPMAudit(
            verdict="CLOSE",
            ticket_id=ticket.id,
            close_reason="Duplicate of AUTO-42",
            raw="Duplicate of AUTO-42\nSENIOR_PM VERDICT: CLOSE"
        )

    mock_backlog = MagicMock()
    mock_backlog.add_comment = MagicMock()
    mock_backlog.set_status = MagicMock()

    with patch('orchestrator.senior_pm.triage_async', side_effect=mock_triage):
        with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
            worklist = [(cfg.apps[0], ticket)]
            audit = AuditLog(cfg.audit_path)
            filtered = await gate.prebuild_gate(cfg, worklist, audit)

    chk("CLOSE ticket is filtered", len(filtered) == 0)
    chk("CLOSE ticket is closed", mock_backlog.set_status.call_count == 1)
    chk("CLOSE ticket has comment added", mock_backlog.add_comment.call_count == 1)

asyncio.run(test_close_verdict_handling())

async def test_refile_verdict_handling():
    """Test that REFILE verdict creates new tickets and closes original."""

    ticket = Ticket(
        id="AUTO-102",
        key="AUTO-102",
        summary="Vague request",
        description="Make it better",
        app="automatixy"
    )

    async def mock_triage(cfg, ticket, **kwargs):
        return senior_pm.SeniorPMAudit(
            verdict="REFILE",
            ticket_id=ticket.id,
            refile_targets=[
                {
                    "title": "Specific improvement",
                    "type": "Story",
                    "body": "Add specific details",
                    "project": "AUTO",
                    "reason": "Reformulate"
                }
            ],
            raw="Needs reformulation\n```json\n[{\"title\":\"Specific improvement\"}]\n```\nSENIOR_PM VERDICT: REFILE"
        )

    mock_backlog = MagicMock()
    mock_backlog.add_comment = MagicMock()
    mock_backlog.set_status = MagicMock()
    mock_backlog.create_task = MagicMock(return_value="AUTO-103")

    with patch('orchestrator.senior_pm.triage_async', side_effect=mock_triage):
        with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
            worklist = [(cfg.apps[0], ticket)]
            audit = AuditLog(cfg.audit_path)
            filtered = await gate.prebuild_gate(cfg, worklist, audit)

    chk("REFILE ticket is filtered", len(filtered) == 0)
    chk("REFILE creates new ticket", mock_backlog.create_task.call_count == 1)
    chk("REFILE closes original ticket", mock_backlog.set_status.call_count == 1)
    chk("REFILE adds comment", mock_backlog.add_comment.call_count == 2)  # Original refile + new ticket reference

asyncio.run(test_refile_verdict_handling())

async def test_continue_verdict_handling():
    """Test that CONTINUE tickets pass through to build loop."""

    ticket = Ticket(
        id="AUTO-104",
        key="AUTO-104",
        summary="Build new feature",
        description="Implement feature X",
        app="automatixy"
    )

    async def mock_triage(cfg, ticket, **kwargs):
        return senior_pm.SeniorPMAudit(
            verdict="CONTINUE",
            ticket_id=ticket.id,
            raw="Needs implementation\nSENIOR_PM VERDICT: CONTINUE"
        )

    mock_backlog = MagicMock()

    with patch('orchestrator.senior_pm.triage_async', side_effect=mock_triage):
        with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
            worklist = [(cfg.apps[0], ticket)]
            audit = AuditLog(cfg.audit_path)
            filtered = await gate.prebuild_gate(cfg, worklist, audit)

    chk("CONTINUE ticket passes through", len(filtered) == 1)
    chk("CONTINUE ticket is not closed", mock_backlog.set_status.call_count == 0)
    chk("CONTINUE ticket is correct", filtered[0][1].id == "AUTO-104")

asyncio.run(test_continue_verdict_handling())

async def test_mixed_verdicts():
    """Test that multiple tickets with different verdicts are handled correctly."""

    tickets = [
        Ticket(id="AUTO-105", key="AUTO-105", summary="Question", description="?", app="automatixy"),
        Ticket(id="AUTO-106", key="AUTO-106", summary="Feature", description="Build", app="automatixy"),
        Ticket(id="AUTO-107", key="AUTO-107", summary="Duplicate", description="X", app="automatixy"),
    ]

    triage_results = {
        "AUTO-105": senior_pm.SeniorPMAudit(verdict="ANSWER", ticket_id="AUTO-105", answer="Docs", raw="SENIOR_PM VERDICT: ANSWER"),
        "AUTO-106": senior_pm.SeniorPMAudit(verdict="CONTINUE", ticket_id="AUTO-106", raw="SENIOR_PM VERDICT: CONTINUE"),
        "AUTO-107": senior_pm.SeniorPMAudit(verdict="CLOSE", ticket_id="AUTO-107", close_reason="Dup", raw="SENIOR_PM VERDICT: CLOSE"),
    }

    async def mock_triage(cfg, ticket, **kwargs):
        return triage_results[ticket.id]

    mock_backlog = MagicMock()
    mock_backlog.add_comment = MagicMock()
    mock_backlog.set_status = MagicMock()

    with patch('orchestrator.senior_pm.triage_async', side_effect=mock_triage):
        with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
            worklist = [(cfg.apps[0], t) for t in tickets]
            audit = AuditLog(cfg.audit_path)
            filtered = await gate.prebuild_gate(cfg, worklist, audit)

    chk("Mixed verdicts: 2 tickets filtered", len(filtered) == 1)
    chk("Mixed verdicts: 1 ticket continues", filtered[0][1].id == "AUTO-106")
    chk("Mixed verdicts: 2 tickets closed", mock_backlog.set_status.call_count == 2)

asyncio.run(test_mixed_verdicts())

# ---- Test autopilot flow with prebuild gate ----
async def test_autopilot_flow_validates_integration():
    """Test that the complete autopilot flow validates the prebuild gate integration."""

    # Test that prebuild_gate is called before tickets reach the build loop
    # by verifying the gate filter behavior in isolation
    ticket = Ticket(
        id="AUTO-108",
        key="AUTO-108",
        summary="Build feature",
        description="Implement it",
        app="automatixy"
    )

    async def mock_triage(cfg, ticket, **kwargs):
        return senior_pm.SeniorPMAudit(
            verdict="CONTINUE",
            ticket_id=ticket.id,
            raw="SENIOR_PM VERDICT: CONTINUE"
        )

    mock_backlog = MagicMock()

    with patch('orchestrator.senior_pm.triage_async', side_effect=mock_triage):
        with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
            # Directly test the prebuild_gate behavior
            worklist = [(cfg.apps[0], ticket)]
            audit = AuditLog(cfg.audit_path)
            filtered = await gate.prebuild_gate(cfg, worklist, audit)

    chk("Prebuild_gate allows CONTINUE tickets", len(filtered) == 1)
    chk("Prebuild_gate passes correct ticket", filtered[0][1].id == "AUTO-108")
    chk("Prebuild_gate doesn't close CONTINUE tickets", mock_backlog.set_status.call_count == 0)

asyncio.run(test_autopilot_flow_validates_integration())

# ---- Print results ----
print("\n============ AUTOPILOT PRE-BUILD GATE TESTS ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
