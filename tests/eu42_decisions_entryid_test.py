"""EU-42 regression: decisions.add(..., entry_id=...) de-dups by entry id, not always by ticket id.

The out-of-scope routing files its cockpit 'Needs you' proposal under a DISTINCT key
(f'{ticket.id}#out-of-scope') so it can co-exist with a normal needs_human decision recorded for the
SAME ticket. Before EU-42, decisions.add de-duped strictly by ticket.id, so the second add for a
ticket clobbered the first — the out-of-scope proposal would silently overwrite (or be overwritten by)
the needs_human ask. This harness pins:

  * a needs_human decision and an out-of-scope decision for the same ticket BOTH survive (distinct ids),
  * re-adding the SAME entry_id replaces in place (still de-dups — no duplicate pile-up across passes),
  * the legacy no-entry_id call still de-dups by ticket id (backwards compatible).

Storage is a real temp pending_decisions.json (no network); notifications are silenced.
"""
import sys, types, tempfile
from pathlib import Path

# Stub the Agent SDK so importing the orchestrator package never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import decisions
from orchestrator.contracts import Ticket
from orchestrator.config import Config, AppConfig

# silence the Telegram ping decisions.add emits
decisions.notify = types.SimpleNamespace(send=lambda *a, **k: None)

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
app = AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev",
                protected_branch="main", backlog_backend="none")
cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"))
tkt = Ticket(id="EU-42", key="EU-42", summary="route out-of-scope findings", description="")

# --- 1) needs_human ask + out-of-scope proposal for the SAME ticket both survive --------------- #
decisions.add(cfg, tkt, app.name, "Scope decision: which date format?")          # default id = EU-42
decisions.add(cfg, tkt, app.name, "File out-of-scope findings?", entry_id="EU-42#out-of-scope")
items = decisions.load(cfg)
ids = sorted(i["id"] for i in items)
chk("both decisions survive (distinct entry ids)", len(items) == 2, str(ids))
chk("the needs_human decision is kept under the ticket id", "EU-42" in ids, str(ids))
chk("the out-of-scope decision is kept under its distinct id", "EU-42#out-of-scope" in ids, str(ids))
chk("the out-of-scope proposal text is preserved",
    any(i["id"] == "EU-42#out-of-scope" and "out-of-scope" in i["question"] for i in items), str(items))

# --- 2) re-adding the SAME entry_id replaces in place (still de-dups across passes) ------------ #
decisions.add(cfg, tkt, app.name, "File out-of-scope findings (pass 2)?", entry_id="EU-42#out-of-scope")
items = decisions.load(cfg)
oos = [i for i in items if i["id"] == "EU-42#out-of-scope"]
chk("re-adding same entry_id does not duplicate", len(items) == 2 and len(oos) == 1, str([i["id"] for i in items]))
chk("re-adding same entry_id updates the question in place", oos and "pass 2" in oos[0]["question"], str(oos))

# --- 3) legacy call (no entry_id) still de-dups by ticket id (backwards compatible) ------------ #
decisions.add(cfg, tkt, app.name, "Updated scope decision")                      # default id = EU-42 again
items = decisions.load(cfg)
eu42 = [i for i in items if i["id"] == "EU-42"]
chk("no-entry_id add still de-dups by ticket id (no duplicate EU-42)", len(eu42) == 1, str([i["id"] for i in items]))
chk("no-entry_id add updates the EU-42 decision in place", eu42 and eu42[0]["question"] == "Updated scope decision", str(eu42))
chk("the distinct out-of-scope decision is untouched by the ticket-id add",
    any(i["id"] == "EU-42#out-of-scope" for i in items), str([i["id"] for i in items]))

passed = sum(1 for _, c, _ in results if c)
for n, c, d in results:
    print(f"  {'✓' if c else '✗'} {n}" + (f"  [{d}]" if (not c and d) else ""))
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
