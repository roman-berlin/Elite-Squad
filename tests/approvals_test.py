"""QA for the Approvals inbox — pending detection, approve (apply+push), disapprove (+ log)."""
import asyncio, sys, tempfile, types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator import approvals, notify, council
from orchestrator.config import Config, AppConfig
notify.send = lambda *a, **k: None
approvals._commit_push = lambda msg: "committed + pushed to origin"   # don't touch real git
notes = []
council.add_commander_note = lambda cfg, txt: notes.append(txt)

d = Path(tempfile.mkdtemp())
(d / "audit.jsonl").write_text("")
app = AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV", protected_branch="MAIN",
                backlog_backend="none")
cfg = Config(apps=[app], audit_path=str(d / "audit.jsonl"), use_worktree=False)

check("no reports -> nothing pending", approvals.pending(cfg) == [])

# EU-325: adjutant.py (and its "adjutant" KINDS entry) is gone — KINDS is empty until an officer
# opts back into this single-report approval flow. Register a synthetic kind so pending/approve/
# disapprove still exercise the real KINDS-driven dispatch, generically, with no officer module
# hardcoded.
async def _fake_apply(cfg): return "Applied. edited officers/engineer.md"
approvals.KINDS = {"test_kind": ("Test Officer — test action", "test_kind-report.md", _fake_apply)}

(d / "test_kind-report.md").write_text("## Propose: recruit a DB soldier", encoding="utf-8")
pend = approvals.pending(cfg)
check("a test_kind report -> 1 pending (test_kind)", len(pend) == 1 and pend[0]["kind"] == "test_kind", str(pend))

res = asyncio.run(approvals.approve(cfg, "test_kind"))
check("approve applies + pushes", "edited officers" in res and "pushed" in res, res[:80])
check("approved report is no longer pending", approvals.pending(cfg) == [])

(d / "test_kind-report.md").write_text("## DIFFERENT proposal: retire a stale specialist", encoding="utf-8")
check("a new/changed report becomes pending again", len(approvals.pending(cfg)) == 1)

# A kind not registered in KINDS is simply rejected, no import of any officer module.
res_drill = asyncio.run(approvals.approve(cfg, "drill"))
check("approve('drill') is rejected as an unknown kind (no officer module imported)",
      res_drill == "unknown approval kind: drill", res_drill)

approvals.disapprove(cfg, "test_kind", "roster is fine for now")
check("disapprove clears test_kind from pending", all(p["kind"] != "test_kind" for p in approvals.pending(cfg)))
check("disapprove logs the reason to Unit Memory", any("roster is fine" in n for n in notes), str(notes))

print("\n================ APPROVALS QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
