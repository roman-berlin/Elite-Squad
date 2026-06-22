"""Cockpit backlog QA: the War Room shows a project-scoped 'Tickets to work' panel (the open Jira
backlog), the project selector's 'All projects' (*) aggregates every backlogged app instead of crashing
on cfg.app('*'), the fetch is TTL-cached so the SSE poll doesn't hammer Jira, and a Jira error degrades
gracefully. /tickets renders an all-projects index (grouped, read-only) vs a single-project run form."""
import sys, types, tempfile
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

from orchestrator import warroom, intake, server, sync
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- fake Jira backlog: two apps, by app_name (None = all backlogged apps) ---
appEU = types.SimpleNamespace(name="Elite-Unit", backlog_backend="jira")
appAUTO = types.SimpleNamespace(name="automatixy", backlog_backend="jira")
def tk(i, s): return types.SimpleNamespace(id=i, key=i, summary=s)
DATA = {
    None: [(appEU, tk("EU-20", "Rename officers in ORG.md")),
           (appEU, tk("EU-24", "Patrol reports filed N")),
           (appAUTO, tk("AUTO-9", "Lead webhook"))],
    "Elite-Unit": [(appEU, tk("EU-20", "Rename officers in ORG.md")), (appEU, tk("EU-24", "Patrol reports filed N"))],
    "automatixy": [(appAUTO, tk("AUTO-9", "Lead webhook"))],
}
calls = {"n": 0}
def fake_drain(cfg, app_name, limit):
    calls["n"] += 1
    if app_name in DATA:
        return DATA[app_name]
    raise ValueError(f"no app '{app_name}'")     # mirrors cfg.app('*') blowing up
intake.from_drain = fake_drain

cfg = Config(apps=[AppConfig(name="Elite-Unit", repo_path="/x/eu", base_branch="dev", protected_branch="main",
                             backlog_backend="jira"),
                   AppConfig(name="automatixy", repo_path="/x/auto", base_branch="DEV", protected_branch="MAIN",
                             backlog_backend="jira")],
             audit_path=str(Path(tempfile.mkdtemp()) / "audit.jsonl"), use_worktree=False)

# --- _backlog_items: '*'/None span all apps (no cfg.app('*') crash); specific app is scoped ---
warroom._BACKLOG_CACHE.clear(); calls["n"] = 0
items_all, err_all = warroom._backlog_items(cfg, "*")
chk("'*' spans all backlogged apps (no crash)", err_all is None and len(items_all) == 3)
chk("'*' maps to from_drain(name=None)", calls["n"] == 1)   # one fetch, app_name None
items_none, _ = warroom._backlog_items(cfg, None)
chk("None and '*' share the same scope (served from cache)", len(items_none) == 3 and calls["n"] == 1)
warroom._BACKLOG_CACHE.clear()
items_eu, _ = warroom._backlog_items(cfg, "Elite-Unit")
chk("specific app is scoped to that app", len(items_eu) == 2)

# --- TTL cache: a second call inside the window does NOT re-fetch ---
warroom._BACKLOG_CACHE.clear(); calls["n"] = 0
warroom._backlog_items(cfg, "Elite-Unit"); warroom._backlog_items(cfg, "Elite-Unit")
chk("TTL cache prevents a second Jira fetch", calls["n"] == 1)

# --- error degradation: a fetch failure with NO cache -> empty + error; WITH cache -> serves stale ---
warroom._BACKLOG_CACHE.clear()
def boom(cfg, app_name, limit): raise RuntimeError("jira 503")
intake.from_drain = boom
it_err, err = warroom._backlog_items(cfg, "automatixy")
chk("error with no cache -> empty + error surfaced", it_err == [] and err and "jira 503" in err)
warroom._BACKLOG_CACHE["x"] = (9e18, [(appAUTO, tk("AUTO-1", "cached"))])   # far-future ts = fresh
it_stale, err2 = warroom._backlog_items(cfg, "x")
chk("error with cache -> serves stale, no error", err2 is None and len(it_stale) == 1)
intake.from_drain = fake_drain

# --- _backlog_html: app badge only in the multi-project view; rows + empty + error states ---
warroom._BACKLOG_CACHE.clear()
h_all = warroom._backlog_html(cfg, "*")
chk("multi-project html shows the app badge", "blapp" in h_all and "Elite-Unit" in h_all and "EU-20" in h_all)
warroom._BACKLOG_CACHE.clear()
h_eu = warroom._backlog_html(cfg, "Elite-Unit")
chk("single-project html omits the app badge", "EU-20" in h_eu and "blapp" not in h_eu)
warroom._BACKLOG_CACHE.clear()
intake.from_drain = lambda c, n, l: []
chk("empty backlog -> friendly empty state", "Nothing of yours open" in warroom._backlog_html(cfg, "Elite-Unit"))
intake.from_drain = fake_drain

# --- /tickets route: '*' -> grouped read-only index (no crash); a project -> the run form ---
sync.can_promote = lambda: False
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()
warroom._BACKLOG_CACHE.clear()
all_pg = client.get("/tickets?app=*").get_data(as_text=True)
chk("/tickets?app=* does not crash (200 + all-projects index)", "all projects" in all_pg and "EU-20" in all_pg and "AUTO-9" in all_pg)
chk("/tickets?app=* is read-only (no cross-app run checkboxes)", 'name=ticket' not in all_pg and "Develop selected" not in all_pg)
one_pg = client.get("/tickets?app=Elite-Unit").get_data(as_text=True)
chk("/tickets?app=<proj> shows the run form (checkboxes + develop)", 'name=ticket' in one_pg and "Develop selected" in one_pg and "EU-20" in one_pg)

print("\n============ COCKPIT BACKLOG QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
