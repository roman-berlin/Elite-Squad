"""EU-258 QA — the Reviewer must TAG each review pass into the usage ledger with the ticket id +
pass number, exactly as the Builder has since EU-38.

The defect: `reviewer.review` called `run_agent_with_fallback(..., tag="reviewer", cfg=cfg,
routing_tier=...)` and simply omitted `ticket_id`/`pass_number`, though both are in scope from its own
signature. The plumbing was never the problem — `agent.run_agent_with_fallback` accepts the kwargs and
`usage.record` writes them as ledger fields `k`/`p`; the call site just never passed them. Live evidence
at triage (2026-07-16): 480/480 `tag="reviewer"` rows in state/usage_ledger.jsonl carried no `k` =
$430.50 unattributable (13.9% of $3,086.21 window spend). Ledger/offline-analytics only — loop.py's
`_burn("reviewer", ...)` feeds the in-memory store the live budget gates sum, so per-ticket budgets
were never affected.

tests/eu38_ledger_tagging_test.py pins the BUILDER call site only, which is why this drifted unpinned.
Same three-part shape as that harness: bind the real signature (a drift there kills every live review
pass with TypeError, and the monkeypatched `run_agent_with_fallback` everywhere else hides it), assert
`review` forwards the right values, and assert the row `usage.record` actually emits carries k + p.
"""
import asyncio, inspect, json, sys, tempfile, types
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

from orchestrator import agent, reviewer, usage
from orchestrator.agent import AgentRun
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, Verdict

# ============================================================================
# Regression: the REAL run_agent_with_fallback must accept the kwargs reviewer.review passes.
# Binds against the genuine signature — the fake used elsewhere in the suite would hide a drift.
# ============================================================================
sig = inspect.signature(agent.run_agent_with_fallback)
params = sig.parameters
has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
check("regression: real run_agent_with_fallback accepts ticket_id",
      "ticket_id" in params or has_var_kw, str(list(params)))
check("regression: real run_agent_with_fallback accepts pass_number",
      "pass_number" in params or has_var_kw, str(list(params)))
try:
    sig.bind("prompt", "options", tag="reviewer", ticket_id="EU-258", pass_number=2,
             cfg=None, routing_tier=None)
    bind_ok, bind_err = True, ""
except TypeError as e:
    bind_ok, bind_err = False, str(e)
check("regression: reviewer's call shape binds to the real signature", bind_ok, bind_err)

# usage.record is the far end of the plumbing — it must accept them too.
rec_params = inspect.signature(usage.record).parameters
check("regression: usage.record accepts ticket_id + pass_number",
      "ticket_id" in rec_params and "pass_number" in rec_params, str(list(rec_params)))

# ============================================================================
# Happy path: review() forwards ticket_id=ticket.id and pass_number=iteration.
# Drives the real review() with run_agent_with_fallback stubbed to capture the call.
# ============================================================================
_VERDICT_JSON = json.dumps({
    "verdict": "PASS",
    "spec_conformance": {"met": True, "gaps": []},
    "quality": {"issues": []},
    "required_changes": [],
    "summary": "ok",
})

captured = {}
async def capture_fallback(prompt, options, tag="", ticket_id=None, pass_number=None,
                           cfg=None, routing_tier=None):
    captured.update(tag=tag, ticket_id=ticket_id, pass_number=pass_number)
    return AgentRun(text=_VERDICT_JSON, final=_VERDICT_JSON, cost_usd=0.2, num_turns=5,
                    is_error=False, tools=["Read"])

class _FakeModels:
    @staticmethod
    def for_reviewer(cfg, diff, iteration): return ("sonnet", "test-pin")
    SONNET = "claude-sonnet-5"
    OPUS = "claude-opus-4-8"

reviewer.run_agent_with_fallback = capture_fallback
sys.modules["orchestrator.models"] = _FakeModels     # `from . import models` inside review()
_orig_preamble = reviewer.memory.preamble
reviewer.memory.preamble = lambda: "UNIT MEMORY HEAD\n"

tk = Ticket(id="EU-258", key="EU-258", summary="tag reviewer ledger rows",
            description="d", acceptance_criteria=["row carries k and p"], app="automatixy")
app = AppConfig(name="automatixy", repo_path="/tmp/x", base_branch="DEV",
                protected_branch="MAIN", backlog_backend="none")
cfg = Config(apps=[app])

res = asyncio.run(reviewer.review("diff --git a/x b/x\n+one line", tk, app, cfg, iteration=2))
reviewer.memory.preamble = _orig_preamble

check("happy: review() completed and parsed a verdict", res is not None and res.verdict == Verdict.PASS)
check("happy: tagged tag='reviewer'", captured.get("tag") == "reviewer", repr(captured.get("tag")))
check("happy: ticket_id forwarded == ticket.id", captured.get("ticket_id") == "EU-258",
      repr(captured.get("ticket_id")))
check("happy: pass_number forwarded == iteration", captured.get("pass_number") == 2,
      repr(captured.get("pass_number")))

# ============================================================================
# End-to-end: the ledger row those kwargs produce carries k + p. This is the ticket's actual
# acceptance criterion — asserting the forwarding alone would not prove the row is sliceable.
# ============================================================================
with tempfile.TemporaryDirectory() as td:
    usage.configure(Path(td) / "audit.jsonl")
    usage.record("claude-sonnet-5", 1000, 200, cost_usd=0.2, tag="reviewer",
                 ticket_id=captured.get("ticket_id"), pass_number=captured.get("pass_number"))
    rows = [json.loads(l) for l in (Path(td) / "usage_ledger.jsonl").read_text().splitlines() if l.strip()]
    row = next((r for r in rows if r.get("g") == "reviewer"), {})   # "g" is the tag; "t" is the timestamp
    check("e2e: reviewer ledger row carries k (ticket id)", row.get("k") == "EU-258", repr(row))
    check("e2e: reviewer ledger row carries p (pass number)", row.get("p") == 2, repr(row))

print("\n========= EU-258 REVIEWER LEDGER ATTRIBUTION QA =========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
