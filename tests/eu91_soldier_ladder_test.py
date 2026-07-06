"""EU-91 regression tests — cheap-first model ladder for delegated soldier builds.

Verifies that for_soldier_build applies the same Sonnet-first escalation as the Builder
(never runs Opus from pass 1 on ordinary effort levels), and that build_delegated wires
the iteration correctly so the first soldier pass gets Sonnet, not Opus.

Covers:
  (a) for_soldier_build(cfg, effort='medium', iteration=1) -> Sonnet (not Opus)
  (b) for_soldier_build(cfg, effort='medium', iteration=2) -> Opus (escalated)
  (c) for_soldier_build(cfg, effort='max',    iteration=1) -> Opus (heavy-effort pin)
  (d) build_delegated on a non-pinned M ticket calls _soldier with a Sonnet model on pass 1
"""
from __future__ import annotations

import asyncio
import sys
import types
import tempfile
import os

# ---------------------------------------------------------------------------
# Stub the Agent SDK before any orchestrator import so the module tree loads
# without a real SDK installation.
# ---------------------------------------------------------------------------
sdk = types.ModuleType("claude_agent_sdk")


class _Dummy:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _Dummy
sys.modules.setdefault("claude_agent_sdk", sdk)
sys.path.insert(0, ".")

# ---------------------------------------------------------------------------
# Imports (after SDK stub is in place).
# ---------------------------------------------------------------------------
from orchestrator import models as M, squad
from orchestrator.agent import AgentRun
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, BuildRequest

# ---------------------------------------------------------------------------
# Test helpers (mirrors the style used across the suite).
# ---------------------------------------------------------------------------
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    """Record a named check (True = pass). Prints nothing now; the report at the end tallies."""
    results.append((name, bool(cond), str(detail)))


# ---------------------------------------------------------------------------
# Shared configs.
# ---------------------------------------------------------------------------
_audit_fd, _audit_path = tempfile.mkstemp(suffix=".jsonl")
os.close(_audit_fd)

ON = Config(apps=[], audit_path=_audit_path, auto_model=True, builder_model=M.OPUS)
OFF = Config(apps=[], audit_path=_audit_path, auto_model=False, builder_model=M.OPUS)

# ===========================================================================
# (a) for_soldier_build – medium effort, pass 1 → Sonnet
# ===========================================================================
m_a, why_a = M.for_soldier_build(ON, effort="medium", iteration=1)
chk(
    "(a) medium effort pass-1 → Sonnet (cheap-first ladder, not Opus)",
    m_a == M.SONNET,
    f"got {m_a}",
)
chk(
    "(a) reason string mentions the chosen model",
    "sonnet" in why_a.lower(),
    why_a,
)

# ===========================================================================
# (b) for_soldier_build – medium effort, pass 2 → Opus (escalated)
# ===========================================================================
m_b, why_b = M.for_soldier_build(ON, effort="medium", iteration=2)
chk(
    "(b) medium effort pass-2 → Opus (retry escalation)",
    m_b == M.OPUS,
    f"got {m_b}",
)
chk(
    "(b) reason mentions escalation",
    "escalat" in why_b.lower() or "retry" in why_b.lower(),
    why_b,
)

# ===========================================================================
# (c) for_soldier_build – max effort, pass 1 → Opus (heavy-effort pin)
# ===========================================================================
m_c, why_c = M.for_soldier_build(ON, effort="max", iteration=1)
chk(
    "(c) 'max' effort pass-1 → Opus (heavy-effort top-pin, no Sonnet-first)",
    m_c == M.OPUS,
    f"got {m_c}",
)
# Also verify 'maximum' and 'ultra' are pinned the same way.
chk(
    "(c) 'maximum' effort → Opus pin",
    M.for_soldier_build(ON, effort="maximum", iteration=1)[0] == M.OPUS,
)
chk(
    "(c) 'ultra' effort → Opus pin",
    M.for_soldier_build(ON, effort="ultra", iteration=1)[0] == M.OPUS,
)

# auto_model=OFF → always returns configured model regardless of effort/iteration.
chk(
    "(c) auto_model=False → fixed builder_model regardless of effort/iteration",
    M.for_soldier_build(OFF, effort="medium", iteration=1)[0] == M.OPUS
    and M.for_soldier_build(OFF, effort="medium", iteration=2)[0] == M.OPUS,
)

# The floor is Sonnet — Haiku must never be chosen for code.
chk(
    "floor: Sonnet never drops to Haiku for code",
    m_a != M.HAIKU,
)

# Escalation must never exceed the configured ceiling.
_sonnet_ceiling = Config(
    apps=[], audit_path=_audit_path, auto_model=True, builder_model=M.SONNET
)
chk(
    "ceiling: escalation never exceeds builder_model (Sonnet ceiling stays Sonnet on retry 5)",
    M.for_soldier_build(_sonnet_ceiling, effort="medium", iteration=5)[0] == M.SONNET,
)

# ===========================================================================
# (d) build_delegated integration — non-pinned M ticket → soldiers dispatched
#     with Sonnet on the first pass.
# ===========================================================================
# Minimal planner output: two M-sized subtasks (to pass the >=2 gate).
_PLAN_TWO_M = (
    '[{"role":"ordnance-be","title":"add endpoint","detail":"implement POST /export","size":"M"},'
    '{"role":"logistics-db","title":"migration","detail":"add export_jobs table","size":"M"}]'
)

# Captured (tag, model) pairs from each _soldier invocation.
_captured_soldier_models: list[tuple[str, str]] = []


class _TrackingOpts:
    """Minimal ClaudeAgentOptions stand-in that preserves kwargs as attributes."""

    def __init__(self, *a, **k):
        self.__dict__.update(k)


_orig_opts = squad.ClaudeAgentOptions
squad.ClaudeAgentOptions = _TrackingOpts  # type: ignore[attr-defined]


async def _tracking_run_agent(prompt, options, tag="", **kw):
    """Intercept agent calls; record the model from the options object for soldier turns."""
    if tag == "squad-lead":
        # Planner: return the two-M-subtask plan.
        return AgentRun(
            text=_PLAN_TWO_M,
            final=_PLAN_TWO_M,
            cost_usd=0.05,
            num_turns=2,
            is_error=False,
            tools=["Read"],
        )
    if tag.startswith("soldier"):
        model_used = getattr(options, "model", "?")
        _captured_soldier_models.append((tag, model_used))
        return AgentRun(
            text="done",
            final=f"implemented {tag}",
            cost_usd=0.15,
            num_turns=4,
            is_error=False,
            tools=["Edit"],
        )
    return AgentRun(
        text="solo", final="SOLO done", cost_usd=0.5, num_turns=8, is_error=False, tools=[]
    )


_orig_run_agent = squad.run_agent
squad.run_agent = _tracking_run_agent  # type: ignore[attr-defined]

# Stub detect_domain_gap → no gap (normal squad path).
_orig_detect = squad.detect_domain_gap


async def _no_gap(ticket_text, sq, **kw):
    # Legacy 2-tuple return (no burn dict) — _plan's defensive indexing must tolerate it.
    return False, None


squad.detect_domain_gap = _no_gap  # type: ignore[attr-defined]

# App + big ticket (≥3 AC → delegates even without L/XL size).
_app = AppConfig(
    name="automatixy",
    repo_path="/tmp/eu91",
    base_branch="DEV",
    protected_branch="MAIN",
    backlog_backend="none",
)
_ticket = Ticket(
    id="AUTO-99",
    key="AUTO-99",
    summary="Add CSV export feature touching DB, API and UI",
    description="Sizeable feature.",
    acceptance_criteria=["Export button visible", "CSV downloaded", "Rows scoped to tenant"],
    app="automatixy",
)
_cfg_d = Config(
    apps=[_app],
    audit_path=_audit_path,
    auto_model=True,
    builder_model=M.OPUS,
    delegation_enabled=True,
)

_captured_soldier_models.clear()
_res_d, _n_d = asyncio.run(
    squad.build_delegated(
        BuildRequest(_ticket, "DEV", iteration=1), _app, _cfg_d
    )
)

# Restore patched objects.
squad.run_agent = _orig_run_agent  # type: ignore[attr-defined]
squad.ClaudeAgentOptions = _orig_opts  # type: ignore[attr-defined]
squad.detect_domain_gap = _orig_detect  # type: ignore[attr-defined]

chk(
    "(d) build_delegated dispatched >=2 soldiers for a 3-AC ticket",
    _n_d >= 2,
    f"n={_n_d}",
)
chk(
    "(d) ALL M-ticket soldiers run on Sonnet (not Opus) on first pass",
    all(m == M.SONNET for _, m in _captured_soldier_models),
    str(_captured_soldier_models),
)
chk(
    "(d) at least one soldier model captured (wiring proof)",
    len(_captured_soldier_models) >= 1,
    str(_captured_soldier_models),
)

# Sanity-check: iteration=2 (retry) should make build_delegated use Opus.
# build_delegated only delegates on iteration=1, so iteration=2 exits early (None, 0).
# Instead, test _soldier directly with iteration=2 to confirm escalation is wired end-to-end.
_captured_soldier_models.clear()
squad.run_agent = _tracking_run_agent  # type: ignore[attr-defined]
squad.ClaudeAgentOptions = _TrackingOpts  # type: ignore[attr-defined]

_st_m = squad.Subtask(role="ordnance-be", title="retry", detail="fix x", size="M")
asyncio.run(squad._soldier(_st_m, BuildRequest(_ticket, "DEV", iteration=2), _app, _cfg_d, 1, 1,
                           iteration=2))
_m_retry = _captured_soldier_models[-1][1] if _captured_soldier_models else "?"

squad.run_agent = _orig_run_agent  # type: ignore[attr-defined]
squad.ClaudeAgentOptions = _orig_opts  # type: ignore[attr-defined]

chk(
    "(d/retry) _soldier with iteration=2, M-ticket → Opus (escalated, not Sonnet)",
    _m_retry == M.OPUS,
    f"got {_m_retry}",
)

# ===========================================================================
# Report
# ===========================================================================
print("\n========== EU-91 SOLDIER CHEAP-FIRST LADDER REGRESSION ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    marker = "PASS" if ok else "FAIL"
    suffix = f"  ({detail})" if detail and not ok else ""
    print(f"  [{marker}] {name}{suffix}")
print("-----------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results) - passed} FAILURE(S) ❌")
sys.exit(0 if passed == len(results) else 1)
