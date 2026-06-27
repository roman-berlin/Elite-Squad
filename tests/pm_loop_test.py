"""PM wired into the build loop: decide -> re-build with the decision; escalate -> park + comment + move on."""
import sys, types, asyncio
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop
from orchestrator import pm as pm_mod
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, BuildResult, Outcome

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

HALT = "I must STOP and report — a precondition is not met; holding for the Commander. No files were written."

class Audit:
    def __init__(s): s.ev = []
    def record(s, e, **k): s.ev.append({"event": e, **k})
class Git:
    def has_changes(s): return False
class Backlog:
    def __init__(s): s.comments = []
    def add_comment(s, t, b): s.comments.append(b)
    def set_status(s, *a): pass

loop._notify = lambda c, t: None
dec_added = []
loop.decisions.add = lambda cfg, ticket, app, note: dec_added.append(note)

built = []
class FakeBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")
    @staticmethod
    async def build(req, app, cfg, audit=None, **_):   # EU-72: absorb store=/spec= kwargs
        built.append(req.ticket.description or "")
        return BuildResult(ok=True, summary=HALT, cost_usd=0.0, num_turns=1, raw=HALT, tools=[])
loop.builder_mod = FakeBuilder

def mkcfg(**kw):
    return Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path="/tmp/x.jsonl", use_worktree=False, max_iterations=3, **kw)
cfg = mkcfg()
app = cfg.app("automatixy")
ticket = Ticket(id="AUTO-14", key="AUTO-14", summary="s", description="orig spec", ephemeral=False, app="automatixy")

# ---- real _consult_pm: enable/disable/error gating ----
async def fake_review(cfg, app, ticket, question, context=""):
    return {"verdict": "DECIDE", "body": "ok"}
pm_mod.review = fake_review
_au0 = Audit()
chk("_consult_pm returns the PM verdict when enabled", asyncio.run(loop._consult_pm(cfg, ticket, app, _au0, "r"))["verdict"] == "DECIDE")
chk("_consult_pm audits the pm_review", any(e["event"] == "pm_review" for e in _au0.ev))
chk("_consult_pm is None when pm_enabled is off", asyncio.run(loop._consult_pm(mkcfg(pm_enabled=False), ticket, app, Audit(), "r")) is None)
async def boom(*a, **k): raise RuntimeError("pm boom")
pm_mod.review = boom
chk("_consult_pm is None on PM error (fail-safe -> Commander)", asyncio.run(loop._consult_pm(cfg, ticket, app, Audit(), "r")) is None)

# ---- ESCALATE: park + Commander decision + clear comment + ESCALATED ----
async def pm_esc(c, t, a, au, rep): return {"verdict": "ESCALATE", "body": "Recommend keeping Premium (billing-critical)"}
loop._consult_pm = pm_esc
dec_added.clear(); bl = Backlog(); au = Audit()
rep = asyncio.run(loop._attempt(ticket, app, cfg, Git(), bl, au, loop.Budget(0), "autodev/AUTO-14"))
chk("ESCALATE -> Outcome.ESCALATED", rep.outcome == Outcome.ESCALATED, str(rep.outcome))
chk("ESCALATE -> Commander decision filed (with PM recommendation)", any("Recommend keeping Premium" in n for n in dec_added), str(dec_added))
chk("ESCALATE -> parked with a 'what's needed' Jira comment", any("Recommend keeping Premium" in c for c in bl.comments), str(bl.comments))
chk("ESCALATE -> needs_human audited (product blocker)", any(e["event"] == "needs_human" for e in au.ev))

# ---- DECIDE: inject decision into the ticket, re-build, (2nd halt) escalate ----
async def pm_dec(c, t, a, au, rep): return {"verdict": "DECIDE", "body": "Use 4 nav groups: Operate/Billing/Configure/Observe."}
loop._consult_pm = pm_dec
built.clear(); dec_added.clear(); au = Audit()
rep2 = asyncio.run(loop._attempt(ticket, app, cfg, Git(), Backlog(), au, loop.Budget(0), "autodev/AUTO-14"))
chk("DECIDE -> pm_decided audited", any(e["event"] == "pm_decided" for e in au.ev))
chk("DECIDE -> re-built with the PM decision injected into the ticket", any("Use 4 nav groups" in d for d in built), f"builds={len(built)}")
chk("DECIDE -> PM consulted once, then escalated on the 2nd halt", sum(1 for e in au.ev if e["event"] == "pm_decided") == 1 and rep2.outcome == Outcome.ESCALATED)

# ---- NO HALT LANGUAGE: a no-changes build still routes to the PM once before erroring (EU-10) ----
# A benign summary with <2 halt markers used to skip the PM entirely and go straight to ERRORED.
NO_HALT = "Looked at the code; everything already matches the acceptance criteria."
class QuietBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")
    @staticmethod
    async def build(req, app, cfg, audit=None, **_):   # EU-72: absorb store=/spec= kwargs
        return BuildResult(ok=True, summary=NO_HALT, cost_usd=0.0, num_turns=1, raw=NO_HALT, tools=[])
loop.builder_mod = QuietBuilder

# PM consulted but cannot decide (None) -> falls through to ERRORED, having spent one cheap call.
pm_calls = []
async def pm_none(c, t, a, au, rep): pm_calls.append(rep); return None
loop._consult_pm = pm_none
au = Audit()
rep3 = asyncio.run(loop._attempt(ticket, app, cfg, Git(), Backlog(), au, loop.Budget(0), "autodev/AUTO-14"))
chk("no halt language -> PM consulted once before erroring", len(pm_calls) == 1, f"calls={len(pm_calls)}")
chk("no halt language + PM no-decision -> ERRORED", rep3.outcome == Outcome.ERRORED, str(rep3.outcome))
chk("no halt language + PM no-decision -> no_changes audited", any(e["event"] == "no_changes" for e in au.ev))

# PM consulted and DECIDES -> rebuilds with the decision injected, even with no halt language.
async def pm_dec2(c, t, a, au, rep): return {"verdict": "DECIDE", "body": "Ship the empty-state copy first."}
loop._consult_pm = pm_dec2
au = Audit()
rep4 = asyncio.run(loop._attempt(ticket, app, cfg, Git(), Backlog(), au, loop.Budget(0), "autodev/AUTO-14"))
chk("no halt language + PM DECIDE -> pm_decided audited (re-build)", any(e["event"] == "pm_decided" for e in au.ev))

loop.builder_mod = FakeBuilder  # restore for any later assertions

print("\n================ PM-IN-LOOP QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
