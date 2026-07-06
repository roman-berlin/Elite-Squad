"""EU-86 — specialist-squad wired end-to-end through loop._attempt().

Acceptance criteria verified here:
  AC#1: All five specialist-squad functions are reachable from the live build path
        (builder.build() and/or loop.py):
          • squad.should_delegate()         — delegation guard on every build
          • squad.build_delegated()         — dispatches planner + soldiers
          • squad._run_gate()               — runs each specialist's domain gate
          • hr.ensure_no_charter_written()  — asserts the ephemeral invariant (EU-86)
          • hr.record_domain_use()          — accrues domain-use credit

  AC#2: An integration test covers the happy path end-to-end — the specialist
        squad is actually invoked during a full build cycle driven by loop._attempt().

  AC#3: python3 tests/run_all.py stays green (see gate at the bottom).

All offline — SDK and agents are stubbed; no real models, no network.
"""
import asyncio
import contextlib
import io
import os
import sys
import tempfile
import types
from pathlib import Path

# ── SDK stub (no real model calls) ───────────────────────────────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): self.__dict__.update(k)
    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop                      # noqa: E402
import orchestrator.squad as squad                    # noqa: E402
import orchestrator.builder as builder                # noqa: E402
import orchestrator.hr as hr                          # noqa: E402
from orchestrator.agent import AgentRun               # noqa: E402
from orchestrator.config import Config, AppConfig     # noqa: E402
from orchestrator.contracts import (                  # noqa: E402
    BuildRequest, BuildResult, GateResult, Outcome,
    ReviewResult, Ticket, TicketReport, Verdict,
)

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# ── Common fixtures ───────────────────────────────────────────────────────────
_APP = AppConfig(
    name="automatixy", repo_path="/tmp",
    base_branch="DEV", protected_branch="MAIN", backlog_backend="none",
)
BIG_AC = ["criterion one", "criterion two", "criterion three", "criterion four"]


def _ticket() -> Ticket:
    return Ticket(
        id="EU-86", key="EU-86",
        summary="Add tenant-scoped CSV export with specialist delegation",
        description="A sizable feature touching the DB schema, backend route, and frontend UI.",
        acceptance_criteria=BIG_AC,
        app="automatixy",
    )


_aud_fd, _aud_path = tempfile.mkstemp(suffix=".jsonl")
os.close(_aud_fd)

# Config for the normal (non-synthesis) squad delegation tests.
_cfg = Config(
    apps=[_APP], audit_path=_aud_path,
    use_worktree=False, delegation_enabled=True,
    pm_enabled=False, test_gate=False,
    merge_to_dev=True, dry_run=False, max_iterations=2,
)

# Config for the ephemeral-specialist (domain-gap synthesis) tests.
_cfg_auto = Config(
    apps=[_APP], audit_path=_aud_path,
    use_worktree=False, delegation_enabled=True, auto_mode=True,
    pm_enabled=False, test_gate=False,
    merge_to_dev=True, dry_run=False, max_iterations=2,
)


class _Backlog:
    def set_status(self, *a, **k): pass
    def add_comment(self, *a, **k): pass


class _Audit:
    def __init__(self): self.events: list[tuple[str, dict]] = []

    def record(self, e: str, **k): self.events.append((e, k))


class _Git:
    def has_changes(self): return True
    def diff_against_base(self): return "diff --git a/x.py b/x.py\n+foo = 1"
    def changed_paths(self): return ["orchestrator/squad.py"]
    def commit_all(self, *a): return "sha1"
    def trial_merge(self, *a): return True
    def current_sha(self): return "merge_sha"
    def land_trial(self, *a): pass
    def delete_local_branch(self, *a): pass
    def delete_remote_branch(self, *a): pass
    def sync_main_base(self): return ""
    def abandon_trial(self, *a): pass


# ── Fake planner + soldiers (track call tags, return valid output) ────────────
PLAN_JSON = (
    '[{"role":"logistics-db","title":"schema migration","detail":"add exports table","size":"M"},'
    '{"role":"ordnance-be","title":"export endpoint","detail":"POST /exports","size":"L"},'
    '{"role":"vanguard-fe","title":"export button","detail":"Export button UI","size":"S"}]'
)

_squad_calls: list[str] = []


async def _fake_squad_agent(prompt, options, tag="", ticket_id=None, pass_number=None):
    """Planner returns a 3-subtask plan; gap-detect returns 'covered'; soldiers return 'done'."""
    _squad_calls.append(tag)
    if tag == "squad-lead":
        return AgentRun(text=PLAN_JSON, final=PLAN_JSON, cost_usd=0.05,
                        num_turns=2, is_error=False, tools=["Read"])
    if tag == "gap-detect":
        return AgentRun(text='{"covered": true, "domain": "vanguard-fe"}',
                        final='{"covered": true, "domain": "vanguard-fe"}',
                        cost_usd=0.01, num_turns=1, is_error=False, tools=[])
    # soldier or solo-builder fallback
    return AgentRun(text="Implemented subtask.",
                    final=f"Implemented {tag}",
                    cost_usd=0.10, num_turns=4, is_error=False, tools=["Edit"])


# ══════════════════════════════════════════════════════════════════════════════
# 1. squad.should_delegate — the delegation guard called from builder.build()
# ══════════════════════════════════════════════════════════════════════════════
req_big = BuildRequest(_ticket(), "autodev/EU-86", iteration=1)
chk("should_delegate: True for big ticket (delegation_enabled=True)",
    squad.should_delegate(_cfg, req_big) is True)

chk("should_delegate: False for small ticket (few AC → too small to split)",
    squad.should_delegate(
        _cfg,
        BuildRequest(
            Ticket(id="EU-86", key="EU-86", summary="fix typo", description="x",
                   acceptance_criteria=[], app="automatixy"),
            "b", iteration=1,
        ),
    ) is False)

chk("should_delegate: False on retry (iteration > 1 → focused solo pass)",
    squad.should_delegate(_cfg, BuildRequest(_ticket(), "b", iteration=2)) is False)

cfg_off = Config(apps=[_APP], audit_path=_aud_path, delegation_enabled=False)
chk("should_delegate: False when delegation_enabled=False",
    squad.should_delegate(cfg_off, req_big) is False)


# ══════════════════════════════════════════════════════════════════════════════
# 2. squad.build_delegated — planner + soldiers, called from builder.build()
# ══════════════════════════════════════════════════════════════════════════════
squad.run_agent = _fake_squad_agent
_squad_calls.clear()
bd_result, n_sub = asyncio.run(
    squad.build_delegated(req_big, _APP, _cfg)
)

chk("build_delegated: returns a BuildResult (not None)", bd_result is not None)
chk("build_delegated: n_subtasks >= 2 (squad was actually split)",
    n_sub >= 2, str(n_sub))
chk("build_delegated: squad-lead (planner) was called",
    "squad-lead" in _squad_calls, str(_squad_calls))
chk("build_delegated: soldiers were dispatched",
    sum(1 for t in _squad_calls if t.startswith("soldier")) >= 2,
    str(_squad_calls))
chk("build_delegated: ok=True when all soldiers succeed",
    getattr(bd_result, "ok", False) is True)
chk("build_delegated: summary names the squad",
    "Squad delegation" in getattr(bd_result, "summary", ""),
    (getattr(bd_result, "summary", ""))[:60])


# ══════════════════════════════════════════════════════════════════════════════
# 3. squad._run_gate — domain gate executor, reachable via _run_synthesis
# ══════════════════════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as _gdir:
    status_pass, _ = asyncio.run(squad._run_gate("echo domain-ok", _gdir))
    chk("_run_gate: passing command → 'pass'",
        status_pass == "pass", f"status={status_pass!r}")

    status_fail, _ = asyncio.run(squad._run_gate("exit 1", _gdir))
    chk("_run_gate: non-zero exit → 'fail'",
        status_fail == "fail", f"status={status_fail!r}")

status_empty, report_empty = asyncio.run(squad._run_gate("", "/tmp"))
chk("_run_gate: empty gate → 'manual' (not a hard failure)",
    status_empty == "manual", f"status={status_empty!r}")
chk("_run_gate: empty gate report mentions manual QA",
    "manual QA" in report_empty, report_empty[:80])


# ══════════════════════════════════════════════════════════════════════════════
# 4. hr.ensure_no_charter_written — ephemeral invariant guard (EU-86)
#    Reachable from: _run_synthesis → build_delegated → builder.build()
# ══════════════════════════════════════════════════════════════════════════════
_good = [
    {"lane_key": "mql5-algo", "name": "MQL5 Engineer", "ephemeral": True},
    {"lane_key": "backtester", "name": "Backtester", "ephemeral": True},
]
_missing_ephemeral = [{"lane_key": "rust-cli", "name": "Rust Eng"}]  # no ephemeral key

with tempfile.TemporaryDirectory() as _od:
    # All ephemeral=True, no disk files → no error, no warning.
    try:
        hr.ensure_no_charter_written(_good, officers_path=_od)
        chk("ensure_no_charter_written: all-ephemeral charters → no error", True)
    except Exception as e:
        chk("ensure_no_charter_written: all-ephemeral charters → no error", False, str(e))

    # Missing ephemeral=True → warning printed, does NOT raise.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            hr.ensure_no_charter_written(_missing_ephemeral, officers_path=_od)
            chk("ensure_no_charter_written: missing ephemeral=True → does not raise", True)
        except Exception as e:
            chk("ensure_no_charter_written: missing ephemeral=True → does not raise",
                False, str(e))
    chk("ensure_no_charter_written: logs invariant warning for missing ephemeral=True",
        "invariant" in buf.getvalue().lower() or "ephemeral" in buf.getvalue().lower(),
        buf.getvalue()[:200])

    # Officer file on disk → warning printed, does NOT raise.
    (Path(_od) / "mql5-algo.md").write_text("# MQL5\n", encoding="utf-8")
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        try:
            hr.ensure_no_charter_written(_good, officers_path=_od)
            chk("ensure_no_charter_written: on-disk officer file → does not raise", True)
        except Exception as e:
            chk("ensure_no_charter_written: on-disk officer file → does not raise",
                False, str(e))
    chk("ensure_no_charter_written: logs invariant warning when officer file exists",
        "invariant" in buf2.getvalue().lower() or "officers" in buf2.getvalue().lower()
        or "mql5" in buf2.getvalue().lower(),
        buf2.getvalue()[:200])

# Empty charter list → no-op (no error).
try:
    hr.ensure_no_charter_written([], officers_path="/tmp")
    chk("ensure_no_charter_written: empty list → no-op (no error)", True)
except Exception as e:
    chk("ensure_no_charter_written: empty list → no-op (no error)", False, str(e))


# ══════════════════════════════════════════════════════════════════════════════
# 5. hr.record_domain_use — usage tracker; called by build_delegated after a
#    successful synthesis; also confirms ensure_no_charter_written is on the
#    _run_synthesis call path.
# ══════════════════════════════════════════════════════════════════════════════
_record_calls: list[str] = []
_ensure_calls: list[list] = []

_orig_record = hr.record_domain_use
_orig_ensure = hr.ensure_no_charter_written
_orig_promote = hr.check_promote
_orig_synth = hr.synthesize_specialists
_orig_detect = squad.detect_domain_gap

# Stub lifecycle helpers to avoid real audit writes / TTY prompts.
hr.record_domain_use = lambda domain, cfg: _record_calls.append(domain)
hr.check_promote = lambda domain, cfg, *a, **k: False
hr.ensure_no_charter_written = lambda charters, **kw: _ensure_calls.append(list(charters))


async def _fake_detect_gap(ticket_text, sq, **kw):
    # Legacy 2-tuple return (no burn dict) — _plan's defensive indexing must tolerate it.
    return (True, "mql5")


async def _fake_synthesize(domain, ticket_text, cfg, approver=None):
    return [{
        "name": "MQL5 Algo Engineer", "lane_key": "mql5-algo",
        "identity": "end-to-end EA specialist", "knowledge": "MT5 platform docs",
        "skills": ["implement", "verify via Strategy Tester"],
        "constraints": "only .mq5 files; no other areas",
        "domain_gate": "echo gate-pass",
        "ephemeral": True,
    }]


squad.detect_domain_gap = _fake_detect_gap
hr.synthesize_specialists = _fake_synthesize

with tempfile.TemporaryDirectory() as _syn_dir:
    _app_syn = AppConfig(
        name="x", repo_path=_syn_dir,
        base_branch="DEV", protected_branch="MAIN", backlog_backend="none",
    )
    _syn_res, _n_syn = asyncio.run(
        squad.build_delegated(
            BuildRequest(_ticket(), "b", iteration=1), _app_syn, _cfg_auto,
        )
    )

chk("record_domain_use: called after successful synthesis",
    "mql5" in _record_calls, str(_record_calls))
chk("ensure_no_charter_written: called during _run_synthesis (wired end-to-end)",
    len(_ensure_calls) > 0, str(_ensure_calls))
chk("ensure_no_charter_written: received the synthesized charters",
    any(c.get("lane_key") == "mql5-algo" for clist in _ensure_calls for c in clist),
    str(_ensure_calls))

# Restore stubs.
squad.detect_domain_gap = _orig_detect
hr.synthesize_specialists = _orig_synth
hr.record_domain_use = _orig_record
hr.ensure_no_charter_written = _orig_ensure
hr.check_promote = _orig_promote


# ══════════════════════════════════════════════════════════════════════════════
# 6. INTEGRATION — loop._attempt → builder.build → squad delegation (happy path)
#
# Drives the REAL loop._attempt() with delegation_enabled=True and a big ticket.
# The specialist squad is invoked during the full build cycle: _attempt calls
# builder.build(), which calls squad.should_delegate() → squad.build_delegated()
# → squad planner + soldiers.  Gate, reviewer, and land are stubbed to focus on
# the specialist-squad path.
# ══════════════════════════════════════════════════════════════════════════════
squad.run_agent = _fake_squad_agent
builder.run_agent = _fake_squad_agent  # also stubs the solo-build fallback

_delegated_calls: list[int] = []
_land_calls: list[tuple] = []
_orig_build_delegated = squad.build_delegated


async def _tracked_build_delegated(req, app, cfg, audit=None, **kw):
    """Thin wrapper that records calls before delegating to the real implementation."""
    _delegated_calls.append(req.iteration)
    return await _orig_build_delegated(req, app, cfg, audit=audit, **kw)


squad.build_delegated = _tracked_build_delegated


# Stubs for loop internals that aren't under test.
async def _fake_review(diff, ticket, app, cfg, iteration, store=None, build_artifact=None):
    return ReviewResult(verdict=Verdict.PASS, spec_met=True, cost_usd=0.0, summary="LGTM")


async def _fake_te(ticket, app, cfg, store=None, build_artifact=None):
    from orchestrator.contracts import TestEngineerResult
    return TestEngineerResult(ok=True, coverage="100%")


def _fake_land(ticket, app, cfg, git, backlog, audit, branch,
               iteration, cost, build, review, security_block=None, coverage=""):
    _land_calls.append((ticket.id, iteration))
    return TicketReport(ticket.id, Outcome.MERGED, iteration, cost, app.name, branch)


_orig_land = loop._land
_orig_notify = loop._notify
_orig_gate = loop.run_gate
_orig_reviewer = loop.reviewer_mod
_orig_te = loop.test_engineer_mod

loop._land = _fake_land
loop._notify = lambda *a, **k: None
loop.run_gate = lambda *a, **k: GateResult(passed=True, report="ok")
loop.reviewer_mod = types.SimpleNamespace(review=_fake_review)
loop.test_engineer_mod = types.SimpleNamespace(ensure_coverage=_fake_te)

_squad_calls.clear()
_audit = _Audit()

try:
    e2e_report = asyncio.run(
        loop._attempt(
            _ticket(), _APP, _cfg,
            _Git(), _Backlog(), _audit,
            loop.Budget(0), "autodev/EU-86",
        )
    )

    chk("e2e: _attempt completes (outcome MERGED)",
        e2e_report.outcome == Outcome.MERGED, str(e2e_report.outcome))
    chk("e2e: squad.build_delegated was invoked during the build cycle",
        len(_delegated_calls) > 0, f"delegated_calls={_delegated_calls}")
    chk("e2e: squad-lead (planner) ran during the build cycle",
        "squad-lead" in _squad_calls, str(_squad_calls))
    chk("e2e: soldiers were dispatched during the build cycle",
        any(t.startswith("soldier") for t in _squad_calls), str(_squad_calls))
    chk("e2e: _land was called (build cycle completed the full path)",
        len(_land_calls) == 1, str(_land_calls))
    chk("e2e: audit records 'build' event",
        any(e == "build" for e, _ in _audit.events),
        str([e for e, _ in _audit.events]))
    chk("e2e: audit records 'delegation' event (squad delegation confirmed)",
        any(e == "delegation" for e, _ in _audit.events),
        str([e for e, _ in _audit.events]))
    chk("e2e: audit records 'gate' event (verification gate ran)",
        any(e == "gate" for e, _ in _audit.events),
        str([e for e, _ in _audit.events]))
    chk("e2e: audit records 'review' event (review ran)",
        any(e == "review" for e, _ in _audit.events),
        str([e for e, _ in _audit.events]))

finally:
    # Restore everything unconditionally so other tests aren't polluted.
    loop._land = _orig_land
    loop._notify = _orig_notify
    loop.run_gate = _orig_gate
    loop.reviewer_mod = _orig_reviewer
    loop.test_engineer_mod = _orig_te
    squad.build_delegated = _orig_build_delegated


# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════
print("\n============ EU-86 SPECIALIST E2E QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------")
print(f"  {passed}/{len(results)} passed", "✅" if passed == len(results) else "❌")
sys.exit(0 if passed == len(results) else 1)
