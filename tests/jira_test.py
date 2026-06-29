"""QA for the ticket-context upgrades: read ALL Commander comments + download image attachments."""
import os, sys, types
# stub 'requests' so the jira module imports without the dep (we test methods via SimpleNamespace)
req = types.ModuleType("requests")
req.Session = lambda *a, **k: None
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator.backlog import jira
jira._adf_to_text = lambda x: x if isinstance(x, str) else ""   # treat comment/desc bodies as plain text

# ---- _to_ticket: all comments + image references ----
self = types.SimpleNamespace(
    ac_field=None, base_url="https://toibis.atlassian.net", app_name="automatixy",
    fetch_images=True,
    _download_images=lambda key, atts: ["/tmp/imgs/mock1.png", "/tmp/imgs/mock2.png"] if atts else [])
issue = {"key": "AUTO-16", "fields": {
    "summary": "redesign", "description": "Original description.",
    "comment": {"comments": [
        {"body": "first: the spacing is too tight"},
        {"body": "[General] Merged to DEV; moved to QA."},
        {"body": "second: make the date band bigger"},
        {"body": "third: use mint green not blue"},
    ]},
    "attachment": [{"mimeType": "image/png", "content": "http://x/1.png", "filename": "mock.png", "id": 1}],
    "labels": [], "issuetype": {"name": "Task"}}}
t = jira.JiraAdapter._to_ticket(self, issue)
check("ALL commander comments included (not just last 2)",
      all(s in t.description for s in ["first: the spacing", "second: make the date band bigger", "third: use mint green"]))
check("the unit's own [General] comment is skipped", "[General]" not in t.description)
check("image paths referenced for the Builder to view",
      "/tmp/imgs/mock1.png" in t.description and "/tmp/imgs/mock2.png" in t.description)
check("original description preserved", "Original description." in t.description)

# ---- _download_images: image only, written, returns paths ----
class _Resp:
    content = b"PNGDATA"
    def raise_for_status(self): pass
class _Sess:
    def __init__(s): s.calls = []
    def get(s, url, timeout=None): s.calls.append(url); return _Resp()
import shutil, tempfile
from pathlib import Path
shutil.rmtree(Path(tempfile.gettempdir()) / "general-ticket-images" / "AUTO-99", ignore_errors=True)  # isolate: idempotent download skips existing files
sess = _Sess()
atts = [{"mimeType": "image/png", "content": "http://x/1.png", "filename": "a.png", "id": 1},
        {"mimeType": "application/pdf", "content": "http://x/d.pdf", "filename": "d.pdf", "id": 2}]
paths = jira.JiraAdapter._download_images(types.SimpleNamespace(fetch_images=True, session=sess), "AUTO-99", atts)
check("downloads the image, skips the pdf", len(paths) == 1 and paths[0].endswith("a.png"), str(paths))
check("image bytes actually written", os.path.exists(paths[0]) and open(paths[0], "rb").read() == b"PNGDATA")
check("only fetched the image url (1 GET)", len(sess.calls) == 1)
check("fetch_images=False -> nothing downloaded",
      jira.JiraAdapter._download_images(types.SimpleNamespace(fetch_images=False, session=_Sess()), "K", atts) == [])

# ---- close_ticket: successful close with comment ----
class _MockAudit:
    def __init__(s): s.records = []
    def record(s, event, **fields): s.records.append({"event": event, **fields})

_mock_transitions = {
    "transitions": [
        {"id": "71", "to": {"name": "Done", "statusCategory": {"key": "done"}}},
        {"id": "81", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}}
    ]
}

class _JiraResp:
    def __init__(s, ok=True, transitions=None): s.ok = ok; s.status_code = 200 if ok else 500; s.transitions = transitions or _mock_transitions
    def raise_for_status(s):
        if not s.ok:
            raise req.RequestException("API error")
    def json(s): return s.transitions

class _JiraSession:
    def __init__(s): s.calls = []; s.resp = _JiraResp()
    def post(s, url, json=None):
        s.calls.append(("POST", url, json))
        return s.resp
    def get(s, url, params=None):
        s.calls.append(("GET", url, params))
        return s.resp
    def put(s, url, json=None):
        s.calls.append(("PUT", url, json))
        return s.resp

audit = _MockAudit()
sess = _JiraSession()
adapter = types.SimpleNamespace(
    session=sess, _url=lambda p: f"https://test.atlassian.net/rest/api/3/{p}",
    ac_field="customfield_10000"
)

# Test close_ticket
result = jira.JiraAdapter.close_ticket(adapter, "AUTO-123", "Duplicate of AUTO-42", audit)
check("close_ticket succeeds", result == True)
check("close_ticket adds comment", any("comment" in call[1] and "Duplicate" in str(call[2]) for call in sess.calls if call[0] == "POST"))
check("close_ticket executes transition", any("transitions" in call[1] for call in sess.calls))
check("close_ticket audits the action", any(r["event"] == "jira_close" and r["ticket_id"] == "AUTO-123" for r in audit.records))

# Test close_ticket failure
sess_fail = _JiraSession()
sess_fail.resp = _JiraResp(ok=False)
audit_fail = _MockAudit()
adapter_fail = types.SimpleNamespace(session=sess_fail, _url=lambda p: f"https://test.atlassian.net/rest/api/3/{p}")
result_fail = jira.JiraAdapter.close_ticket(adapter_fail, "AUTO-456", "Bad close", audit_fail)
check("close_ticket failure returns False", result_fail == False)
check("close_ticket failure audits failure", any(r["event"] == "jira_close_failed" for r in audit_fail.records))

# ---- transition_ticket: move to another project ----
sess_proj = _JiraSession()
sess_proj.resp = _JiraResp(transitions={"values": [{"id": "10000"}]})
adapter_proj = types.SimpleNamespace(
    session=sess_proj, _url=lambda p: f"https://test.atlassian.net/rest/api/3/{p}",
    ac_field="customfield_10000"
)
audit_proj = _MockAudit()

proj_result = jira.JiraAdapter.transition_ticket(
    adapter_proj, "AUTO-789", "NEW", "New AC text here", audit_proj
)

check("transition_ticket attempts project lookup", any("project" in call[1] and call[2] == {"key": "NEW"} for call in sess_proj.calls if call[0] == "GET"))
check("transition_ticket audits the action", any(r["event"] == "jira_transition" and r["target_project"] == "NEW" for r in audit_proj.records))

# ---- doctrine lookup helpers ----
import tempfile, os
from pathlib import Path

# Test read_claude_md
test_dir = Path(tempfile.mkdtemp())
claude_path = test_dir / "CLAUDE.md"
claude_path.write_text("# Test CLAUDE.md\nThis is test content.", encoding="utf-8")

claude_content = jira.read_claude_md(str(test_dir))
check("read_claude_md reads file content", claude_content.startswith("# Test CLAUDE.md"))

# Test read_config_yaml
config_path = test_dir / "config.yaml"
config_path.write_text("test: value\napp: automatixy", encoding="utf-8")

config_content = jira.read_config_yaml(str(config_path))
check("read_config_yaml reads file content", "test: value" in config_content)

# Test with missing files
check("read_claude_md handles missing file", jira.read_claude_md("/nonexistent/path") == "")
check("read_config_yaml handles missing file", jira.read_config_yaml("/nonexistent/config.yaml") == "")

# Cleanup
import shutil
shutil.rmtree(test_dir, ignore_errors=True)

print("\n================ TICKET-CONTEXT QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
