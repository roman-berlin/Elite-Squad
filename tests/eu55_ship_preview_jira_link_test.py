"""EU-55 / F12(a) regression: the ship-preview page must render each ticket as a real Jira
link built from the app's configured `backlog.base_url`.

The bug: server.py read `(app_cfg.backlog or {}).get("site", "")`, but no backlog block ever
defines `site` (config uses `base_url`, and server.py:1000 itself keys off `base_url`). So
`jira_base` was always "" and the link branch silently degraded every ticket to plain text.

These checks FAIL on the old `site` lookup (no `/browse/` href is emitted) and PASS once the
code reads `base_url`. A negative control proves the link only appears when a base_url exists.
"""
import sys, types, tempfile, subprocess, os
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import server
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- a real app repo: MAIN baseline + 2 ticketed commits on DEV ---
tmp = Path(tempfile.mkdtemp())
def G(*a): subprocess.run(["git", *a], cwd=tmp, check=True, capture_output=True, text=True)
subprocess.run(["git", "init", str(tmp)], check=True, capture_output=True)
G("config", "user.email", "t@t"); G("config", "user.name", "t")
G("checkout", "-b", "MAIN")
(tmp / "a.txt").write_text("base\n"); G("add", "-A"); G("commit", "-m", "baseline")
G("checkout", "-b", "DEV")
(tmp / "b.txt").write_text("1\n"); G("add", "-A"); G("commit", "-m", "AUTO-4: authz hardening")
(tmp / "d.txt").write_text("3\n"); G("add", "-A"); G("commit", "-m", "AUTO-7: add the budget panel")

os.environ["GENERAL_COCKPIT_PROMOTE"] = "1"

def render(backlog: dict | None, *, backend: str = "jira", audit_name: str = "audit.jsonl") -> str:
    app = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend=backend, backlog=backlog or {})
    cfg = Config(apps=[app], audit_path=str(tmp / audit_name), use_worktree=False)
    cfg.detected_auth = lambda: "test"
    return server.create_app(cfg).test_client().get("/ship-preview?app=automatixy").get_data(as_text=True)

# 1) HAPPY PATH: a configured base_url renders a real /browse/<id> anchor per ticket.
body = render({"base_url": "https://acme.atlassian.net", "project_key": "AUTO"})
chk("renders Jira link for AUTO-7 from base_url",
    'href="https://acme.atlassian.net/browse/AUTO-7"' in body, body[:0])
chk("renders Jira link for AUTO-4 from base_url",
    'href="https://acme.atlassian.net/browse/AUTO-4"' in body)
chk("both tickets still listed in the live summary", "AUTO-4" in body and "AUTO-7" in body)

# 2) trailing slash on base_url is stripped — no double slash in the href.
body_slash = render({"base_url": "https://acme.atlassian.net/", "project_key": "AUTO"},
                    audit_name="slash.jsonl")
chk("trailing slash trimmed (no //browse)",
    'href="https://acme.atlassian.net/browse/AUTO-7"' in body_slash
    and "atlassian.net//browse" not in body_slash)

# 3) REGRESSION CONTROL — the old `site` key is dead: a backlog with ONLY `site` (no base_url)
#    must NOT produce a link. This is exactly the config shape the old code read, and it proves
#    the fix no longer keys off `site`.
body_site_only = render({"site": "https://legacy.example.com", "project_key": "AUTO"},
                        audit_name="siteonly.jsonl")
chk("legacy `site`-only backlog yields NO Jira link", "/browse/AUTO-7" not in body_site_only)
chk("ticket still shown as plain text when no base_url", "AUTO-7" in body_site_only)

# 4) no backlog at all -> degrades safely to plain text, never 500s.
body_none = render(None, backend="none", audit_name="none.jsonl")
chk("no backlog -> no link, plain text fallback", "/browse/AUTO-7" not in body_none and "AUTO-7" in body_none)

print("\n=========== EU-55 SHIP-PREVIEW JIRA LINK QA ===========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
