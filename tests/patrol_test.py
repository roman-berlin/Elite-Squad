"""QA for scheduled patrols — runs all three recon officers, files findings, and is resilient to
one officer failing. Stubs the officers + the backlog so nothing real runs. PASS/FAIL checklist."""
import asyncio, sys, tempfile, types
from pathlib import Path

REPO = "."
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, REPO)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))

from orchestrator import patrol, filing, notify
from orchestrator.config import Config, AppConfig

notify.send = lambda *a, **k: None  # no Telegram in tests

# canned officer reports: Scout 2 findings, Provost 1, QM 0
REPORTS = {
    "scout": ('Scout recon of DEV.\nRuntime and a11y mostly fine.\n'
              '===TICKETS===\n[{"title":"Fix cut-off column on /leads","type":"Bug","severity":"HIGH","body":"x"},'
              '{"title":"Missing alt text on hero","type":"Task","severity":"MEDIUM","body":"y"}]\n===END==='),
    "provost": ('Provost security recon.\nNo secrets in the diff.\n'
                '===TICKETS===\n[{"title":"Add authz check on export endpoint","type":"Task","severity":"HIGH","body":"z"}]\n===END==='),
    "quartermaster": ('Quartermaster readiness.\nDEV builds green; migrations applied.\n'
                      '===TICKETS===\n[]\n===END==='),
}
_fail = {"who": None}
async def fake_inspect(key, cfg, app_name, audit=None):
    if _fail["who"] == key:
        raise RuntimeError("officer boom")
    return REPORTS[key]
patrol._inspect = fake_inspect

class FakeBacklog:
    created = 0
    def find_open_by_summary(self, title): return None
    def create_task(self, title, body, labels=None, issue_type="Task"):
        FakeBacklog.created += 1
        return f"AUTO-{100 + FakeBacklog.created}"
filing.make_backlog = lambda app: FakeBacklog()

class FakeAudit:
    def __init__(self): self.events = []
    def record(self, event, **kw): self.events.append((event, kw))

d = Path(tempfile.mkdtemp())
(d / "audit.jsonl").write_text("", encoding="utf-8")
app = AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV", protected_branch="MAIN",
                backlog_backend="none")
cfg = Config(apps=[app], audit_path=str(d / "audit.jsonl"), use_worktree=False)

# ===================== full patrol, do_file=True =====================
FakeBacklog.created = 0
audit = FakeAudit()
summary = asyncio.run(patrol.patrol(cfg, "automatixy", do_file=True, audit=audit))
check("summary names all three officers",
      all(x in summary for x in ("Scout", "Provost Marshal", "Quartermaster")))
check("summary shows filed counts (2 + 1) and a clean one",
      "filed 2" in summary and "filed 1" in summary and "clean" in summary, summary)
check("filed exactly 3 tickets (Scout 2 + Provost 1; QM 0)", FakeBacklog.created == 3, str(FakeBacklog.created))
check("audit records one patrol event w/ findings=3, filed=True",
      sum(1 for e, _ in audit.events if e == "patrol") == 1
      and audit.events[0][1].get("findings") == 3 and audit.events[0][1].get("filed") is True,
      str(audit.events))
scout_md = (d / "scout-report.md").read_text(encoding="utf-8")
check("officer report written, ===TICKETS=== block stripped",
      "Scout recon of DEV" in scout_md and "===TICKETS===" not in scout_md)
check("all three report files written",
      all((d / f).exists() for f in ("scout-report.md", "provost-report.md", "quartermaster-report.md")))

# ===================== resilience: Provost throws =====================
FakeBacklog.created = 0
_fail["who"] = "provost"
summary2 = asyncio.run(patrol.patrol(cfg, "automatixy", do_file=True, audit=FakeAudit()))
check("a failing officer doesn't abort the patrol", "patrol error" in summary2 and "Provost Marshal" in summary2)
check("other officers still ran + filed (Scout's 2)", FakeBacklog.created == 2, str(FakeBacklog.created))
check("Scout + Quartermaster still reported", "Scout" in summary2 and "Quartermaster" in summary2)
_fail["who"] = None

# ===================== officers subset =====================
FakeBacklog.created = 0
summary3 = asyncio.run(patrol.patrol(cfg, "automatixy", officers=["scout"], do_file=True, audit=FakeAudit()))
check("subset runs only the chosen officer", "Scout" in summary3 and "Provost Marshal" not in summary3
      and "Quartermaster" not in summary3)
check("subset filed only Scout's 2", FakeBacklog.created == 2, str(FakeBacklog.created))

# ===================== propose-only (do_file=False) =====================
FakeBacklog.created = 0
audit4 = FakeAudit()
summary4 = asyncio.run(patrol.patrol(cfg, "automatixy", do_file=False, audit=audit4))
check("propose-only creates NO tickets", FakeBacklog.created == 0, str(FakeBacklog.created))
check("propose-only says 'proposed' + audit filed=False",
      "proposed 2" in summary4 and audit4.events[0][1].get("filed") is False, summary4)

# ===================== report =====================
print("\n================ SCHEDULED PATROL QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if (detail and not ok) else ""))
print("----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAILURE(S) ❌")
