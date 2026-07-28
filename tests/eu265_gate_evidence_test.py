"""EU-265: the loop's REAL, green verification-gate result must reach the Reviewer's EU-268
execution-AC gate as machine-sourced evidence — instead of the Builder-prose lottery that forced
FAIL on every "all tests pass"-shaped AC despite the gate having just run those very tests.

The defect (triage 2026-07-17, verified live): `run_gate` must be green before `review()` is ever
called (loop.py continues/bounces otherwise), yet `_has_execution_evidence` scanned ONLY the
Builder's narrative fields — so a ticket whose AC matches "all tests pass" was forced to
spec_met=False + FAIL on every pass (reviewer.py `_enforce_execution_gate`), burned max_passes,
then escalated. `_enforce_bounce_once` explicitly never relieves an execution-gate gap.

Pinned here:
  1. loop._gate_execution_evidence — a REAL green gate (non-empty commands) yields evidence naming
     the commands; a trivially-green no-op gate ("(no commands configured)"), a red gate, or a
     missing gate yields '' (the AUTO-109 hole EU-268 closed must stay closed).
  2. reviewer._enforce_execution_gate with gate_evidence — a generic "all tests pass" AC is
     satisfied by loop gate evidence; a browser/device-matrix AC ("Desktop Chrome"/"Mobile
     Safari") still forces FAIL unless the gate itself ran that matrix (playwright/cypress in the
     evidence). Empty gate_evidence keeps the EU-268 behaviour byte-identical (no weakened pin).
  3. review() threads gate_evidence through to the enforcement (end-to-end, stubbed SDK).
  4. loop wiring: _attempt passes the computed gate evidence into reviewer_mod.review — for a real
     gate the captured kwarg is non-empty and names the command; for a no-op gate it is ''.

All offline — SDK and agents stubbed; no network, no real models.
"""
import asyncio
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

# ── SDK stub (no real model calls) — mirrors tests/reviewer_execution_ac_test.py ──────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): self.__dict__.update(k)
    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import loop                             # noqa: E402
from orchestrator import reviewer as reviewer_mod         # noqa: E402
from orchestrator.config import Config, AppConfig         # noqa: E402
from orchestrator.contracts import (BuildArtifact, BuildResult, GateResult, ReviewResult,
                                    ReviewVerdict, Ticket, Verdict, Outcome)  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


GENERIC_AC = "all tests pass"
BROWSER_AC = "All 6 tests pass on Desktop Chrome and Mobile Safari"

# ══════════════════════════════════════════════════════════════════════════════
# 1. loop._gate_execution_evidence — what counts as machine evidence
# ══════════════════════════════════════════════════════════════════════════════
_app_real = AppConfig(name="a", repo_path="/tmp", base_branch="DEV", protected_branch="MAIN",
                      backlog_backend="none", gate_commands=["python3 tests/run_all.py"])
_app_noop = AppConfig(name="b", repo_path="/tmp", base_branch="DEV", protected_branch="MAIN",
                      backlog_backend="none", gate_commands=[])

_green = GateResult(passed=True, report="all commands passed")
_ev = loop._gate_execution_evidence(_app_real, _green, ["orchestrator/loop.py"])
chk("green REAL gate -> non-empty evidence", bool(_ev), repr(_ev))
chk("evidence names the gate command", "python3 tests/run_all.py" in _ev, repr(_ev))
chk("evidence says the gate PASSED", "PASSED" in _ev, repr(_ev))

chk("trivially-green no-op gate ('(no gate commands configured)') -> NO evidence",
    loop._gate_execution_evidence(
        _app_noop, GateResult(passed=True, report="(no gate commands configured)"), []) == "")
chk("trivially-green no-op gate ('(no commands configured)') -> NO evidence",
    loop._gate_execution_evidence(
        _app_noop, GateResult(passed=True, report="(no commands configured)"), []) == "")
chk("green gate but app has NO commands at all -> NO evidence (stub-shaped pass)",
    loop._gate_execution_evidence(_app_noop, _green, []) == "")
chk("red gate -> NO evidence",
    loop._gate_execution_evidence(_app_real, GateResult(passed=False, report="1 failed"), []) == "")
chk("no gate at all -> NO evidence", loop._gate_execution_evidence(_app_real, None, []) == "")

# Per-app (monorepo) gate groups: the commands that actually RAN are the touched component's.
_app_mono = AppConfig(name="m", repo_path="/tmp", base_branch="DEV", protected_branch="MAIN",
                      backlog_backend="none", gate_commands=["repo-wide-suite"],
                      gate_commands_by_app={"web": ["bun test"]})
_ev_mono = loop._gate_execution_evidence(
    _app_mono, GateResult(passed=True, report="app gates passed: web"), ["apps/web/x.ts"])
chk("monorepo group gate -> evidence names the component's command, not the repo-wide one",
    "bun test" in _ev_mono and "repo-wide-suite" not in _ev_mono, repr(_ev_mono))

# ══════════════════════════════════════════════════════════════════════════════
# 2. reviewer._enforce_execution_gate with loop gate evidence
# ══════════════════════════════════════════════════════════════════════════════
_tk_generic = Ticket(id="EU-265", key="EU-265", summary="s", description="d",
                     acceptance_criteria=[GENERIC_AC])
_tk_browser = Ticket(id="AUTO-109", key="AUTO-109", summary="Google Sync", description="d",
                     acceptance_criteria=[BROWSER_AC])
_no_evidence_ba = BuildArtifact(files_changed=["x.py"], diff_digest="did the work",
                                decisions=[], open_questions=[])
GATE_EV = "verification gate PASSED — all commands passed\ncommands run:\n$ python3 tests/run_all.py"
PW_GATE_EV = "verification gate PASSED — all commands passed\ncommands run:\n$ npx playwright test"


def _pass_result() -> ReviewResult:
    return ReviewResult(verdict=Verdict.PASS, spec_met=True, spec_gaps=[], summary="fine")


_r = reviewer_mod._enforce_execution_gate(_pass_result(), _tk_generic, _no_evidence_ba, "",
                                          gate_evidence=GATE_EV)
chk("generic 'all tests pass' AC + green-gate evidence -> spec_met stays True",
    _r.spec_met is True, str(_r.spec_met))
chk("generic 'all tests pass' AC + green-gate evidence -> verdict stays PASS",
    _r.verdict == Verdict.PASS, str(_r.verdict))
chk("generic 'all tests pass' AC + green-gate evidence -> no spec_gap added",
    _r.spec_gaps == [], str(_r.spec_gaps))

# Empty gate_evidence -> EU-268 behaviour unchanged (the conservative default stays).
_r2 = reviewer_mod._enforce_execution_gate(_pass_result(), _tk_generic, _no_evidence_ba, "",
                                           gate_evidence="")
chk("empty gate_evidence -> still forced to FAIL (EU-268 pin intact)",
    _r2.spec_met is False and _r2.verdict == Verdict.FAIL, f"{_r2.spec_met},{_r2.verdict}")

# A browser/device-matrix AC is NOT satisfied by a green pytest-style repo gate — the AUTO-109
# failure mode was precisely a cross-browser AC (EU-268 doctrine preserved).
_r3 = reviewer_mod._enforce_execution_gate(_pass_result(), _tk_browser, _no_evidence_ba, "",
                                           gate_evidence=GATE_EV)
chk("browser-matrix AC + pytest-style gate evidence -> STILL forced to FAIL",
    _r3.spec_met is False and _r3.verdict == Verdict.FAIL, f"{_r3.spec_met},{_r3.verdict}")
chk("browser-matrix AC + pytest-style gate evidence -> spec_gap names the AC",
    any(BROWSER_AC in g for g in _r3.spec_gaps), str(_r3.spec_gaps))

# ...unless the gate itself ran the browser suite (playwright named in the evidence).
_r4 = reviewer_mod._enforce_execution_gate(_pass_result(), _tk_browser, _no_evidence_ba, "",
                                           gate_evidence=PW_GATE_EV)
chk("browser-matrix AC + a gate that RAN playwright -> spec_met stays True",
    _r4.spec_met is True and _r4.verdict == Verdict.PASS, f"{_r4.spec_met},{_r4.verdict}")

# Mixed ACs: the generic one is covered by the gate, the browser one still gaps -> FAIL, and only
# the browser AC is named unverified.
_tk_mixed = Ticket(id="EU-265b", key="EU-265b", summary="s", description="d",
                   acceptance_criteria=[GENERIC_AC, BROWSER_AC])
_r5 = reviewer_mod._enforce_execution_gate(_pass_result(), _tk_mixed, _no_evidence_ba, "",
                                           gate_evidence=GATE_EV)
chk("mixed ACs + pytest gate evidence -> FAIL names ONLY the browser AC",
    _r5.verdict == Verdict.FAIL and any(BROWSER_AC in g for g in _r5.spec_gaps)
    and not any(f'"{GENERIC_AC}"' in g for g in _r5.spec_gaps), str(_r5.spec_gaps))

# Builder-prose attachment evidence still unlocks everything (the existing EU-268 path is intact).
_prose_ev_ba = BuildArtifact(files_changed=["x.py"], diff_digest="d",
                             decisions=["gate report attached: 6/6 passed"], open_questions=[])
_r6 = reviewer_mod._enforce_execution_gate(_pass_result(), _tk_browser, _prose_ev_ba, "",
                                           gate_evidence="")
chk("attachment evidence in the handoff still unlocks a browser AC (EU-268 path unchanged)",
    _r6.spec_met is True and _r6.verdict == Verdict.PASS, f"{_r6.spec_met},{_r6.verdict}")

# ══════════════════════════════════════════════════════════════════════════════
# 3. review() end-to-end threads gate_evidence into the enforcement
# ══════════════════════════════════════════════════════════════════════════════
class _RR:
    def __init__(self, t):
        (self.final, self.text, self.is_error, self.cost_usd, self.num_turns, self.tools,
         self.provider, self.model_version) = t, t, False, 0.0, 1, [], "Anthropic", "claude-opus-4-8"
        self.input_tokens = self.output_tokens = 0


_PASS_MET_JSON = ('```json\n{"verdict":"PASS","spec_conformance":{"met":true,"gaps":[]},'
                  '"quality":{"issues":[]},"required_changes":[],"summary":"all tests pass"}\n```')


async def _fake_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None,
                          cfg=None, routing_tier=None):
    return _RR(_PASS_MET_JSON)


_orig_run_agent_fb = reviewer_mod.run_agent_with_fallback
reviewer_mod.run_agent_with_fallback = _fake_run_agent
try:
    _rcfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                                   protected_branch="MAIN", backlog_backend="none")],
                   audit_path="/tmp/eu265-audit.jsonl", use_worktree=False, auto_model=False)
    _rapp = _rcfg.app("automatixy")

    _res = asyncio.run(reviewer_mod.review("diff --git a/x b/x\n+ok\n", _tk_generic, _rapp, _rcfg,
                                           build_artifact=_no_evidence_ba, gate_evidence=GATE_EV))
    chk("review(): generic exec AC + gate_evidence -> spec_met True (no force-FAIL)",
        _res.spec_met is True and _res.verdict == Verdict.PASS, f"{_res.spec_met},{_res.verdict}")

    _res2 = asyncio.run(reviewer_mod.review("diff --git a/x b/x\n+ok\n", _tk_generic, _rapp, _rcfg,
                                            build_artifact=_no_evidence_ba))
    chk("review(): same AC with NO gate_evidence (default '') -> still forced FAIL (EU-268 pin)",
        _res2.spec_met is False and _res2.verdict == Verdict.FAIL,
        f"{_res2.spec_met},{_res2.verdict}")
finally:
    reviewer_mod.run_agent_with_fallback = _orig_run_agent_fb

# ══════════════════════════════════════════════════════════════════════════════
# 4. loop wiring — _attempt hands the computed evidence to reviewer_mod.review
# ══════════════════════════════════════════════════════════════════════════════
_CAPTURED: dict = {}


class _StubBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")

    @staticmethod
    async def build(req, app, cfg, audit=None, store=None, spec=None):
        if store is not None:
            store.put(BuildArtifact(files_changed=[], diff_digest="d",
                                    decisions=[], open_questions=[]))
        return BuildResult(ok=True, summary="built", cost_usd=0.1, num_turns=2)


class _CapReviewer:
    @staticmethod
    async def review(diff, ticket, app, cfg, iteration=1, store=None, build_artifact=None,
                     already_bounced=None, gate_evidence="", final_pass=False):
        _CAPTURED["gate_evidence"] = gate_evidence
        if store is not None:
            store.put(ReviewVerdict(verdict=Verdict.PASS, blocking=[], notes=[]))
        return ReviewResult(verdict=Verdict.PASS, spec_met=True, cost_usd=0.1)

    @staticmethod
    def collect_unverifiable_fingerprints(result):
        return set()


class _Git:
    def has_changes(self): return True
    def current_sha(self): return "base0001"
    def base_sha(self): return "base0001"
    def diff_against_base(self): return "diff --git a/x b/x\n+clean_line"
    def changed_paths(self): return ["orchestrator/loop.py"]


class _Backlog:
    def set_status(self, *a, **k): pass
    def add_comment(self, *a, **k): pass


class _Audit:
    def __init__(self): self.events = []
    def record(self, e, **k): self.events.append({"event": e, **k})


def _fake_land(tk, app, cfg, git, backlog, audit, branch, iteration, cost, build, review,
               commenter=None):
    from orchestrator.contracts import TicketReport
    return TicketReport(tk.id, Outcome.MERGED, iteration, cost, app.name, branch)


def _mkcfg() -> Config:
    d = Path(tempfile.mkdtemp())
    return Config(apps=[], audit_path=str(d / "audit.jsonl"), use_worktree=False,
                  pm_enabled=False, max_iterations=2)


def _wire_ticket(tid: str) -> Ticket:
    return Ticket(id=tid, key=tid, summary="s", description="d",
                  acceptance_criteria=[GENERIC_AC], app="a", ephemeral=True)


_orig = (loop.builder_mod, loop.reviewer_mod, loop.run_gate, loop._land, loop._notify)
loop.builder_mod = _StubBuilder
loop.reviewer_mod = _CapReviewer
loop.run_gate = lambda app, changed=None, **_: GateResult(passed=True, report="all commands passed")
loop._land = _fake_land
loop._notify = lambda c, t: None
try:
    _wapp = AppConfig(name="a", repo_path="/tmp", base_branch="DEV", protected_branch="MAIN",
                      backlog_backend="none", gate_commands=["python3 tests/run_all.py"])
    _CAPTURED.clear()
    rep = asyncio.run(loop._attempt(_wire_ticket("EU-265W1"), _wapp, _mkcfg(), _Git(), _Backlog(),
                                    _Audit(), loop.Budget(0), "autodev/EU-265W1"))
    chk("wiring: green REAL gate -> review() receives non-empty gate_evidence",
        bool(_CAPTURED.get("gate_evidence")), repr(_CAPTURED.get("gate_evidence")))
    chk("wiring: the evidence names the gate command",
        "python3 tests/run_all.py" in (_CAPTURED.get("gate_evidence") or ""),
        repr(_CAPTURED.get("gate_evidence")))
    chk("wiring: the ticket still lands", rep.outcome == Outcome.MERGED, str(rep.outcome))

    # A no-op gate app: run_gate reports the trivially-green marker -> NO evidence reaches review.
    loop.run_gate = lambda app, changed=None, **_: GateResult(
        passed=True, report="(no gate commands configured)")
    _wapp_noop = AppConfig(name="a", repo_path="/tmp", base_branch="DEV", protected_branch="MAIN",
                           backlog_backend="none", gate_commands=[])
    _CAPTURED.clear()
    asyncio.run(loop._attempt(_wire_ticket("EU-265W2"), _wapp_noop, _mkcfg(), _Git(), _Backlog(),
                              _Audit(), loop.Budget(0), "autodev/EU-265W2"))
    chk("wiring: no-op gate -> review() receives gate_evidence == '' (AUTO-109 hole stays closed)",
        _CAPTURED.get("gate_evidence") == "", repr(_CAPTURED.get("gate_evidence")))
finally:
    (loop.builder_mod, loop.reviewer_mod, loop.run_gate, loop._land, loop._notify) = _orig

print("\n================= EU-265 GATE-EVIDENCE -> REVIEWER =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
