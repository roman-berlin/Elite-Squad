"""EU-173 crash pin (2026-07-05 forensics) — the gate-fail path must never NameError on commenter.

The crash: manual commit 745fedc ("Resolve conflicts from stash", 2026-07-01 14:17) referenced
`commenter` inside loop._attempt without threading the parameter. The gate-fail branch calls
commenter.summarize_gate_event() UNCONDITIONALLY, so the first failing gate after the restart
killed EU-173 with `NameError: name 'commenter' is not defined` (audit.jsonl line 2541,
2026-07-01T15:17:11). Fixed 17 minutes later in 0ab888a by threading the kwarg and defaulting a
TicketCommenter when the caller omits it (loop._attempt's `if commenter is None` head).

This harness pins the fixed contract: _attempt driven WITHOUT a commenter kwarg, into a FAILING
gate, must (1) not raise, (2) construct the default TicketCommenter and call
summarize_gate_event("Gate", "FAILED", …) on every failing pass, (3) resolve the exhausted attempt
normally (ESCALATED + needs_human), exactly what EU-173 never got to do.
"""
import asyncio
import sys
import tempfile
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop                       # noqa: E402
from orchestrator import jira_adapter                  # noqa: E402
from orchestrator.config import Config, AppConfig      # noqa: E402
from orchestrator.contracts import (Ticket, BuildResult, GateResult,   # noqa: E402
                                    Outcome)

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


class Audit:
    def __init__(s): s.ev = []
    def record(s, e, **k): s.ev.append({"event": e, **k})


class Git:
    def has_changes(s): return True
    def diff_against_base(s): return "diff --git a/x b/x\n+change"
    def changed_paths(s): return ["x.py"]


class OkBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")
    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        return BuildResult(ok=True, summary="did it", cost_usd=0.0, num_turns=1, raw="did it", tools=[])


loop._notify = lambda c, t: None
loop.builder_mod = OkBuilder
# The EU-173 trigger: the verification gate FAILS (that day: a pre-existing red base branch).
loop.run_gate = lambda app, changed_paths=None, **_: GateResult(
    passed=False, report="✗ scheduler_single_source_test.py soft-tally FAIL: only 16/17")

# Record every summarize_gate_event on the REAL TicketCommenter class — proves the default
# instance was constructed and the crash line executed (no LLM call: stub the summarizer).
gate_summaries = []
_orig_summarize = jira_adapter.TicketCommenter.summarize_gate_event
def _spy_summarize(self, gate, status, details, ticket_id):
    gate_summaries.append((gate, status, ticket_id))
    return None   # None → nothing to post; the loop must carry on regardless
jira_adapter.TicketCommenter.summarize_gate_event = _spy_summarize

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False,
             per_ticket_token_budget=0, per_ticket_time_budget_min=0)
app = cfg.app("automatixy")
ticket = Ticket(id="EU-173", key="EU-173", summary="s", description="d", ephemeral=True,
                app="automatixy")

au = Audit()
try:
    # THE regression: no commenter kwarg — exactly how server.py/decisions.py entry points call it.
    rep = asyncio.run(loop._attempt(ticket, app, cfg, Git(), None, au, loop.Budget(0),
                                    "autodev/EU-173"))
    crashed = None
except NameError as exc:      # the EU-173 failure mode, verbatim
    rep, crashed = None, exc
finally:
    jira_adapter.TicketCommenter.summarize_gate_event = _orig_summarize

chk("gate-fail path without a commenter kwarg does not NameError (EU-173, audit l.2541)",
    crashed is None, repr(crashed))
chk("a default TicketCommenter was constructed and the crash line ran on every failing pass",
    len(gate_summaries) >= 1 and all(g == ("Gate", "FAILED", "EU-173") for g in gate_summaries),
    str(gate_summaries))
chk("the attempt still resolves normally (ESCALATED — what EU-173 never reached)",
    rep is not None and rep.outcome == Outcome.ESCALATED, str(rep and rep.outcome))
chk("needs_human recorded — the escalation completed instead of dying mid-gate",
    any(e["event"] == "needs_human" for e in au.ev))

print("\n============ EU-173 COMMENTER CRASH PIN ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
