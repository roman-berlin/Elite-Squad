"""EU-83: '#out-of-scope' decision ids must never reach Jira REST URLs.

Regression tests for the bug where decisions.to_worklist passed 'EU-81#out-of-scope'
straight through as the Jira issue key, causing /rest/api/3/issue/EU-81#out-of-scope/comment
→ 405 and silently dropping the filing.

Covers:
 1. JiraAdapter._url raises ValueError when '#' is in the path (the hard REST guard).
 2. decisions.add stores the 'extra' payload in the entry (allows out_of_scope_report).
 3. decisions.to_worklist strips the '#…' suffix from the ticket id/key (defence-in-depth).
 4. handle_reply intercepts '#out-of-scope' entries and calls _file_out_of_scope_resume
    instead of queuing a rebuild (EU-42 intent: file findings, not re-build the original).
 5. _file_out_of_scope_resume calls file_findings with the stored report (happy path).
 6. _file_out_of_scope_resume falls back gracefully when no report is stored (pre-EU-83 entry).
 7. _file_out_of_scope_resume is a no-op in dry-run mode (nothing filed).
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

import orchestrator.loop as loop
from orchestrator import decisions, filing
from orchestrator.backlog.jira import JiraAdapter
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
app = AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev",
                protected_branch="main", backlog_backend="none")
cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"))

# Silence Telegram pings
decisions.notify = types.SimpleNamespace(send=lambda *a, **k: None)


# --- 1) JiraAdapter._url guard: '#' in a REST path raises ValueError immediately ----------- #
# Use __new__ to build a bare adapter — only base_url is needed for _url().
adapter = object.__new__(JiraAdapter)
adapter.base_url = "https://example.atlassian.net"

url_ok = adapter._url("issue/EU-81/comment")
chk("_url with a clean key returns a valid URL", "EU-81" in url_ok, url_ok)

_url_raised = False
_url_exc_msg = ""
try:
    adapter._url("issue/EU-81#out-of-scope/comment")
except ValueError as exc:
    _url_raised = True
    _url_exc_msg = str(exc)
chk("_url raises ValueError when '#' is in the path", _url_raised, _url_exc_msg)
chk("ValueError message names the EU-83 regression context",
    "EU-83" in _url_exc_msg or "decision-entry" in _url_exc_msg, _url_exc_msg)


# --- 2) decisions.add stores 'extra' payload in the entry ---------------------------------- #
tkt = Ticket(id="EU-81", key="EU-81", summary="test ticket", description="")
decisions.add(cfg, tkt, app.name, "Out-of-scope findings?",
              entry_id="EU-81#out-of-scope",
              extra={"out_of_scope_report": "===TICKETS===\n[]\n===END==="})
items = decisions.load(cfg)
oos = [i for i in items if i.get("id") == "EU-81#out-of-scope"]
chk("extra payload is stored in the decision entry", len(oos) == 1, str(items))
chk("out_of_scope_report is preserved verbatim",
    oos and "===TICKETS===" in oos[0].get("out_of_scope_report", ""), str(oos))

# Clean up so we start fresh for the next tests
(tmp / "pending_decisions.json").unlink(missing_ok=True)


# --- 2b) loop._route_out_of_scope (non-pm source, autofile OFF) attaches the report -------- #
# Regression (EU-92): case 2 proves decisions.add STORES `extra`; this proves the PRODUCER —
# loop._route_out_of_scope's propose-first branch — actually PASSES it. An EU-92 refactor dropped
# the payload, so the Commander could approve an out-of-scope proposal and _file_out_of_scope_resume
# would find nothing to file. Pin the loop call-site so that path can't silently regress again.
ROUTE_REPORT = (
    "Reviewer prose.\n"
    "===TICKETS===\n"
    '[{"title": "Tidy a stale flag", "type": "Task", "severity": "LOW", "body": "remove dead flag"}]\n'
    "===END===\n"
)
route_tkt = Ticket(id="EU-84", key="EU-84", summary="route producer test", description="")
route_cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"))
route_cfg.out_of_scope_autofile = False   # knob OFF → propose-first branch (not auto-file)
# source="reviewer" (NON-pm): pm-findings would force auto-file and skip propose-first entirely.
loop._route_out_of_scope(route_cfg, route_tkt, app, None, ROUTE_REPORT, source="reviewer")

route_items = decisions.load(route_cfg)
route_oos = [i for i in route_items if i.get("id") == "EU-84#out-of-scope"]
chk("_route_out_of_scope(non-pm, autofile off) records an out-of-scope decision",
    len(route_oos) == 1, str(route_items))
chk("the routed entry carries out_of_scope_report (EU-83 resume payload survives the EU-92 route)",
    bool(route_oos) and route_oos[0].get("out_of_scope_report") == ROUTE_REPORT, str(route_oos))

# Clean up so the handle_reply tests start fresh
(tmp / "pending_decisions.json").unlink(missing_ok=True)


# --- 3) to_worklist strips '#' suffix from ticket id/key ----------------------------------- #
resolved_with_hash = {
    "id": "EU-81#out-of-scope", "app": app.name, "answer": "yes file them",
    "summary": "EU-81 summary", "description": "EU-81 desc",
    "acceptance": [], "ephemeral": False,
}
wl = decisions.to_worklist(cfg, resolved_with_hash)
chk("to_worklist returns a non-empty worklist even for #-suffixed entries", len(wl) == 1, str(wl))
_, rebuilt_tkt = wl[0]
chk("to_worklist strips '#out-of-scope' from the ticket id",
    rebuilt_tkt.id == "EU-81", repr(rebuilt_tkt.id))
chk("to_worklist strips '#out-of-scope' from the ticket key",
    rebuilt_tkt.key == "EU-81", repr(rebuilt_tkt.key))
chk("to_worklist without '#' suffix leaves the id unchanged",
    decisions.to_worklist(cfg, {**resolved_with_hash, "id": "EU-81"})[0][1].id == "EU-81")


# --- 4 & 5) handle_reply with '#out-of-scope' calls _file_out_of_scope_resume, not rebuild -- #
REPORT_WITH_FINDINGS = (
    "Reviewer verdict: PASS for the in-scope change.\n"
    "===TICKETS===\n"
    '[{"title": "Fix null deref in loader", "type": "Bug", "severity": "HIGH",'
    ' "body": "orchestrator/x.py line 42"}]\n'
    "===END===\n"
)

filed_calls = []

class StubBacklog:
    def find_open_by_summary(self, summary): return None
    def create_task(self, summary, description, labels=None, issue_type="Task"):
        key = f"EU-{900 + len(filed_calls)}"
        filed_calls.append((summary, list(labels or [])))
        return key

# Patch make_backlog and notify for this section
orig_make_backlog = filing.make_backlog
filing.make_backlog = lambda a: StubBacklog()

run_bg_calls = []
orig_run_bg = decisions._run_bg
decisions._run_bg = lambda *a, **k: run_bg_calls.append(a) or True

notify_msgs = []
decisions.notify = types.SimpleNamespace(send=lambda msg: notify_msgs.append(msg))

# Seed a '#out-of-scope' decision entry with stored report
tkt2 = Ticket(id="EU-81", key="EU-81", summary="EU-81 ticket", description="some task")
decisions.add(cfg, tkt2, app.name, "Out-of-scope findings?",
              entry_id="EU-81#out-of-scope",
              extra={"out_of_scope_report": REPORT_WITH_FINDINGS})

class CaptureAudit:
    def __init__(self): self.records = []
    def record(self, event, **kw): self.records.append((event, kw))

audit = CaptureAudit()
handled = decisions.handle_reply(cfg, audit, "EU-81#out-of-scope: yes file them")
chk("handle_reply returns True for '#out-of-scope' entry", handled)
chk("handle_reply does NOT queue a rebuild (_run_bg not called)",
    len(run_bg_calls) == 0, str(run_bg_calls))
chk("handle_reply files the out-of-scope finding via file_findings",
    len(filed_calls) == 1, str(filed_calls))
chk("the filed finding carries the 'out-of-scope' label",
    filed_calls and "out-of-scope" in filed_calls[0][1], str(filed_calls))
chk("handle_reply sends a success notification",
    any("filed" in m.lower() for m in notify_msgs), str(notify_msgs))

# Restore
filing.make_backlog = orig_make_backlog
decisions._run_bg = orig_run_bg


# --- 6) _file_out_of_scope_resume falls back gracefully when no report stored -------------- #
notify_msgs2 = []
decisions.notify = types.SimpleNamespace(send=lambda msg: notify_msgs2.append(msg))

resolved_no_report = {
    "id": "EU-77#out-of-scope", "app": app.name, "answer": "yes",
    "summary": "old ticket", "description": "", "acceptance": [], "ephemeral": False,
}
decisions._file_out_of_scope_resume(cfg, resolved_no_report)
chk("no-report fallback sends a notification instead of crashing",
    any("no stored report" in m or "nothing to file" in m for m in notify_msgs2), str(notify_msgs2))


# --- 7) _file_out_of_scope_resume is a no-op in dry-run mode -------------------------------- #
filed_dry = []
filing.make_backlog = lambda a: type("B", (), {
    "find_open_by_summary": lambda s, x: None,
    "create_task": lambda s, *a, **k: filed_dry.append(a) or "EU-999",
})()
dry_cfg = Config(apps=[app], audit_path=str(tmp / "audit2.jsonl"), dry_run=True)

notify_msgs3 = []
decisions.notify = types.SimpleNamespace(send=lambda msg: notify_msgs3.append(msg))

resolved_dry = {
    "id": "EU-81#out-of-scope", "app": app.name, "answer": "yes",
    "summary": "ticket", "description": "", "acceptance": [], "ephemeral": False,
    "out_of_scope_report": REPORT_WITH_FINDINGS,
}
decisions._file_out_of_scope_resume(dry_cfg, resolved_dry)
chk("_file_out_of_scope_resume dry-run does NOT file anything",
    len(filed_dry) == 0, str(filed_dry))

# Restore
filing.make_backlog = orig_make_backlog
decisions.notify = types.SimpleNamespace(send=lambda *a, **k: None)


# --- summary ------------------------------------------------------------------------------ #
passed = sum(1 for _, c, _ in results if c)
for n, c, d in results:
    print(f"  {'✓' if c else '✗'} {n}" + (f"  [{d}]" if (not c and d) else ""))
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
