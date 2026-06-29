"""Senior PM officer comprehensive tests - covering doctrine answers, duplicate detection,
re-filing mis-projected tickets, locked ticket handling, and audit entry formats."""
import sys, types, asyncio
import tempfile
import os
from unittest.mock import AsyncMock, MagicMock, patch

# Stub the Agent SDK (no network / real models)
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import senior_pm, recon
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket
from orchestrator.audit import AuditLog

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ---- Test configuration ----
cfg = Config(
    apps=[
        AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                  protected_branch="MAIN", backlog_backend="none",
                  backlog={"project_key": "AUTO"}),
        AppConfig(name="elite-unit", repo_path=".", base_branch="dev",
                  protected_branch="main", backlog_backend="none",
                  backlog={"project_key": "EU"})
    ],
    audit_path=tempfile.mktemp(),
    use_worktree=True,
    worktree_dir=tempfile.mkdtemp()
)

# ---- (a) Answering from doctrine/config ----
print("\n==== Testing Answer from Doctrine/Config ====")

async def test_doctrine_answer():
    """Test Senior PM can answer questions based on repo docs/config."""
    ticket = Ticket(
        id="AUTO-100",
        key="AUTO-100",
        summary="Where is the tenant isolation config?",
        description="What file contains the tenant isolation rules?",
        app="automatixy"
    )

    # Mock recon.run_officer to return a doctrine-based answer
    async def fake_run_officer(**kw):
        return (
            "CITATION: CLAUDE.md - tenant isolation is enforced at query level\n"
            "CITATION: .claude/rules/tenant-isolation.md - explicit tenant_id filter required\n"
            "Answer: The tenant isolation rules are in .claude/rules/tenant-isolation.md.\n"
            "SENIOR_PM VERDICT: ANSWER"
        )

    with patch('orchestrator.recon.run_officer', side_effect=fake_run_officer):
        audit = await senior_pm.triage_async(cfg, ticket, repo_context="CLAUDE.md excerpt here")

    chk("doctrine answer returns ANSWER verdict", audit.verdict == "ANSWER")
    chk("doctrine answer includes multiple citations", len(audit.citations) == 2)
    chk("doctrine answer citations include CLAUDE.md",
        any(c["source"] == "CLAUDE.md" for c in audit.citations))
    chk("doctrine answer citations include rules file",
        any(".claude/rules/tenant-isolation.md" in c["source"] for c in audit.citations))
    chk("doctrine answer provides actionable answer", ".claude/rules/tenant-isolation.md" in audit.answer)

asyncio.run(test_doctrine_answer())

async def test_config_answer():
    """Test Senior PM can answer from config.yaml defaults."""
    ticket = Ticket(
        id="AUTO-101",
        key="AUTO-101",
        summary="Is there a separate production environment?",
        description="Do we have a production environment separate from DEV?",
        app="automatixy"
    )

    async def fake_run_officer(**kw):
        return (
            "CITATION: config.yaml defaults - DEV environment is intentionally live\n"
            "CITATION: vercel.json - /api/* rewrites point to DEV backend\n"
            "Answer: No separate production environment exists yet. DEV *is* the live environment.\n"
            "SENIOR_PM VERDICT: ANSWER"
        )

    with patch('orchestrator.recon.run_officer', side_effect=fake_run_officer):
        audit = await senior_pm.triage_async(cfg, ticket, repo_context="config.yaml excerpt")

    chk("config answer returns ANSWER verdict", audit.verdict == "ANSWER")
    chk("config answer includes config citations", len(audit.citations) == 2)
    chk("config answer explains DEV is live", "DEV *is* the live environment" in audit.answer)

asyncio.run(test_config_answer())

# ---- (b) Closing duplicate with citation (mock Jira) ----
print("\n==== Testing Duplicate Detection & Closing ====")

async def test_duplicate_close():
    """Test Senior PM can detect and close duplicates with proper citations."""
    ticket = Ticket(
        id="AUTO-102",
        key="AUTO-102",
        summary="Add billing parent route",
        description="Add a parent route for billing",
        app="automatixy"
    )

    async def fake_run_officer(**kw):
        return (
            "CITATION: AUTO-42 - identical billing parent route request\n"
            "This is a duplicate of AUTO-42.\n"
            "SENIOR_PM VERDICT: CLOSE"
        )

    with patch('orchestrator.recon.run_officer', side_effect=fake_run_officer):
        audit = await senior_pm.triage_async(cfg, ticket)

    chk("duplicate detection returns CLOSE verdict", audit.verdict == "CLOSE")
    chk("duplicate close includes citation to original", len(audit.citations) == 1)
    chk("duplicate close cites original ticket ID", "AUTO-42" in audit.citations[0]["source"])
    chk("duplicate close reason explains duplicate", "duplicate" in audit.close_reason.lower())

asyncio.run(test_duplicate_close())

async def test_invalid_close():
    """Test Senior PM can close invalid/out-of-scope tickets."""
    ticket = Ticket(
        id="AUTO-103",
        key="AUTO-103",
        summary="Remove all authentication",
        description="Remove authentication from the app",
        app="automatixy"
    )

    async def fake_run_officer(**kw):
        return (
            "CITATION: ARCHITECTURE.md §1.2 - authentication is mandatory for security\n"
            "Out of scope: authentication is a required security feature.\n"
            "SENIOR_PM VERDICT: CLOSE"
        )

    with patch('orchestrator.recon.run_officer', side_effect=fake_run_officer):
        audit = await senior_pm.triage_async(cfg, ticket)

    chk("invalid ticket returns CLOSE verdict", audit.verdict == "CLOSE")
    chk("invalid close includes architectural citation", len(audit.citations) == 1)
    chk("invalid close explains why", "Out of scope" in audit.close_reason or "security" in audit.close_reason.lower())

asyncio.run(test_invalid_close())

# ---- (c) Re-filing mis-projected ticket (EU↔AUTO) ----
print("\n==== Testing Cross-Project Re-filing ====")

async def test_eu_to_auto_refile():
    """Test re-filing a ticket misrouted to EU that belongs in AUTO."""
    ticket = Ticket(
        id="EU-50",
        key="EU-50",
        summary="Fix billing performance",
        description="Billing queries are slow",
        app="elite-unit"
    )

    async def fake_run_officer(**kw):
        return (
            "CITATION: Unit Memory - Elite Unit is the orchestrator, not a SaaS product\n"
            "CITATION: product classification - billing belongs to automatixy\n"
            "This ticket is misrouted: billing is an automatixy feature, not the orchestrator.\n"
            "```json\n"
            "[\n"
            '  {"title": "Fix billing performance", "type": "Bug", "body": "Optimize billing queries in the automatixy app", "project": "AUTO", "reason": "Misrouted: billing belongs to automatixy, not the orchestrator"}\n'
            "]\n"
            "```\n"
            "SENIOR_PM VERDICT: REFILE"
        )

    with patch('orchestrator.recon.run_officer', side_effect=fake_run_officer):
        audit = await senior_pm.triage_async(cfg, ticket)

    chk("misrouted EU→AUTO returns REFILE verdict", audit.verdict == "REFILE")
    chk("refile includes project re-routing citations", len(audit.citations) >= 1)
    chk("refile creates new ticket for AUTO", len(audit.refile_targets) == 1)
    chk("refiled ticket targets AUTO project", audit.refile_targets[0].get("project") == "AUTO")
    chk("refiled ticket has clear title", "billing" in audit.refile_targets[0].get("title", "").lower())

asyncio.run(test_eu_to_auto_refile())

async def test_auto_to_eu_refile():
    """Test re-filing a ticket misrouted to AUTO that belongs in EU."""
    ticket = Ticket(
        id="AUTO-200",
        key="AUTO-200",
        summary="Fix autopilot memory leak",
        description="The autopilot loop has a memory leak",
        app="automatixy"
    )

    async def fake_run_officer(**kw):
        return (
            "CITATION: product boundaries - autopilot is the orchestrator itself\n"
            "This ticket is about the Elite Unit's own tooling, not the automatixy product.\n"
            "```json\n"
            "[\n"
            '  {"title": "Fix autopilot memory leak", "type": "Bug", "body": "The autopilot loop in orchestrator/autopilot.py has a memory leak", "project": "EU", "reason": "Misrouted: autopilot is orchestrator tooling, not a product feature"}\n'
            "]\n"
            "```\n"
            "SENIOR_PM VERDICT: REFILE"
        )

    with patch('orchestrator.recon.run_officer', side_effect=fake_run_officer):
        audit = await senior_pm.triage_async(cfg, ticket)

    chk("misrouted AUTO→EU returns REFILE verdict", audit.verdict == "REFILE")
    chk("refile creates new ticket for EU", len(audit.refile_targets) == 1)
    chk("refiled ticket targets EU project", audit.refile_targets[0].get("project") == "EU")
    chk("refiled ticket explains tooling context", "orchestrator" in audit.refile_targets[0].get("body", "").lower())

asyncio.run(test_auto_to_eu_refile())

async def test_vague_ticket_refile():
    """Test re-filing a vague ticket that needs specific reformulation."""
    ticket = Ticket(
        id="AUTO-150",
        key="AUTO-150",
        summary="Improve performance",
        description="Make the app faster",
        app="automatixy"
    )

    async def fake_run_officer(**kw):
        return (
            "CITATION: Unit Memory Lesson - vague requirements need specific acceptance criteria\n"
            "This ticket is too vague to implement. Needs specific performance targets and scope.\n"
            "```json\n"
            "[\n"
            '  {"title": "Optimize billing query performance", "type": "Story", "body": "Reduce billing endpoint response time from 2s to 200ms by adding database indexes", "project": "AUTO", "reason": "Vague original needs specific performance target and implementation approach"}\n'
            "]\n"
            "```\n"
            "SENIOR_PM VERDICT: REFILE"
        )

    with patch('orchestrator.recon.run_officer', side_effect=fake_run_officer):
        audit = await senior_pm.triage_async(cfg, ticket)

    chk("vague ticket returns REFILE verdict", audit.verdict == "REFILE")
    chk("refile creates specific ticket", len(audit.refile_targets) == 1)
    chk("refiled ticket has performance target", "200ms" in audit.refile_targets[0].get("body", ""))
    chk("refiled ticket has implementation approach", "database indexes" in audit.refile_targets[0].get("body", "").lower())

asyncio.run(test_vague_ticket_refile())

# ---- (d) Locked ticket skip ----
print("\n==== Testing Locked Ticket Handling ====")

async def test_locked_ticket_skip():
    """Test that tickets with locked worktrees are skipped."""
    from orchestrator import loop, gate
    from unittest.mock import patch
    import fcntl

    ticket = Ticket(
        id="AUTO-104",
        key="AUTO-104",
        summary="Implement new feature",
        description="Add a new feature",
        app="automatixy"
    )

    # Create a locked worktree scenario
    locked_path = tempfile.mkdtemp()
    lock_file = os.path.join(locked_path + ".lock")

    # Create lock file
    with open(lock_file, "w") as f:
        f.write("1234\n")

    # Lock it
    lock_fd = os.open(lock_file, os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)

    try:
        # Mock _worktree_path to return our locked path
        with patch('orchestrator.loop._worktree_path', return_value=locked_path):
            # Mock backlog functions
            mock_backlog = MagicMock()
            mock_backlog.add_comment = MagicMock()
            mock_backlog.set_status = MagicMock()

            with patch('orchestrator.backlog.base.make_backlog', return_value=mock_backlog):
                worklist = [(cfg.apps[0], ticket)]
                audit = AuditLog(cfg.audit_path)

                # The prebuild_gate should skip locked tickets
                filtered = await gate.prebuild_gate(cfg, worklist, audit)

                chk("locked ticket is filtered from worklist", len(filtered) == 0)
                chk("locked ticket doesn't trigger backlog operations", mock_backlog.set_status.call_count == 0)

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

asyncio.run(test_locked_ticket_skip())

# ---- (e) Each audit entry format ----
print("\n==== Testing Audit Entry Formats ====")

def test_answer_audit_format():
    """Test audit entry format for ANSWER verdict."""
    parsed = {
        "verdict": "ANSWER",
        "body": "Use the tenant-isolation.md rules.",
        "raw": "CITATION: CLAUDE.md - tenant isolation\nAnswer: Use the tenant-isolation.md rules.\nSENIOR_PM VERDICT: ANSWER",
        "citations": [{"source": "CLAUDE.md", "claim": "tenant isolation"}],
        "tickets": []
    }

    audit = senior_pm.audit_from_parse(parsed, "AUTO-105")
    audit_dict = audit.to_dict()

    chk("ANSWER audit has verdict field", audit_dict.get("verdict") == "ANSWER")
    chk("ANSWER audit has ticket_id field", audit_dict.get("ticket_id") == "AUTO-105")
    chk("ANSWER audit has citations field", isinstance(audit_dict.get("citations"), list))
    chk("ANSWER audit has answer field", "tenant-isolation.md" in audit_dict.get("answer", ""))
    chk("ANSWER audit has raw field", "SENIOR_PM VERDICT: ANSWER" in audit_dict.get("raw", ""))
    chk("ANSWER audit citations have source+claim", all("source" in c and "claim" in c for c in audit_dict.get("citations", [])))

test_answer_audit_format()

def test_close_audit_format():
    """Test audit entry format for CLOSE verdict."""
    parsed = {
        "verdict": "CLOSE",
        "body": "Duplicate of AUTO-42",
        "raw": "Duplicate of AUTO-42\nSENIOR_PM VERDICT: CLOSE",
        "citations": [{"source": "AUTO-42", "claim": "identical request"}],
        "tickets": []
    }

    audit = senior_pm.audit_from_parse(parsed, "AUTO-106")
    audit_dict = audit.to_dict()

    chk("CLOSE audit has verdict field", audit_dict.get("verdict") == "CLOSE")
    chk("CLOSE audit has ticket_id field", audit_dict.get("ticket_id") == "AUTO-106")
    chk("CLOSE audit has citations field", isinstance(audit_dict.get("citations"), list))
    chk("CLOSE audit has close_reason field", "Duplicate" in audit_dict.get("close_reason", ""))
    chk("CLOSE audit has raw field", "SENIOR_PM VERDICT: CLOSE" in audit_dict.get("raw", ""))

test_close_audit_format()

def test_refile_audit_format():
    """Test audit entry format for REFILE verdict."""
    parsed = {
        "verdict": "REFILE",
        "body": "Needs reformulation",
        "raw": "```json\n[{\"title\":\"New ticket\"}]\n```\nSENIOR_PM VERDICT: REFILE",
        "citations": [{"source": "Unit Memory", "claim": "vague requirements need AC"}],
        "tickets": [{"title": "New ticket", "type": "Story", "body": "Specific AC", "project": "AUTO", "reason": "Reformulate"}]
    }

    audit = senior_pm.audit_from_parse(parsed, "AUTO-107")
    audit_dict = audit.to_dict()

    chk("REFILE audit has verdict field", audit_dict.get("verdict") == "REFILE")
    chk("REFILE audit has ticket_id field", audit_dict.get("ticket_id") == "AUTO-107")
    chk("REFILE audit has citations field", isinstance(audit_dict.get("citations"), list))
    chk("REFILE audit has refile_targets field", isinstance(audit_dict.get("refile_targets"), list))
    chk("REFILE audit has raw field", "SENIOR_PM VERDICT: REFILE" in audit_dict.get("raw", ""))
    chk("REFILE audit refile_targets have required fields", all(
        "title" in t and "type" in t and "body" in t and "project" in t
        for t in audit_dict.get("refile_targets", [])
    ))

test_refile_audit_format()

def test_citation_extraction_format():
    """Test that citations are properly extracted and formatted."""
    text = (
        "CITATION: ARCHITECTURE.md §2.3 - tenant isolation is enforced at query level\n"
        "CITATION: ORG.md - the CTO approves all pricing changes\n"
        "CITATION: config.yaml - DEV is intentionally live\n"
        "Answer: Use the parent route.\n"
        "SENIOR_PM VERDICT: ANSWER"
    )

    parsed = senior_pm.parse_verdict(text)
    citations = parsed.get("citations", [])

    chk("citation extraction finds all citations", len(citations) == 3)
    chk("citation format includes source", all("source" in c for c in citations))
    chk("citation format includes claim", all("claim" in c for c in citations))
    chk("citation sources are preserved", any("ARCHITECTURE.md" in c["source"] for c in citations))
    chk("citation claims are preserved", any("tenant isolation" in c["claim"] for c in citations))

test_citation_extraction_format()

# ---- Test integration with autopilot ----
print("\n==== Testing Autopilot Integration ====")

async def test_prebuild_gate_calls_senior_pm():
    """Test that prebuild_gate properly calls Senior PM triage."""
    from orchestrator import gate

    ticket = Ticket(
        id="AUTO-108",
        key="AUTO-108",
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

            chk("prebuild_gate calls Senior PM triage", len(triage_calls) == 1)
            chk("prebuild_gate calls triage with correct ticket", triage_calls[0] == "AUTO-108")

asyncio.run(test_prebuild_gate_calls_senior_pm())

# ---- Test parse_verdict edge cases ----
print("\n==== Testing Parse Verdict Edge Cases ====")

def test_multiple_verdict_lines():
    """Test that only the last verdict line is respected."""
    text = (
        "Some text\n"
        "SENIOR_PM VERDICT: ANSWER\n"
        "More text\n"
        "SENIOR_PM VERDICT: CLOSE\n"
    )

    parsed = senior_pm.parse_verdict(text)
    chk("last verdict line is respected", parsed["verdict"] == "CLOSE")

test_multiple_verdict_lines()

def test_case_insensitive_verdict():
    """Test that verdict parsing is case-insensitive."""
    text = "Answer: yes\nsenior_pm verdict: answer"
    parsed = senior_pm.parse_verdict(text)
    chk("verdict parsing is case-insensitive", parsed["verdict"] == "ANSWER")

test_case_insensitive_verdict()

def test_malformed_json_refile():
    """Test that malformed JSON in REFILE is handled gracefully."""
    text = (
        "Needs refile\n"
        "```json\n"
        "{invalid json}\n"
        "```\n"
        "SENIOR_PM VERDICT: REFILE"
    )

    parsed = senior_pm.parse_verdict(text)
    chk("malformed JSON doesn't crash parsing", parsed["verdict"] == "REFILE")
    chk("malformed JSON results in empty tickets list", len(parsed.get("tickets", [])) == 0)

test_malformed_json_refile()

def test_missing_citation_warning():
    """Test that missing citations on ANSWER/REFILE are handled."""
    # This should log a warning but not flip the verdict
    text = "Answer: use defaults\nSENIOR_PM VERDICT: ANSWER"
    parsed = senior_pm.parse_verdict(text)
    chk("missing citation doesn't flip verdict", parsed["verdict"] == "ANSWER")
    chk("missing citation results in empty citations list", len(parsed.get("citations", [])) == 0)

test_missing_citation_warning()

def test_no_verdict_line():
    """Test that unclear reply defaults to REFILE (fail-safe)."""
    text = "Hmm, I'm not sure what to do here."
    parsed = senior_pm.parse_verdict(text)
    chk("unclear reply defaults to REFILE", parsed["verdict"] == "REFILE")

test_no_verdict_line()

# ---- Print results ----
print("\n============ SENIOR PM COMPREHENSIVE TESTS ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
