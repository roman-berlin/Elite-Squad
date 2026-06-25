"""Back-link QA: every cockpit SUB-page has a '← cockpit' link home. 17 of them get it from the shared
_wrap helper; the /tasks board renders via D.render_html (bypasses _wrap) so it injects its own. The
home page '/' is the root and correctly has none."""
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

from orchestrator import server, sync, cockpit_views
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

BACK = "&larr; cockpit"

# --- the shared helper that wraps 17 sub-pages always carries the back link ---
w = cockpit_views._wrap("Some Page", "<p>body</p>")
chk("_wrap injects the back link (covers all _wrap pages)", BACK in w and "href='/'" in w)

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp / "app"), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
sync.can_promote = lambda: True
sync.promote_status = lambda c: {"ahead": 0, "subjects": [], "error": None}
sync.app_promote_status = lambda a: {"ahead": 0, "base": "DEV", "prot": "MAIN", "error": None}
client = server.create_app(cfg).test_client()

# --- /tasks board (the one that bypassed _wrap) now has a back link ---
t = client.get("/tasks").get_data(as_text=True)
chk("/tasks board has a back link", BACK in t)

# --- a representative _wrap sub-page has it too ---
r = client.get("/report").get_data(as_text=True)
chk("/report (a _wrap page) has a back link", BACK in r)

# --- the back link CARRIES the active project, so 'back' returns to it, not 'All projects' (the bug) ---
rp = client.get("/report?app=automatixy").get_data(as_text=True)
chk("a _wrap sub-page back link preserves the active project", "href='/?app=automatixy'" in rp)
tp = client.get("/tasks?app=automatixy").get_data(as_text=True)
chk("/tasks back link preserves the active project", "/?app=automatixy" in tp)
chk("no active project → plain '/' (no dangling ?app=)", "href='/'" in client.get("/report").get_data(as_text=True))

# --- the home page is the root: it should NOT carry a '← cockpit' link to itself ---
home = client.get("/").get_data(as_text=True)
chk("home '/' has no self-referential back link", BACK not in home)

print("\n============ BACK-LINK QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
