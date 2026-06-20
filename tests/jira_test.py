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

print("\n================ TICKET-CONTEXT QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
