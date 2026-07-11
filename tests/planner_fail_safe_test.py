"""EU-266: the Planner's fail-safe must not be silently indistinguishable from a real 0-AC plan.

`planner.py` used to collapse BOTH a genuine parse/agent error AND a legitimately empty-AC plan into
the same `PlannerResult(verdict="BUILD", testable_ac=[])` shape — starving the Builder of a fail-first
AC contract with no signal that anything went wrong. This harness pins the fix (no live model — SDK
stubbed, same pattern as planner_test.py):

  1. parse_plan on unparsable text sets plan_extraction_failed=True (still BUILD, still empty AC).
  2. parse_plan on a genuine empty-AC JSON plan sets plan_extraction_failed=False — distinguishable
     from (1).
  3. plan() when the agent call raises sets plan_extraction_failed=True on the result AND the audit
     hook sees a distinct failure signal (not a plain successful 'planner' record).
  4. loop.py's planner-consumption path references _pres.plan_extraction_failed and does NOT print the
     normal 'planner · BUILD · N testable AC' success line when it's set.
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
# 1. parse_plan — an unparsable reply is a FAILURE, distinct from a legitimate empty plan
# ══════════════════════════════════════════════════════════════════════════════
p_fail = planner.parse_plan("no json here at all")
chk("parse: unparsable reply → verdict BUILD, empty AC (unchanged fail-safe shape)",
    p_fail.verdict == "BUILD" and p_fail.testable_ac == [], str(p_fail))
chk("parse: unparsable reply → plan_extraction_failed=True (the new distinct signal)",
    getattr(p_fail, "plan_extraction_failed", None) is True, str(p_fail))

# ══════════════════════════════════════════════════════════════════════════════
# 2. parse_plan — a genuine 0-AC plan is NOT an extraction failure
# ══════════════════════════════════════════════════════════════════════════════
p_zero = planner.parse_plan('{"verdict":"BUILD","testable_ac":[],"approach":"x"}')
chk("parse: genuine 0-AC JSON plan parses to BUILD with empty AC",
    p_zero.verdict == "BUILD" and p_zero.testable_ac == [], str(p_zero))
chk("parse: genuine 0-AC plan → plan_extraction_failed=False (distinguishable from a real failure)",
    getattr(p_zero, "plan_extraction_failed", None) is False, str(p_zero))

# a normal, fully-populated plan must also default to False
CLEAN = ('{"verdict": "BUILD", "approach": "Wrap deploy in a retry loop (deploy.py).",'
         ' "testable_ac": ["3 failures → aborts"], "in_scope_files": ["orchestrator/deploy.py"]}')
p_ok = planner.parse_plan(CLEAN)
chk("parse: a normal populated plan → plan_extraction_failed=False",
    getattr(p_ok, "plan_extraction_failed", None) is False, str(p_ok))

# ══════════════════════════════════════════════════════════════════════════════
# 3. plan() — an agent exception is a distinct failure signal, both on the result and via audit
# ══════════════════════════════════════════════════════════════════════════════
_cfg = Config(apps=[AppConfig(name="automatixy", repo_path="/tmp", base_branch="dev",
                              backlog_backend="none")], audit_path="/tmp/audit.jsonl",
              use_worktree=False)


class _Audit:
    def __init__(self): self.events = []
    def record(self, e, **k): self.events.append({"event": e, **k})


async def _boom(prompt, options, tag="", **kw):
    raise RuntimeError("model down")


_orig_run_agent = planner.run_agent
planner.run_agent = _boom
au = _Audit()
res_err = asyncio.run(planner.plan(_cfg, _ticket(), audit=au))
planner.run_agent = _orig_run_agent

chk("plan: agent exception → BUILD result, never raises",
    res_err.verdict == "BUILD", res_err.raw[:80])
chk("plan: agent exception → plan_extraction_failed=True on the result",
    getattr(res_err, "plan_extraction_failed", None) is True, str(res_err))
chk("plan: agent exception → a DISTINCT audit signal (not just a plain successful 'planner' record)",
    any(e.get("plan_extraction_failed") or e.get("event") == "planner_extraction_failed"
        for e in au.events),
    str(au.events))

# ══════════════════════════════════════════════════════════════════════════════
# 4. loop.py consumes the signal — grep-verifiable reference, and it must gate the success print
# ══════════════════════════════════════════════════════════════════════════════
import pathlib  # noqa: E402
loop_src = pathlib.Path("orchestrator/loop.py").read_text()
chk("loop.py: references _pres.plan_extraction_failed in the planner consumption path",
    "plan_extraction_failed" in loop_src)
chk("loop.py: guards the normal BUILD success print behind the failure check "
    "(extraction failure must not print the silent-success line)",
    "plan_extraction_failed" in loop_src and "planner · BUILD ·" in loop_src)

# ══════════════════════════════════════════════════════════════════════════════
passed = sum(1 for _, ok, _ in results if ok)
print(f"\nplanner_fail_safe_test: {passed}/{len(results)} passed")
for n, ok, det in results:
    if not ok:
        print(f"  FAIL: {n}" + (f" — {det}" if det else ""))
sys.exit(0 if passed == len(results) else 1)
