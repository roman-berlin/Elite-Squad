"""Choose-tickets QA (EU-63): /tickets is per-tab — every request renders exactly ONE concrete
project's run form, with MULTI-SELECT checkboxes (not read-only 'develop →' links). The retired
"All projects" grouped index (one form per app) is gone: '*'/empty falls back to the active tab /
first app, so there is always a single run form scoped to one app/Jira."""
import sys, types, tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.RequestException = Exception
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

import orchestrator.server as srv
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

d = Path(tempfile.mkdtemp()); (d / "audit.jsonl").write_text("")
cfg = Config(apps=[AppConfig(name="Elite-Unit", repo_path=str(d), base_branch="dev", protected_branch="main", backlog_backend="none"),
                   AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV", protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
T = lambda i, s: types.SimpleNamespace(id=i, summary=s)
A = lambda n: types.SimpleNamespace(name=n)
srv.intake.from_drain = lambda c, name, lim: [
    (A("Elite-Unit"), T("EU-38", "bloat")), (A("Elite-Unit"), T("EU-40", "rename")),
    (A("automatixy"), T("AUTO-37", "skip")), (A("automatixy"), T("AUTO-38", "alt"))]
client = srv.create_app(cfg).test_client()

# --- '*'/empty falls back to ONE concrete project (first app) — a single run form, not a grouped index ---
b = client.get("/tickets?app=*").get_data(as_text=True)
chk("'*' renders a checkbox per ticket (multi-select)", b.count("type=checkbox name=ticket") == 4, str(b.count("type=checkbox name=ticket")))
chk("exactly ONE run form (no per-project grouping)", b.count("action=/api/run-selected") == 1)
chk("'*' falls back to the first app's tab — Elite-Unit", 'name=app value="Elite-Unit"' in b)
chk("no retired grouped 'Develop selected in <app>' label", "Develop selected in" not in b)
chk("single 'Develop selected' button", b.count("Develop selected") == 1)
chk("no read-only 'develop →' links remain", "develop &rarr;" not in b)

# --- focusing a concrete tab scopes the form to THAT app ---
s = client.get("/tickets?app=automatixy").get_data(as_text=True)
chk("per-tab view keeps checkboxes + one form", "type=checkbox name=ticket" in s and s.count("action=/api/run-selected") == 1)
chk("per-tab form targets that app", 'name=app value="automatixy"' in s)

# --- "Select all" toggle: one per run form, scoped to its own form, never itself submittable ---
chk("exactly one 'Select all' (single form)", b.count(">Select all<") == 1, str(b.count(">Select all<")))
chk("select-all toggles ticket boxes in its own form via JS", "querySelectorAll('input[name=ticket]')" in b)
chk("select-all is nameless -> not submitted as a ticket (still 4 ticket boxes)", b.count("type=checkbox name=ticket") == 4)
chk("per-tab: exactly one 'Select all'", s.count(">Select all<") == 1, str(s.count(">Select all<")))

print("\n============ CHOOSE-TICKETS MULTI-SELECT QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
