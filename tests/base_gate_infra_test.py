"""2026-07-15 red-base massacre regression — a base-gate TIMEOUT is infra, and any base-level
verdict halts that APP's work (run + drain) instead of parking the whole queue.

The incident (twice in one day: the 04:20 and 17:41 waves): a box under load timed the 1800s
base suite out, the red survived its confirmation re-run (sustained load reproduces), got cached
with the 30-min red TTL, and every subsequent drain pick insta-blocked its ticket to needs_human
— ~60 tickets force-parked, one Commander decision each, queue stalled for hours. Layers pinned
here so no single edit can reopen the class:

  1. gate.base_gate_timed_out()  — timeout-shaped reds are recognized (the marker run_commands
     itself writes), and the marker survives report truncation.
  2. gate.base_gate_check()      — a timeout red is NEVER cached and gets NO confirmation re-run
     (doubling a gate_timeout_sec suite on a loaded box is the harm, not the cure); a genuine
     red still gets its confirmation re-run and is still cached with TTL.
  3. loop                        — a timed-out base returns ERRORED with infra-classified notes
     (EU-228: no strike, no needs_human decision); _run_inner halts THAT APP's remaining tickets
     (they stay queued) while other apps in the same run keep building; a GENUINE red still
     parks exactly one ticket and halts the same way. Both verdicts are emitted from module
     constants (_RED_BASE_NOTES / _BASE_INFRA_NOTES) that must keep matching the halt prefixes
     (_BASE_LEVEL_PREFIXES) — pinned below so a reworded literal can't silently disarm the halt.
  4. autopilot                   — base-level reports arm a PER-APP red_base_hold for the red
     cache TTL (healthy apps keep draining), base timeouts are excluded from the EU-228
     offline-hold (whose connectivity probe would announce a misleading "restored"), and three
     consecutive timeout waves escalate to the Commander (source-pinned; the strike-free tally
     is exercised directly via _tally_errored).

No network, no real SDK — stubbed like every other harness.
"""
import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path

# ── stub claude_agent_sdk / requests so the orchestrator imports cleanly ──
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import autopilot, gate, infra_classify, loop  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402
from orchestrator.contracts import GateResult, Outcome, Ticket, TicketReport  # noqa: E402

results: list[tuple[str, bool, str]] = []

def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


class _Audit:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []
    def record(self, event, **kw):
        self.events.append((event, kw))
    def of(self, event):
        return [kw for e, kw in self.events if e == event]


# ════════════════════════════════════════════════════════════
# 1. gate.base_gate_timed_out — marker recognition
# ════════════════════════════════════════════════════════════

chk("timeout report → timed_out True",
    gate.base_gate_timed_out("$ python tests/run_all.py\n(timed out after 1800s)"))
chk("genuine red report → timed_out False",
    not gate.base_gate_timed_out("FAILED: eu64_resume_and_cap_test.py"))
chk("empty report → timed_out False", not gate.base_gate_timed_out(""))
chk("None report → timed_out False", not gate.base_gate_timed_out(None))
chk("marker matches what run_commands writes",
    gate._BASE_GATE_TIMEOUT_MARKER in "(timed out after 1800s)")


# ════════════════════════════════════════════════════════════
# 2. gate.base_gate_check — timeout reds: no cache, no confirm re-run; genuine reds unchanged
# ════════════════════════════════════════════════════════════

_tmp = Path(tempfile.mkdtemp())
_app = AppConfig(name="alpha", repo_path=str(_tmp), base_branch="DEV",
                 protected_branch="MAIN", backlog_backend="none")
_cfg = types.SimpleNamespace(audit_path=str(_tmp / "audit.jsonl"))
_git = types.SimpleNamespace(current_sha=lambda: "cafebabe")
_cache = _tmp / "red_base_cache.json"

def _runner(result: GateResult):
    calls = []
    def run(app, changed):
        calls.append(1)
        return result
    return run, calls

# timeout red: no confirmation re-run, not cached
_run, _calls = _runner(GateResult(passed=False, report="$ x\n(timed out after 9s)"))
_ok, _fp, _report = gate.base_gate_check(_app, _cfg, _git, runner=_run)
chk("timeout red → passed False", _ok is False)
chk("timeout red → NO confirmation re-run (1 gate run — never double a suite on a loaded box)",
    len(_calls) == 1, len(_calls))
chk("timeout red → report carries the timeout marker", gate.base_gate_timed_out(_report), _report)
_cached = json.loads(_cache.read_text()) if _cache.exists() else {}
chk("timeout red → NOT written to red_base_cache.json",
    f"{_app.repo_path}@cafebabe" not in _cached, str(_cached))

# marker survives truncation: >4000 chars of genuine failure BEFORE the timed-out command
_long = "FAILED: aaa_test.py\n" + ("x" * 4500) + "\n$ slow_cmd\n(timed out after 9s)"
_run, _calls = _runner(GateResult(passed=False, report=_long))
_ok, _fp, _report = gate.base_gate_check(_app, _cfg, _git, runner=_run)
chk("timeout past the 4000-char cut → returned report STILL carries the marker "
    "(gate and loop must read the same verdict)", gate.base_gate_timed_out(_report),
    _report[:120])
_cached = json.loads(_cache.read_text()) if _cache.exists() else {}
chk("timeout past the cut → still NOT cached",
    f"{_app.repo_path}@cafebabe" not in _cached, str(_cached))

# genuine red: confirmation re-run + cached (TTL) exactly as before
_run, _calls = _runner(GateResult(passed=False, report="FAILED: something_test.py"))
_ok, _fp, _report = gate.base_gate_check(_app, _cfg, _git, runner=_run)
chk("genuine red → passed False", _ok is False)
chk("genuine red → confirmation re-run still happens (2 gate runs)", len(_calls) == 2, len(_calls))
_cached = json.loads(_cache.read_text()) if _cache.exists() else {}
_hit = _cached.get(f"{_app.repo_path}@cafebabe")
chk("genuine red → cached with passed=False", isinstance(_hit, dict) and _hit.get("passed") is False,
    str(_hit))


# ════════════════════════════════════════════════════════════
# 3. constants & classification — emitters and halt prefixes can't drift apart
# ════════════════════════════════════════════════════════════

chk("_BASE_INFRA_NOTES starts with the timeout halt prefix",
    loop._BASE_INFRA_NOTES.startswith(loop._BASE_LEVEL_PREFIXES[1]), loop._BASE_INFRA_NOTES)
chk("_RED_BASE_NOTES starts with the red-base halt prefix",
    loop._RED_BASE_NOTES.startswith(loop._BASE_LEVEL_PREFIXES[0]), loop._RED_BASE_NOTES)
_loop_src = Path("./orchestrator/loop.py").read_text(encoding="utf-8")
chk("loop emits the genuine red via the CONSTANT (notes=_RED_BASE_NOTES), never an inline literal",
    "notes=_RED_BASE_NOTES" in _loop_src and 'notes="red base' not in _loop_src)
chk("loop emits the timeout via the CONSTANT (notes=_BASE_INFRA_NOTES)",
    "notes=_BASE_INFRA_NOTES" in _loop_src)
chk("infra_classify tags _BASE_INFRA_NOTES as 'timeout' (EU-228 no-strike path)",
    infra_classify.classify(loop._BASE_INFRA_NOTES) == "timeout",
    infra_classify.classify(loop._BASE_INFRA_NOTES))
chk("genuine red-base notes are NOT infra (still park, one Commander decision)",
    infra_classify.classify(loop._RED_BASE_NOTES) == "")

# EU-228 tally: a base-infra ERRORED report charges no strike
_counts: dict[str, int] = {}
_park, _errored, _retrying, _infra, _changed = autopilot._tally_errored(
    [TicketReport("EU-1", Outcome.ERRORED, 0, 0.0, "alpha", notes=loop._BASE_INFRA_NOTES)], _counts)
chk("base-infra ERRORED → classified infra by _tally_errored", "EU-1" in _infra)
chk("base-infra ERRORED → no error strike charged", _counts == {} and not _park, str(_counts))


# ════════════════════════════════════════════════════════════
# 4. loop._run_inner — a base-level report halts THAT app; other apps keep building
# ════════════════════════════════════════════════════════════

def _drive(factories):
    """Two apps × 2 tickets each, interleaved (alpha, beta, alpha, beta). factories maps
    app name → report factory. Returns (calls, reports, audit)."""
    tmp = Path(tempfile.mkdtemp())
    apps = {n: AppConfig(name=n, repo_path=str(tmp / n), base_branch="DEV",
                         protected_branch="MAIN", backlog_backend="none")
            for n in ("alpha", "beta")}
    for a in apps.values():
        Path(a.repo_path).mkdir(parents=True, exist_ok=True)
    worklist = []
    for i in range(2):
        for n in ("alpha", "beta"):
            worklist.append((apps[n], Ticket(id=f"{n}-{i}", key=f"{n}-{i}", summary=f"t{i}",
                                             description="", acceptance_criteria=[], url=None,
                                             app=n, ephemeral=True)))
    cfg = Config(apps=list(apps.values()), audit_path=str(tmp / "audit.jsonl"),
                 use_worktree=False)
    audit = _Audit()
    calls: list[str] = []
    async def fake_process_ticket(ticket, app_, cfg_, git_, backlog_, audit_, budget_, stop_event_):
        calls.append(ticket.id)
        return factories[app_.name](ticket, app_)
    orig_pt, orig_git = loop.process_ticket, loop._make_git
    loop.process_ticket = fake_process_ticket
    loop._make_git = lambda cfg_, app_: types.SimpleNamespace(
        ensure_clean=lambda: None, current_sha=lambda: "aa", base_sha=lambda: "aa")
    try:
        reports = asyncio.run(loop.run(cfg, worklist, audit))
    finally:
        loop.process_ticket, loop._make_git = orig_pt, orig_git
    return calls, reports, audit

_ok_report = lambda t, a: TicketReport(t.id, Outcome.ERRORED, 1, 0.1, a.name, notes="normal error")

# 4a. alpha's base times out → alpha-1 skipped, beta unaffected
_calls, _reports, _audit = _drive({
    "alpha": lambda t, a: TicketReport(t.id, Outcome.ERRORED, 0, 0.0, a.name,
                                       notes=loop._BASE_INFRA_NOTES),
    "beta": _ok_report,
})
chk("timeout halt: alpha processed once, beta processed twice",
    _calls == ["alpha-0", "beta-0", "beta-1"], str(_calls))
_halts = _audit.of("base_halt_run")
chk("timeout halt: base_halt_run recorded for alpha only",
    len(_halts) == 1 and _halts[0].get("app") == "alpha", str(_halts))
chk("timeout halt: alpha's unprocessed ticket listed as skipped",
    _halts and _halts[0].get("skipped") == ["alpha-1"], str(_halts))
chk("timeout halt: reports cover alpha-0 + both beta tickets (skipped one stays unreported/queued)",
    sorted(r.ticket_id for r in _reports) == ["alpha-0", "beta-0", "beta-1"],
    str([r.ticket_id for r in _reports]))

# 4b. GENUINE red base on alpha (ESCALATED) → same per-app halt
_calls, _reports, _audit = _drive({
    "alpha": lambda t, a: TicketReport(t.id, Outcome.ESCALATED, 0, 0.0, a.name,
                                       notes=loop._RED_BASE_NOTES),
    "beta": _ok_report,
})
chk("red-base halt: alpha processed once, beta unaffected",
    _calls == ["alpha-0", "beta-0", "beta-1"], str(_calls))
chk("red-base halt: base_halt_run recorded with alpha-1 skipped",
    (_h := _audit.of("base_halt_run")) and _h[0].get("skipped") == ["alpha-1"], str(_h))

# 4c. control — normal outcomes must NOT trip the halt
_calls, _reports, _audit = _drive({"alpha": _ok_report, "beta": _ok_report})
chk("control: all 4 tickets processed when nothing is base-level", len(_calls) == 4, str(_calls))
chk("control: no base_halt_run event", not _audit.of("base_halt_run"))


# ════════════════════════════════════════════════════════════
# 5. autopilot — per-app hold, offline-hold exclusion, escalation (source pins + TTL linkage)
# ════════════════════════════════════════════════════════════

_ap_src = Path("./orchestrator/autopilot.py").read_text(encoding="utf-8")
chk("autopilot keeps a PER-APP hold dict", "red_base_hold: dict[str, float] = {}" in _ap_src)
chk("autopilot filters held apps out of the worklist (healthy apps keep draining)",
    "a.name not in red_base_hold" in _ap_src)
chk("autopilot keys halts on the shared loop constant (_BASE_LEVEL_PREFIXES import)",
    "from .loop import _BASE_LEVEL_PREFIXES" in _ap_src
    and "startswith(_BASE_LEVEL_PREFIXES)" in _ap_src)
chk("hold duration reuses gate._RED_BASE_RED_TTL_S (stays in step with the red cache)",
    "_RED_BASE_RED_TTL_S" in _ap_src)
chk("hold is audited as red_base_drain_hold", "red_base_drain_hold" in _ap_src)
chk("base timeouts are EXCLUDED from the EU-228 offline-hold (no fake 'connectivity restored')",
    "infra_errored - _base_ids" in _ap_src)
chk("3 consecutive timeout waves escalate to the Commander",
    "base_timeout_waves == 3" in _ap_src)

chk("loop routes base-gate timeouts via base_gate_timed_out",
    "base_gate_timed_out(base_report)" in _loop_src)
chk("loop records base_gate_infra (not red_base_block) for timeouts",
    "base_gate_infra" in _loop_src)
chk("loop comments the held ticket on the tracker (board visibility)",
    "no work was done on this ticket" in _loop_src)


# ════════════════════════════════════════════════════════════
# Report
# ════════════════════════════════════════════════════════════

print("\n========= BASE-GATE INFRA / RED-BASE MASSACRE REGRESSION =========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("-" * 66)
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
