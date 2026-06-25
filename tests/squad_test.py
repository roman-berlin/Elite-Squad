"""QA for squad delegation — planner parsing, routing, aggregation, and fail-safe fallback.
Stubs the Agent SDK + run_agent so no real model runs. Prints a PASS/FAIL checklist."""
import asyncio, sys, types
from pathlib import Path

REPO = "."
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, REPO)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))

from orchestrator import squad, builder
from orchestrator.agent import AgentRun
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, BuildRequest

# ---- fake run_agent: planner returns a 3-subtask plan; soldiers + solo return work ----
PLAN = ('Here is the split:\n```json\n'
        '[{"role":"logistics-db","title":"migration","detail":"add export_jobs table","size":"M"},'
        '{"role":"ordnance-be","title":"endpoint","detail":"POST /exports uses the table","size":"L"},'
        '{"role":"frontend","title":"button","detail":"Export button on /leads","size":"S"}]\n```')
_mode = {"plan": PLAN, "raise_plan": False}
async def fake_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None):
    if tag == "squad-lead":
        if _mode["raise_plan"]:
            raise RuntimeError("planner exploded")
        return AgentRun(text=_mode["plan"], final=_mode["plan"], cost_usd=0.10, num_turns=3,
                        is_error=False, tools=["Read"])
    if tag.startswith("soldier"):
        return AgentRun(text="done", final="implemented " + tag, cost_usd=0.20, num_turns=5,
                        is_error=False, tools=["Edit"])
    return AgentRun(text="solo", final="SOLO build done", cost_usd=0.5, num_turns=9,
                    is_error=False, tools=["Edit"])
squad.run_agent = fake_run_agent
builder.run_agent = fake_run_agent

class FakeAudit:
    def __init__(self): self.events = []
    def record(self, event, **kw): self.events.append((event, kw))

def mk(ac, summary="Add tenant-scoped CSV export", desc="A sizable feature touching db, api and ui."):
    return Ticket(id="AUTO-50", key="AUTO-50", summary=summary, description=desc,
                  acceptance_criteria=ac, app="automatixy")
app = AppConfig(name="automatixy", repo_path="/tmp/x", base_branch="DEV", protected_branch="MAIN",
                backlog_backend="none")
big_ac = ["criteria one", "criteria two", "criteria three", "criteria four"]

# ============================ parsing ============================
subs = squad.parse_subtasks(PLAN)
check("parse: 3 subtasks from fenced+prose JSON", len(subs) == 3, str(len(subs)))
check("parse: unknown role 'frontend' -> generalist", subs[2].role == "generalist", subs[2].role)
check("parse: roles + sizes kept", subs[0].role == "logistics-db" and subs[1].size == "L")
check("parse: size->effort (L->high, S->medium, M->medium)",
      subs[1].effort() == "high" and subs[2].effort() == "medium" and subs[0].effort() == "medium")
check("parse: bad size -> M",
      squad.parse_subtasks('[{"role":"devops","detail":"x","size":"HUGE"}]')[0].size == "M")
check("parse: empty detail dropped",
      squad.parse_subtasks('[{"role":"devops","detail":"","size":"S"}]') == [])
check("parse: garbage -> []", squad.parse_subtasks("no json here") == [])

# ============================ should_delegate ============================
cfg = Config(apps=[app], audit_path="/tmp/a.jsonl", use_worktree=False)
cfg.delegation_enabled = False
check("gate: disabled -> no delegate", squad.should_delegate(cfg, BuildRequest(mk(big_ac), "b", iteration=1)) is False)
cfg.delegation_enabled = True
check("gate: armed + many AC -> delegate", squad.should_delegate(cfg, BuildRequest(mk(big_ac), "b", iteration=1)) is True)
check("gate: retry (iteration>1) -> no delegate", squad.should_delegate(cfg, BuildRequest(mk(big_ac), "b", iteration=2)) is False)
check("gate: tiny ticket -> no delegate",
      squad.should_delegate(cfg, BuildRequest(mk([], summary="fix typo in label", desc="typo"), "b", iteration=1)) is False)

# ============================ build_delegated (aggregation + audit) ============================
audit = FakeAudit()
res, n = asyncio.run(squad.build_delegated(BuildRequest(mk(big_ac), "b", iteration=1), app, cfg, audit=audit))
check("delegated: 3 soldiers dispatched", n == 3, str(n))
check("delegated: cost aggregated (plan .10 + 3x .20)", abs(res.cost_usd - 0.70) < 1e-6, str(res.cost_usd))
check("delegated: turns aggregated (3 + 3x5)", res.num_turns == 18, str(res.num_turns))
check("delegated: tools aggregated", res.tools == ["Read", "Edit", "Edit", "Edit"], str(res.tools))
check("delegated: ok=True", res.ok is True)
check("delegated: summary names the squad", "Squad delegation" in res.summary and "Ordnance BE" in res.summary)
check("delegated: audit has 1 delegation + 3 soldier_build",
      sum(1 for e, _ in audit.events if e == "delegation") == 1
      and sum(1 for e, _ in audit.events if e == "soldier_build") == 3, str([e for e, _ in audit.events]))

# ============================ fallback: thin plan -> (None,0) ============================
_mode["plan"] = '[{"role":"ordnance-be","title":"only","detail":"one slice","size":"M"}]'
res1, n1 = asyncio.run(squad.build_delegated(BuildRequest(mk(big_ac), "b", iteration=1), app, cfg))
check("fallback: <2 subtasks -> (None, 0)", res1 is None and n1 == 0, f"{res1},{n1}")
_mode["plan"] = PLAN   # restore

# ============================ build() routing ============================
async def run_build(c, req): return await builder.build(req, app, c, audit=FakeAudit())
cfg.delegation_enabled = False
r = asyncio.run(run_build(cfg, BuildRequest(mk(big_ac), "b", iteration=1)))
check("route: disabled -> SOLO build", "SOLO build done" in r.summary, r.summary[:40])
cfg.delegation_enabled = True
r = asyncio.run(run_build(cfg, BuildRequest(mk(big_ac), "b", iteration=1)))
check("route: armed + big -> delegated", "Squad delegation" in r.summary)
r = asyncio.run(run_build(cfg, BuildRequest(mk([], summary="tiny tweak", desc="x"), "b", iteration=1)))
check("route: armed + tiny -> SOLO build", "SOLO build done" in r.summary)
_mode["raise_plan"] = True
r = asyncio.run(run_build(cfg, BuildRequest(mk(big_ac), "b", iteration=1)))
check("route: planner exception -> SOLO build (fail-safe)", "SOLO build done" in r.summary)
_mode["raise_plan"] = False

# ============================ report ============================
print("\n================ SQUAD DELEGATION QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if (detail and not ok) else ""))
print("-----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAILURE(S) ❌")
