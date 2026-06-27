"""EU-85 (iter-2) — Adjutant domain-gap path: ADVISORY full-roster preview, wired to a real caller.

Iteration 1 shipped an UNREACHABLE adjutant domain-gap path that returned a SINGLE-role charter
recommendation (`_make_charter_recommendation` / `_format_charter_recommendation`) with no caller —
it claimed to deliver the fix-scope item but nothing invoked it, and a single role can't satisfy
AC#1 (four specialists for an MQL5 ticket).

Iteration 2:
  • WIRES the path to a real caller: `general adjutant --ticket KEY` (build_parser accepts --ticket).
  • Replaces the single-role recommendation with an ADVISORY preview of the FULL roster, by
    delegating to `hr.synthesize_specialists` (the SAME synthesizer the build loop uses) — so an
    MQL5 ticket previews all four specialists (algo developer, backtester, strategist, data analyst).
  • The report states explicitly that it PROVISIONS NOTHING — real provisioning is squad/HR at
    build time.
  • The dead single-role helpers are deleted.

All pure / offline — the SDK + run_agent + hr.synthesize_specialists are stubbed; no real models,
no Jira, no network.
"""
import asyncio
import sys
import types
import tempfile
from pathlib import Path

# ── Stub the Claude Agent SDK (no model calls in unit tests) ─────────────────
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import adjutant, hr, memory, models
from orchestrator.agent import AgentRun
from orchestrator.config import Config, AppConfig
import orchestrator.squad as _squad

# Silence real memory/preamble reads in tests.
memory.preamble = lambda: ""

results = []
def chk(name, cond, detail=""):
    results.append((name, bool(cond), detail))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _cfg(tmp: Path) -> Config:
    audit = tmp / "audit.jsonl"
    audit.write_text("")
    return Config(
        apps=[AppConfig(name="automatixy", repo_path=str(tmp),
                        base_branch="DEV", protected_branch="MAIN",
                        backlog_backend="none")],
        audit_path=str(audit),
        use_worktree=False,
        auto_model=False,
    )


def _fake_run(text: str) -> AgentRun:
    r = AgentRun.__new__(AgentRun)
    r.final = text
    r.text = text
    r.cost_usd = 0.0
    r.num_turns = 1
    r.tools = []
    r.is_error = False
    return r


def _charter(name, lane, gate):
    return {
        "name": name, "lane_key": lane,
        "identity": f"{name} — a narrow specialist.",
        "knowledge": "the ticket + the platform docs",
        "skills": ["read the ticket", "implement", "self-check"],
        "constraints": "stay in your lane; touch only your files",
        "domain_gate": gate, "ephemeral": True,
    }


# AC#1 roster: algo developer + backtester + strategist + data analyst (FOUR specialists).
MQL5_ROSTER = [
    _charter("MQL5 Algorithmic Trading Developer", "mql5-algo", "mql5 compile + Strategy Tester run"),
    _charter("Strategy Backtester", "backtester", "run backtest; assert profit factor > 1"),
    _charter("Trading Strategist", "strategist", "review entry/exit logic vs the spec"),
    _charter("Market Data Analyst", "data-analyst", "validate tick/OHLC data integrity"),
]


# ── 1. _format_roster_preview renders the FULL roster + advisory framing ──────

report = adjutant._format_roster_preview("mql5", MQL5_ROSTER)

chk("preview: all four specialist names present",
    all(c["name"] in report for c in MQL5_ROSTER),
    [c["name"] for c in MQL5_ROSTER if c["name"] not in report])
chk("preview: AC#1 roles present (algo/backtest/strateg/analyst)",
    all(k in report.lower() for k in ("algorithmic", "backtester", "strategist", "analyst")))
chk("preview: counts four specialists", "4 specialist(s)" in report, report[:200])
chk("preview: domain in heading", "mql5" in report)
chk("preview: ephemeral marker present", "ephemeral" in report.lower())
chk("preview: labelled ADVISORY", "advisory" in report.lower())
chk("preview: states it provisions nothing", "provisions nothing" in report.lower())
# Point 2 (advisory branch): explicitly names squad/HR as the real provisioning surface.
chk("preview: names squad as the real provisioning surface", "squad.build_delegated" in report)
chk("preview: names hr.synthesize_specialists as the real surface", "hr.synthesize_specialists" in report)


# ── 2. _format_roster_preview with an EMPTY roster degrades gracefully ────────

empty_report = adjutant._format_roster_preview("mql5", [])
chk("empty preview: says nothing to preview", "nothing to preview" in empty_report.lower())
chk("empty preview: no 'Proposed roster' section", "Proposed roster" not in empty_report)
chk("empty preview: still advisory", "advisory" in empty_report.lower())


# ── 3. propose() out-of-lane → delegates to hr.synthesize_specialists, full roster ──

with tempfile.TemporaryDirectory() as tmp:
    cfg3 = _cfg(Path(tmp))

    async def _fake_gap(text, squad):
        return (True, "mql5")

    synth_calls = []
    async def _fake_synth(domain, ticket_text, cfg, *, approver=None):
        synth_calls.append(domain)
        # Prove the advisory preview auto-returns the roster (no human/TTY needed).
        assert approver is not None and approver("?") == "y", "preview must inject an auto-approver"
        return MQL5_ROSTER

    _orig_detect = _squad.detect_domain_gap
    _orig_synth = hr.synthesize_specialists
    _squad.detect_domain_gap = _fake_gap
    hr.synthesize_specialists = _fake_synth

    # run_agent must NOT be called on the gap path (no expensive personnel pass).
    _orig_run_agent = adjutant.run_agent
    agent_calls3 = []
    def _spy_agent(prompt, options, tag=""):
        async def _inner():
            agent_calls3.append(tag)
            return _fake_run("# Personnel review")
        return _inner()
    adjutant.run_agent = _spy_agent

    result3 = asyncio.run(adjutant.propose(cfg3, ticket_text="Build an MQL5 Expert Advisor"))

    adjutant.run_agent = _orig_run_agent
    _squad.detect_domain_gap = _orig_detect
    hr.synthesize_specialists = _orig_synth

chk("propose gap: delegated to hr.synthesize_specialists exactly once", synth_calls == ["mql5"], str(synth_calls))
chk("propose gap: returned the roster preview", "Specialist-Roster Preview" in result3)
chk("propose gap: preview carries ALL FOUR specialists",
    all(c["name"] in result3 for c in MQL5_ROSTER))
chk("propose gap: did NOT run the personnel agent pass", agent_calls3 == [], str(agent_calls3))


# ── 4. propose() in-lane (covered) → normal personnel pass, no roster preview ──

with tempfile.TemporaryDirectory() as tmp:
    cfg4 = _cfg(Path(tmp))

    async def _fake_covered(text, squad):
        return (False, None)

    _orig_detect = _squad.detect_domain_gap
    _squad.detect_domain_gap = _fake_covered

    normal_calls = []
    def _mock_normal(prompt, options, tag=""):
        async def _inner():
            normal_calls.append(tag)
            return _fake_run("# Personnel review\nNo action needed.")
        return _inner()
    _orig_run_agent = adjutant.run_agent
    adjutant.run_agent = _mock_normal

    # If the in-lane path ever reached synthesis, this would blow up — it must not.
    _orig_synth = hr.synthesize_specialists
    async def _explode_synth(*a, **k):
        raise AssertionError("synthesis must not run for an in-lane ticket")
    hr.synthesize_specialists = _explode_synth

    result4 = asyncio.run(adjutant.propose(cfg4, ticket_text="Add a React button component"))

    adjutant.run_agent = _orig_run_agent
    _squad.detect_domain_gap = _orig_detect
    hr.synthesize_specialists = _orig_synth

chk("propose in-lane: returns the normal personnel report", "Personnel review" in result4)
chk("propose in-lane: ran the normal agent pass", len(normal_calls) > 0)
chk("propose in-lane: no roster preview heading", "Specialist-Roster Preview" not in result4)


# ── 5. propose() without ticket_text → normal pass, gap detection NOT called ──

with tempfile.TemporaryDirectory() as tmp:
    cfg5 = _cfg(Path(tmp))

    detect_calls5 = []
    async def _detect_spy(text, squad):
        detect_calls5.append(text)
        return (True, "mql5")   # would be a gap, but must NOT be consulted

    _orig_detect = _squad.detect_domain_gap
    _squad.detect_domain_gap = _detect_spy

    no_ticket_calls = []
    def _mock_normal5(prompt, options, tag=""):
        async def _inner():
            no_ticket_calls.append(tag)
            return _fake_run("# Normal personnel report")
        return _inner()
    _orig_run_agent = adjutant.run_agent
    adjutant.run_agent = _mock_normal5

    result5 = asyncio.run(adjutant.propose(cfg5))   # no ticket_text

    adjutant.run_agent = _orig_run_agent
    _squad.detect_domain_gap = _orig_detect

chk("propose no-ticket: gap detection NOT called", len(detect_calls5) == 0)
chk("propose no-ticket: normal agent called", len(no_ticket_calls) > 0)
chk("propose no-ticket: normal report returned", "Normal personnel report" in result5)


# ── 6. The dead single-role helpers are GONE (no unreachable code claiming a fix) ──

chk("removed: _make_charter_recommendation", not hasattr(adjutant, "_make_charter_recommendation"))
chk("removed: _format_charter_recommendation", not hasattr(adjutant, "_format_charter_recommendation"))
chk("removed: _CHARTER_REC_SYSTEM", not hasattr(adjutant, "_CHARTER_REC_SYSTEM"))


# ── 7. The CLI is wired: `general adjutant --ticket KEY` parses (real caller) ──

from orchestrator import main as _main   # offline-safe with the SDK stub
parser = _main.build_parser()
ns = parser.parse_args(["adjutant", "--ticket", "EU-85"])
chk("cli: adjutant subcommand selected", ns.command == "adjutant")
chk("cli: --ticket captured", getattr(ns, "ticket", None) == "EU-85")
chk("cli: --app present (optional, defaults None)", hasattr(ns, "app"))
# The plain `adjutant` invocation still works (ticket defaults to None → normal review).
ns2 = parser.parse_args(["adjutant"])
chk("cli: bare adjutant still valid (ticket None)", getattr(ns2, "ticket", "x") is None)


# ── Results ──────────────────────────────────────────────────────────────────

passed = [n for n, ok, _ in results if ok]
failed = [(n, d) for n, ok, d in results if not ok]
print(f"\neu85_adjutant_roster_preview_test: {len(passed)}/{len(results)} passed")
for n, d in failed:
    print(f"  FAIL: {n}" + (f" — {d}" if d else ""))
if failed:
    sys.exit(1)
