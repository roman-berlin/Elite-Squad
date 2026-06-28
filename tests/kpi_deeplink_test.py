"""KPI deep-link QA (EU-32): the War Room KPI cards must link to a view SCOPED to the count they
show. Parked -> /tasks?filter=parked (only the parked subset), Needs/Merged -> the matching subset,
and Security blocks -> a security-scoped forensics view (not the unrelated /council)."""
import sys, types, tempfile, json
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

from orchestrator import warroom, server
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace
tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"
_t = [0]
def ev(**kw):
    _t[0] += 1
    kw.setdefault("ts", f"2026-06-22T09:{_t[0]:02d}:00")
    with audit.open("a") as f:
        f.write(json.dumps(kw) + "\n")
def run(tid, term, app="automatixy", **extra):
    ev(event="ticket_start", ticket_id=tid, app=app)
    ev(event=term, ticket_id=tid, app=app, **extra)

run("AUTO-100", "merged")                                                   # merged -> DEV
run("AUTO-200", "pr_opened", note="Provost flagged a CRITICAL security vuln")  # security block
run("AUTO-300", "ticket_exception", error="boom")                           # errored; also parked below
run("AUTO-400", "dryrun_land")                                              # dry-run: neither merged nor needs-you
# AUTO-300 is the auto-skipped/parked ticket.
(tmp / "blocked_tickets.json").write_text(json.dumps({"AUTO-300": "stuck"}), encoding="utf-8")

scfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev",
                              protected_branch="main", backlog_backend="none")],
              audit_path=str(audit), use_worktree=False)
scfg.detected_auth = lambda: "test"

# --- 1. KPI cards point at scoped destinations (not the flat log / unrelated councils) ---
cards = {c["label"]: c.get("href") for c in warroom.kpis(scfg, warroom.D.load_tasks(str(audit)), None)}
chk("Parked card -> /tasks?filter=parked", cards.get("Parked") == "/tasks?filter=parked", str(cards.get("Parked")))
chk("Needs you card -> /needs (unified inbox)", cards.get("Needs you") == "/needs", str(cards.get("Needs you")))
chk("Merged total card -> /tasks?filter=merged", cards.get("Merged total") == "/tasks?filter=merged", str(cards.get("Merged total")))
chk("Security blocks card -> security-scoped forensics",
    cards.get("Security blocks") == "/forensics?cat=security_block", str(cards.get("Security blocks")))
chk("Security blocks no longer points at /council", cards.get("Security blocks") != "/council")

client = server.create_app(scfg).test_client()

# --- 2. /tasks?filter=parked shows ONLY the parked subset ---
body = client.get("/tasks?filter=parked").get_data(as_text=True)
chk("parked view: shows the parked ticket", "AUTO-300" in body)
chk("parked view: hides the non-parked merged ticket", "AUTO-100" not in body)
chk("parked view: banner names the active filter", "Parked" in body and "show all" in body)

# --- 3. /tasks?filter=merged shows ONLY merged ---
body = client.get("/tasks?filter=merged").get_data(as_text=True)
chk("merged view: shows the merged ticket", "AUTO-100" in body)
chk("merged view: hides a non-merged ticket", "AUTO-400" not in body)

# --- 4. unfiltered /tasks still shows everything ---
body = client.get("/tasks").get_data(as_text=True)
chk("unfiltered /tasks shows all tickets", "AUTO-100" in body and "AUTO-400" in body)

# --- 5. /forensics?cat=security_block is a security-scoped view ---
r = client.get("/forensics?cat=security_block"); body = r.get_data(as_text=True)
chk("/forensics?cat= returns 200", r.status_code == 200, str(r.status_code))
chk("security view: titled for the security finding", "Security finding" in body)
chk("security view: lists the security-blocked ticket", "AUTO-200" in body)
chk("security view: excludes the merged ticket", "AUTO-100" not in body)

print("\n============ KPI DEEP-LINK QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
