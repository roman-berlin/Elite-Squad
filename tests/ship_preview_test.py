"""Ship-preview QA: the commits DEV is ahead of MAIN are listed with their tickets parsed from the
commit subjects, grouped per ticket, and the review page renders them with the final Ship button."""
import sys, types, tempfile, subprocess
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import sync, server
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- a real app repo: MAIN baseline + 3 commits on DEV (two ticketed, one not) ---
tmp = Path(tempfile.mkdtemp())
def G(*a): subprocess.run(["git", *a], cwd=tmp, check=True, capture_output=True, text=True)
subprocess.run(["git", "init", str(tmp)], check=True, capture_output=True)
G("config", "user.email", "t@t"); G("config", "user.name", "t")
G("checkout", "-b", "MAIN")
(tmp / "a.txt").write_text("base\n"); G("add", "-A"); G("commit", "-m", "baseline")
G("checkout", "-b", "DEV")
(tmp / "b.txt").write_text("1\n"); G("add", "-A"); G("commit", "-m", "AUTO-4: superadmin authz hardening")
(tmp / "c.txt").write_text("2\n"); G("add", "-A"); G("commit", "-m", "fix a typo in the footer")
(tmp / "d.txt").write_text("3\n"); G("add", "-A"); G("commit", "-m", "AUTO-7: add the budget panel")

app = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV", protected_branch="MAIN",
                backlog_backend="none")

commits = sync.app_promote_commits(app)
chk("lists all 3 DEV-ahead commits", len(commits) == 3, str(len(commits)))
chk("newest commit first", commits[0]["subject"].startswith("AUTO-7"), commits[0]["subject"])
subj_by_ticket = {c["ticket"]: c["subject"] for c in commits}
chk("parses AUTO-7 ticket", "AUTO-7" in subj_by_ticket)
chk("parses AUTO-4 ticket", "AUTO-4" in subj_by_ticket)
chk("untagged commit -> empty ticket", "" in subj_by_ticket and "typo" in subj_by_ticket[""])
chk("each commit carries a sha", all(c["sha"] for c in commits))

# nothing ahead -> empty list
app_sync = AppConfig(name="x", repo_path=str(tmp), base_branch="MAIN", protected_branch="MAIN",
                     backlog_backend="none")
chk("no commits when base==prot", sync.app_promote_commits(app_sync) == [])

# --- the /ship-preview route renders the review page ---
import os
os.environ["GENERAL_COCKPIT_PROMOTE"] = "1"
cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()
r = client.get("/ship-preview?app=automatixy")
body = r.get_data(as_text=True)
chk("/ship-preview returns 200", r.status_code == 200, str(r.status_code))
chk("page titles the app + production", "Ship automatixy" in body and "production" in body)
chk("page lists the tickets going live", "AUTO-4" in body and "AUTO-7" in body)
chk("page shows the commit subjects", "budget panel" in body and "superadmin authz" in body)
chk("page shows the untagged commit under 'No ticket'", "No ticket" in body and "typo" in body)
chk("page has the final Ship button posting to /api/ship-main",
    "/api/ship-main" in body and "Ship automatixy to production" in body)
chk("page summarises ticket count", "2 tickets" in body or "2 ticket" in body)

# --- EU-26 regression: /ship-preview with NO ?app= must not 500 (app0 NameError) ---
# direct hit / bookmark / refresh drops the query string -> appq falls back to cfg.apps[0].name
r_noq = client.get("/ship-preview")
b_noq = r_noq.get_data(as_text=True)
chk("EU-26: /ship-preview (no ?app=) returns 200, not 500", r_noq.status_code == 200, str(r_noq.status_code))
chk("EU-26: no-query falls back to first app's preview",
    "Ship automatixy" in b_noq and "AUTO-7" in b_noq)

# empty-state branch: no apps configured -> 'No app selected', still 200
cfg_empty = Config(apps=[], audit_path=str(tmp / "a3.jsonl"), use_worktree=False)
cfg_empty.detected_auth = lambda: "test"
r_empty = server.create_app(cfg_empty).test_client().get("/ship-preview")
b_empty = r_empty.get_data(as_text=True)
chk("EU-26: no apps -> /ship-preview (no ?app=) returns 200", r_empty.status_code == 200, str(r_empty.status_code))
chk("EU-26: no apps -> renders 'No app selected' empty state", "No app selected" in b_empty)

# in-sync app -> 'nothing to ship'
r2 = client.get("/ship-preview?app=automatixy")   # still ahead; check the empty path via a synced app
cfg2 = Config(apps=[app_sync], audit_path=str(tmp / "a2.jsonl"), use_worktree=False)
cfg2.detected_auth = lambda: "test"
b2 = server.create_app(cfg2).test_client().get("/ship-preview?app=x").get_data(as_text=True)
chk("in-sync app -> nothing to ship", "Nothing to ship" in b2 or "in sync" in b2)

print("\n=============== SHIP PREVIEW QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
