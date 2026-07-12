"""EU-248: max-turns exhaustion misclassified as a crash.

"Command failed with exit code 1" is not a crash — it is max-turns exhaustion (turnCount 97 /
maxTurns 96) surfacing as a process error. The claude CLI exits 1 by design after an
error_max_turns result; the SDK's structured-error replacement is racy, so a trailing raw
ProcessError reaches the orchestrator instead. Before this fix that either got silently re-raised
past the usage ledger / audit trail (unmetered spend) or, on the clean path, parked the ticket as
Outcome.ERRORED — wrongly ticking the EU-219 consecutive-error counter toward Blocked, over and
over, for a ticket that just needed to be split.

Two seams, pinned here:
  agent.py — (a) an error_max_turns ResultMessage (or num_turns >= max_turns) sets a machine-
             readable AgentRun.is_turn_limit flag; (b) a raw ProcessError trailing a CONSUMED
             result degrades to a clean is_error return (metering/audit still fire, no exception
             escapes) instead of re-raising; (c) a ProcessError with NO result seen is a genuine
             crash and still re-raises; (d) the narrowed "...error result: success" quirk match
             still degrades cleanly, but "...error result: error_during_execution" is NOT swallowed.
  loop.py  — a failed build whose num_turns >= builder_mod.turns_for(cfg, eff) routes to
             _try_scrum_split (scrum_split/REQUEUED), not Outcome.ERRORED — so the EU-219 counter
             (which only bumps on Outcome.ERRORED, autopilot.py:882-893) is never touched by this
             class of failure.
"""
import sys, types, asyncio, json, tempfile
from pathlib import Path

# ---- SDK stub (must precede orchestrator imports) ---- #
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


class ProcessError(Exception):
    """Mirrors claude_agent_sdk.ProcessError's shape closely enough for isinstance() checks."""
    def __init__(self, message, exit_code=None, stderr=None):
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(message)


sdk.AssistantMessage = AssistantMessage
sdk.ResultMessage = ResultMessage
sdk.TextBlock = TextBlock
sdk.ToolUseBlock = ToolUseBlock
sdk.ClaudeAgentOptions = ClaudeAgentOptions
sdk.ProcessError = ProcessError
sdk.query = None   # set per-test below
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.agent as agent            # noqa: E402
import orchestrator.loop as loop              # noqa: E402
from orchestrator import usage, notify        # noqa: E402
from orchestrator.config import Config, AppConfig                      # noqa: E402
from orchestrator.contracts import Ticket, BuildResult, Outcome        # noqa: E402

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


notify.send = lambda *a, **k: None


def _query_for(events, raise_after=None):
    async def fake_query(prompt=None, options=None):
        for e in events:
            yield e
        if raise_after is not None:
            raise raise_after
    return fake_query


class _AuditSink:
    def __init__(self):
        self.events = []

    def record(self, event, **fields):
        self.events.append((event, fields))


# ==================================================================================================
# AC (a): error_max_turns ResultMessage then a trailing raw ProcessError -> clean AgentRun,
#         is_turn_limit set, no exception, exactly one usage-ledger row + one agent_call audit event.
# ==================================================================================================
tmp = Path(tempfile.mkdtemp())
usage.configure(tmp / "audit.jsonl")
sink = _AuditSink()
agent.configure_audit(sink)

opts = ClaudeAgentOptions(model="claude-opus-4-8", max_turns=96)
agent.query = _query_for(
    [AssistantMessage(content=[TextBlock(text="working…")], error=None),
     ResultMessage(subtype="error_max_turns", is_error=True, num_turns=97, total_cost_usd=1.23,
                  result="", usage={"input_tokens": 40000, "output_tokens": 800})],
    raise_after=ProcessError("Command failed with exit code 1", exit_code=1,
                             stderr="Check stderr output for details"))
run = asyncio.run(agent._run_agent_unrouted("p", opts, tag="builder",
                                            ticket_id="EU-248", pass_number=1))
agent.configure_audit(None)

chk("trailing ProcessError after error_max_turns raises NO exception", run is not None)
chk("AgentRun.is_error is set", run.is_error)
chk("AgentRun.is_turn_limit (the new machine-readable flag) is set", run.is_turn_limit)
chk("metering fields survived (cost/turns from the consumed result)",
    run.cost_usd == 1.23 and run.num_turns == 97, (run.cost_usd, run.num_turns))

ledger_rows = [json.loads(l) for l in (tmp / "usage_ledger.jsonl").read_text().splitlines() if l.strip()]
chk("exactly one usage-ledger row recorded (not skipped)", len(ledger_rows) == 1, str(ledger_rows))

call_events = [f for e, f in sink.events if e == "agent_call"]
chk("exactly one agent_call audit event recorded (not skipped)", len(call_events) == 1, str(sink.events))
if call_events:
    chk("the agent_call audit event carries the real turn count",
        call_events[0].get("turns") == 97, str(call_events[0]))

# ---- the same subtype signal also fires via the plain num_turns >= max_turns fallback ---- #
agent.query = _query_for(
    [ResultMessage(subtype="success", is_error=True, num_turns=96, total_cost_usd=0.5, result="",
                  usage={"input_tokens": 100, "output_tokens": 50})],
    raise_after=ProcessError("Command failed with exit code 1"))
run_fallback = asyncio.run(agent._run_agent_unrouted(
    "p", ClaudeAgentOptions(model="claude-opus-4-8", max_turns=96), tag="builder"))
chk("num_turns >= max_turns ALSO sets is_turn_limit (subtype needn't be error_max_turns)",
    run_fallback.is_turn_limit, (run_fallback.num_turns,))

# ==================================================================================================
# AC (c): a mid-stream ProcessError with NO ResultMessage seen still re-raises onto the infra path.
# ==================================================================================================
agent.query = _query_for([AssistantMessage(content=[TextBlock(text="hi")], error=None)],
                         raise_after=ProcessError("Command failed with exit code 1"))
raised = False
try:
    asyncio.run(agent._run_agent_unrouted("p", ClaudeAgentOptions(model="claude-opus-4-8"),
                                          tag="builder"))
except ProcessError:
    raised = True
chk("mid-stream ProcessError with saw_result False still re-raises (crash path preserved)", raised)

# ==================================================================================================
# AC (d): "...error result: success" degrades cleanly (narrowed quirk match); a genuine
#         "error_during_execution" result is NOT swallowed and re-raises.
# ==================================================================================================
REAL_ERR = "API Error: 500 internal error — upstream provider failure"
agent.query = _query_for(
    [ResultMessage(is_error=True, result=REAL_ERR, num_turns=4, total_cost_usd=0.02,
                  usage={"input_tokens": 10, "output_tokens": 5})],
    raise_after=Exception("Claude Code returned an error result: success"))
run_quirk = asyncio.run(agent._run_agent_unrouted("p", ClaudeAgentOptions(model="claude-opus-4-8"),
                                                  tag="builder"))
chk("'...result: success' quirk still degrades to a clean is_error return",
    run_quirk is not None and run_quirk.is_error)
chk("the clean degrade preserves the REAL provider text, not 'success'",
    "upstream provider failure" in (run_quirk.final or ""), run_quirk.final)
chk("a max-turns quirk-degrade is NOT itself flagged turn-limit (no error_max_turns subtype seen)",
    not run_quirk.is_turn_limit)

agent.query = _query_for(
    [ResultMessage(is_error=True, result="boom", num_turns=4, total_cost_usd=0.02,
                  usage={"input_tokens": 10, "output_tokens": 5})],
    raise_after=Exception("Claude Code returned an error result: error_during_execution"))
raised_exec = False
try:
    asyncio.run(agent._run_agent_unrouted("p", ClaudeAgentOptions(model="claude-opus-4-8"),
                                          tag="builder"))
except Exception as e:
    raised_exec = "error_during_execution" in str(e)
chk("'...error result: error_during_execution' is NOT swallowed — it re-raises", raised_exec)


# ==================================================================================================
# AC (b): loop-level routing — a failed build at/above the turn ceiling goes to _try_scrum_split
#         (scrum_split/REQUEUED), never Outcome.ERRORED, so the EU-219 consecutive-error counter
#         (autopilot.py:882-893 only bumps on Outcome.ERRORED) is structurally never touched.
# ==================================================================================================
from orchestrator import scrum as _scrum_mod   # noqa: E402


class _Git:
    def has_changes(self): return True
    def diff_against_base(self): return "diff --git a/x b/x\n+change"
    def changed_paths(self): return ["x.py"]


class _FakeBuilderTurnLimit:
    """A build that died with is_error=True at exactly the pass's turn ceiling — the EU-248 class:
    no turn-limit phrase anywhere in its summary/raw, only num_turns tells the story."""
    @staticmethod
    def effort_plan(cfg, it, ticket):
        return ("high", "sized")

    @staticmethod
    def turns_for(cfg, eff):
        return 96

    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        return BuildResult(ok=False, summary="ran out of runway mid-refactor", cost_usd=3.5,
                           num_turns=97, raw="(last assistant message — no turn-limit phrase)",
                           tools=[])


class _FakeBuilderRealCrash:
    """A build that died BELOW the turn ceiling — a genuine failure, must stay ERRORED (no split)."""
    @staticmethod
    def effort_plan(cfg, it, ticket):
        return ("high", "sized")

    @staticmethod
    def turns_for(cfg, eff):
        return 96

    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        return BuildResult(ok=False, summary="crashed early", cost_usd=0.1, num_turns=5,
                           raw="Traceback...", tools=[])


class Audit:
    def __init__(self):
        self.ev = []

    def record(self, e, **k):
        self.ev.append({"event": e, **k})


notified = []
loop._notify = lambda c, t: notified.append(t)
loop.run_gate = lambda app, changed_paths=None, **_: None

tmp2 = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp2 / "audit.jsonl"), use_worktree=False)
app = cfg.app("automatixy")


async def _split_ok(cfg_, app_name, ticket_, recap="", reason="", **k):
    return {"ok": True, "keys": ["AUTO-248a", "AUTO-248b"]}


_scrum_mod.split = _split_ok
loop.builder_mod = _FakeBuilderTurnLimit
tkt = Ticket(id="AUTO-248", key="AUTO-248", summary="s", description="d", ephemeral=False, app="automatixy")
au = Audit()
rep = asyncio.run(loop._attempt(tkt, app, cfg, _Git(), None, au, loop.Budget(0), "autodev/AUTO-248"))

chk("build at/above the turn ceiling -> Outcome.REQUEUED (Scrum split), not ERRORED",
    rep.outcome == Outcome.REQUEUED, str(rep.outcome))
scrum_events = [e for e in au.ev if e["event"] == "scrum_split"]
chk("routes through _try_scrum_split: scrum_split audit event with reason='turn-limit'",
    any(e.get("reason") == "turn-limit" and e.get("into") for e in scrum_events), str(scrum_events))
# Mirrors the increment predicate at autopilot.py:882-893 verbatim: only Outcome.ERRORED bumps the
# EU-219 consecutive-error counter — REQUEUED structurally can never trip it.
chk("EU-219 consecutive-error counter would NOT be incremented (outcome is not ERRORED)",
    rep.outcome is not Outcome.ERRORED, str(rep.outcome))

# ---- when the Scrum Master declines, the clean path still falls back to ERRORED (no crash) ---- #
async def _split_no(cfg_, app_name, ticket_, recap="", reason="", **k):
    return {"ok": False, "keys": []}


_scrum_mod.split = _split_no
au2 = Audit()
tkt2 = Ticket(id="AUTO-249", key="AUTO-249", summary="s", description="d", ephemeral=False, app="automatixy")
rep2 = asyncio.run(loop._attempt(tkt2, app, cfg, _Git(), None, au2, loop.Budget(0), "autodev/AUTO-249"))
chk("unsplittable turn-limit build still falls back to Outcome.ERRORED (no crash)",
    rep2.outcome == Outcome.ERRORED, str(rep2.outcome))

# ---- a build failing WELL BELOW the turn ceiling is a real crash -> straight to ERRORED, no split ---- #
async def _split_boom(cfg_, app_name, ticket_, recap="", reason="", **k):
    raise AssertionError("must not attempt a split for a build that didn't hit the turn ceiling")


_scrum_mod.split = _split_boom
loop.builder_mod = _FakeBuilderRealCrash
au3 = Audit()
tkt3 = Ticket(id="AUTO-250", key="AUTO-250", summary="s", description="d", ephemeral=False, app="automatixy")
rep3 = asyncio.run(loop._attempt(tkt3, app, cfg, _Git(), None, au3, loop.Budget(0), "autodev/AUTO-250"))
chk("build failure BELOW the turn ceiling -> ERRORED directly, split never attempted",
    rep3.outcome == Outcome.ERRORED, str(rep3.outcome))
chk("no scrum_split event for a genuine sub-ceiling crash",
    not any(e["event"] == "scrum_split" for e in au3.ev), str(au3.ev))

print("\n============ EU-248 TURN-LIMIT CLASSIFICATION QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
