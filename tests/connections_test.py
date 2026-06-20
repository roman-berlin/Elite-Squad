"""Jira-connection store QA: the cockpit's quick-connect store (add / list / assign / remove with tokens
masked), the adapter preferring an assigned connection over env vars (so two Jira accounts run side by
side), and the /jira cockpit page + its POST endpoints."""
import sys, types, tempfile, json, os
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

# stub requests so importing/instantiating the Jira adapter is cheap + offline
req = types.ModuleType("requests")
class _Sess:
    def __init__(s):
        s.auth = None
        s.headers = types.SimpleNamespace(update=lambda *a, **k: None)
req.Session = lambda: _Sess()
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import connections

# redirect the store to a temp file (the real one lives at the repo root)
_tmp = Path(tempfile.mkdtemp(prefix="conntest-")) / "jira_connections.json"
connections._file = lambda cfg=None: _tmp

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- empty store: safe defaults, env-var fallback ---
chk("empty: no connections listed", connections.list_connections() == [])
chk("empty: for_app -> None (adapter falls back to env vars)", connections.for_app("automatixy") is None)
chk("empty: assigned_id -> ''", connections.assigned_id(None, "automatixy") == "")

# --- add + masking ---
cid = connections.add(None, name="Automatixy Jira", base_url="https://acme.atlassian.net/",
                      email="me@acme.com", token="abcd1234SECRET", project_key="AUTO")
lst = connections.list_connections()
chk("add: one connection listed", len(lst) == 1)
chk("add: trailing slash stripped from base_url", lst[0]["base_url"] == "https://acme.atlassian.net")
chk("add: raw token NEVER in the masked list", lst[0].get("token") is None)
chk("add: token masked to last 4", lst[0]["token_hint"] == "••••CRET")
chk("add: project_key kept", lst[0]["project_key"] == "AUTO")

# --- assign makes for_app resolve the FULL connection (token included) for the adapter ---
chk("before assign: for_app still None", connections.for_app("automatixy") is None)
connections.assign(None, "automatixy", cid)
full = connections.for_app("automatixy")
chk("assign: assigned_id set", connections.assigned_id(None, "automatixy") == cid)
chk("assign: for_app returns the token for the adapter", full and full["token"] == "abcd1234SECRET")

# --- two Jira accounts side by side (Roman's case: Automatixy vs the algo-trading robot) ---
cid2 = connections.add(None, name="Robot Jira", base_url="https://robot.atlassian.net",
                       email="me@robot.com", token="zzzz9999TOKEN")  # no project_key
connections.assign(None, "robot", cid2)
chk("side by side: automatixy -> acme account", connections.for_app("automatixy")["email"] == "me@acme.com")
chk("side by side: robot -> robot account", connections.for_app("robot")["email"] == "me@robot.com")

# --- clearing an assignment falls back to env vars ---
connections.assign(None, "automatixy", "")
chk("clear: for_app -> None again", connections.for_app("automatixy") is None)
connections.assign(None, "automatixy", cid)  # restore

# --- persisted to disk as the gitignored json (token only here) ---
chk("persisted: store file written", _tmp.exists())
disk = json.loads(_tmp.read_text())
chk("persisted: token stored in the gitignored file", any(c["token"] == "zzzz9999TOKEN" for c in disk["connections"]))

# --- the ADAPTER prefers an assigned connection over config/env ---
from orchestrator.backlog.jira import JiraAdapter
ns = types.SimpleNamespace
a_auto = JiraAdapter(ns(name="automatixy", backlog={"base_url": "https://IGNORED.example", "project_key": "OLD"}))
chk("adapter: base_url from the connection (overrides config)", a_auto.base_url == "https://acme.atlassian.net")
chk("adapter: project_key from the connection when set", a_auto.project == "AUTO")
chk("adapter: auth = (connection email, connection token)", a_auto.session.auth == ("me@acme.com", "abcd1234SECRET"))

a_robot = JiraAdapter(ns(name="robot", backlog={"base_url": "https://IGNORED.example", "project_key": "KEEP"}))
chk("adapter: empty connection project_key keeps the config project", a_robot.project == "KEEP")
chk("adapter: second account auth is independent", a_robot.session.auth == ("me@robot.com", "zzzz9999TOKEN"))

# --- no connection + no env vars -> fail fast with a helpful message ---
for k in ("JIRA_EMAIL", "JIRA_API_TOKEN"):
    os.environ.pop(k, None)
try:
    JiraAdapter(ns(name="noconn", backlog={"base_url": "https://x.example"}))
    chk("adapter: missing creds raises", False)
except RuntimeError as exc:
    chk("adapter: missing creds -> error points to cockpit/env", "cockpit" in str(exc).lower() or "JIRA_EMAIL" in str(exc))

# --- env-var fallback still works when no connection is assigned (back-compat) ---
os.environ["JIRA_EMAIL"] = "env@x.com"
os.environ["JIRA_API_TOKEN"] = "ENVTOKEN"
a_env = JiraAdapter(ns(name="noconn", backlog={"base_url": "https://x.example"}))
chk("adapter: env-var fallback intact", a_env.session.auth == ("env@x.com", "ENVTOKEN"))

# --- remove cascades the project assignment ---
connections.remove(None, cid)
chk("remove: connection gone", all(c["id"] != cid for c in connections.list_connections()))
chk("remove: its project assignment cleared", connections.assigned_id(None, "automatixy") == "")
chk("remove: the other account is untouched", connections.for_app("robot") and connections.for_app("robot")["id"] == cid2)

# --- the cockpit /jira page + endpoints (Flask test client) ---
from orchestrator import server
from orchestrator.config import Config, AppConfig
tmp = Path(tempfile.mkdtemp())
repo = tmp / "app"; repo.mkdir()
scfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(repo), base_branch="DEV",
                              protected_branch="MAIN", backlog_backend="none")],
              audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
scfg.detected_auth = lambda: "test"
client = server.create_app(scfg).test_client()

r = client.get("/jira?app=automatixy"); body = r.get_data(as_text=True)
chk("/jira returns 200", r.status_code == 200, str(r.status_code))
chk("/jira shows the project switcher", "automatixy" in body and "Project" in body)
chk("/jira shows the saved Robot connection", "Robot Jira" in body)
chk("/jira masks the token (no raw token in HTML)", "zzzz9999TOKEN" not in body and "••••" in body)
chk("/jira has a quick-connect form", "Quick connect" in body and "API token" in body)
chk("/jira token input is a password field", 'name=token type=password' in body)

# quick-connect a new Jira via the endpoint
r2 = client.post("/api/jira-connect", data={"app": "automatixy", "name": "New Jira",
                 "base_url": "https://new.atlassian.net", "email": "n@new.com", "token": "NEWTOKEN9",
                 "project_key": "NEW", "assign": "1"})
chk("/api/jira-connect redirects back to /jira", r2.status_code in (301, 302) and "/jira" in r2.headers.get("Location", ""))
chk("/api/jira-connect saved the connection", any(c["name"] == "New Jira" for c in connections.list_connections()))
chk("/api/jira-connect auto-assigned it to the project",
    (connections.for_app("automatixy") or {}).get("token") == "NEWTOKEN9")

# assign + forget endpoints
new_id = connections.assigned_id(None, "automatixy")
client.post("/api/jira-assign", data={"app": "automatixy", "id": cid2})
chk("/api/jira-assign switched the project's Jira", connections.assigned_id(None, "automatixy") == cid2)
client.post("/api/jira-forget", data={"app": "automatixy", "id": new_id})
chk("/api/jira-forget removed the connection", all(c["id"] != new_id for c in connections.list_connections()))

print("\n============== JIRA CONNECTIONS QA ==============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
