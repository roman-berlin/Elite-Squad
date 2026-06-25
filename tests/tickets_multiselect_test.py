"""Choose-tickets QA: the All-projects view must let you MULTI-SELECT tickets (checkboxes), not just
read-only 'develop →' links. A single run targets one app/Jira, so each project is its OWN checkbox form
with its own 'Develop selected in <app>' button — multi-select within a project, separate runs across."""
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

# --- All-projects: checkbox forms per project (multi-select), NOT read-only links ---
b = client.get("/tickets?app=*").get_data(as_text=True)
chk("all-projects view has a checkbox per ticket (multi-select restored)", b.count("type=checkbox name=ticket") == 4, str(b.count("type=checkbox name=ticket")))
chk("one run form per project (2)", b.count("action=/api/run-selected") == 2)
chk("each form targets its own app — Elite-Unit", 'name=app value="Elite-Unit"' in b)
chk("each form targets its own app — automatixy", 'name=app value="automatixy"' in b)
chk("a per-project 'Develop selected in <app>' button", b.count("Develop selected in") == 2)
chk("no read-only 'develop →' links remain", "develop &rarr;" not in b)

# --- single-project view still works (one form, checkboxes) ---
s = client.get("/tickets?app=automatixy").get_data(as_text=True)
chk("single-project view keeps checkboxes + one form", "type=checkbox name=ticket" in s and s.count("action=/api/run-selected") == 1)
chk("single-project form targets that app", 'name=app value="automatixy"' in s)

# --- "Select all" toggle: one per run form, scoped to its own form, never itself submittable ---
chk("all-projects: a 'Select all' per project form (2)", b.count(">Select all<") == 2, str(b.count(">Select all<")))
chk("select-all toggles ticket boxes in its own form via JS", "querySelectorAll('input[name=ticket]')" in b)
chk("select-all is nameless -> not submitted as a ticket (still 4 ticket boxes)", b.count("type=checkbox name=ticket") == 4)
chk("single-project: exactly one 'Select all'", s.count(">Select all<") == 1, str(s.count(">Select all<")))

print("\n============ CHOOSE-TICKETS MULTI-SELECT QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
