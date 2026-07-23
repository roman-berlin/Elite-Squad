"""2026-07-22 EU-438 postmortem (EU-443) — a base-gate red that is ENVIRONMENTAL (gate preflight/
import failure, broken/missing interpreter, state-file leak) must be classified infra and NEVER
cached, so it can never park the whole To-Do queue the way a transient environmental red cached as
genuine force-parked EU-438 (4 red-base verdicts in ~11 minutes that then self-healed).

The incident: an environmental flake lasting >a few seconds reproduces on base_gate_check's
immediate confirmation re-run (the box/load window is unchanged), so a transient red was cached as a
genuine code red and force-parked every To-Do ticket until the red cache TTL expired or dev moved.
The fingerprint that survived was an environment signal, not an assertion.

The fix layers (each pinned below so no single edit can reopen the class):

  1. gate.base_gate_environmental() — a SUPERSET of base_gate_timed_out: timeout, gate preflight/
     import failure, broken/missing interpreter, state-file (.pid / audit) leak. High-precision: a
     genuine assertion/harness failure never matches.
  2. gate.base_gate_check()         — an environmental red is NEVER written to red_base_cache.json
     (so it can't park subsequent picks), exactly like a timeout red.
  3. loop                           — a non-timeout environmental red returns the ERRORED infra path
     (_BASE_ENV_NOTES), NOT the ESCALATED _RED_BASE_NOTES park; a GENUINE red still parks.
  4. autopilot._tally_errored       — a base-level ERRORED verdict charges NO strike (prefix-keyed,
     so the non-timeout notes — which infra_classify must stay too narrow to catch — are covered).

No network, no real SDK — stubbed like every other harness.
"""
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

from orchestrator import autopilot, gate, loop  # noqa: E402
from orchestrator.config import AppConfig  # noqa: E402
from orchestrator.contracts import GateResult, Outcome, TicketReport  # noqa: E402

results: list[tuple[str, bool, str]] = []

def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# ════════════════════════════════════════════════════════════
# 1. gate.base_gate_environmental — the predicate (superset of base_gate_timed_out)
# ════════════════════════════════════════════════════════════

# Every ENVIRONMENTAL signature → True (AC2's signature list).
chk("timeout marker → environmental",
    gate.base_gate_environmental("$ python tests/run_all.py\n(timed out after 9s)"))
chk("preflight import failure → environmental",
    gate.base_gate_environmental(
        "gate health check FAILED — interpreter '.venv/bin/python' cannot import required modules "
        "(requests).\nModuleNotFoundError: No module named 'requests'"))
chk("broken/missing interpreter → environmental",
    gate.base_gate_environmental(
        "gate health check: cannot run interpreter '/no/such/python': "
        "[Errno 2] No such file or directory: '/no/such/python'"))
chk("stale PID-file leak → environmental",
    gate.base_gate_environmental(
        "Traceback (most recent call last):\n  File \"run_all.py\", line 12, in <module>\n"
        "    pid = Path('/tmp/general-autopilot.pid').read_text()\n"
        "FileNotFoundError: [Errno 2] No such file or directory: "
        "'/tmp/general-autopilot.pid'"))
chk("audit-path leak → environmental",
    gate.base_gate_environmental(
        "FileNotFoundError: [Errno 2] No such file or directory: 'state/audit.jsonl'"))

# A GENUINE code red must NEVER match (AC3 / EU-174 regression guard).
chk("genuine assertion failure → NOT environmental",
    not gate.base_gate_environmental(
        "FAILED: eu999_real_feature_test.py\n  AssertionError: expected 5 got 4 (exit 1)"))
chk("genuine harness failure → NOT environmental",
    not gate.base_gate_environmental("$ python tests/run_all.py\n(exit 1)\nFAILED: eu64_test.py"))
chk("empty/None report → NOT environmental",
    not gate.base_gate_environmental("") and not gate.base_gate_environmental(None))
# A ticket's own missing-fixture FileNotFoundError is a real code red, NOT a state-file leak —
# the statefile regex requires a .pid / audit.jsonl fragment so this must not trip.
chk("missing-fixture FileNotFoundError (no state path) → NOT environmental",
    not gate.base_gate_environmental(
        "FAILED: eu999_test.py\nFileNotFoundError: tests/fixtures/missing_data.json"))
# Superset property: every timeout is environmental (base_gate_timed_out ⊂ base_gate_environmental).
chk("base_gate_environmental is a superset of base_gate_timed_out",
    gate.base_gate_timed_out("(timed out after 9s)")
    and gate.base_gate_environmental("(timed out after 9s)"))


# ════════════════════════════════════════════════════════════
# 2. gate.base_gate_check — an environmental red is NEVER cached (AC2)
# ════════════════════════════════════════════════════════════

def _harness(report_factory):
    """Fresh tmp app/cfg/cache + a runner stub that always returns report_factory()."""
    tmp = Path(tempfile.mkdtemp())
    app = AppConfig(name="alpha", repo_path=str(tmp), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")
    cfg = types.SimpleNamespace(audit_path=str(tmp / "audit.jsonl"))
    git = types.SimpleNamespace(current_sha=lambda: "cafebabe")
    cache = tmp / "red_base_cache.json"

    def run(app_, changed):
        return report_factory()
    return app, cfg, git, cache, run

_ENV_REPORTS = {
    "timeout": lambda: GateResult(passed=False,
                                  report="$ x\n(timed out after 9s)"),
    "preflight": lambda: GateResult(passed=False, report=(
        "gate health check FAILED — interpreter '.venv/bin/python' cannot import required "
        "modules (requests).")),
    "interpreter": lambda: GateResult(passed=False, report=(
        "gate health check: cannot run interpreter '/bad/python': No such file or directory")),
    "pid_leak": lambda: GateResult(passed=False, report=(
        "FileNotFoundError: [Errno 2] No such file or directory: '/tmp/general-autopilot.pid'")),
    "audit_leak": lambda: GateResult(passed=False, report=(
        "FileNotFoundError: [Errno 2] No such file or directory: 'state/audit.jsonl'")),
}

for _label, _factory in _ENV_REPORTS.items():
    _app, _cfg, _git, _cache, _run = _harness(_factory)
    _ok, _fp, _report = gate.base_gate_check(_app, _cfg, _git, runner=_run)
    _cached = json.loads(_cache.read_text()) if _cache.exists() else {}
    chk(f"{_label}: verdict is RED (infra, not a pass)", _ok is False)
    chk(f"{_label}: NOT written to red_base_cache.json (would park subsequent tickets)",
        f"{_app.repo_path}@cafebabe" not in _cached, str(_cached))

# Genuine red (AC3): STILL cached with passed=False (the EU-174 brake is not disarmed).
_app, _cfg, _git, _cache, _run = _harness(
    lambda: GateResult(passed=False, report="FAILED: eu999_real_assertion_test.py\n"
                                            "  AssertionError: expected 5 got 4"))
_ok, _fp, _report = gate.base_gate_check(_app, _cfg, _git, runner=_run)
_cached = json.loads(_cache.read_text()) if _cache.exists() else {}
_hit = _cached.get(f"{_app.repo_path}@cafebabe")
chk("genuine red → verdict is RED", _ok is False)
chk("genuine red → STILL cached with passed=False (EU-174 brake intact)",
    isinstance(_hit, dict) and _hit.get("passed") is False, str(_hit))
chk("genuine red → confirmation re-run still happens (2 gate runs, not 1)",
    _fp != "", _fp)   # a fingerprint was computed → it was treated as a comparable code red


# ════════════════════════════════════════════════════════════
# 3. loop constants & routing — non-timeout environmental uses the ERRORED infra path
# ════════════════════════════════════════════════════════════

chk("_BASE_ENV_NOTES starts with a base-level halt prefix (autopilot keys halts on it)",
    (loop._BASE_ENV_NOTES or "").startswith(loop._BASE_LEVEL_PREFIXES), loop._BASE_ENV_NOTES)
chk("_BASE_ENV_NOTES is distinct from the timeout notes",
    loop._BASE_ENV_NOTES != loop._BASE_INFRA_NOTES)
chk("_RED_BASE_NOTES still starts with the red-base halt prefix (unchanged)",
    loop._RED_BASE_NOTES.startswith(loop._BASE_LEVEL_PREFIXES[0]), loop._RED_BASE_NOTES)

_loop_src = Path("./orchestrator/loop.py").read_text(encoding="utf-8")
chk("loop routes environmental reds via base_gate_environmental (not timed_out alone)",
    "base_gate_environmental(base_report)" in _loop_src)
chk("loop still routes timeouts via base_gate_timed_out (source pin from EU-228)",
    "base_gate_timed_out(base_report)" in _loop_src)
chk("loop emits the env verdict via the CONSTANT literal (notes=_BASE_ENV_NOTES), never inline",
    "notes=_BASE_ENV_NOTES" in _loop_src)
chk("loop still emits the genuine red via _RED_BASE_NOTES (ESCALATED park unchanged)",
    "notes=_RED_BASE_NOTES" in _loop_src)
chk("loop records base_gate_infra for environmental reds too",
    "base_gate_infra" in _loop_src)


# ════════════════════════════════════════════════════════════
# 4. autopilot._tally_errored — a base-level ERRORED verdict charges NO strike
# ════════════════════════════════════════════════════════════

for _notes_label, _notes in (("timeout", loop._BASE_INFRA_NOTES),
                             ("environmental", loop._BASE_ENV_NOTES)):
    _counts: dict[str, int] = {}
    _park, _errored, _retrying, _infra, _changed = autopilot._tally_errored(
        [TicketReport("EU-1", Outcome.ERRORED, 0, 0.0, "alpha", notes=_notes)], _counts)
    chk(f"{_notes_label} base notes → classified infra by _tally_errored (no strike)",
        "EU-1" in _infra and not _park, f"{_notes_label}: park={_park} infra={_infra}")
    chk(f"{_notes_label} base notes → NO error strike charged",
        _counts == {}, f"{_notes_label}: { _counts}")


# ════════════════════════════════════════════════════════════
# Report
# ════════════════════════════════════════════════════════════

print("\n========= EU-443 RED-BASE ENVIRONMENTAL CLASSIFIER REGRESSION =========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("-" * 70)
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
