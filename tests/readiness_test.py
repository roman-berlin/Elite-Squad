"""Ticket-readiness gate QA: an under-specified ticket (no acceptance criteria + a thin description) is
handed back BEFORE the builder spends any Opus; a well-specified one sails straight through."""
import sys, types, asyncio
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import readiness, loop
from orchestrator.config import Config
from orchestrator.contracts import Outcome, TicketReport

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace
def ticket(**kw):
    base = dict(id="AUTO-9", summary="Do the thing", description="", acceptance_criteria=[],
                ephemeral=False, branch_name=lambda p: f"{p}/AUTO-9")
    base.update(kw)
    return ns(**base)

# --- assess(): the pure heuristic ---
chk("ready: has acceptance criteria", readiness.assess(ticket(acceptance_criteria=["x works"]))[0])
chk("ready: substantial description", readiness.assess(
    ticket(description="Add a budget panel to the cockpit showing today/week/month token burn with a "
                       "per-model breakdown and a daily ceiling bar."))[0])
chk("ready: AC even with a thin description", readiness.assess(
    ticket(acceptance_criteria=["panel renders"], description="x"))[0])

nr, missing = readiness.assess(ticket(description="fix it"))
chk("NOT ready: no AC + thin description", not nr)
chk("missing names the gaps", any("acceptance" in m for m in missing) and any("description" in m for m in missing))
chk("NOT ready: description just restates the title",
    not readiness.assess(ticket(summary="Refactor auth", description="Refactor auth"))[0])
chk("NOT ready: empty everything", not readiness.assess(ticket())[0])

# --- the gate in process_ticket: hand back BEFORE building, never call the build ---
calls = {"attempt": 0}
async def fake_attempt(*a, **k):
    calls["attempt"] += 1
    return TicketReport("AUTO-9", Outcome.MERGED, 1, 0.0, "automatixy", "b")
loop._attempt = fake_attempt
loop._cleanup = lambda *a, **k: None
loop._notify = lambda *a, **k: None
loop.decisions = ns(add=lambda *a, **k: None, reply_hint=lambda *a, **k: "↩️ reply")

class Backlog:
    def __init__(s): s.status = None; s.comment = None
    def set_status(s, t, v): s.status = v
    def add_comment(s, t, b): s.comment = b
class Audit:
    def __init__(s): s.events = []
    def record(s, k, **kw): s.events.append(k)
class Git:
    def __init__(s): s.checked_out = False
    def checkout_feature(s, b): s.checked_out = True

app = ns(name="automatixy", branch_prefix="autodev")
cfg = Config(apps=[], audit_path="/tmp/x.jsonl", readiness_gate=True)

# not-ready ticket -> handed back, build NEVER runs
bl, au, g = Backlog(), Audit(), Git()
r = asyncio.run(loop.process_ticket(ticket(description="fix it"), app, cfg, g, bl, au, budget=None))
chk("not-ready -> ESCALATED (handed back)", r.outcome == Outcome.ESCALATED, str(r.outcome))
chk("not-ready -> build was NEVER called (no Opus spent)", calls["attempt"] == 0)
chk("not-ready -> moved to Needs Human", bl.status == "Needs Human", str(bl.status))
chk("not-ready -> comment lists what's missing", bl.comment and "Not ready" in bl.comment)
chk("not-ready -> never checked out a branch", not g.checked_out)
chk("not-ready -> audited", "not_ready" in au.events)

# ready ticket -> proceeds to the build
calls["attempt"] = 0
bl2, au2, g2 = Backlog(), Audit(), Git()
r2 = asyncio.run(loop.process_ticket(ticket(acceptance_criteria=["it works"]), app, cfg, g2, bl2, au2, budget=None))
chk("ready -> build runs", calls["attempt"] == 1)
chk("ready -> reached MERGED via the build", r2.outcome == Outcome.MERGED)

# gate OFF -> always builds, even a thin ticket
calls["attempt"] = 0
cfg_off = Config(apps=[], audit_path="/tmp/x.jsonl", readiness_gate=False)
asyncio.run(loop.process_ticket(ticket(description="fix it"), app, cfg_off, Git(), Backlog(), Audit(), budget=None))
chk("gate OFF -> thin ticket still builds (opt-in, no behaviour change by default)", calls["attempt"] == 1)

print("\n================ READINESS GATE QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
