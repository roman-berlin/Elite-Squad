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
    mode = "file"   # "file" -> create new; "dedupe" -> all already-open; "fail" -> create_task raises
    def find_open_by_summary(self, title):
        return "AUTO-999" if FakeBacklog.mode == "dedupe" else None
    def create_task(self, title, body, labels=None, issue_type="Task", priority=None):
        if FakeBacklog.mode == "fail":
            raise RuntimeError("403 Forbidden — bad token")
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

# ===================== full patrol, do_file=True — genuinely NEW findings =====================
FakeBacklog.mode = "file"
FakeBacklog.created = 0
audit = FakeAudit()
summary = asyncio.run(patrol.patrol(cfg, "automatixy", do_file=True, audit=audit))
check("summary names all three officers",
      all(x in summary for x in ("QA Engineer", "Security Engineer", "Release Manager")))
check("NEW path: summary shows 'N new' counts (2 + 1) and a clean one — never 'filed N'",
      "2 new" in summary and "1 new" in summary and "clean" in summary and "filed 2" not in summary, summary)
check("filed exactly 3 tickets (Scout 2 + Provost 1; QM 0)", FakeBacklog.created == 3, str(FakeBacklog.created))
check("audit records one patrol event w/ findings=3, filed=3, deduped=0, failed=0",
      sum(1 for e, _ in audit.events if e == "patrol") == 1
      and audit.events[0][1].get("findings") == 3 and audit.events[0][1].get("filed") == 3
      and audit.events[0][1].get("deduped") == 0 and audit.events[0][1].get("failed") == 0,
      str(audit.events))
check("EU-579: summary carries the REAL filed Jira keys (the cockpit qa_findings source)",
      isinstance(summary, str) and getattr(summary, "filed", None) == ["AUTO-101", "AUTO-102", "AUTO-103"],
      repr(getattr(summary, "filed", None)))
scout_md = (d / "scout-report.md").read_text(encoding="utf-8")
check("officer report written, ===TICKETS=== block stripped",
      "Scout recon of DEV" in scout_md and "===TICKETS===" not in scout_md)
check("all three report files written",
      all((d / f).exists() for f in ("scout-report.md", "provost-report.md", "quartermaster-report.md")))

# ===================== resilience: Provost throws =====================
FakeBacklog.created = 0
_fail["who"] = "provost"
summary2 = asyncio.run(patrol.patrol(cfg, "automatixy", do_file=True, audit=FakeAudit()))
check("a failing officer doesn't abort the patrol", "patrol error" in summary2 and "Security Engineer" in summary2)
check("other officers still ran + filed (Scout's 2)", FakeBacklog.created == 2, str(FakeBacklog.created))
check("QA Engineer + Release Manager still reported", "QA Engineer" in summary2 and "Release Manager" in summary2)
_fail["who"] = None

# ===================== officers subset =====================
FakeBacklog.created = 0
summary3 = asyncio.run(patrol.patrol(cfg, "automatixy", officers=["scout"], do_file=True, audit=FakeAudit()))
check("subset runs only the chosen officer", "QA Engineer" in summary3 and "Security Engineer" not in summary3
      and "Release Manager" not in summary3)
check("subset filed only Scout's 2", FakeBacklog.created == 2, str(FakeBacklog.created))

# ===================== propose-only (do_file=False) =====================
FakeBacklog.mode = "file"
FakeBacklog.created = 0
audit4 = FakeAudit()
summary4 = asyncio.run(patrol.patrol(cfg, "automatixy", do_file=False, audit=audit4))
check("propose-only creates NO tickets", FakeBacklog.created == 0, str(FakeBacklog.created))
check("propose-only says 'proposed' + audit filed=0",
      "proposed 2" in summary4 and audit4.events[0][1].get("filed") == 0, summary4)

# ===================== DEDUPE path: every finding already open =====================
# AC: reports "0 new · N already-open" (NOT "filed N"); audit filed:0, deduped:N.
FakeBacklog.mode = "dedupe"
FakeBacklog.created = 0
audit5 = FakeAudit()
summary5 = asyncio.run(patrol.patrol(cfg, "automatixy", officers=["scout"], do_file=True, audit=audit5))
check("DEDUPE: creates NO new tickets", FakeBacklog.created == 0, str(FakeBacklog.created))
check("DEDUPE: reports 'already-open', never 'new' or 'filed'",
      "already-open" in summary5 and " new" not in summary5 and "filed" not in summary5, summary5)
check("DEDUPE: audit filed=0, deduped=2, failed=0",
      audit5.events[0][1].get("filed") == 0 and audit5.events[0][1].get("deduped") == 2
      and audit5.events[0][1].get("failed") == 0, str(audit5.events))
check("EU-579: DEDUPE created nothing new — .filed stays empty (no stale keys surface)",
      getattr(summary5, "filed", None) == [], repr(getattr(summary5, "filed", None)))

# ===================== FAILURE path: create_task raises =====================
# AC: reports "✗ failed", escalates to Telegram + Needs-you; audit failed:>=1.
FakeBacklog.mode = "fail"
FakeBacklog.created = 0
sent = []
notify.send = lambda text, *a, **k: sent.append(text)
audit6 = FakeAudit()
summary6 = asyncio.run(patrol.patrol(cfg, "automatixy", officers=["scout"], do_file=True, audit=audit6))
notify.send = lambda *a, **k: None  # restore quiet default
check("FAILURE: creates NO tickets", FakeBacklog.created == 0, str(FakeBacklog.created))
check("FAILURE: summary marks '✗' failed (Scout's 2)",
      "✗ 2 failed" in summary6, summary6)
check("FAILURE: audit failed>=1 (and filed=0)",
      audit6.events[0][1].get("failed") == 2 and audit6.events[0][1].get("filed") == 0, str(audit6.events))
check("FAILURE: a DISTINCT escalation Telegram is sent",
      any("FAILED" in s for s in sent), str(sent))
from orchestrator import decisions as _dec
_pending = _dec.load(cfg)
check("FAILURE: surfaced in Needs-you (a pending decision exists)",
      any("filing-failure" in (i.get("id") or "") for i in _pending), str(_pending))
_dec._save(cfg, [])  # clean up so it doesn't leak into other surfaces

# ===================== report =====================
print("\n================ SCHEDULED PATROL QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if (detail and not ok) else ""))
print("----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAILURE(S) ❌")
