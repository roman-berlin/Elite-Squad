"""Multi-Jira: each app uses its OWN credentials (several accounts side by side); a bad connection is skipped."""
import sys, os, types
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator.backlog.jira import JiraAdapter
from orchestrator.config import AppConfig, Config
from orchestrator import intake
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

def mkapp(name, backlog):
    return AppConfig(name=name, repo_path=".", base_branch="DEV", protected_branch="MAIN",
                     backlog_backend="jira", backlog=backlog)

# --- connection 1: the default env vars ---
os.environ["JIRA_EMAIL"] = "primary@toibis.com"; os.environ["JIRA_API_TOKEN"] = "primtok"
a = JiraAdapter(mkapp("automatixy", {"base_url": "https://toibis.atlassian.net", "project_key": "AUTO"}))
chk("default connection uses JIRA_EMAIL / JIRA_API_TOKEN", a.session.auth == ("primary@toibis.com", "primtok"))

# --- connection 2: its OWN env vars -> a different Jira account, side by side ---
os.environ["OTHER_EMAIL"] = "me@other.com"; os.environ["OTHER_TOKEN"] = "othertok"
b = JiraAdapter(mkapp("otherproj", {"base_url": "https://other.atlassian.net", "project_key": "PROJ",
                                    "email_env": "OTHER_EMAIL", "token_env": "OTHER_TOKEN"}))
chk("second connection uses its own env vars (multi-Jira)", b.session.auth == ("me@other.com", "othertok"))
chk("the two connections are independent accounts", a.session.auth != b.session.auth)

# --- missing creds -> a clear, actionable error ---
os.environ.pop("MISSING_EMAIL", None)
try:
    JiraAdapter(mkapp("automatixy", {"base_url": "https://z.atlassian.net", "project_key": "Z",
                                     "email_env": "MISSING_EMAIL", "token_env": "OTHER_TOKEN"}))
    chk("missing creds raises", False)
except RuntimeError as e:
    chk("missing creds -> clear error naming the app + env var", "MISSING_EMAIL" in str(e) and "automatixy" in str(e), str(e)[:80])

# --- resilient drain across all Jiras: one bad connection skipped, the others still pulled ---
good = mkapp("good", {}); bad = mkapp("bad", {})
cfg = Config(apps=[bad, good], audit_path="/tmp/x.jsonl", use_worktree=False)
class FakeBacklog:
    def get_ready_tasks(self, limit):
        return [Ticket(id="G-1", key="G-1", summary="s", description="d", app="good")]
def fake_make(app):
    if app.name == "bad":
        raise RuntimeError("bad creds / network")
    return FakeBacklog()
intake.make_backlog = fake_make
items = intake.from_drain(cfg, None, 5)
chk("drain spans all apps, skips the broken Jira", len(items) == 1 and items[0][1].id == "G-1", f"{len(items)} items")
chk("the healthy Jira still drained", items and items[0][0].name == "good")

print("\n================ MULTI-JIRA QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
