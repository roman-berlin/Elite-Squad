"""The Planner officer (Phase-2 §2) — parse + fail-safe + burn accounting + builder brief.

The Planner is the single up-front design call that replaces the Architect ADR + squad-lead
planning + Scrum split + Senior-PM prebuild triage. Pinned here (no live model — SDK stubbed):

  1. parse_plan — a clean JSON reply yields verdict + approach + testable_ac + in_scope_files;
     tolerant of prose/fences around the JSON and of a comma-string list.
  2. fail-safe — empty/garbled reply, or an invalid verdict, defaults to BUILD with empty fields
     (so the Builder always proceeds from the raw ticket; a Planner hiccup never blocks).
  3. as_builder_brief — renders approach + testable AC + in-scope files for the Builder prompt,
     and is EMPTY for a non-BUILD verdict or an empty plan (so the prompt is unchanged then).
  4. plan() — threads burn (cost/tokens) onto the result, audits a 'planner' event, and an agent
     exception still returns a BUILD result (never raises).
"""
import asyncio
import sys
import types

# ── SDK stub (no real model calls) ───────────────────────────────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _Opts:
    def __init__(self, **kw): self.__dict__.update(kw)


class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self


sdk.ClaudeAgentOptions = _Opts
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.planner as planner              # noqa: E402
from orchestrator.agent import AgentRun             # noqa: E402
from orchestrator.config import Config, AppConfig   # noqa: E402
from orchestrator.contracts import Ticket           # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def _ticket(tid="EU-1", **kw) -> Ticket:
    return Ticket(id=tid, key=tid, summary=kw.get("summary", "Add a retry to the deploy step"),
                  description=kw.get("description", "The deploy flakes; add a bounded retry."),
                  acceptance_criteria=kw.get("ac", ["deploy retries up to 3×", "gives up after 3"]),
                  app="automatixy", ephemeral=True, labels=kw.get("labels", []))


# ══════════════════════════════════════════════════════════════════════════════
# 1. parse_plan — clean JSON
# ══════════════════════════════════════════════════════════════════════════════
CLEAN = ('{"verdict": "BUILD", "approach": "Wrap deploy in a retry loop (deploy.py).",'
         ' "testable_ac": ["3 failures → aborts", "1st-try success → no retry"],'
         ' "in_scope_files": ["orchestrator/deploy.py"], "answer": ""}')
p = planner.parse_plan(CLEAN)
chk("parse: verdict BUILD", p.verdict == "BUILD", p.verdict)
chk("parse: approach extracted", "retry loop" in p.approach, p.approach)
chk("parse: testable_ac list", p.testable_ac == ["3 failures → aborts", "1st-try success → no retry"],
    str(p.testable_ac))
chk("parse: in_scope_files list", p.in_scope_files == ["orchestrator/deploy.py"], str(p.in_scope_files))

# tolerant of prose + code fences around the JSON
WRAPPED = "Here's the plan:\n```json\n" + CLEAN + "\n```\nDone."
chk("parse: tolerates prose + fences", planner.parse_plan(WRAPPED).approach == p.approach)

# comma-string list coerced
COMMA = '{"verdict":"BUILD","in_scope_files":"a.py, b.py","testable_ac":"x"}'
pc = planner.parse_plan(COMMA)
chk("parse: comma-string in_scope_files coerced", pc.in_scope_files == ["a.py", "b.py"], str(pc.in_scope_files))
chk("parse: comma-string testable_ac coerced", pc.testable_ac == ["x"], str(pc.testable_ac))

# non-BUILD verdict carries the answer
ANS = '{"verdict":"ANSWER","answer":"Already implemented at loop.py:42.","approach":"","testable_ac":[]}'
pa = planner.parse_plan(ANS)
chk("parse: ANSWER verdict + answer", pa.verdict == "ANSWER" and "loop.py:42" in pa.answer, str(pa))

# ══════════════════════════════════════════════════════════════════════════════
# 2. fail-safe defaults
# ══════════════════════════════════════════════════════════════════════════════
chk("failsafe: empty reply → BUILD, empty fields",
    planner.parse_plan("").verdict == "BUILD" and planner.parse_plan("").testable_ac == [])
chk("failsafe: garbage reply → BUILD", planner.parse_plan("total nonsense, no json").verdict == "BUILD")
chk("failsafe: invalid verdict → BUILD",
    planner.parse_plan('{"verdict":"NONSENSE","approach":"x"}').verdict == "BUILD")

# ══════════════════════════════════════════════════════════════════════════════
# 3. as_builder_brief
# ══════════════════════════════════════════════════════════════════════════════
brief = p.as_builder_brief()
chk("brief: carries approach", "APPROACH" in brief and "retry loop" in brief, brief[:80])
chk("brief: carries testable AC with 'write a test for EACH'", "write a test for EACH" in brief.upper()
    or "TESTABLE ACCEPTANCE" in brief, brief[:120])
chk("brief: lists in-scope files", "orchestrator/deploy.py" in brief)
chk("brief: EMPTY for a non-BUILD verdict", planner.parse_plan(ANS).as_builder_brief() == "")
chk("brief: EMPTY for an empty BUILD plan", planner.parse_plan("").as_builder_brief() == "")

# ══════════════════════════════════════════════════════════════════════════════
# 4. plan() — burn threaded, audited, exception-safe
# ══════════════════════════════════════════════════════════════════════════════
_cfg = Config(apps=[AppConfig(name="automatixy", repo_path="/tmp", base_branch="dev",
                              backlog_backend="none")], audit_path="/tmp/audit.jsonl",
              use_worktree=False)


class _Audit:
    def __init__(self): self.events = []
    def record(self, e, **k): self.events.append({"event": e, **k})


async def _fake_run_agent(prompt, options, tag="", ticket_id=None, **kw):
    chk("plan: run_agent tagged 'planner' with ticket_id", tag == "planner" and ticket_id == "EU-1")
    return AgentRun(text=CLEAN, final=CLEAN, cost_usd=0.03, num_turns=2, is_error=False,
                    tools=["Read"], input_tokens=5000, output_tokens=300,
                    provider="Anthropic", model_version="claude-opus-4-8")


_orig = planner.run_agent
planner.run_agent = _fake_run_agent
au = _Audit()
res = asyncio.run(planner.plan(_cfg, _ticket(), audit=au))
planner.run_agent = _orig
chk("plan: parsed the agent reply", res.verdict == "BUILD" and res.in_scope_files == ["orchestrator/deploy.py"])
chk("plan: burn threaded onto the result (Planner spend is countable)",
    abs(res.cost_usd - 0.03) < 1e-9 and res.input_tokens == 5000 and res.output_tokens == 300,
    f"{res.cost_usd},{res.input_tokens},{res.output_tokens}")
chk("plan: 'planner' audit event with verdict + counts",
    any(e["event"] == "planner" and e["verdict"] == "BUILD" and e["testable_ac"] == 2
        for e in au.events), str(au.events))


async def _boom(prompt, options, tag="", **kw):
    raise RuntimeError("model down")


planner.run_agent = _boom
res_err = asyncio.run(planner.plan(_cfg, _ticket(), audit=_Audit()))
planner.run_agent = _orig
chk("plan: agent exception → BUILD result, never raises",
    res_err.verdict == "BUILD" and "planner error" in res_err.raw, res_err.raw[:80])

# ══════════════════════════════════════════════════════════════════════════════
passed = sum(1 for _, ok, _ in results if ok)
print(f"\nplanner_test: {passed}/{len(results)} passed")
for n, ok, det in results:
    if not ok:
        print(f"  FAIL: {n}" + (f" — {det}" if det else ""))
sys.exit(0 if passed == len(results) else 1)
