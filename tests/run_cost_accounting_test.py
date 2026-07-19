"""Run-cost accounting — telemetry pins that survive the Phase-2 §2 collapse.

The EU-139-run (2026-07-05) audit found several stages whose spend never reached run_end's
total_cost_usd. The build-delegation stages it pinned (squad-lead planning, soldier dispatch,
build_delegated's `sunk` out-param) were REMOVED in Phase-2 §2 along with the build squad; the
provost security gate and the Test Engineer stage were likewise deleted. What remains worth
guarding:
  5. loop._attempt: the ticket report cost — the number run_end sums — = builder + review.
  6. usage.record stamps "k" on the ledger row when a ticket_id is given (the choke-point that
     makes per-ticket burn slicing work).

All offline — SDK and agents are stubbed; no real models, no network.
"""
import asyncio
import json
import os
import sys
import tempfile
import types

# ── SDK stub (no real model calls) ───────────────────────────────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): self.__dict__.update(k)
    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop                       # noqa: E402
import orchestrator.usage as usage                     # noqa: E402
from orchestrator.config import Config, AppConfig      # noqa: E402
from orchestrator.contracts import (                   # noqa: E402
    BuildArtifact, BuildResult, GateResult, Outcome,
    ReviewResult, ReviewVerdict, Ticket, TicketReport, Verdict,
)

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


_aud_fd, _aud_path = tempfile.mkstemp(suffix=".jsonl")
os.close(_aud_fd)

_APP = AppConfig(name="automatixy", repo_path="/tmp",
                 base_branch="DEV", protected_branch="MAIN", backlog_backend="none")


def _ticket(tid: str = "EU-139") -> Ticket:
    return Ticket(id=tid, key=tid,
                  summary="Fix provost gate contradictory error handling",
                  description="A sizable change.",
                  acceptance_criteria=["one", "two", "three", "four"],
                  app="automatixy", ephemeral=True)


# The EU-139 run's actual ledger numbers — pinned so the harness mirrors the audit evidence.
GAP_COST, GAP_IN, GAP_OUT = 0.041794, 18898, 552
LEAD_COST, LEAD_IN, LEAD_OUT = 0.344076, 68517, 1507
SOLDIER_COST = 0.15


# ══════════════════════════════════════════════════════════════════════════════
# 5. loop._attempt — the ticket report cost (what run_end sums) = builder + review.
#    EU-139 arithmetic: builder 0.6524547 + review 0.4528022 = 1.1052569.
#    (Phase-2 §2: the provost gate and the Test Engineer stage that used to add to this
#     sum are both deleted — the Builder writes its own tests now.)
# ══════════════════════════════════════════════════════════════════════════════
BUILD_C, REVIEW_C = 0.6524547, 0.4528022


class _Audit:
    def __init__(self): self.events = []
    def record(self, e, **k): self.events.append({"event": e, **k})


class _Backlog:
    def set_status(self, *a, **k): pass
    def add_comment(self, *a, **k): pass


class _Git:
    def has_changes(self): return True
    def diff_against_base(self): return "diff --git a/x b/x\n+added_line"
    def changed_paths(self): return ["orchestrator/loop.py"]


class _StubBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")

    @staticmethod
    async def build(req, app, cfg, audit=None, store=None, spec=None):
        if store is not None:
            store.put(BuildArtifact(files_changed=[], diff_digest="d",
                                    decisions=[], open_questions=[]))
        return BuildResult(ok=True, summary="built", cost_usd=BUILD_C, num_turns=22)


class _StubReviewer:
    @staticmethod
    async def review(diff, ticket, app, cfg, iteration=1, store=None, build_artifact=None,
                     already_bounced=None, gate_evidence=""):   # EU-265: mirror the real signature
        if store is not None:
            store.put(ReviewVerdict(verdict=Verdict.PASS, blocking=[], notes=[]))
        return ReviewResult(verdict=Verdict.PASS, spec_met=True, cost_usd=REVIEW_C)

    @staticmethod
    def collect_unverifiable_fingerprints(result):   # EU-352: loop.py always calls this
        return set()


_land_costs: list[float] = []


def _fake_land(tk, app, cfg, git, backlog, audit, branch, iteration, cost, build, review,
               commenter=None):
    _land_costs.append(cost)
    return TicketReport(tk.id, Outcome.MERGED, iteration, cost, app.name, branch)


_orig = (loop.builder_mod, loop.reviewer_mod,
         loop.run_gate, loop._land, loop._notify)
loop.builder_mod = _StubBuilder
loop.reviewer_mod = _StubReviewer
loop.run_gate = lambda app, changed=None, **_: GateResult(passed=True, report="")
loop._land = _fake_land
loop._notify = lambda c, t: None

_loop_cfg = Config(apps=[_APP], audit_path=_aud_path, use_worktree=False,
                   pm_enabled=False, max_iterations=1)
try:
    _report = asyncio.run(loop._attempt(_ticket(), _APP, _loop_cfg, _Git(), _Backlog(),
                                        _Audit(), loop.Budget(0), "autodev/EU-139"))
finally:
    (loop.builder_mod, loop.reviewer_mod,
     loop.run_gate, loop._land, loop._notify) = _orig

_expected = BUILD_C + REVIEW_C
chk("loop: ticket landed (MERGED)", _report.outcome == Outcome.MERGED, str(_report.outcome))
chk("loop: report cost = builder + review (no more provost gate or TE to sum)",
    _land_costs and abs(_land_costs[0] - _expected) < 1e-9,
    f"got={_land_costs}, want={_expected}")
chk("loop: TicketReport.cost_usd carries the full ticket spend",
    abs(_report.cost_usd - _expected) < 1e-9, str(_report.cost_usd))


# ══════════════════════════════════════════════════════════════════════════════
# 6. usage.record — a tagged call with ticket_id lands a "k" on the ledger row
#    (the choke-point that turns the ticket_id threading into sliceable burn).
# ══════════════════════════════════════════════════════════════════════════════
_led_fd, _led_audit = tempfile.mkstemp(suffix=".jsonl")
os.close(_led_fd)
usage.configure(_led_audit)
try:
    usage.record("claude-haiku-4-5", GAP_IN, GAP_OUT, GAP_COST, "gap-detect", ticket_id="EU-139")
    usage.record("claude-sonnet-4-6", LEAD_IN, LEAD_OUT, LEAD_COST, "squad-lead", ticket_id="EU-139")
    usage.record("claude-sonnet-4-6", 1000, 200, SOLDIER_COST, "soldier·ordnance-be", ticket_id="EU-139")
    usage.record("claude-haiku-4-5", 10, 5, 0.001, "gap-detect")   # no ticket context
    _ledger = usage._path()
    rows = [json.loads(ln) for ln in _ledger.read_text(encoding="utf-8").splitlines()]
finally:
    usage._PATH = None   # unconfigure — later harness code must not touch the tmp ledger

chk("ledger: gap-detect row carries k=EU-139",
    any(r.get("g") == "gap-detect" and r.get("k") == "EU-139" for r in rows), str(rows))
chk("ledger: squad-lead row carries k=EU-139",
    any(r.get("g") == "squad-lead" and r.get("k") == "EU-139" for r in rows), str(rows))
chk("ledger: soldier row carries k=EU-139",
    any(r.get("g") == "soldier·ordnance-be" and r.get("k") == "EU-139" for r in rows), str(rows))
chk("ledger: no-ticket gap-detect row has no k (shape additive, not forced)",
    any(r.get("g") == "gap-detect" and "k" not in r for r in rows), str(rows))


# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════
passed = [n for n, ok, _ in results if ok]
failed = [(n, d) for n, ok, d in results if not ok]
print(f"\nrun_cost_accounting_test: {len(passed)}/{len(results)} passed")
for n, d in failed:
    print(f"  FAIL: {n}" + (f" — {d}" if d else ""))
if failed:
    sys.exit(1)
