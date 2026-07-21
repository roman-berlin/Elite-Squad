"""QA for the turn-budget scaling + the 'ran out of turns -> needs you' surfacing."""
import sys, tempfile, types
from pathlib import Path

REPO = "."
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, REPO)

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator import builder, loop, notify
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, Outcome
notify.send = lambda *a, **k: None

d = Path(tempfile.mkdtemp())
(d / "audit.jsonl").write_text("")
app = AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV", protected_branch="MAIN",
                backlog_backend="none")
cfg = Config(apps=[app], audit_path=str(d / "audit.jsonl"), use_worktree=False)

# ---- turns_for scaling (base 60) ----
check("turns: low/medium = base 60", builder.turns_for(cfg, "low") == 60 and builder.turns_for(cfg, "medium") == 60)
check("turns: high = 96", builder.turns_for(cfg, "high") == 96, str(builder.turns_for(cfg, "high")))
check("turns: max/xhigh = 144", builder.turns_for(cfg, "max") == 144 and builder.turns_for(cfg, "xhigh") == 144)
cfg2 = Config(apps=[app], audit_path=str(d / "audit.jsonl"), use_worktree=False)
cfg2.builder_max_turns = 100
check("turns: custom base scales (high=160, max=240)",
      builder.turns_for(cfg2, "high") == 160 and builder.turns_for(cfg2, "max") == 240)

# ---- turn-limit detection ----
check("detect: 'maximum number of turns'", loop._is_turn_limit("Claude Code returned an error result: Reached maximum number of turns (60)"))
check("detect: generic error is NOT a turn limit", not loop._is_turn_limit("Some other crash: KeyError 'x'"))

# ---- _exception_report on a turn limit: FIRST try the Scrum Master split, escalate only if it can't ----
import asyncio
from orchestrator import scrum as _scrum_mod, decisions
class FakeAudit:
    def __init__(s): s.events = []
    def record(s, e, **kw): s.events.append((e, kw))
TURN_ERR = RuntimeError("Reached maximum number of turns (60)")

# (a) the Scrum Master CAN split it -> the unit splits + re-queues, the Commander is NOT bothered
async def _split_ok(*a, **k): return {"ok": True, "keys": ["AUTO-13a", "AUTO-13b"]}
_scrum_mod.split = _split_ok
tkA = Ticket(id="AUTO-13", key="AUTO-13", summary="big migration", description="d", app="automatixy")
aA = FakeAudit()
repA = asyncio.run(loop._exception_report(cfg, tkA, app, TURN_ERR, aA))
check("too-big ticket -> Scrum Master splits it (REQUEUED, not ESCALATED)", repA.outcome == Outcome.REQUEUED, str(repA.outcome))
check("split is audited (scrum_split, reason=turn-limit, into=[...])",
      any(e == "scrum_split" and kw.get("reason") == "turn-limit" and kw.get("into") for e, kw in aA.events), str(aA.events))
check("a successful split does NOT escalate to you", not any(e == "needs_human" for e, _ in aA.events))

# (b) the splitter DECLINES (can't split further) -> 2026-07-19 senior ladder: requeue ONCE with
# a boosted turn budget; only the SECOND blow-out escalates to the Commander with the honest note.
async def _split_no(*a, **k): return {"ok": False, "keys": []}
_scrum_mod.split = _split_no
tkB = Ticket(id="AUTO-14", key="AUTO-14", summary="atomic but heavy", description="d", app="automatixy")
aB = FakeAudit()
repB = asyncio.run(loop._exception_report(cfg, tkB, app, TURN_ERR, aB))
check("unsplittable too-big ticket -> FIRST blow-out requeues once (boosted budget), not ESCALATED",
      repB.outcome == Outcome.REQUEUED, str(repB.outcome))
check("the requeue is audited as turn_limit_retry",
      any(e == "turn_limit_retry" for e, _ in aB.events), str(aB.events))
check("the split refusal is audited (scrum_split_failed)",
      any(e == "scrum_split_failed" for e, _ in aB.events), str(aB.events))
aB2 = FakeAudit()
repB2 = asyncio.run(loop._exception_report(cfg, tkB, app, TURN_ERR, aB2))
check("SECOND blow-out -> ESCALATED (needs you), not ERRORED", repB2.outcome == Outcome.ESCALATED, str(repB2.outcome))
check("escalation records needs_human(reason=turn-limit)",
      any(e == "needs_human" and kw.get("reason") == "turn-limit" for e, kw in aB2.events), str(aB2.events))
check("turn-limit is never a plain 'ticket_exception'", not any(e == "ticket_exception" for e, _ in aB2.events))
check("escalation files a decision card for you", any(p.get("id") == "AUTO-14" for p in decisions.load(cfg)), "no card")

# (c) an EPHEMERAL ticket has no backlog to file sub-tickets into -> skip the split, escalate directly
async def _split_boom(*a, **k): raise AssertionError("must not try to split an ephemeral ticket")
_scrum_mod.split = _split_boom
tkE = Ticket(id="ADHOC-1", key="ADHOC-1", summary="x", description="d", app="automatixy", ephemeral=True)
repE = asyncio.run(loop._exception_report(cfg, tkE, app, TURN_ERR, FakeAudit()))
check("ephemeral too-big ticket -> escalates without attempting a split", repE.outcome == Outcome.ESCALATED, str(repE.outcome))

# ---- _exception_report: a real (non-turn-limit) error stays ERRORED ----
a2 = FakeAudit()
rep2 = asyncio.run(loop._exception_report(cfg, tkB, app, RuntimeError("KeyError: boom"), a2))
check("generic error -> ERRORED", rep2.outcome == Outcome.ERRORED)
check("generic error -> records ticket_exception", any(e == "ticket_exception" for e, _ in a2.events))

print("\n================ TURN BUDGET QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
