"""EU-38 QA — the Builder must TAG each build pass into the usage ledger with the ticket id + pass
number (one of the ticket's acceptance criteria). `_solo_build` now calls `run_agent(..., tag="builder",
ticket_id=..., pass_number=...)`, so the REAL `agent.run_agent` has to accept those kwargs — otherwise
every live builder pass dies with `TypeError` before a single token is spent.

The rest of the suite monkeypatches `run_agent`, so a signature drift between the call site (builder.py)
and the definition (agent.py) is invisible there. This harness asserts the two are compatible (regression
that FAILS on the un-updated agent.py and PASSES once it accepts the kwargs) AND that `_solo_build`
forwards the right values (happy path)."""
import asyncio, inspect, sys, types

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

from orchestrator import builder, agent
from orchestrator.agent import AgentRun
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, BuildRequest

# ============================================================================
# Regression: the REAL run_agent must accept the kwargs builder._solo_build passes.
# Pins the exact defect — builder.py calls run_agent(ticket_id=, pass_number=) but if
# agent.run_agent's signature doesn't accept them, the live pass TypeErrors. The fake
# run_agent used elsewhere hides this; here we bind against the genuine signature.
# ============================================================================
sig = inspect.signature(agent.run_agent)
params = sig.parameters
has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
check("regression: real run_agent accepts ticket_id",
      "ticket_id" in params or has_var_kw, str(list(params)))
check("regression: real run_agent accepts pass_number",
      "pass_number" in params or has_var_kw, str(list(params)))
try:
    sig.bind("prompt", "options", tag="builder", ticket_id="EU-38", pass_number=4)
    bind_ok, bind_err = True, ""
except TypeError as e:
    bind_ok, bind_err = False, str(e)
check("regression: builder's run_agent call binds to the real signature", bind_ok, bind_err)

# ============================================================================
# Happy path: _solo_build forwards ticket_id=ticket.id and pass_number=iteration.
# Drives the real _solo_build with run_agent + heavy deps stubbed to capture the call.
# ============================================================================
captured = {}
async def capture_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None, cfg=None):
    captured.update(prompt=prompt, tag=tag, ticket_id=ticket_id, pass_number=pass_number)
    return AgentRun(text="ok", final="built", cost_usd=0.1, num_turns=3, is_error=False, tools=["Edit"])

class _FakeGuard:
    @staticmethod
    def warn_if_absent(_): pass
    @staticmethod
    def hooks_config(): return None
class _FakeModels:
    @staticmethod
    def for_builder(cfg, ticket, eff, it): return ("sonnet", "test-pin")
    # EU-108: stub the new fallback exports so run_agent_with_fallback can import them
    SONNET = "claude-sonnet-4-6"
    OPUS = "claude-opus-4-8"
    @staticmethod
    def activate_sonnet_fallback(until_epoch): pass
    @staticmethod
    def _get_next_friday_0900_utc(): return 0
    @staticmethod
    def sonnet_fallback_notification_sent(): return False
    @staticmethod
    def mark_sonnet_fallback_notified(): pass
    @staticmethod
    def fallback_reset_time_str(): return ""
    @staticmethod
    def sonnet_fallback_active(cfg): return False

# EU-108: patch run_agent_with_fallback in builder to avoid real async iteration
builder.run_agent_with_fallback = capture_run_agent
sys.modules["orchestrator.guard"] = _FakeGuard          # `from . import guard` -> our fake
sys.modules["orchestrator.models"] = _FakeModels        # `from . import models`
_orig_preamble = builder.memory.preamble
builder.memory.preamble = lambda: "UNIT MEMORY HEAD\n"

tk = Ticket(id="EU-38", key="EU-38", summary="cut context bloat", description="d",
            acceptance_criteria=["tag the ledger"], app="automatixy")
app = AppConfig(name="automatixy", repo_path="/tmp/x", base_branch="DEV", protected_branch="MAIN",
                backlog_backend="none")
req = BuildRequest(ticket=tk, branch="dev", iteration=3,
                   prior_issues=[f"issue {i}" for i in range(40)])
cfg = Config(apps=[app])

res = asyncio.run(builder._solo_build(req, app, cfg))
builder.memory.preamble = _orig_preamble

check("happy: _solo_build completed without error", res is not None and res.ok)
check("happy: tagged tag='builder'", captured.get("tag") == "builder", repr(captured.get("tag")))
check("happy: ticket_id forwarded == ticket.id", captured.get("ticket_id") == "EU-38",
      repr(captured.get("ticket_id")))
check("happy: pass_number forwarded == iteration", captured.get("pass_number") == 3,
      repr(captured.get("pass_number")))
# the same pass must have its feedback capped in the prompt that reaches run_agent
prompt = captured.get("prompt", "")
check("happy: prompt feeds back the NEWEST issue", "issue 39" in prompt)
check("happy: prompt drops the OLDEST issue", "- issue 0" not in prompt)
check("happy: prompt marks the elision", "elided" in prompt)

print("\n========= EU-38 LEDGER TAGGING / SIGNATURE QA =========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
