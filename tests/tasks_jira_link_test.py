"""Task-log 'Open in Jira' link (2026-07-19): every ticket row on /tasks carries a link to its
real Jira issue, derived from the app's own base_url — config `backlog.base_url` first, then the
cockpit connection store — and NOT rendered for non-Jira-backed apps or non-key ids.

Pins:
  (1) dashboard._jira_base_for resolves from config.backlog.base_url;
  (2) …falls back to the connection store when config has none;
  (3) …returns '' for a non-Jira backend, an unknown app, or no cfg (never raises);
  (4) _jira_link builds {base}/browse/{KEY}, only for a real Jira key, with target/noopener;
  (5) the /tasks table row links each ticket (with stopPropagation so the row still toggles);
  (6) the Needs-you panel row links its ticket too;
  (7) a non-Jira app's /tasks page renders NO Jira link."""
import sys
import types
import tempfile
import json
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import dashboard, connections
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"

jira_app = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev",
                     protected_branch="main", backlog_backend="jira")
jira_app.backlog = {"base_url": "https://toibis.atlassian.net/", "project_key": "AUTO"}
conn_app = AppConfig(name="viaconn", repo_path=str(tmp), base_branch="dev",
                     protected_branch="main", backlog_backend="jira")
conn_app.backlog = {}                       # no base_url in config → must fall back to the store
none_app = AppConfig(name="local-only", repo_path=str(tmp), base_branch="dev",
                     protected_branch="main", backlog_backend="none")
cfg = Config(apps=[jira_app, conn_app, none_app], audit_path=str(audit), use_worktree=False)

# ── (1) config base_url ──
chk("(1) resolves base_url from config.backlog",
    dashboard._jira_base_for(cfg, "automatixy") == "https://toibis.atlassian.net")

# ── (2) connection-store fallback ──
_orig_for_app = connections.for_app
connections.for_app = lambda name, c=None: (
    {"base_url": "https://conn.atlassian.net", "project_key": "VC"} if name == "viaconn" else None)
try:
    chk("(2) falls back to the connection store when config has no base_url",
        dashboard._jira_base_for(cfg, "viaconn") == "https://conn.atlassian.net")
finally:
    connections.for_app = _orig_for_app

# ── (3) graceful empties ──
chk("(3a) non-Jira backend → ''", dashboard._jira_base_for(cfg, "local-only") == "")
chk("(3b) unknown app → ''", dashboard._jira_base_for(cfg, "nope") == "")
chk("(3c) no cfg → '' (never raises)", dashboard._jira_base_for(None, "automatixy") == "")

# ── (4) _jira_link shape ──
link = dashboard._jira_link("https://toibis.atlassian.net", "AUTO-100", stop_prop=True)
chk("(4a) link targets {base}/browse/{KEY}", 'href="https://toibis.atlassian.net/browse/AUTO-100"' in link)
chk("(4b) opens in a new tab, noopener", "target=_blank" in link and "rel=noopener" in link)
chk("(4c) stop_prop guards the row toggle", "event.stopPropagation()" in link)
chk("(4d) no base → no link", dashboard._jira_link("", "AUTO-100") == "")
chk("(4e) a non-Jira id (ephemeral run) → no link",
    dashboard._jira_link("https://x.atlassian.net", "adhoc-smoke") == "")

# ── (5)+(6) end-to-end through the server /tasks page ──
for tid, term, extra in [("AUTO-100", "merged", {}),
                         ("AUTO-178", "ticket_exception", {"error": "boom"})]:
    with audit.open("a") as f:
        f.write(json.dumps({"event": "ticket_start", "ticket_id": tid, "app": "automatixy",
                            "ts": "2026-06-22T09:01:00"}) + "\n")
        f.write(json.dumps({"event": term, "ticket_id": tid, "app": "automatixy",
                            "ts": "2026-06-22T09:02:00", **extra}) + "\n")

from orchestrator import server
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()
body = client.get("/tasks?app=automatixy&filter=all").get_data(as_text=True)
chk("(5) the runs table links the merged ticket to Jira",
    "https://toibis.atlassian.net/browse/AUTO-100" in body)
chk("(6) the Needs-you panel links its ticket to Jira",
    "https://toibis.atlassian.net/browse/AUTO-178" in body)
chk("(5b) the link carries stopPropagation inside the toggling row", "event.stopPropagation()" in body)

# ── (7) a non-Jira app renders no link ──
body_none = client.get("/tasks?app=local-only&filter=all").get_data(as_text=True)
chk("(7) non-Jira app: /tasks renders no Jira link", "class=jira" not in body_none)

print("\n============ TASK-LOG JIRA LINK QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
