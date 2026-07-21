"""EU-396 — a Builder no-changes claim is verified by the Reviewer (unchanged tree, per-AC
file:line evidence) BEFORE it escalates to the Commander, instead of being forwarded unverified.

Asserts:
A. Confident PASS with evidence -> the ticket is transitioned to QA (never Done), the evidence is
   posted as a comment, and the auto-close is separately audited (no_changes_autoclose).
B. Anything less than confident -> escalates exactly as before (Needs Human / ESCALATED), but the
   Reviewer's per-AC findings are attached to both the Jira comment and the parked note.
C. verify_no_changes_enabled=False restores the pre-EU-396 behaviour (Reviewer never consulted).
"""
import sys
import types
import asyncio

sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, BuildResult, Outcome

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# A quiet no-changes report — no halt-marker language, so it falls straight through the PM/
# deliberate-halt branches into the EU-396 verify seam (mirrors pm_loop_test.py's NO_HALT case).
NO_HALT = "Looked at the code; everything already matches the acceptance criteria. No changes made."


class Audit:
    def __init__(s):
        s.ev = []

    def record(s, e, **k):
        s.ev.append({"event": e, **k})


class Git:
    def has_changes(s):
        return False


class Backlog:
    def __init__(s):
        s.comments = []
        s.statuses = []

    def add_comment(s, t, b):
        s.comments.append(b)

    def set_status(s, t, status):
        s.statuses.append(status)


loop._notify = lambda c, t: None
loop.decisions.add = lambda cfg, ticket, app, note: None


class QuietBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket):
        return ("low", "sized")

    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        return BuildResult(ok=True, summary=NO_HALT, cost_usd=0.0, num_turns=1, raw=NO_HALT, tools=[])


loop.builder_mod = QuietBuilder


async def pm_none(c, t, a, au, rep):
    return None


loop._consult_pm = pm_none


def mkcfg(**kw):
    return Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path="/tmp/eu396.jsonl", use_worktree=False, max_iterations=3, **kw)


def mkticket():
    return Ticket(id="AUTO-396", key="AUTO-396", summary="s", description="d",
                 acceptance_criteria=["Widget renders on /leads", "Export button is disabled offline"],
                 ephemeral=False, app="automatixy")


CONFIDENT = {
    "confident": True,
    "findings": [
        {"criterion": "Widget renders on /leads", "satisfied": True,
         "evidence": "src/pages/Leads.tsx:42 — <Widget /> is already rendered unconditionally"},
        {"criterion": "Export button is disabled offline", "satisfied": True,
         "evidence": "src/components/Export.tsx:17 — disabled={!isOnline}"},
    ],
    "summary": "Both ACs already satisfied.",
    "cost_usd": 0.01, "input_tokens": 100, "output_tokens": 50,
}

UNSURE = {
    "confident": False,
    "findings": [
        {"criterion": "Widget renders on /leads", "satisfied": True,
         "evidence": "src/pages/Leads.tsx:42 — <Widget /> is rendered"},
        {"criterion": "Export button is disabled offline", "satisfied": False,
         "evidence": "no offline-detection code found anywhere in src/components/Export.tsx"},
    ],
    "summary": "One AC unmet.",
    "cost_usd": 0.01, "input_tokens": 100, "output_tokens": 50,
}


# ---- A: confident PASS -> QA, evidence comment, audited auto-close, SKIPPED outcome ----
async def fake_verify_confident(ticket, app, cfg, report):
    return dict(CONFIDENT)


loop.reviewer_mod.verify_no_changes = fake_verify_confident
au = Audit()
bl = Backlog()
# _attempt signature is (ticket, app, cfg, git, backlog, audit, budget, branch)
cfg = mkcfg()
app = cfg.app("automatixy")
rep = asyncio.run(loop._attempt(mkticket(), app, cfg, Git(), bl, au, loop.Budget(0), "autodev/AUTO-396"))
chk("confident verify -> Outcome.SKIPPED (not escalated)", rep.outcome == Outcome.SKIPPED, str(rep.outcome))
chk("confident verify -> ticket transitioned to QA (never Done)", bl.statuses == ["QA"], str(bl.statuses))
chk("confident verify -> evidence comment posted", any("Leads.tsx:42" in c for c in bl.comments), str(bl.comments))
chk("confident verify -> no_changes_verify audited", any(e["event"] == "no_changes_verify" and e.get("confident") for e in au.ev))
chk("confident verify -> no_changes_autoclose audited separately", any(e["event"] == "no_changes_autoclose" for e in au.ev))
chk("confident verify -> NOT parked to Needs Human (no plain no_changes escalate)", not any(e["event"] == "no_changes" for e in au.ev))


# ---- B: not confident -> escalates as before, WITH per-AC findings attached ----
async def fake_verify_unsure(ticket, app, cfg, report):
    return dict(UNSURE)


loop.reviewer_mod.verify_no_changes = fake_verify_unsure
au2 = Audit()
bl2 = Backlog()
cfg2 = mkcfg()
app2 = cfg2.app("automatixy")
rep2 = asyncio.run(loop._attempt(mkticket(), app2, cfg2, Git(), bl2, au2, loop.Budget(0), "autodev/AUTO-396"))
chk("not confident -> Outcome.ESCALATED (today's behaviour preserved)", rep2.outcome == Outcome.ESCALATED, str(rep2.outcome))
chk("not confident -> ticket parked to Needs Human", bl2.statuses == ["Needs Human"], str(bl2.statuses))
chk("not confident -> no_changes_verify audited with confident=False", any(e["event"] == "no_changes_verify" and not e.get("confident") for e in au2.ev))
chk("not confident -> plain no_changes escalate still audited", any(e["event"] == "no_changes" for e in au2.ev))
chk("not confident -> Reviewer's per-AC findings attached to the parked comment", any("no offline-detection code" in c for c in bl2.comments), str(bl2.comments))
chk("not confident -> NOT auto-closed to QA", "QA" not in bl2.statuses)


# ---- C: verify_no_changes_enabled=False -> the Reviewer is never consulted (old behaviour) ----
calls = []


async def fake_verify_tracked(ticket, app, cfg, report):
    calls.append(1)
    return dict(CONFIDENT)


loop.reviewer_mod.verify_no_changes = fake_verify_tracked
au3 = Audit()
bl3 = Backlog()
cfg3 = mkcfg(verify_no_changes_enabled=False)
app3 = cfg3.app("automatixy")
rep3 = asyncio.run(loop._attempt(mkticket(), app3, cfg3, Git(), bl3, au3, loop.Budget(0), "autodev/AUTO-396"))
chk("verify_no_changes_enabled=False -> Reviewer never consulted", len(calls) == 0, f"calls={len(calls)}")
chk("verify_no_changes_enabled=False -> falls back to plain Needs-Human escalate", rep3.outcome == Outcome.ESCALATED and bl3.statuses == ["Needs Human"])


print("\n================ EU-396 NO-CHANGES VERIFY QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
if passed != len(results):
    sys.exit(1)
