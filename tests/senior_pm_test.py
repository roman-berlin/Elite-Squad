"""Senior PM officer: answer/close/refile parse + citations + audit + recon squad."""
import sys, types, asyncio
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

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- parse_verdict (pure) ---
chk("ANSWER parsed", senior_pm.parse_verdict("Use the tenant-isolation.md rules.\nCITATION: CLAUDE.md - tenant isolation is enforced at query level\nSENIOR_PM VERDICT: ANSWER")["verdict"] == "ANSWER")
# ANSWER with citation preserved
_ans_text = ("CITATION: ARCHITECTURE.md §2.3 - billing is grouped under parent account\n"
             "Answer: use the parent route.\nSENIOR_PM VERDICT: ANSWER")
_ans = senior_pm.parse_verdict(_ans_text)
chk("ANSWER captures citation", len(_ans.get("citations", [])) == 1, str(_ans.get("citations")))
chk("ANSWER citation source", _ans["citations"][0]["source"] == "ARCHITECTURE.md §2.3", str(_ans["citations"]))
# CLOSE verdict
_close_text = "Duplicate of AUTO-42; same ask.\nSENIOR_PM VERDICT: CLOSE"
_close = senior_pm.parse_verdict(_close_text)
chk("CLOSE parsed", _close["verdict"] == "CLOSE")
chk("CLOSE body captured", "Duplicate" in _close.get("body", ""), str(_close.get("body")))
# REFILE with JSON block
_refile_text = ('''Ticket is vague; needs split.
```json
[
  {"title": "Fix billing performance", "type": "Bug", "body": "Optimize billing queries", "project": "AUTO", "reason": "Performance issue"}
]
```
SENIOR_PM VERDICT: REFILE''')
_refile = senior_pm.parse_verdict(_refile_text)
chk("REFILE parsed", _refile["verdict"] == "REFILE")
chk("REFILE extracts tickets", len(_refile.get("tickets", [])) == 1, str(_refile.get("tickets")))
chk("REFILE ticket title", _refile["tickets"][0]["title"] == "Fix billing performance", str(_refile["tickets"]))
# Unclear reply → CONTINUE fail-safe (EU-134: conservative bias to build)
chk("unclear -> CONTINUE (fail-safe: build)", senior_pm.parse_verdict("hmm, not sure")["verdict"] == "CONTINUE")
# Body excludes verdict line
d = senior_pm.parse_verdict("Answer: use the Observe pattern.\nSENIOR_PM VERDICT: ANSWER")
chk("body excludes the verdict line", "SENIOR_PM VERDICT" not in d["body"] and "Observe" in d["body"])

# --- Citation extraction ---
# Multiple citations
_multi = ("CITATION: CLAUDE.md - tenant isolation is enforced\n"
          "CITATION: ORG.md - the CTO approves pricing\n"
          "Answer: ask the CTO.\nSENIOR_PM VERDICT: ANSWER")
_multi_parsed = senior_pm.parse_verdict(_multi)
chk("multiple citations captured", len(_multi_parsed.get("citations", [])) == 2, str(_multi_parsed.get("citations")))
# Missing citation on ANSWER logged (but doesn't flip verdict) — we can't easily test the log,
# but we verify the verdict stays ANSWER
_no_cit = "Answer: use defaults.\nSENIOR_PM VERDICT: ANSWER"
_no_cit_parsed = senior_pm.parse_verdict(_no_cit)
chk("missing citation doesn't flip verdict", _no_cit_parsed["verdict"] == "ANSWER")

# --- audit_from_parse ---
audit = senior_pm.audit_from_parse(_ans, "AUTO-100")
chk("audit_from_parse builds ANSWER audit", audit.verdict == "ANSWER" and audit.ticket_id == "AUTO-100")
chk("audit preserves answer text", "Answer: use the parent route." in audit.answer, f"got: {audit.answer}")
chk("audit preserves citations", len(audit.citations) == 1)
# CLOSE audit
_close_parsed = senior_pm.parse_verdict(_close_text)
close_audit = senior_pm.audit_from_parse(_close_parsed, "AUTO-101")
chk("audit_from_parse builds CLOSE audit", close_audit.verdict == "CLOSE")
chk("audit preserves close reason", "Duplicate" in close_audit.close_reason)
# REFILE audit
_refile_parsed = senior_pm.parse_verdict(_refile_text)
refile_audit = senior_pm.audit_from_parse(_refile_parsed, "AUTO-102")
chk("audit_from_parse builds REFILE audit", refile_audit.verdict == "REFILE")
chk("audit preserves refile targets", len(refile_audit.refile_targets) == 1)

cfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path="/tmp/x.jsonl", use_worktree=False)

# --- triage_async delegates through recon.run_officer (so the Senior PM can field a squad) ---
captured = {}
async def fake_run_officer(**kw):
    captured.update(kw)
    return ("CITATION: ARCHITECTURE.md - use the parent route\nAnswer: group under parent.\nSENIOR_PM VERDICT: ANSWER")
recon.run_officer = fake_run_officer
ticket = Ticket(id="AUTO-103", key="AUTO-103", summary="How to structure billing?",
                description="Should we have one route or four?", app="automatixy")
audit = asyncio.run(senior_pm.triage_async(cfg, ticket))
chk("triage_async parses ANSWER", audit.verdict == "ANSWER" and "parent" in audit.answer)
chk("Senior PM goes through recon squad path", captured.get("officer") == "senior_pm")
chk("Senior PM label is correct", captured.get("label") == "Senior PM")
chk("Senior PM soldiers are read-only", captured.get("soldier_tools") == ["Read", "Grep", "Glob"])

# --- CLOSE path ---
async def fake_close(**kw):
    return "Duplicate of AUTO-42.\nSENIOR_PM VERDICT: CLOSE"
recon.run_officer = fake_close
audit2 = asyncio.run(senior_pm.triage_async(cfg, ticket))
chk("triage_async parses CLOSE", audit2.verdict == "CLOSE" and "Duplicate" in audit2.close_reason)

# --- REFILE path ---
async def fake_refile(**kw):
    return ('''Ticket needs split.
```json
[
  {"title": "Billing parent route", "type": "Story", "body": "Add parent billing route", "project": "AUTO", "reason": "Split into focused piece"}
]
```
SENIOR_PM VERDICT: REFILE''')
recon.run_officer = fake_refile
audit3 = asyncio.run(senior_pm.triage_async(cfg, ticket))
chk("triage_async parses REFILE", audit3.verdict == "REFILE")
chk("triage_async captures refile targets", len(audit3.refile_targets) == 1)

# --- Convenience wrappers ---
# answer_ticket
qa_ticket = Ticket(id="AUTO-104", key="AUTO-104", summary="Where is config?",
                    description="What's the path for tenant isolation rules?", app="automatixy")
async def fake_qa(**kw):
    return "CITATION: CLAUDE.md - tenant isolation is enforced\nAnswer: see .claude/rules/tenant-isolation.md\nSENIOR_PM VERDICT: ANSWER"
recon.run_officer = fake_qa
ans = asyncio.run(senior_pm.answer_ticket(cfg, qa_ticket, "Where are the tenant rules?"))
chk("answer_ticket returns answer text", "tenant-isolation.md" in ans)

# close_ticket
async def fake_close_wrap(**kw):
    return "Feature deprecated.\nSENIOR_PM VERDICT: CLOSE"
recon.run_officer = fake_close_wrap
success, reason = asyncio.run(senior_pm.close_ticket(cfg, ticket))
chk("close_ticket returns success=True", success is True)
chk("close_ticket returns reason", "deprecated" in reason)

# refile_ticket
async def fake_refile_wrap(**kw):
    return ('''Vague; needs reformulation.
```json
[{"title": "Clear ticket", "type": "Task", "body": "Specific AC", "project": "AUTO", "reason": "Reformulate"}]
```
SENIOR_PM VERDICT: REFILE''')
recon.run_officer = fake_refile_wrap
success, tickets = asyncio.run(senior_pm.refile_ticket(cfg, ticket))
chk("refile_ticket returns success=True", success is True)
chk("refile_ticket returns tickets", len(tickets) == 1 and tickets[0]["title"] == "Clear ticket")

print("\n================ SENIOR PM OFFICER QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
