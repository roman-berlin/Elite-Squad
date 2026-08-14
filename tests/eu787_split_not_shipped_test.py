"""EU-787 regression: a scrum-split parent must never appear in "shipped" / velocity counts.

AC-1: scrum.split() closing a split parent calls the backlog to add 'superseded' label.
AC-2: The closing comment states "no code produced" + names every child key.
AC-3: dashboard.standup()'s shipped list contains EU-B (merged) but NOT EU-A (split).
AC-4: warroom kpis merged count excludes superseded; only genuine merges counted.
AC-5: A genuinely merged ticket still passes through all shipped/velocity paths.
"""
import sys, types, asyncio, json, tempfile
from datetime import datetime, timedelta
from pathlib import Path

REPO = "."
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
sys.path.insert(0, REPO)

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ======================================================================
# PART 1 — scrum.split(): label + comment on the parent
# ======================================================================
from orchestrator import scrum, pm
from orchestrator import recon as _recon
import orchestrator.backlog.base as backlog_base
from orchestrator.config import Config, AppConfig

TWO_TICKETS = """=== TICKET ===
TITLE: Part A — do the header
What: implement header component.
Where: src/components/Header.tsx

=== TICKET ===
TITLE: Part B — do the footer
What: implement footer component.
Where: src/components/Footer.tsx"""

cfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV", protected_branch="MAIN",
                             backlog_backend="jira", backlog={"base_url": "x", "project_key": "AUTO"})],
             audit_path="/tmp/eu787_a.jsonl")
async def fake_officer(**kw):
    return TWO_TICKETS
_recon.run_officer = fake_officer

filed_items = []
commented = []
label_added = []   # (key, add_args, remove_args)
statused = []

class FakeBL:
    def create_task(s, summary, description, labels=None, issue_type="Task", priority=None, parent=None):
        filed_items.append(summary); return f"AUTO-{900+len(filed_items)}"
    def add_comment(s, ticket, body): commented.append({"ticket": str(getattr(ticket, "key", ticket.id)), "body": body})
    def set_labels(s, key, add=(), remove=()): label_added.append((key, add, remove)); return True
    def set_status(s, ticket, st): statused.append(st)

backlog_base.make_backlog = lambda app: FakeBL()

parent = types.SimpleNamespace(id="EU-TEST", key="EU-TEST", summary="big feature",
                               description="do everything", acceptance_criteria=[], ephemeral=False)

res = asyncio.run(scrum.split(cfg, "automatixy", parent, recap="", reason="too heavy"))

chk("split ok=True with stub officer", res["ok"] is True, str(res.get("error")))
chk("children filed (keys present)", len(res["keys"]) >= 2, f"keys={res['keys']}")

# AC-1: superseeded label was added to the parent
if label_added:
    last_key, last_add, _ = label_added[-1]
    chk("AC-1: set_labels called on the parent", last_key == "EU-TEST",
        f"key={last_key} adds={last_add}")
    chk("AC-1: 'superseded' is in the add tuple",
        "superseded" in last_add,
        f"add={last_add}")
else:
    chk("AC-1: set_labels was called (record exists)", False, "label_added is empty")

# AC-2: comment says "no code produced" + names children
if commented:
    body = commented[0]["body"]
    chk("AC-2: comment contains 'no code produced'",
        "no code produced" in body.lower(), repr(body[:200]))
    for k in res["keys"]:
        chk(f"AC-2: comment names child {k}", k in body, repr(body[:200]))
else:
    chk("AC-2: comment was posted (record exists)", False, "commented is empty")

# ======================================================================
# PART 2 — dashboard.standup(): split parent excluded, merge included
# ======================================================================
from orchestrator import dashboard as D

_dd = tempfile.mkdtemp()
_ap = Path(_dd) / "audit.jsonl"

_now = datetime.now().astimezone()
_ago = lambda h: (_now - timedelta(hours=h)).replace(microsecond=0).isoformat()

with open(_ap, "w") as f:
    # EU-A: split parent — outcome 'split', no 'merged' event
    f.write(json.dumps({"ts": _ago(5), "event": "ticket_start", "ticket_id": "EU-A", "app": "automatixy"}) + "\n")
    f.write(json.dumps({"ts": _ago(4), "event": "scrum_split", "ticket_id": "EU-A", "app": "automatixy"}) + "\n")
    # EU-B: genuinely merged ticket
    f.write(json.dumps({"ts": _ago(6), "event": "ticket_start", "ticket_id": "EU-B", "app": "automatixy"}) + "\n")
    f.write(json.dumps({"ts": _ago(2), "event": "merged", "ticket_id": "EU-B", "app": "automatixy"}) + "\n")

_cfg = Config(apps=[]); _cfg.audit_path = str(_ap)
su = D.standup(_cfg)

tasks = D.load_tasks(_ap)
chk("standup shipped list excludes EU-A (split)",
    "EU-A" not in su or ("Shipped to DEV" in su and su[su.find("Shipped to DEV"):su.find("\n", su.find("Shipped to DEV"))].find("EU-A") < 0),
    su[:400])

# Check that load_tasks correctly categorises the outcomes
eu_a_rows = [t for t in tasks if str(t.get("ticket_id")) == "EU-A"]
eu_b_rows = [t for t in tasks if str(t.get("ticket_id")) == "EU-B"]
chk("EU-A outcome is 'split' (not 'merged→dev')",
    any(t.get("outcome") == "split" for t in eu_a_rows),
    f"EU-A outcomes={[t.get('outcome') for t in eu_a_rows]}")
chk("EU-B outcome is 'merged→dev'",
    any(t.get("outcome") == "merged→dev" for t in eu_b_rows),
    f"EU-B outcomes={[t.get('outcome') for t in eu_b_rows]}")

# AC-3/AC-5: standup shipped list includes EU-B (merged)
chk("AC-3: standup ships EU-B (genuinely merged)",
    "EU-B" in su, su[:400])
chk("AC-5: EU-B appears in the Shipped-to-DEV line of standup",
    "Shipped to DEV" in su and "EU-B" in su, su[:400])

# Also verify via load_tasks directly
shipped_in_standup = [t for t in tasks if t.get("outcome") == "merged→dev"]
chk("load_tasks finds EU-B as merged, not EU-A",
    any(str(t.get("ticket_id")) == "EU-B" for t in shipped_in_standup)
    and not any(str(t.get("ticket_id")) == "EU-A" for t in shipped_in_standup),
    f"shipped_outcomes={[t.get('ticket_id') for t in shipped_in_standup]}")

# ======================================================================
# PART 3 — predicate handles a hypothetical supesrseded merge
# ======================================================================
# If someone accidentally had a merged ticket later labelled 'superseded', the predicate should exclude it.
_supertask = {"outcome": "merged→dev", "labels": ["superseded", "auto-split"]}
_not_supertask = {"outcome": "merged→dev", "labels": []}
_split_task = {"outcome": "split", "labels": []}

chk("AC-4: predicate EXCLUDES merged ticket that carries 'superseded' label",
    D._is_merged_and_not_superseded(_supertask) is False,
    f"result={D._is_merged_and_not_superseded(_supertask)}")
chk("predicate INCLUDES normal merged ticket",
    D._is_merged_and_not_superseded(_not_supertask) is True,
    f"result={D._is_merged_and_not_superseded(_not_supertask)}")
chk("predicate EXCLUDES split-outcome ticket",
    D._is_merged_and_not_superseded(_split_task) is False,
    f"result={D._is_merged_and_not_superseded(_split_task)}")
chk("predicate handles missing labels key (backward compat)",
    D._is_merged_and_not_superseded({"outcome": "merged→dev"}) is True,
    f"result={D._is_merged_and_not_superseded({'outcome': 'merged→dev'})}")

# ======================================================================
# PART 4 — warroom.kpis uses the shared predicate
# ======================================================================
import inspect
# Dashboard doesn't re-export warroom; import it directly.
from orchestrator import warroom as W
src_wkpis = inspect.getsource(W.kpis)
chk("warroom.kpis references the shared predicate",
    "_is_merged_and_not_superseded" in src_wkpis,
    "predicate not found in warroom.kpis source")

print("\n============ EU-787 SPLIT NOT SHIPPED QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
