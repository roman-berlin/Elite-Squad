"""EU-42: loop._route_out_of_scope sends real-but-off-spec findings to the backlog instead of losing them.

The Builder's eu42_filing_test covers filing.parse_tickets/file_findings in isolation. THIS harness
exercises the new wiring in loop.py — the decision the ticket actually adds:

  * PROPOSE-FIRST (default, out_of_scope_autofile=False): surface findings in the cockpit 'Needs you'
    (decisions.add) — never auto-file, never silently drop. This is the regression the ticket fixes:
    an off-spec finding a Reviewer notices must NOT evaporate.
  * AUTO-FILE (out_of_scope_autofile=True, live): route through filing.file_findings so each finding
    lands as a de-duped backlog ticket carrying the 'out-of-scope' label.
  * AUTO-FILE + dry-run: file nothing (no backlog writes).
  * No findings: do nothing at all.
  * Routing must NEVER raise — a broken filing path must not sink the build run.

All Jira/Telegram/decision-store side effects are stubbed; no network, no files written outside tmp.
"""
import sys, types, tempfile
from pathlib import Path

# Stub the Agent SDK so importing the orchestrator package never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import loop, filing
from orchestrator import pm as pm_mod
from orchestrator import reviewer as reviewer_mod
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# --- 0) PRODUCER SIDE: the Reviewer and PM-triage system prompts must actually carry the out-of-scope
#        findings channel. Without it neither review.raw nor the PM's reply ever contains a
#        ===TICKETS=== block, so the entire routing path below is a dead no-op. Guard it so the
#        producer side can't silently regress (the consumer-side checks pass even if it's missing). -- #
chk("Reviewer system prompt carries the filing channel (TICKET_BLOCK_RULE)",
    filing.TICKET_BLOCK_RULE in reviewer_mod.REVIEWER_SYSTEM)
chk("Reviewer system prompt names the ===TICKETS=== marker for the Reviewer to emit",
    "===TICKETS===" in reviewer_mod.REVIEWER_SYSTEM)
chk("PM-triage system prompt carries the filing channel (TICKET_BLOCK_RULE)",
    filing.TICKET_BLOCK_RULE in pm_mod.PM_TRIAGE_SYSTEM)
chk("PM-triage system prompt names the ===TICKETS=== marker for the PM to emit",
    "===TICKETS===" in pm_mod.PM_TRIAGE_SYSTEM)

tmp = Path(tempfile.mkdtemp())
app = AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev",
                protected_branch="main", backlog_backend="none")
tkt = Ticket(id="EU-42", key="EU-42", summary="route out-of-scope findings", description="")

REPORT = (
    "Reviewer verdict: PASS for the in-scope change.\n"
    "Spotted two real but off-spec issues while reviewing.\n"
    "===TICKETS===\n"
    '[{"title": "Fix unguarded null deref in parser", "type": "Bug", "severity": "HIGH",'
    ' "body": "orchestrator/x.py crashes on empty input"},'
    ' {"title": "Stale config key still referenced", "type": "Task", "severity": "LOW",'
    ' "body": "remove dead flag"}]\n'
    "===END===\n"
)


class StubBacklog:
    """In-memory stand-in for the Jira filing contract used by filing.file_findings."""
    def __init__(self, open_summaries=None):
        self._open = {s.strip().lower(): k for s, k in (open_summaries or {}).items()}
        self.created = []          # (summary, labels, issue_type)
    def find_open_by_summary(self, summary):
        return self._open.get(str(summary).strip().lower())
    def create_task(self, summary, description, labels=None, issue_type="Task"):
        key = f"EU-{900 + len(self.created)}"
        self.created.append((summary, list(labels or []), issue_type))
        return key


# --- capture side effects: decisions.add (cockpit 'Needs you'), audit.record, _notify ----------- #
class CaptureAudit:
    def __init__(self): self.records = []
    def record(self, event, **kw): self.records.append((event, kw))

decision_calls = []
loop.decisions.add = (lambda cfg, ticket, app_name, question, entry_id=None, **_kw:
                      decision_calls.append((ticket.id, question, entry_id)))
notify_calls = []
loop._notify = lambda cfg, text: notify_calls.append(text)


def run(report, autofile, dry_run, open_summaries=None):
    """Drive _route_out_of_scope once with a fresh backlog stub + audit, returning (stub, audit)."""
    decision_calls.clear(); notify_calls.clear()
    stub = StubBacklog(open_summaries)
    filing.make_backlog = lambda a: stub
    cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), dry_run=dry_run)
    cfg.out_of_scope_autofile = autofile
    audit = CaptureAudit()
    loop._route_out_of_scope(cfg, tkt, app, audit, report, source="reviewer")
    return stub, audit


# --- 1) PROPOSE-FIRST (default): findings surface in 'Needs you', nothing is auto-filed ---------- #
stub, audit = run(REPORT, autofile=False, dry_run=True)
chk("propose-first surfaces a decision in 'Needs you'", len(decision_calls) == 1, str(decision_calls))
chk("the proposal names the off-spec finding(s)",
    decision_calls and "null deref" in decision_calls[0][1], str(decision_calls))
chk("propose-first uses a distinct out-of-scope decision id (won't clobber a needs_human decision)",
    decision_calls and decision_calls[0][2] == "EU-42#out-of-scope", str(decision_calls))
chk("propose-first files NOTHING into the backlog", stub.created == [], str(stub.created))
chk("propose-first is audited as 'out_of_scope_proposed'",
    any(e == "out_of_scope_proposed" for e, _ in audit.records), str(audit.records))

# --- 2) AUTO-FILE (live): each finding lands as a de-duped 'out-of-scope'-labeled backlog ticket -- #
stub, audit = run(REPORT, autofile=True, dry_run=False)
chk("auto-file creates one ticket per finding", len(stub.created) == 2, str(stub.created))
chk("auto-file does NOT also propose (no double-handling)", decision_calls == [], str(decision_calls))
chk("every auto-filed ticket carries the 'out-of-scope' label",
    all("out-of-scope" in labels for _, labels, _ in stub.created), str(stub.created))
chk("auto-file is audited as 'out_of_scope_filed' with the filed keys",
    any(e == "out_of_scope_filed" and kw.get("filed") for e, kw in audit.records), str(audit.records))

# --- 3) AUTO-FILE de-dup: a matching OPEN ticket is not re-created across passes ------------------ #
stub, audit = run(REPORT, autofile=True, dry_run=False,
                  open_summaries={"Fix unguarded null deref in parser": "EU-500"})
chk("de-dup files only the genuinely-new finding", len(stub.created) == 1, str(stub.created))
chk("the already-open finding is NOT re-created",
    all("null deref" not in s for s, *_ in stub.created), str(stub.created))

# --- 4) AUTO-FILE + dry-run: route is exercised but NO backlog write happens --------------------- #
stub, audit = run(REPORT, autofile=True, dry_run=True)
chk("auto-file dry-run writes nothing to the backlog", stub.created == [], str(stub.created))
chk("auto-file dry-run does not propose either", decision_calls == [], str(decision_calls))

# --- 5) no findings -> the routing is a no-op (no decision, no file, no audit) -------------------- #
stub, audit = run("Plain report, no ===TICKETS=== block here.", autofile=False, dry_run=True)
chk("no findings -> no decision proposed", decision_calls == [], str(decision_calls))
chk("no findings -> nothing filed", stub.created == [], str(stub.created))
chk("no findings -> nothing audited", audit.records == [], str(audit.records))

# --- 6) routing must NEVER raise, even if filing blows up (best-effort, must not sink the run) ---- #
_orig_ff = filing.file_findings
filing.file_findings = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("backlog exploded"))
raised = False
try:
    cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), dry_run=False)
    cfg.out_of_scope_autofile = True
    loop._route_out_of_scope(cfg, tkt, app, CaptureAudit(), REPORT, source="reviewer")
except Exception:
    raised = True
finally:
    filing.file_findings = _orig_ff
chk("a broken filing path is swallowed (routing never raises)", not raised)

passed = sum(1 for _, c, _ in results if c)
for n, c, d in results:
    print(f"  {'✓' if c else '✗'} {n}" + (f"  [{d}]" if (not c and d) else ""))
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
