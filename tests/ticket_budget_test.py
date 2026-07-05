"""QW4 (2026-07-05 audit) — per-ticket budget + per-call instrumentation.

Pins the three guarantees added after the EU-174 forensics (15,535,048 builder tokens / 79 min on
one unwinnable ticket, with no per-ticket bound and zero duration fields anywhere in audit.jsonl):

  1. A ticket whose accumulated token burn crosses cfg.per_ticket_token_budget is PARKED before
     the next pass: Outcome.ESCALATED, a `ticket_budget_exceeded` audit event, a pending decision
     (the BLOCKED path), and a Telegram notify — never a silent continuation.
  2. Budgets set to 0 disable the check (the pass runs normally).
  3. Every agent call is instrumented: run_agent stamps wall-clock duration_s onto the AgentRun,
     the usage ledger row (key "d"), and — when a process configures the sink — an `agent_call`
     audit event carrying model / tokens in+out / cost / duration.
"""
import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path

# ---- SDK stub (must precede orchestrator imports): a query() that yields one ResultMessage ---- #
sdk = types.ModuleType("claude_agent_sdk")


class _Msg:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class AssistantMessage(_Msg):
    pass


class ResultMessage(_Msg):
    pass


class TextBlock(_Msg):
    pass


class ToolUseBlock(_Msg):
    pass


class ClaudeAgentOptions:
    def __init__(self, **kw):
        self.__dict__.update(kw)


async def _fake_query(prompt, options):
    yield ResultMessage(total_cost_usd=0.5, num_turns=3, is_error=False, result="done",
                        usage={"input_tokens": 100, "cache_read_input_tokens": 50,
                               "cache_creation_input_tokens": 0, "output_tokens": 25},
                        error=None)


sdk.AssistantMessage = AssistantMessage
sdk.ResultMessage = ResultMessage
sdk.TextBlock = TextBlock
sdk.ToolUseBlock = ToolUseBlock
sdk.ClaudeAgentOptions = ClaudeAgentOptions
sdk.query = _fake_query
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.agent as agent            # noqa: E402
import orchestrator.loop as loop              # noqa: E402
from orchestrator import decisions, usage     # noqa: E402
from orchestrator.config import Config, AppConfig                       # noqa: E402
from orchestrator.contracts import (Ticket, BuildResult, GateResult,    # noqa: E402
                                    TestEngineerResult, Outcome, TicketReport)

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ---------------------------------------------------------------------------------------------- #
# 1) Token-budget breach → parked BEFORE the next pass (ESCALATED + audit + decision + notify)
# ---------------------------------------------------------------------------------------------- #
class Audit:
    def __init__(s): s.ev = []
    def record(s, e, **k): s.ev.append({"event": e, **k})


class Git:
    def has_changes(s): return True
    def diff_against_base(s): return "diff --git a/x b/x\n+change"
    def changed_paths(s): return ["x.py"]


notified = []
loop._notify = lambda c, t: notified.append(t)
loop.run_gate = lambda app, changed_paths=None, **_: GateResult(passed=True, report="")


class FatBuilder:
    """Burns 500k tokens in pass 1 — over the 400k default before pass 2's check."""
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")
    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        return BuildResult(ok=True, summary="did it", cost_usd=1.0, num_turns=9, raw="did it",
                           tools=[], input_tokens=490_000, output_tokens=10_000)


async def fake_te(ticket, app, cfg, **_):
    return TestEngineerResult(ok=True, coverage="lines 80%")


class FailReviewer:
    @staticmethod
    async def review(diff, ticket, app, cfg, iteration=1, **_):
        from orchestrator.contracts import ReviewResult, Verdict
        return ReviewResult(verdict=Verdict.FAIL, spec_met=False,
                            required_changes=[f"issue #{iteration}"], cost_usd=0.0)


loop.builder_mod = FatBuilder
loop.test_engineer_mod.ensure_coverage = fake_te
loop.reviewer_mod = FailReviewer

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
app = cfg.app("automatixy")
tkt = Ticket(id="AUTO-99", key="AUTO-99", summary="s", description="d", ephemeral=True, app="automatixy")

au = Audit()
rep = asyncio.run(loop._attempt(tkt, app, cfg, Git(), None, au, loop.Budget(0), "autodev/AUTO-99"))

chk("breach → Outcome.ESCALATED (parked, not continued)", rep.outcome == Outcome.ESCALATED,
    str(rep.outcome))
chk("breach → notes name the per-ticket budget", "per-ticket budget exceeded" in (rep.notes or ""),
    rep.notes)
budget_ev = next((e for e in au.ev if e["event"] == "ticket_budget_exceeded"), None)
chk("breach → ticket_budget_exceeded audit event with the measured burn",
    budget_ev is not None and budget_ev.get("tokens_burned", 0) >= 400_000, str(budget_ev))
chk("breach → only ONE build pass ran (the check fires before pass 2)",
    budget_ev is not None and budget_ev.get("iteration") == 2)
chk("breach → a pending decision was filed (the BLOCKED path)",
    any(d.get("id") == "AUTO-99" for d in decisions.load(cfg)), str(decisions.load(cfg)))
chk("breach → Telegram notify fired (never silent)", any("budget" in t for t in notified),
    str(notified))

# ---------------------------------------------------------------------------------------------- #
# 2) Budgets set to 0 disable the check
# ---------------------------------------------------------------------------------------------- #
cfg_off = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                                 protected_branch="MAIN", backlog_backend="none")],
                 audit_path=str(tmp / "audit2.jsonl"), use_worktree=False,
                 per_ticket_token_budget=0, per_ticket_time_budget_min=0)
app_off = cfg_off.app("automatixy")
au2 = Audit()
rep2 = asyncio.run(loop._attempt(Ticket(id="AUTO-98", key="AUTO-98", summary="s", description="d",
                                        ephemeral=True, app="automatixy"),
                                 app_off, cfg_off, Git(), None, au2, loop.Budget(0), "autodev/AUTO-98"))
chk("budgets=0 → no ticket_budget_exceeded event (check disabled)",
    not any(e["event"] == "ticket_budget_exceeded" for e in au2.ev))
chk("budgets=0 → the attempt runs to the pass cap instead",
    rep2.outcome == Outcome.ESCALATED and "max_iterations" in (rep2.notes or ""), rep2.notes)

# ---------------------------------------------------------------------------------------------- #
# 3) Per-call instrumentation: duration on AgentRun + ledger "d" + agent_call audit event
# ---------------------------------------------------------------------------------------------- #
usage.configure(tmp / "audit3.jsonl")
sink = Audit()
agent.configure_audit(sink)
run = asyncio.run(agent.run_agent("hi", ClaudeAgentOptions(model="claude-opus-4-8"),
                                  tag="builder", ticket_id="AUTO-97", pass_number=1))
agent.configure_audit(None)   # do not leak the sink into other harnesses

chk("run_agent returns token counts from the SDK usage block",
    run.input_tokens == 150 and run.output_tokens == 25,
    f"in={run.input_tokens} out={run.output_tokens}")
chk("run_agent stamps a wall-clock duration_s", run.duration_s >= 0.0, str(run.duration_s))
ledger_rows = [json.loads(l) for l in (tmp / "usage_ledger.jsonl").read_text().splitlines()]
chk("ledger row carries the duration key 'd'", ledger_rows and "d" in ledger_rows[-1],
    str(ledger_rows[-1] if ledger_rows else None))
call_ev = next((e for e in sink.ev if e["event"] == "agent_call"), None)
chk("agent_call audit event carries model + tokens + duration + ticket",
    call_ev is not None and call_ev.get("model") == "claude-opus-4-8"
    and call_ev.get("input_tokens") == 150 and call_ev.get("output_tokens") == 25
    and "duration_s" in call_ev and call_ev.get("ticket_id") == "AUTO-97", str(call_ev))

# ── tally ────────────────────────────────────────────────────────────────────────────────────── #
print("\n============ PER-TICKET BUDGET QA (QW4) ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
