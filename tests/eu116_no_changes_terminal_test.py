"""EU-116: no_changes is terminal — ticket moved off In Progress and not re-picked by drain.

A no_changes build (the Builder found nothing to change) must:
1. Move the ticket to a terminal state (Done or Needs Human), not leave it In Progress
2. Close as Done when the fix is already satisfied (likely by a sibling ticket)
3. Park for verification when not clearly done
4. Never be re-picked by the drain on the next cycle
"""
import sys, types, asyncio
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, BuildResult, Outcome

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

NO_CHANGES_SUMMARY = "All acceptance criteria already met — no changes needed."

class Audit:
    def __init__(s): s.ev = []
    def record(s, e, **k): s.ev.append({"event": e, **k})

class Git:
    def has_changes(s): return False  # Simulate no_changes scenario

class Backlog:
    def __init__(s):
        s.status_calls = []
        s.comments = []
        s.status = None

    def add_comment(s, t, b):
        s.comments.append(b)

    def set_status(s, t, status):
        s.status_calls.append(status)
        s.status = status

def mkcfg(**kw):
    return Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path="/tmp/x.jsonl", use_worktree=False, max_iterations=3, **kw)

loop._notify = lambda c, t: None

# Builder that returns no changes (ok=True but has_changes=False)
class NoChangesBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")

    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        return BuildResult(ok=True, summary=NO_CHANGES_SUMMARY, cost_usd=0.0,
                          num_turns=1, raw=NO_CHANGES_SUMMARY, tools=[])

loop.builder_mod = NoChangesBuilder

# PM that cannot decide (None) — so no_changes falls through to terminal-state logic
async def pm_none(c, t, a, au, rep): return None
loop._consult_pm = pm_none

# Test 1: no_changes with sibling keywords → closed as Done (MERGED outcome)
cfg = mkcfg()
app = cfg.app("automatixy")
ticket_with_sibling = Ticket(
    id="AUTO-50", key="AUTO-50", summary="Fix ChatPanel placeholder contrast",
    description="This fix was already done by sibling ticket AUTO-32. All acceptance criteria already satisfied.",
    ephemeral=False, app="automatixy"
)

bl = Backlog()
au = Audit()
rep = asyncio.run(loop._attempt(ticket_with_sibling, app, cfg, Git(), bl, au, loop.Budget(0), "autodev/AUTO-50"))

chk("no_changes + sibling keywords → moved to Done", "Done" in bl.status_calls, f"status_calls={bl.status_calls}")
chk("no_changes + sibling keywords → MERGED outcome", rep.outcome == Outcome.MERGED, str(rep.outcome))
chk("no_changes + sibling keywords → no_changes audited", any(e["event"] == "no_changes" for e in au.ev))
chk("no_changes + sibling keywords → 'already satisfied' comment", any("already satisfied" in c for c in bl.comments), str(bl.comments))
chk("no_changes + sibling keywords → NOT In Progress", "In Progress" not in bl.status_calls, f"status_calls={bl.status_calls}")

# Test 2: no_changes without clear sibling keywords → parked for verification (ESCALATED)
cfg2 = mkcfg()
app2 = cfg2.app("automatixy")
ticket_generic = Ticket(
    id="AUTO-51", key="AUTO-51", summary="Add button to settings page",
    description="Add a new button to the settings page.",  # No sibling keywords
    ephemeral=False, app="automatixy"
)

bl2 = Backlog()
au2 = Audit()
rep2 = asyncio.run(loop._attempt(ticket_generic, app2, cfg2, Git(), bl2, au2, loop.Budget(0), "autodev/AUTO-51"))

chk("no_changes (generic) → moved to Needs Human", "Needs Human" in bl2.status_calls, f"status_calls={bl2.status_calls}")
chk("no_changes (generic) → ESCALATED outcome", rep2.outcome == Outcome.ESCALATED, str(rep2.outcome))
chk("no_changes (generic) → 'verify if already satisfied' comment", any("verify if" in c.lower() and "already" in c.lower() for c in bl2.comments), str(bl2.comments))
chk("no_changes (generic) → NOT In Progress", "In Progress" not in bl2.status_calls, f"status_calls={bl2.status_calls}")

# Test 3: no_changes in dry-run → status unchanged (ERRORED outcome, no status change)
cfg3 = mkcfg(dry_run=True)
app3 = cfg3.app("automatixy")
ticket_dry = Ticket(
    id="AUTO-52", key="AUTO-52", summary="Dry-run ticket",
    description="Should not move status in dry-run mode.", ephemeral=False, app="automatixy"
)

bl3 = Backlog()
au3 = Audit()
rep3 = asyncio.run(loop._attempt(ticket_dry, app3, cfg3, Git(), bl3, au3, loop.Budget(0), "autodev/AUTO-52"))

chk("no_changes (dry-run) → status unchanged (no set_status calls)", len(bl3.status_calls) == 0, f"status_calls={bl3.status_calls}")
chk("no_changes (dry-run) → ERRORED outcome", rep3.outcome == Outcome.ERRORED, str(rep3.outcome))

# Test 4: no_changes with ephemeral ticket → status unchanged (ERRORED outcome)
cfg4 = mkcfg()
app4 = cfg4.app("automatixy")
ticket_ephemeral = Ticket(
    id="ephemeral-123", key="ephemeral-123", summary="Ephemeral ticket",
    description="Ephemeral tickets have no backlog.", ephemeral=True, app="automatixy"
)

bl4 = Backlog()
au4 = Audit()
rep4 = asyncio.run(loop._attempt(ticket_ephemeral, app4, cfg4, Git(), bl4, au4, loop.Budget(0), "ephemeral-123"))

chk("no_changes (ephemeral) → status unchanged", len(bl4.status_calls) == 0, f"status_calls={bl4.status_calls}")
chk("no_changes (ephemeral) → ERRORED outcome", rep4.outcome == Outcome.ERRORED, str(rep4.outcome))

print("\n================ EU-116 NO-CHANGES TERMINAL TEST ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
