"""EU-55 / F12(c) edge regression: an EXPLICIT `id:` reply whose ticket id matches NO parked
decision must fall through to the CTO chat — it must NOT pop the (unrelated) oldest pending
decision, and it must NOT be silently dropped.

This pins the second half of the F12 footgun. `freetext_not_eaten_test.py` proves BARE free text
doesn't eat a parked decision, but it stubs `handle_reply`, so it can't prove what the new
`route_message` control flow does when `is_explicit_reply` is True yet the resolver finds no match:

    if load(cfg) and is_explicit_reply(text):
        if handle_reply(cfg, audit, _strip_reply_marker(text)):   # -> False on an unknown id
            return True
    # ...falls through to the CTO chat

Here we run the REAL `handle_reply` + `resolve` against a real parked-decisions store. `resolve`
with a non-matching id leaves the store untouched (decisions.py:89-94) and returns None, so the
message must reach the council/CTO path while AUTO-1 stays parked. A regression that "eats" the
message (e.g. `return handle_reply(...)`) or that pops the oldest on an id miss would fail here.
"""
import sys, types, tempfile, time
from pathlib import Path

# --- stub heavy/optional deps so the module imports cleanly (mirrors the sibling F12 harness) ---
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

from orchestrator import decisions
from orchestrator.contracts import Ticket
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Capture the CTO-chat path; never start a real council thread or build. Note we DO NOT stub
# decisions.handle_reply — the whole point is to exercise the real resolver on an id miss.
freetext = []
council = types.ModuleType("orchestrator.council")
async def _respond(cfg, text):
    freetext.append(text)
council.respond_to_commander = _respond
council.add_commander_note = lambda cfg, text: freetext.append(text)
# EU-743: route_message's free-text reply thread now claims/clears a 'CTO is typing…' flag via
# council.set_typing(); the real module always has it, so the stub must too or _answer() dies with
# AttributeError before it ever reaches respond_to_commander (the id-miss-routes-to-CTO check).
council.set_typing = lambda *a, **k: None
council.is_typing = lambda: False
sys.modules["orchestrator.council"] = council

resumed = []
decisions._run_bg = lambda *a, **k: resumed.append(a) or True   # tripwire: must never fire on an id miss
decisions.notify = types.SimpleNamespace(send=lambda *a, **k: None, configured=lambda: False)
audit = types.SimpleNamespace(record=lambda *a, **k: None)

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="jira",
                             backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)

def seed():
    decisions._save(cfg, [])
    decisions.add(cfg, Ticket(id="AUTO-1", key="AUTO-1", summary="do thing", description="d",
                              acceptance_criteria=["x"], app="automatixy", ephemeral=True),
                  "automatixy", "pick a date")

def _wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()

# --- 1) explicit `id:` reply to an UNKNOWN ticket: store untouched, message goes to the CTO chat ---
seed(); resumed.clear(); freetext.clear()
decisions.route_message(cfg, audit, "EU-99: ship it on Tuesday")
chk("unknown-id explicit reply does NOT resume any build", not resumed)
chk("unknown-id explicit reply leaves AUTO-1 parked", len(decisions.load(cfg)) == 1)
chk("the one still-parked decision is AUTO-1", decisions.load(cfg)[0]["id"] == "AUTO-1")
chk("unknown-id explicit reply routes to the CTO chat",
    _wait_for(lambda: freetext == ["EU-99: ship it on Tuesday"]), str(freetext))

# --- 2) the real resolver itself: a non-matching id pops nothing and returns None ----------------
seed()
chk("resolve(unknown id) returns None", decisions.resolve(cfg, "x", "EU-99") is None)
chk("resolve(unknown id) leaves the store intact", len(decisions.load(cfg)) == 1)

# --- 3) sanity: the MATCHING id still resolves through the real resolver (no over-blocking) -------
seed()
got = decisions.resolve(cfg, "use DD/MM", "AUTO-1")
chk("resolve(matching id) pops AUTO-1", got is not None and got["id"] == "AUTO-1")
chk("resolve(matching id) empties the store", len(decisions.load(cfg)) == 0)

print("\n======== EU-55 EXPLICIT-REPLY-NO-MATCH QA ========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
