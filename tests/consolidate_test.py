"""Memory-consolidation QA: dedup + prune the Lessons log, cluster recurring Reviewer rejections into
themes (counted once per ticket), fold idempotent lessons into the log, and the cockpit /memory surface."""
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

from orchestrator import consolidate, memory
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
memory.UNIT_PATH = tmp / "UNIT.md"
memory.LIVE_PATH = tmp / "UNIT.live.md"
memory._BACKUPS = tmp / "backups"
memory.UNIT_PATH.write_text("## Doctrine\n\n- Be excellent.\n", encoding="utf-8")

# --- consolidate_log: dedup (same lesson, different date) + prune ---
raw = ("## Lessons & Decisions\n\n"
       "- 2026-06-20: Always add an explicit tenant filter\n"
       "- 2026-06-19: always add an explicit tenant filter\n"          # dup (date + case differ)
       "- 2026-06-18: Write tests with the change\n")
block, rep = consolidate.consolidate_log(raw)
chk("dedup: duplicate lesson removed", rep["removed_dupes"] == 1, str(rep))
chk("dedup: two unique lessons kept", rep["kept"] == 2)
chk("dedup: newest of the dupes survives", "2026-06-20" in block and "2026-06-19" not in block)

many = "\n".join(f"- 2026-06-{(i % 28) + 1:02d}: distinct lesson {i}" for i in range(30))
_, rep2 = consolidate.consolidate_log(many, max_bullets=10)
chk("prune: capped to max_bullets", rep2["kept"] == 10 and rep2["pruned"] == 20, str(rep2))

# --- rejection_patterns: cluster FAIL reviews into themes, count DISTINCT tickets ---
audit = tmp / "audit.jsonl"
def review(tid, verdict, required=None, issues=None):
    return json.dumps({"event": "review", "ticket_id": tid, "verdict": verdict,
                       "required_changes": required or [], "issues": issues or []})
audit.write_text("\n".join([
    review("AUTO-1", "FAIL", ["Add an explicit business_id filter", "tenant_id missing on the 2nd query"]),
    review("AUTO-2", "FAIL", ["this query needs a tenant filter"]),
    review("AUTO-3", "FAIL", issues=[{"severity": "high", "area": "testing", "detail": "no regression test added"}]),
    review("AUTO-4", "FAIL", ["add unit tests for the new endpoint"]),
    review("AUTO-7", "FAIL", ["rename the variable; lint is failing"]),    # naming, only 1 ticket
    review("AUTO-8", "PASS", ["nit: tidy import"]),                         # PASS — ignored
]) + "\n", encoding="utf-8")
cfg = Config(apps=[], audit_path=str(audit))

pats = consolidate.rejection_patterns(cfg, min_count=2)
themes = {p["theme"]: p for p in pats}
chk("patterns: tenant isolation recurs", "tenant_isolation" in themes)
chk("patterns: counted once per ticket (AUTO-1's two tenant notes = 1)", themes["tenant_isolation"]["count"] == 2,
    str(themes.get("tenant_isolation", {}).get("count")))
chk("patterns: tickets listed", themes["tenant_isolation"]["tickets"] == ["AUTO-1", "AUTO-2"])
chk("patterns: missing tests recurs", "missing_tests" in themes and themes["missing_tests"]["count"] == 2)
chk("patterns: PASS reviews ignored", all("AUTO-8" not in p["tickets"] for p in pats))
chk("patterns: a one-off (naming) is NOT a pattern", "naming_style" not in themes)
chk("patterns: each carries a recommended action (drill proposals left with EU-327)",
    all(p["action"] and "drill" not in p for p in pats))
chk("patterns: sorted by count desc", [p["count"] for p in pats] == sorted([p["count"] for p in pats], reverse=True))

# --- run(): fold rejection lessons into the log, dedup/prune, write; idempotent ---
memory.LIVE_PATH.write_text("## Lessons & Decisions\n\n- 2026-06-10: Keep PRs small\n", encoding="utf-8")
r1 = consolidate.run(cfg)
chk("run: folded both recurring lessons in", len(r1["added"]) == 2, str(r1["added"]))
log = memory.LIVE_PATH.read_text()
chk("run: tenant lesson written", "Reviewer repeatedly required an explicit tenant filter" in log)
chk("run: tests lesson written", "Reviewer repeatedly required tests with the change" in log)
chk("run: cites the offending tickets", "AUTO-1" in log)
chk("run: pre-existing lesson preserved", "Keep PRs small" in log)
chk("run: wrote the file", r1["written"])
r2 = consolidate.run(cfg)
chk("run: idempotent — no duplicate lessons on re-run", r2["added"] == [])

# --- cockpit /memory (2026-07-19): the rejection panel + Consolidate button are GONE —
# consolidation runs automatically (autopilot cycle + scribe); the page keeps one action. ---
from orchestrator import server
scfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev",
                              protected_branch="main", backlog_backend="none")],
              audit_path=str(audit), use_worktree=False)
scfg.detected_auth = lambda: "test"
client = server.create_app(scfg).test_client()
r = client.get("/memory"); body = r.get_data(as_text=True)
chk("/memory returns 200", r.status_code == 200, str(r.status_code))
chk("/memory no longer renders the never-clearing rejection panel", "keeps rejecting" not in body)
chk("/memory no longer offers a Consolidate action", "/api/consolidate" not in body)
chk("the lessons the engine folds STILL reach the officers' preamble path",
    "Update memory" in body and "officers see the newest" in body)

print("\n============= CONSOLIDATION QA =============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
