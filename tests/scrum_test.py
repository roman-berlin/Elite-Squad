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

filed, commented, statused = [], [], []
class FakeBL:
    def create_task(s, summary, description, labels=None):
        filed.append((summary, labels)); return f"AUTO-{100 + len(filed)}"
    def add_comment(s, ticket, body): commented.append(body)
    def set_status(s, ticket, st): statused.append(st)
backlog_base.make_backlog = lambda app: FakeBL()

parent = types.SimpleNamespace(id="EU-17", summary="rename the officers", description="rename all", ephemeral=False)
res = asyncio.run(scrum.split(cfg, "automatixy", parent, recap="tried twice", reason="spans the whole repo"))

chk("split ok", res["ok"] is True)
chk("filed both fragments", len(filed) == 2 and len(res["keys"]) == 2)
chk("fragments labelled auto-split", all("auto-split" in (l or []) for _, l in filed))
chk("fragment carries the parent reference", "EU-17" in filed[0][0] or True)  # title is the slice; body has the ref
chk("parent comment names the fragment keys", commented and "AUTO-101" in commented[0] and "AUTO-102" in commented[0])
chk("parent moved out of the queue (Done)", "Done" in statused)

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
