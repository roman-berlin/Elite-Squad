"""EU-753 + EU-754 — the model the Commander picks must be the model that runs, and a Claude plan
limit must not pause a drain that never calls Claude.

Both defects were measured live on 2026-07-28.

EU-754 SWITCH LATENCY. He selected GLM in the cockpit at 09:11 to stop spending Claude limits. The
drain kept dispatching to Claude until 10:45 — 94 minutes. The cockpit write itself was instant and
correct (``state/model_backend.json``); the running drain simply never re-read it, because the ONLY
re-read sat ~70 lines into the cycle preamble behind ``if not plan_check.get("hit")`` — and one
cycle covers the entire worklist. Two consequences, both fixed here: the re-read now happens FIRST
in the cycle (so a switch binds at the next ticket, not the next cycle-with-no-limit), and it is no
longer gated on the plan-limit state at all — which had the perverse effect of freezing the
preference exactly when a Claude limit was active, i.e. when switching AWAY from Claude matters
most.

EU-753 THE PLAN PROBE IS CLAUDE-ONLY. ``usage.plan_limit_hit`` reads the *Claude* subscription. The
preamble probed it unconditionally and PAUSED the whole drain on a hit — so a unit configured
"qwen only" (or GLM only, the Commander's 2026-07-28 configuration) could sit idle behind a limit
belonging to a model it never calls. The probe is now conditional on the run being able to reach
Claude at all. Hybrid stays protected: ``backends._HYBRID_BUILD_TAGS`` routes only the builder to
the secondary, so Claude carries planner/reviewer whenever it is EITHER side of the pair — the
check is "Claude on either side", not "Claude is primary".

Offline: SDK stubbed, real backend_pref/model_registry over a tmp state dir, no network.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import types
from types import SimpleNamespace

_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


_sdk.__getattr__ = lambda _n: _D
sys.modules.setdefault("claude_agent_sdk", _sdk)
sys.path.insert(0, ".")

from orchestrator import autopilot, backend_pref  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# ── a hermetic state dir with a real registry record (the Qwen endpoint's shape) ──────────────
_tmp = pathlib.Path(tempfile.mkdtemp())
QWEN = "f6be0ded-2b6d-4721-9fc1-4aabee7cee5b"
(_tmp / "model_registry.json").write_text(json.dumps({"models": {QWEN: {
    "id": QWEN, "display_name": "Qwen3.8-Max-Preview (builder)", "provider": "anthropic",
    "base_url": "https://token-plan.example/apps/anthropic", "model_id": "qwen3.8-max-preview",
}}}), encoding="utf-8")
cfg = SimpleNamespace(audit_path=str(_tmp / "audit.jsonl"), apps=[])


def _pref(backend: str | None, secondary: str | None = None) -> None:
    payload: dict = {"apps": {}}
    if backend is not None:
        payload["backend"] = backend
    if secondary is not None:
        payload["secondary"] = secondary
    (_tmp / "model_backend.json").write_text(json.dumps(payload), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 1. EU-753: _run_can_use_claude — behavioural, against the REAL preference store
# ══════════════════════════════════════════════════════════════════════════════════════════════
_pref("opus")
chk("(1) opus only -> the run uses Claude (probe the plan, as always)",
    autopilot._run_can_use_claude(cfg) is True)

_pref("glm")
chk("(2) GLM only, no secondary -> the run CANNOT use Claude (skip the Claude plan probe)",
    autopilot._run_can_use_claude(cfg) is False)

_pref(QWEN)
chk("(3) a registry backend (Qwen) only -> the run cannot use Claude",
    autopilot._run_can_use_claude(cfg) is False,
    f"resolved primary={backend_pref.active(cfg)!r}")
# guard the premise of (3): if the id had degraded to 'opus' the assertion would pass vacuously
chk("(3a) …and the premise holds — the Qwen id survived resolution, it did not degrade to opus",
    backend_pref.active(cfg) == QWEN, backend_pref.active(cfg))

# HYBRID — the case that must NOT skip. Either side being Claude means Claude runs some role.
_pref("opus", secondary=QWEN)
chk("(4) hybrid opus+Qwen (the live pairing) -> still uses Claude",
    autopilot._run_can_use_claude(cfg) is True)
_pref(QWEN, secondary="opus")
chk("(5) hybrid with Claude as the SECONDARY -> still uses Claude (it carries the fallback)",
    autopilot._run_can_use_claude(cfg) is True)
_pref("glm", secondary=QWEN)
chk("(6) two non-Claude models paired -> cannot use Claude",
    autopilot._run_can_use_claude(cfg) is False)

# never raises — an unreadable preference degrades to True (probe as before), because being blind
# to a real limit is worse than one extra cached probe
(_tmp / "model_backend.json").write_text("{ not json", encoding="utf-8")
chk("(7) a corrupt preference store degrades SAFELY to True (never raises, never skips blind)",
    autopilot._run_can_use_claude(cfg) is True)
chk("(8) a cfg with no state at all -> True (old behaviour preserved)",
    autopilot._run_can_use_claude(SimpleNamespace()) is True)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 2. The cycle-preamble wiring (source order — the defect WAS an ordering/gating bug)
# ══════════════════════════════════════════════════════════════════════════════════════════════
SRC = pathlib.Path("orchestrator/autopilot.py").read_text(encoding="utf-8")

_refresh = SRC.find('audit.record("model_backend_refreshed"')
_probe = SRC.find("plan_check = usage.plan_limit_hit(cfg)")
_hit = SRC.find('if plan_check.get("hit"):')
chk("(9) the preference re-read exists in the cycle", _refresh != -1, str(_refresh))
chk("(10) the plan probe exists in the cycle", _probe != -1, str(_probe))
chk("(11) EU-754: the re-read runs BEFORE the plan probe, not ~70 lines after it",
    _refresh != -1 and _probe != -1 and _refresh < _probe, f"refresh@{_refresh} probe@{_probe}")
chk("(12) EU-754: …and BEFORE the plan-limit branch that used to gate it",
    _refresh != -1 and _hit != -1 and _refresh < _hit, f"refresh@{_refresh} hit@{_hit}")
chk("(13) EU-753: the probe is CONDITIONAL on the run being able to reach Claude",
    "plan_check = usage.plan_limit_hit(cfg) if _uses_claude else" in SRC)
chk("(14) the skip is audited, so a silent no-probe can't be mistaken for 'no limit'",
    'audit.record("plan_limit_check_skipped"' in SRC)
chk("(15) the skipped-probe stub still answers .get('hit') falsy for every downstream reader",
    'else {"hit": False, "blind": False}' in SRC)

# EU-754: the old block gated the re-read on the plan state. That gate must be GONE — this is the
# assertion that actually fails if someone restores it.
_old_gate = 'if not plan_check.get("hit") and getattr(cfg, "model_backend", _main_bk) != _main_bk:'
chk("(16) the plan-limit-gated re-read is gone (it froze the preference during a limit)",
    _old_gate not in SRC)

print("\n=========== EU-753/754 MODEL SWITCH LATENCY + CLAUDE-ONLY PLAN PROBE ===========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
