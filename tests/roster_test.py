"""Roster QA: deterministic structure (officers + chain-of-command chart from the code),
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

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

cfg = Config(apps=[], audit_path=str(Path(tempfile.mkdtemp()) / "audit.jsonl"))

# --- structure is read from the code (can't drift) ---
doc = roster.build_doc(cfg, "Shipped 3 tickets to DEV today.")
# EU-260: the Test Engineer left this list when it left the code — c276155 (Phase-2 §2) deleted
# test_engineer.py + officers/test-engineer.md and dropped the Tests phase, but the roster row survived
# and re-emitted the retired officer into ROSTER.md daily. tests/eu260_org_reality_test.py pins that it
# stays gone (and that the Engineering Manager, still seated on the council, does NOT).
for officer in ["CTO", "Engineering Manager", "Product Manager", "Dev Team Lead", "Code Reviewer",
                "QA Engineer", "Security Engineer", "Release Manager", "SRE",
                "Scrum Master"]:
    chk(f"doc lists {officer}", officer in doc)
chk("doc does NOT list the retired Test Engineer (EU-260)", "Test Engineer" not in doc)
# EU-327 (2026-07-17): the Engineering Coach (drillmaster) is retired — drill()/apply() had zero
# production callers after EU-323/EU-331 relocated its load-bearing pieces (signals, doctrine).
chk("doc does NOT list the retired Engineering Coach (EU-327)", "Engineering Coach" not in doc)
# 2026-07-19 stabilization: the Mayor (liaison) phantom row is gone — liaison.py was deleted in
# 40da120 (Phase-2 §2) but the roster row survived; the retired-subsystems guard now pins it out.
chk("doc does NOT list the retired Mayor / liaison", "Mayor" not in doc and "Inter-unit Ambassador" not in doc)
chk("doc carries the status line", "Shipped 3 tickets to DEV today." in doc)
chk("doc dated 'As of'", "_As of" in doc)

# --- mermaid hierarchy chart ---
mer = roster.mermaid_chart()
chk("chart is mermaid flowchart", mer.startswith("```mermaid") and "flowchart TD" in mer)
chk("chart roots at the Commander -> CTO", "Commander · Roman" in mer and "G[CTO" in mer)
chk("chart hangs every officer off the General", mer.count("G --> ") == 9)   # 10 officers minus the General (EU-110 adds Scrum Master; EU-260 retires the Test Engineer; EU-327 retires the Engineering Coach; 2026-07-19 retires the Mayor / liaison row)

# --- model column is auto-aware ---
fixed = Config(apps=[], audit_path="/tmp/x.jsonl", auto_model=False, builder_model="claude-opus-4-8")
auto = Config(apps=[], audit_path="/tmp/x.jsonl", auto_model=True, builder_model="claude-opus-4-8")
chk("model fixed -> plain name", roster._model_for(fixed, "builder_model") == "opus")
chk("model auto -> marked (auto)", roster._model_for(auto, "builder_model") == "opus (auto)")
chk("deterministic officer (Sentinel) shows no model", roster._model_for(cfg, None) == "—")

# --- F15 regression: the roster's model label must match the model each officer actually runs on ---
# The recon officers (QA Engineer/Security Engineer/Release Manager) run under cfg.reviewer_model (Opus),
# not discussion_model. EU-52 routed them through the economical ladder (models.for_officer), whose
# ceiling DEFAULTS to reviewer_model — so the roster's "reviewer_model" label still matches reality.
# We verify against the officer source (routes through the ladder, and its ceiling is not pointed off
# reviewer_model) so doc-vs-code can't drift again.
_attr = {name: mattr for name, _role, _duty, mattr in roster._OFFICERS}
_recon_src = {"QA Engineer": "scout", "Security Engineer": "provost", "Release Manager": "quartermaster"}
for _name, _mod in _recon_src.items():
    chk(f"{_name} roster label says reviewer_model", _attr[_name] == "reviewer_model", _attr[_name])
    _src = (Path("orchestrator") / f"{_mod}.py").read_text(encoding="utf-8")
    chk(f"{_name} source runs under the reviewer ceiling via the ladder (models.for_officer)",
        "models.for_officer(" in _src
        and ("ceiling_model=" not in _src or "ceiling_model=cfg.reviewer_model" in _src), _name)
# and the labelled model resolves to Opus, not Sonnet, when the two configs differ
_drift = Config(apps=[], audit_path="/tmp/x.jsonl", auto_model=False, discussion_model="claude-sonnet-4-5",
                reviewer_model="claude-opus-4-8")
for _name in _recon_src:
    chk(f"{_name} model column shows opus (recon model)",
        roster._model_for(_drift, _attr[_name]) == "opus", roster._model_for(_drift, _attr[_name]))

# --- html cockpit view ---
html = roster.html_view(cfg, "all quiet")
chk("html view renders the tree", "Chain of command" in html and "CTO" in html)
chk("html view renders the engineer table", "Engineers &amp; duties" in html and "Security Engineer" in html)
chk("html view shows the status", "all quiet" in html)
chk("html view escapes (no raw angle injection)", "<script>" not in roster.html_view(cfg, "<script>x"))

# --- write/read round-trip (refresh without an LLM) ---
async def _no_status(c): return "Quiet day — 2 merges, 0 parks."
roster._status_line = _no_status
p = asyncio.run(roster.refresh(cfg))
chk("refresh wrote ROSTER.md", p.exists() and p.name == "ROSTER.md")
chk("written doc has the chart + officers", "flowchart TD" in p.read_text() and "Scrum Master" in p.read_text())
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

# --- Roster nav button: EU-642 removed it (route + page above remain) ---
from orchestrator import cockpit_views, sync as _sync
_sync.can_promote = lambda: False   # keep the bar off git/network
bar = cockpit_views._control_bar(scfg, "automatixy")
chk("EU-642: Roster button is gone from the top nav bar", 'href="/roster-doc"' not in bar,
    "the retired /roster-doc nav link is back in the control bar")
chk("EU-642: no Roster label/title left in the bar",
    "&#128101; Roster" not in bar and "Engineers" not in bar,
    "leftover Roster button markup in the control bar")

print("\n================== ROSTER QA ==================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
