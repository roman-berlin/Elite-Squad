"""EU-61 (Ordnance BE slice): Jira adapter comment-read primitive + Roman assignee default.

Covers:
  * create_task ALWAYS pins an assignee — Roman by default, the configured one when set,
  * the comments()/latest_answer() read primitive returns the latest HUMAN comment (skips [General]),
  * quick-connect (connections.add) stores Roman as the default assignee so future boards inherit it.
"""
import sys, types
# stub 'requests' so the jira/connections modules import without the dep (offline test)
req = types.ModuleType("requests")
req.Session = lambda *a, **k: None
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator.backlog import jira

ROMAN = "70121:051c9744-3c4d-4dfb-b2e5-d7a0e87c2443"
check("module pins Roman's real accountId", jira.ROMAN_ACCOUNT_ID == ROMAN)


class _Resp:
    def __init__(self, payload=None): self._p = payload or {}
    def raise_for_status(self): pass
    def json(self): return self._p


class _Sess:
    """Captures the last POST body and serves a canned GET response."""
    def __init__(self, get_payload=None):
        self.posts = []
        self._get_payload = get_payload or {}
    def post(self, url, json=None): self.posts.append((url, json)); return _Resp({"key": "AUTO-77"})
    def get(self, url, params=None): return _Resp(self._get_payload)


def _adapter(assignee, session):
    return types.SimpleNamespace(
        project="AUTO", assignee=assignee, session=session,
        base_url="https://toibis.atlassian.net",
        _url=lambda path: f"https://toibis.atlassian.net/rest/api/3/{path.lstrip('/')}")


# ---- create_task: defaults assignee to Roman when none configured ----
sess = _Sess()
key = jira.JiraAdapter.create_task(_adapter(None, sess), "A finding", "details", labels=["security"])
_, body = sess.posts[-1]
check("create_task returns the new key", key == "AUTO-77")
check("create payload carries an assignee accountId", "assignee" in body["fields"])
check("create payload assignee defaults to Roman's accountId",
      body["fields"].get("assignee", {}).get("accountId") == ROMAN,
      str(body["fields"].get("assignee")))

# ---- create_task: a configured assignee wins over the default ----
sess2 = _Sess()
jira.JiraAdapter.create_task(_adapter("70121:other-person", sess2), "X", "y")
_, body2 = sess2.posts[-1]
check("configured assignee overrides the Roman default",
      body2["fields"]["assignee"]["accountId"] == "70121:other-person")

# ---- comments()/latest_answer(): newest human comment, [General] skipped ----
jira._adf_to_text = lambda x: x if isinstance(x, str) else ""
convo = {"comments": [
    {"body": "[General] What should the button say?"},
    {"body": "Call it 'Approve & file'"},
    {"body": "[General] Acknowledged."},
]}
ad = _adapter(None, _Sess(get_payload=convo))
ad.comments = lambda key: jira.JiraAdapter.comments(ad, key)   # bind the real read primitive
got = jira.JiraAdapter.latest_answer(ad, types.SimpleNamespace(key="AUTO-77"))
check("latest_answer returns the Commander's human reply", got == "Call it 'Approve & file'", repr(got))
check("comments() returns the full thread", len(jira.JiraAdapter.comments(ad, "AUTO-77")) == 3)

ad_none = _adapter(None, _Sess(get_payload={"comments": [{"body": "[General] only ours"}]}))
ad_none.comments = lambda key: jira.JiraAdapter.comments(ad_none, key)
check("latest_answer is None when only the unit has spoken",
      jira.JiraAdapter.latest_answer(ad_none, types.SimpleNamespace(key="AUTO-77")) is None)
check("latest_answer accepts a bare key too",
      jira.JiraAdapter.latest_answer(ad, "AUTO-77") == "Call it 'Approve & file'")

# ---- connections.add: pins Roman by default so future boards inherit him ----
import tempfile, json as _json
from pathlib import Path
from orchestrator import connections
tmp = Path(tempfile.mkdtemp()) / "jira_connections.json"
connections._file = lambda cfg=None: tmp
cid = connections.add(None, name="New Board", base_url="https://x.atlassian.net",
                      email="me@x.com", token="tok", project_key="NEW")
stored = _json.loads(tmp.read_text())["connections"][0]
check("quick-connect stores Roman as the default assignee", stored.get("assignee") == ROMAN)
cid2 = connections.add(None, name="Custom", base_url="https://y.atlassian.net",
                       email="me@y.com", token="tok2", assignee="70121:someone-else")
stored2 = _json.loads(tmp.read_text())["connections"][1]
check("an explicit assignee is honoured by quick-connect", stored2.get("assignee") == "70121:someone-else")

print("\n================ EU-61 JIRA-ASSIGNEE QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
