"""Scrum Master QA: PM triage gains a SPLIT verdict; the Scrum Master parses sub-ticket blocks, FILES
them via the backlog (labelled auto-split), and closes the parent (comment + → Done)."""
import sys, types, asyncio
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import scrum, pm
from orchestrator import recon as _recon
import orchestrator.backlog.base as backlog_base
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- PM triage now understands SPLIT (and the other two still parse) ---
chk("triage parses SPLIT", pm.parse_triage("it spans 30 files.\nTRIAGE: SPLIT")["action"] == "SPLIT")
chk("triage RESOLVE still", pm.parse_triage("x\nTRIAGE: RESOLVE")["action"] == "RESOLVE")
chk("triage ESCALATE still", pm.parse_triage("x\nTRIAGE: ESCALATE")["action"] == "ESCALATE")
chk("triage unclear -> ESCALATE", pm.parse_triage("dunno")["action"] == "ESCALATE")

# --- parse_subtickets ---
TXT = """=== TICKET ===
TITLE: Rename officers in ORG.md
What: apply the rename map in ORG.md.
Where: ORG.md
AC: zero old names remain.

=== TICKET ===
TITLE: Rename officers in ROSTER.md + Mermaid
body two here"""
subs = scrum.parse_subtickets(TXT)
chk("parses two sub-tickets", len(subs) == 2)
chk("first title", subs[0]["title"] == "Rename officers in ORG.md")
chk("first body carried (What/Where)", "ORG.md" in subs[0]["body"] and "AC:" in subs[0]["body"])
chk("empty input -> []", scrum.parse_subtickets("") == [])
chk("block with no TITLE is skipped", scrum.parse_subtickets("=== TICKET ===\njust prose, no title") == [])

# --- split(): stub the officer + the backlog, assert it files + closes the parent ---
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV", protected_branch="MAIN",
                             backlog_backend="jira", backlog={"base_url": "x", "project_key": "AUTO"})],
             audit_path="/tmp/scrum_a.jsonl")
async def fake_officer(**kw):
    return TXT
_recon.run_officer = fake_officer

filed, commented, statused, bodies = [], [], [], []
class FakeBL:
    # EU-301: create_task now takes issue_type (Epic vs Task) + parent (epic link).
    def create_task(s, summary, description, labels=None, issue_type="Task", priority=None, parent=None):
        if issue_type == "Epic":
            filed.append((summary, labels, "Epic", None)); bodies.append(description); return "AUTO-EPIC"
        filed.append((summary, labels, "Task", parent)); bodies.append(description)
        return f"AUTO-{100 + len([f for f in filed if f[2] == 'Task'])}"
    def add_comment(s, ticket, body): commented.append(body)
    def set_status(s, ticket, st): statused.append(st)
backlog_base.make_backlog = lambda app: FakeBL()

parent = types.SimpleNamespace(id="EU-17", summary="rename the officers", description="rename all",
                               acceptance_criteria=["all officers renamed"], ephemeral=False)
res = asyncio.run(scrum.split(cfg, "automatixy", parent, recap="tried twice", reason="spans the whole repo"))

_tasks = [f for f in filed if f[2] == "Task"]
chk("split ok", res["ok"] is True)
# EU-301: an EPIC is created + the 2 work pieces + a mandatory verify child = 3 Task children.
chk("an Epic is created", any(f[2] == "Epic" for f in filed) and res.get("epic") == "AUTO-EPIC")
chk("filed 2 work pieces + 1 verify child = 3 children", len(_tasks) == 3 and len(res["keys"]) == 3,
    f"tasks={len(_tasks)} keys={len(res['keys'])}")
chk("a Verify & close child is appended", any("Verify & close" in f[0] for f in _tasks),
    str([f[0] for f in _tasks]))
chk("children linked to the Epic via parent", all(f[3] == "AUTO-EPIC" for f in _tasks),
    str([f[3] for f in _tasks]))
chk("children labelled auto-split", all("auto-split" in (f[1] or []) for f in _tasks))
chk("parent comment names the Epic + children", commented and "AUTO-EPIC" in commented[0])
chk("parent moved out of the queue (Done)", "Done" in statused)
# EU-300/EU-301 (supersedes the 2026-07-08 "fragments → In Progress" rule): children start in To Do;
# the drain marks In Development on ticket_start, so ≤1 child per board is ever In Development.
chk("children are NOT pre-marked In Progress (start To Do)",
    statused.count("In Progress") == 0, str(statused))
# a fresh parent (split-depth 0) → fragments stamped <!-- autosplit-depth: 1 --> so re-splits are bounded.
# HTML-comment marker (fleet fix): invisible in Jira + a human ticket body is very unlikely to type it.
chk("fragment body carries an incremented autosplit-depth: 1 marker",
    any("<!-- autosplit-depth: 1 -->" in b for b in bodies), str(bodies)[:200])
chk("fragment body carries EXACTLY ONE depth marker (echoed ones stripped — no accretion)",
    all(b.count("autosplit-depth") <= 1 for b in bodies), str(bodies)[:200])
# accumulation guard (fleet MAJOR): a body that somehow carries TWO markers must count the DEEPER one,
# else a stale lower marker read first would defeat the bound. _split_depth reads the max.
chk("depth read is the MAX marker, not the first (accumulation can't under-count)",
    scrum._split_depth(types.SimpleNamespace(
        description="x <!-- autosplit-depth: 1 --> y <!-- autosplit-depth: 3 -->")) == 3)

# --- recursion guard: a ticket ALREADY at the max split depth must NOT re-split — it parks (ok=False)
#     instead of fanning out into ever-more sub-tickets forever (adversarial-fleet finding, 2026-07-08). ---
filed.clear(); bodies.clear(); commented.clear(); statused.clear()
_deep = types.SimpleNamespace(id="EU-17d", summary="x",
                              description="already decomposed <!-- autosplit-depth: 3 -->", ephemeral=False)
_rd = asyncio.run(scrum.split(cfg, "automatixy", _deep, reason="a single irreducible action, still too big"))
chk("depth guard: at max split depth → ok=False (parks, no unbounded re-split)", _rd["ok"] is False, str(_rd))
chk("depth guard: error names the max-depth ceiling", "max auto-split depth" in (_rd.get("error") or ""),
    str(_rd.get("error")))
chk("depth guard: NOTHING is filed and the parent is left alone at max depth",
    filed == [] and statused == [], f"filed={filed} statused={statused}")
# spoof-resistance (fleet MINOR): a plain "[split-depth: 3]" in a legit body is NOT the machine marker,
# so it does NOT trip the guard — only the HTML-comment marker counts.
chk("a plain-text '[split-depth: 3]' in a legit body does NOT spoof the guard (depth 0)",
    scrum._split_depth(types.SimpleNamespace(description="AC: verify [split-depth: 3] handling")) == 0)

# --- no sub-tickets -> ok=False, nothing filed, parent left alone ---
async def empty_officer(**kw): return "no ticket blocks here"
_recon.run_officer = empty_officer
filed.clear(); commented.clear(); statused.clear()
res2 = asyncio.run(scrum.split(cfg, "automatixy", parent, reason="x"))
chk("no fragments -> not ok, nothing filed, parent untouched", (not res2["ok"]) and not filed and not statused)

print("\n============ SCRUM MASTER QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
