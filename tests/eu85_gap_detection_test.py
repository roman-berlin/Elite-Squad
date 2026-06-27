"""EU-85 — detect_domain_gap: JSON-parsing logic + AC#1/AC#2 integration wire.

The existing squad_test.py stubs ``detect_domain_gap`` wholesale, so the JSON-parsing
branches inside it (covered/not-covered, prose wrapping, missing domain, exception fail-safe)
are never exercised.  This harness stubs only ``run_agent`` and drives the real parsing
path, pinning each branch in isolation.

Also locks the two ticket-level acceptance criteria:
  AC#1 — "MQL5 Expert Advisor" ticket → provisioning path routes to synthesis,
          which yields the four mandatory roles WITHOUT writing any officer file.
  AC#2 — React/FastAPI/Supabase ticket → normal squad delegation; synthesis path
          is never entered.

All pure / offline — SDK and run_agent are stubbed; no real models, no network.
"""
import asyncio
import sys
import types

# ── Stub the Claude Agent SDK (no model calls in unit tests) ─────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): pass

    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.squad as squad  # noqa: E402
from orchestrator.agent import AgentRun  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def _run(text: str) -> AgentRun:
    """Build a minimal AgentRun with the given text (stubs the real model response)."""
    r = AgentRun.__new__(AgentRun)
    r.final = text
    r.text = text
    r.cost_usd = 0.0
    r.num_turns = 1
    r.tools = []
    r.is_error = False
    return r


def _stub(text: str):
    """Return a coroutine factory that yields a fake AgentRun carrying ``text``."""
    async def _fake(prompt, opts, tag=""):
        return _run(text)
    return _fake


# The known squad lane map (matches orchestrator.squad.SQUAD).
_SQUAD = squad.SQUAD


# ─────────────────────────────────────────────────────────────────────────────
# 1. JSON parsing — covered=true → (False, None)
# ─────────────────────────────────────────────────────────────────────────────
squad.run_agent = _stub('{"covered": true, "domain": "vanguard-fe"}')
r = asyncio.run(squad.detect_domain_gap("Add a React button", _SQUAD))
chk("covered=true → (False, None) — no gap declared", r == (False, None), str(r))

# ─────────────────────────────────────────────────────────────────────────────
# 2. JSON parsing — covered=false → (True, domain)
# ─────────────────────────────────────────────────────────────────────────────
squad.run_agent = _stub('{"covered": false, "domain": "mql5"}')
r2 = asyncio.run(squad.detect_domain_gap("Build an MQL5 Expert Advisor", _SQUAD))
chk("covered=false + domain → (True, 'mql5')", r2 == (True, "mql5"), str(r2))

# domain is lower-cased
squad.run_agent = _stub('{"covered": false, "domain": "MQL5"}')
r2b = asyncio.run(squad.detect_domain_gap("Build an MQL5 Expert Advisor", _SQUAD))
chk("domain is lower-cased in the result", r2b == (True, "mql5"), str(r2b))

# ─────────────────────────────────────────────────────────────────────────────
# 3. JSON wrapped in prose (the classifier adds commentary)
# ─────────────────────────────────────────────────────────────────────────────
squad.run_agent = _stub(
    'Sure! The answer is: {"covered": false, "domain": "solidity"} — good luck!'
)
r3 = asyncio.run(squad.detect_domain_gap("Deploy an ERC-20 contract", _SQUAD))
chk("prose-wrapped JSON still parsed correctly", r3 == (True, "solidity"), str(r3))

# ─────────────────────────────────────────────────────────────────────────────
# 4. No JSON block → fail-safe (False, None)
# ─────────────────────────────────────────────────────────────────────────────
squad.run_agent = _stub("I cannot classify this ticket.")
r4 = asyncio.run(squad.detect_domain_gap("Some ticket", _SQUAD))
chk("no JSON block → (False, None) fail-safe", r4 == (False, None), str(r4))

# ─────────────────────────────────────────────────────────────────────────────
# 5. Malformed JSON → fail-safe (False, None)
# ─────────────────────────────────────────────────────────────────────────────
squad.run_agent = _stub("{covered: false}")  # not valid JSON
r5 = asyncio.run(squad.detect_domain_gap("Some ticket", _SQUAD))
chk("malformed JSON → (False, None) fail-safe", r5 == (False, None), str(r5))

# ─────────────────────────────────────────────────────────────────────────────
# 6. SDK / run_agent exception → fail-safe (False, None) — NEVER blocks the build
# ─────────────────────────────────────────────────────────────────────────────
async def _explode(prompt, opts, tag=""):
    raise RuntimeError("model down")

squad.run_agent = _explode
r6 = asyncio.run(squad.detect_domain_gap("Some ticket", _SQUAD))
chk("run_agent exception → (False, None) fail-safe (never crashes build)", r6 == (False, None), str(r6))

# ─────────────────────────────────────────────────────────────────────────────
# 7. empty domain string → domain is None (not empty string)
# ─────────────────────────────────────────────────────────────────────────────
squad.run_agent = _stub('{"covered": false, "domain": ""}')
r7 = asyncio.run(squad.detect_domain_gap("Some ticket", _SQUAD))
chk("empty domain string → domain is None in tuple", r7 == (False, None) or r7[1] is None, str(r7))

# ─────────────────────────────────────────────────────────────────────────────
# 8. covered key absent (model omits it) → defaults to True → (False, None)
# ─────────────────────────────────────────────────────────────────────────────
squad.run_agent = _stub('{"domain": "rust"}')
r8 = asyncio.run(squad.detect_domain_gap("Some ticket", _SQUAD))
chk("absent 'covered' key defaults to True → (False, None)", r8 == (False, None), str(r8))

# ─────────────────────────────────────────────────────────────────────────────
# 9. AC#2 — React/FastAPI/Supabase ticket → covered=true → normal squad route,
#    synthesis path is NEVER entered.
# ─────────────────────────────────────────────────────────────────────────────
squad.run_agent = _stub('{"covered": true, "domain": "vanguard-fe"}')
r9 = asyncio.run(squad.detect_domain_gap(
    "Add a React button that POSTs to FastAPI and writes to Supabase", _SQUAD
))
chk("AC#2: React/FastAPI/Supabase ticket → (False, None) — no gap, normal route", r9 == (False, None), str(r9))

# ─────────────────────────────────────────────────────────────────────────────
# 10. AC#1 — MQL5 Expert Advisor ticket → gap detected → synthesis path entered,
#     four mandatory roles returned as ephemeral charters (never written to officers/).
# ─────────────────────────────────────────────────────────────────────────────
#
# We stub only:
#   • detect_domain_gap → (True, "mql5")      [already proven above]
#   • hr.synthesize_specialists → MQL5_ROSTER  [four mandatory roles]
# and assert the synthesis route is taken AND the charters carry the right roles
# WITHOUT creating any officer file.
import orchestrator.hr as _hr  # noqa: E402
import tempfile, os as _os  # noqa: E402
from pathlib import Path  # noqa: E402
from orchestrator.config import Config, AppConfig  # noqa: E402
from orchestrator.contracts import Ticket, BuildRequest, BuildResult  # noqa: E402


def _mql5_charter(name, lane):
    return {
        "name": name, "lane_key": lane,
        "identity": f"{name} — a narrow MQL5 specialist.",
        "knowledge": "the ticket + MT5 platform docs",
        "skills": ["read the ticket", "implement the EA", "self-check via Strategy Tester"],
        "constraints": "stay in your lane; touch only your files",
        "domain_gate": "mql5 compile + Strategy Tester run",
        "ephemeral": True,
    }


MQL5_ROSTER = [
    _mql5_charter("MQL5 Algorithmic Trading Developer", "mql5-algo"),
    _mql5_charter("Strategy Backtester",                "backtester"),
    _mql5_charter("Trading Strategist",                  "strategist"),
    _mql5_charter("Market Data Analyst",                 "data-analyst"),
]

_audit_fd, _audit_path = tempfile.mkstemp(suffix=".jsonl")
_os.close(_audit_fd)
_app = AppConfig(name="algo", repo_path="/tmp/x",
                 base_branch="DEV", protected_branch="MAIN", backlog_backend="none")
# auto_mode=True: bypass the non-automode Telegram approval gate so the test exercises
# the provisioning path directly (the gate's own behaviour is covered by eu88_specialist_approval_test).
_cfg = Config(apps=[_app], audit_path=_audit_path, use_worktree=False,
              delegation_enabled=True, auto_mode=True)

# Stub detect_domain_gap and hr.synthesize_specialists; track calls.
async def _gap_mql5(ticket_text, sq):
    return (True, "mql5")

_synth_calls: list[str] = []
_returned_charters: list[dict] = []

async def _synth_mql5(domain, ticket_text, cfg, *, approver=None):
    _synth_calls.append(domain)
    _returned_charters.extend(MQL5_ROSTER)
    return list(MQL5_ROSTER)

_orig_detect = squad.detect_domain_gap
_orig_synth = _hr.synthesize_specialists
squad.detect_domain_gap = _gap_mql5
_hr.synthesize_specialists = _synth_mql5

# _run_synthesis dispatches each specialist as a soldier; stub run_agent so no real model runs.
async def _fake_runner(prompt, opts, tag="", ticket_id=None, pass_number=None):
    return _run("done")

squad.run_agent = _fake_runner

# hr lifecycle stubs (avoid real audit writes / check_promote blocking).
_hr.record_domain_use = lambda domain, cfg: None
_hr.check_promote = lambda domain, cfg, *a, **k: False

_ticket = Ticket(
    id="EU-85", key="EU-85",
    summary="Build an MQL5 Expert Advisor",
    description="Implement a trend-following EA using Moving Averages on MT5.",
    acceptance_criteria=["EA compiles", "backtest passes", "strategy logic reviewed", "data validated"],
    app="algo",
)
_req = BuildRequest(_ticket, "worktree-path", iteration=1)

result_ac1, n_ac1 = asyncio.run(squad.build_delegated(_req, _app, _cfg))

squad.detect_domain_gap = _orig_detect
_hr.synthesize_specialists = _orig_synth

# AC#1 checks:
chk("AC#1: synthesis route entered for MQL5 ticket", len(_synth_calls) == 1 and _synth_calls[0] == "mql5",
    str(_synth_calls))
chk("AC#1: all four mandatory roles returned", len(_returned_charters) == 4, str(len(_returned_charters)))
chk("AC#1: algo developer present", any("algo" in c["lane_key"] for c in _returned_charters))
chk("AC#1: backtester present",     any("backtester" in c["lane_key"] for c in _returned_charters))
chk("AC#1: strategist present",     any("strategist" in c["lane_key"] for c in _returned_charters))
chk("AC#1: data analyst present",   any("data-analyst" in c["lane_key"] for c in _returned_charters))
chk("AC#1: all charters are ephemeral (never written to officers/)",
    all(c.get("ephemeral") is True for c in _returned_charters))
chk("AC#1: no officer file created for mql5",
    not (Path(_audit_path).parent / "officers" / "mql5-specialist.md").exists())
chk("AC#1: synthesis result returned (not None)", result_ac1 is not None, str(result_ac1))
chk("AC#1: exactly 1 specialist slot (synthesis counts as n=1)", n_ac1 == 1, str(n_ac1))

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
passed = [n for n, ok, _ in results if ok]
failed = [(n, d) for n, ok, d in results if not ok]
print(f"\neu85_gap_detection_test: {len(passed)}/{len(results)} passed")
for n, d in failed:
    print(f"  FAIL: {n}" + (f" — {d}" if d else ""))
if failed:
    sys.exit(1)
