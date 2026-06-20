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

# ---- _exception_report: turn limit -> needs you (ESCALATED + decision) ----
class FakeAudit:
    def __init__(s): s.events = []
    def record(s, e, **kw): s.events.append((e, kw))
tk = Ticket(id="AUTO-13", key="AUTO-13", summary="big migration", description="d", app="automatixy")
a1 = FakeAudit()
rep = loop._exception_report(cfg, tk, app, RuntimeError("Reached maximum number of turns (60)"), a1)
check("turn-limit -> ESCALATED (needs you), not ERRORED", rep.outcome == Outcome.ESCALATED, str(rep.outcome))
check("turn-limit -> records needs_human(reason=turn-limit)",
      any(e == "needs_human" and kw.get("reason") == "turn-limit" for e, kw in a1.events), str(a1.events))
check("turn-limit -> no 'ticket_exception' recorded", not any(e == "ticket_exception" for e, _ in a1.events))
from orchestrator import decisions
pend = decisions.load(cfg)
check("turn-limit -> a decision card is filed for you", any(p.get("id") == "AUTO-13" for p in pend), str(pend))

# ---- _exception_report: a real error stays ERRORED ----
a2 = FakeAudit()
rep2 = loop._exception_report(cfg, tk, app, RuntimeError("KeyError: boom"), a2)
check("generic error -> ERRORED", rep2.outcome == Outcome.ERRORED)
check("generic error -> records ticket_exception", any(e == "ticket_exception" for e, _ in a2.events))

print("\n================ TURN BUDGET QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
