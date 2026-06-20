"""Failure-forensics QA: classify failures into a taxonomy, count repeat offenders, auto-write a
deterministic post-mortem after N failures (and NOT before / NOT on success), and render /forensics."""
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

from orchestrator import forensics
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Outcome

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace
tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"
_t = [0]
def ev(**kw):
    _t[0] += 1
    kw.setdefault("ts", f"2026-06-21T10:{_t[0]:02d}:00")
    with audit.open("a") as f:
        f.write(json.dumps(kw) + "\n")
def run(tid, term, app="automatixy", **extra):
    ev(event="ticket_start", ticket_id=tid, app=app)
    ev(event=term, ticket_id=tid, app=app, **extra)

# AUTO-1: three escalations for "ran out of turns" -> too_big, repeat offender
for _ in range(3):
    run("AUTO-1", "needs_human", reason="ran out of turns — ticket too big for one pass")
run("AUTO-2", "ticket_exception", error="Traceback: SDK connection timeout")        # infra
run("AUTO-3", "pr_opened", note="could not merge — conflict on DEV")                  # merge_conflict
run("AUTO-4", "needs_human", reason="product blocker — escalated to Commander")       # product_blocker
run("AUTO-5", "merged")                                                               # success, ignored

cfg = Config(apps=[], audit_path=str(audit), postmortem_after=3)

# --- classify(): each cause from representative text ---
chk("classify: under-specified", forensics.classify("escalated", "not ready — handed back")["category"] == "not_ready")
chk("classify: too big", forensics.classify("escalated", "ran out of turns")["category"] == "too_big")
chk("classify: merge conflict", forensics.classify("PR / needs you", "could not merge — conflict")["category"] == "merge_conflict")
chk("classify: gate/review", forensics.classify("escalated", "Reviewer rejected: tsc typecheck failed")["category"] == "gate_fail")
chk("classify: security", forensics.classify("PR / needs you", "Provost flagged a CRITICAL vuln")["category"] == "security_block")
chk("classify: product blocker", forensics.classify("awaiting decision", "product blocker — escalated to Commander")["category"] == "product_blocker")
chk("classify: infra fallback for bare errored", forensics.classify("errored", "")["category"] == "infra")
chk("classify: unknown when nothing matches", forensics.classify("awaiting decision", "weird mystery state")["category"] == "unknown")
chk("classify: returns a recommended action", bool(forensics.classify("errored", "")["action"]))

# --- scan / taxonomy ---
sc = forensics.scan(cfg)
chk("scan: counts every failed run, skips the merged one", len(sc) == 6, str(len(sc)))
tax = {t["category"]: t["count"] for t in forensics.taxonomy(cfg)}
chk("taxonomy: too_big = 3", tax.get("too_big") == 3, str(tax))
chk("taxonomy: infra = 1", tax.get("infra") == 1)
chk("taxonomy: merge_conflict = 1", tax.get("merge_conflict") == 1)
chk("taxonomy: product_blocker = 1", tax.get("product_blocker") == 1)
chk("taxonomy: sorted most-common first", forensics.taxonomy(cfg)[0]["category"] == "too_big")

# --- counts + offenders ---
chk("fail_count AUTO-1 = 3", forensics.fail_count(cfg, "AUTO-1") == 3)
chk("fail_count is case-insensitive", forensics.fail_count(cfg, "auto-1") == 3)
chk("fail_count AUTO-5 (merged) = 0", forensics.fail_count(cfg, "AUTO-5") == 0)
off = forensics.repeat_offenders(cfg, threshold=2)
chk("repeat offenders: only AUTO-1", [o["ticket_id"] for o in off] == ["AUTO-1"], str(off))
chk("repeat offenders: dominant cause labelled", off and off[0]["category"] == "too_big")

# --- write_postmortem: deterministic content ---
p = forensics.write_postmortem(cfg, "AUTO-1")
chk("postmortem: file written under postmortems/", p and p.exists() and p.parent.name == "postmortems")
md = p.read_text()
chk("postmortem: names the ticket", "AUTO-1" in md)
chk("postmortem: states attempt count", "3 failed attempts" in md)
chk("postmortem: names the dominant cause", "Too big" in md)
chk("postmortem: has a timeline", "## Timeline" in md and md.count("- **") >= 3)
chk("postmortem: gives a recommended fix", "recommended fix" in md.lower())
chk("postmortem: none for a ticket with no failures", forensics.write_postmortem(cfg, "NOPE-9") is None)

# --- maybe_postmortem: trigger semantics ---
import os
pmpath = forensics.postmortem_path(cfg, "AUTO-1")
if pmpath.exists():
    os.remove(pmpath)
audit_calls = []
fake_audit = ns(record=lambda k, **kw: audit_calls.append((k, kw)))
r1 = forensics.maybe_postmortem(cfg, ns(ticket_id="AUTO-1", outcome=Outcome.ESCALATED), fake_audit)
chk("maybe: writes at threshold (3 fails, after=3)", r1 is not None and pmpath.exists())
chk("maybe: audits the post-mortem", any(k == "postmortem" for k, _ in audit_calls))
chk("maybe: NOT below threshold (AUTO-2 has 1 fail)",
    forensics.maybe_postmortem(cfg, ns(ticket_id="AUTO-2", outcome=Outcome.ERRORED), fake_audit) is None)
chk("maybe: NOT on a successful current run (merged)",
    forensics.maybe_postmortem(cfg, ns(ticket_id="AUTO-1", outcome=Outcome.MERGED), fake_audit) is None)
cfg_off = Config(apps=[], audit_path=str(audit), postmortem_after=0)
chk("maybe: OFF when postmortem_after=0",
    forensics.maybe_postmortem(cfg_off, ns(ticket_id="AUTO-1", outcome=Outcome.ESCALATED), fake_audit) is None)

# --- cockpit /forensics page + post-mortem view ---
from orchestrator import server
scfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev",
                              protected_branch="main", backlog_backend="none")],
              audit_path=str(audit), use_worktree=False, postmortem_after=3)
scfg.detected_auth = lambda: "test"
client = server.create_app(scfg).test_client()
r = client.get("/forensics"); body = r.get_data(as_text=True)
chk("/forensics returns 200", r.status_code == 200, str(r.status_code))
chk("/forensics shows the taxonomy", "Too big" in body and "Infra" in body)
chk("/forensics shows repeat offenders", "AUTO-1" in body and "Repeat offenders" in body)
chk("/forensics links the post-mortem", "/forensics?pm=AUTO-1" in body)
rp = client.get("/forensics?pm=AUTO-1"); pmbody = rp.get_data(as_text=True)
chk("/forensics?pm= renders the post-mortem", rp.status_code == 200 and "3 failed attempts" in pmbody)

print("\n=============== FORENSICS QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
