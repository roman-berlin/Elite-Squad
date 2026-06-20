"""Roster QA: deterministic structure (officers + soldiers + chain-of-command chart from the code),
duties, model column (auto-aware), the cheap daily status line, write/read round-trip, cockpit view."""
import sys, types, tempfile, asyncio
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import roster
from orchestrator.config import Config, AppConfig
from orchestrator.squad import SQUAD

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

cfg = Config(apps=[], audit_path=str(Path(tempfile.mkdtemp()) / "audit.jsonl"))

# --- structure is read from the code (can't drift) ---
doc = roster.build_doc(cfg, "Shipped 3 tickets to DEV today.")
for officer in ["The General", "Adjutant", "Product Manager", "Field Engineer", "Inspector General",
                "Scout", "Provost Marshal", "Quartermaster", "Sentinel", "Drillmaster"]:
    chk(f"doc lists {officer}", officer in doc)
chk("doc lists every soldier from squad.SQUAD",
    all(label in doc for label, _ in SQUAD.values()), str(list(SQUAD)))
chk("doc carries the status line", "Shipped 3 tickets to DEV today." in doc)
chk("doc dated 'As of'", "_As of" in doc)

# --- mermaid hierarchy chart ---
mer = roster.mermaid_chart()
chk("chart is mermaid flowchart", mer.startswith("```mermaid") and "flowchart TD" in mer)
chk("chart roots at the Commander -> General", "Commander · Roman" in mer and "G[The General" in mer)
chk("chart hangs every officer off the General", mer.count("G --> ") == 9)   # 10 officers minus the General
chk("chart hangs soldiers off the Field Engineer", mer.count("FE --> S") == len(SQUAD))

# --- model column is auto-aware ---
fixed = Config(apps=[], audit_path="/tmp/x.jsonl", auto_model=False, builder_model="claude-opus-4-8")
auto = Config(apps=[], audit_path="/tmp/x.jsonl", auto_model=True, builder_model="claude-opus-4-8")
chk("model fixed -> plain name", roster._model_for(fixed, "builder_model") == "opus")
chk("model auto -> marked (auto)", roster._model_for(auto, "builder_model") == "opus (auto)")
chk("deterministic officer (Sentinel) shows no model", roster._model_for(cfg, None) == "—")

# --- html cockpit view ---
html = roster.html_view(cfg, "all quiet")
chk("html view renders the tree", "Chain of command" in html and "The General" in html)
chk("html view renders the officer table", "Officers &amp; duties" in html and "Provost Marshal" in html)
chk("html view renders soldiers", "Vanguard FE" in html and "Sapper" in html)
chk("html view shows the status", "all quiet" in html)
chk("html view escapes (no raw angle injection)", "<script>" not in roster.html_view(cfg, "<script>x"))

# --- write/read round-trip (refresh without an LLM) ---
async def _no_status(c): return "Quiet day — 2 merges, 0 parks."
roster._status_line = _no_status
p = asyncio.run(roster.refresh(cfg))
chk("refresh wrote ROSTER.md", p.exists() and p.name == "ROSTER.md")
chk("written doc has the chart + officers", "flowchart TD" in p.read_text() and "Drillmaster" in p.read_text())
chk("latest_status reads the status back", roster.latest_status(cfg) == "Quiet day — 2 merges, 0 parks.")

# --- cockpit /roster-doc route ---
from orchestrator import server
repo = Path(tempfile.mkdtemp()) / "app"; repo.mkdir()
scfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(repo), base_branch="DEV",
                              protected_branch="MAIN", backlog_backend="none")],
              audit_path=str(repo / "audit.jsonl"), use_worktree=False)
scfg.detected_auth = lambda: "test"
r = server.create_app(scfg).test_client().get("/roster-doc")
chk("/roster-doc returns 200", r.status_code == 200, str(r.status_code))
chk("/roster-doc shows the roster", "Chain of command" in r.get_data(as_text=True))

print("\n================== ROSTER QA ==================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
