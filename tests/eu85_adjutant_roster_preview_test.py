"""Adjutant personnel-review path + CLI (the domain-gap roster-preview path was REMOVED).

Phase-2 §2 flag-off collapse (2026-07-06): the ephemeral specialist-roster preview (EU-85) — which
delegated to the now-deleted ``hr.synthesize_specialists`` and the build-delegation squad — was
removed with hr.py. The Engineering Manager (adjutant) now ALWAYS runs the normal hire/retire
personnel pass; ``ticket_text`` is accepted for CLI compatibility but ignored, and adjutant no longer
consults ``squad.detect_domain_gap``. This harness pins the SURVIVING behaviour and guards that the
removed preview / gap-detection / dead single-role helpers stay gone.

All pure / offline — the SDK + run_agent are stubbed; no real models, no Jira, no network.
"""
import asyncio
import sys
import types
import tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import adjutant, memory
from orchestrator.agent import AgentRun
from orchestrator.config import Config, AppConfig
import orchestrator.squad as _squad

memory.preamble = lambda: ""

results = []
def chk(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def _cfg(tmp: Path) -> Config:
    audit = tmp / "audit.jsonl"
    audit.write_text("")
    return Config(
        apps=[AppConfig(name="automatixy", repo_path=str(tmp),
                        base_branch="DEV", protected_branch="MAIN", backlog_backend="none")],
        audit_path=str(audit), use_worktree=False, auto_model=False,
    )


def _fake_run(text: str) -> AgentRun:
    r = AgentRun.__new__(AgentRun)
    r.final = text; r.text = text; r.cost_usd = 0.0; r.num_turns = 1; r.tools = []
    r.is_error = False; r.input_tokens = 0; r.output_tokens = 0; r.provider = ""; r.model_version = ""
    return r


# ── 1. propose() → the normal personnel pass (no ticket_text) ──
with tempfile.TemporaryDirectory() as tmp:
    cfg1 = _cfg(Path(tmp))
    calls1 = []
    def _mock(prompt, options, tag=""):
        async def _inner():
            calls1.append(tag)
            return _fake_run("# Personnel review\nNo action needed.")
        return _inner()
    _orig = adjutant.run_agent
    adjutant.run_agent = _mock
    result1 = asyncio.run(adjutant.propose(cfg1))
    adjutant.run_agent = _orig
chk("propose: runs the normal personnel agent pass", len(calls1) > 0)
chk("propose: returns the personnel report", "Personnel review" in result1)


# ── 2. propose(ticket_text=…) → STILL the normal pass; gap-detection is NOT consulted ──
# The roster-preview path is gone: ticket_text is accepted-but-ignored, and adjutant must no longer
# call squad.detect_domain_gap.
with tempfile.TemporaryDirectory() as tmp:
    cfg2 = _cfg(Path(tmp))
    detect_calls = []
    async def _detect_spy(text, squad):
        detect_calls.append(text)
        return (True, "mql5", {})
    _orig_detect = _squad.detect_domain_gap
    _squad.detect_domain_gap = _detect_spy
    calls2 = []
    def _mock2(prompt, options, tag=""):
        async def _inner():
            calls2.append(tag)
            return _fake_run("# Personnel review")
        return _inner()
    _orig = adjutant.run_agent
    adjutant.run_agent = _mock2
    result2 = asyncio.run(adjutant.propose(cfg2, ticket_text="Build an MQL5 Expert Advisor"))
    adjutant.run_agent = _orig
    _squad.detect_domain_gap = _orig_detect
chk("propose(ticket_text): gap detection NOT consulted (preview path removed)", detect_calls == [], str(detect_calls))
chk("propose(ticket_text): still runs the normal personnel pass", len(calls2) > 0)
chk("propose(ticket_text): no roster-preview heading", "Specialist-Roster Preview" not in result2)


# ── 3. The removed preview + dead single-role helpers stay gone ──
for gone in ("_format_roster_preview", "_make_charter_recommendation",
             "_format_charter_recommendation", "_CHARTER_REC_SYSTEM"):
    chk(f"removed: adjutant.{gone}", not hasattr(adjutant, gone))


# ── 4. The CLI still parses `general adjutant [--ticket KEY]` (ticket accepted-but-ignored) ──
from orchestrator import main as _main
parser = _main.build_parser()
ns = parser.parse_args(["adjutant", "--ticket", "EU-85"])
chk("cli: adjutant subcommand selected", ns.command == "adjutant")
chk("cli: --ticket still captured", getattr(ns, "ticket", None) == "EU-85")
ns2 = parser.parse_args(["adjutant"])
chk("cli: bare adjutant still valid (ticket None)", getattr(ns2, "ticket", "x") is None)


passed = [n for n, ok, _ in results if ok]
failed = [(n, d) for n, ok, d in results if not ok]
print(f"\neu85_adjutant_roster_preview_test: {len(passed)}/{len(results)} passed")
for n, d in failed:
    print(f"  FAIL: {n}" + (f" — {d}" if d else ""))
if failed:
    sys.exit(1)
