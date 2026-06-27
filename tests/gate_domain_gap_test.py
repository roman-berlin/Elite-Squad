"""EU-85 (iter-2): the verification gate is DECOUPLED from domain-gap classification.

Iteration 1 ran ``detect_domain_gap`` a SECOND time inside the gate (via
``gate.domain_gap_preflight``) just to staple an advisory note onto the gate report — a
duplicate of the classification the squad delegation path (``squad._plan``) already performs,
and a note that lied whenever delegation was off or the ticket was too small to delegate.

Iteration 2 removes that redundant path entirely: domain-gap classification now lives in ONE
place (``squad.detect_domain_gap``, called from the delegation path where it actually routes
provisioning). This harness locks that in:

  • ``gate`` no longer exposes ``domain_gap_preflight`` (the redundant classifier entry point).
  • ``run_gate`` no longer accepts a ``gap_note`` parameter.
  • ``run_gate`` core pass/fail behaviour is unchanged (the EU-19 per-app + fallback paths).
  • ``squad.detect_domain_gap`` remains the single canonical classifier.

All pure / offline — the SDK is stubbed; no real models, no network.
"""
import inspect
import sys
import types

# ---------------------------------------------------------------------------
# Minimal SDK stub so gate.py (and its imports) load without the real SDK.
# ---------------------------------------------------------------------------
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): pass

    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import gate  # noqa: E402
from orchestrator.config import AppConfig  # noqa: E402


def _app(**kw) -> AppConfig:
    defaults = dict(
        name="test-app",
        repo_path="/tmp",
        gate_commands=[],
        gate_timeout_sec=30,
        gate_env={},
    )
    defaults.update(kw)
    return AppConfig(**defaults)


results: list[tuple[bool, str]] = []


def chk(label: str, ok: bool, detail: str = "") -> None:
    results.append((ok, label))
    mark = "✓" if ok else "✗"
    suffix = f" — {detail}" if (not ok and detail) else ""
    print(f"  {mark} {label}{suffix}", flush=True)


# ---------------------------------------------------------------------------
# 1. The redundant gate-side classifier is GONE.
# ---------------------------------------------------------------------------
chk("gate has no domain_gap_preflight (redundant classifier removed)",
    not hasattr(gate, "domain_gap_preflight"))

# ---------------------------------------------------------------------------
# 2. run_gate no longer takes a gap_note parameter.
# ---------------------------------------------------------------------------
sig = inspect.signature(gate.run_gate)
chk("run_gate signature has no gap_note param", "gap_note" not in sig.parameters,
    str(list(sig.parameters)))

# Passing gap_note= must now be a TypeError (the kwarg no longer exists).
_kw_rejected = False
try:
    gate.run_gate(_app(gate_commands=["true"]), gap_note="anything")  # type: ignore[call-arg]
except TypeError:
    _kw_rejected = True
chk("run_gate rejects a gap_note kwarg (TypeError)", _kw_rejected)

# ---------------------------------------------------------------------------
# 3. run_gate core behaviour is unchanged (regression guard).
# ---------------------------------------------------------------------------
res_pass = gate.run_gate(_app(gate_commands=["true"]))
chk("clean gate passes", res_pass.passed, repr(res_pass.report))

res_fail = gate.run_gate(_app(gate_commands=["sh -c 'exit 7'"]))
chk("non-zero gate fails", not res_fail.passed, repr(res_fail.report))

res_empty = gate.run_gate(_app(gate_commands=[]))
chk("empty gate is a pass (unchanged)", res_empty.passed)

# No stray domain-gap text leaks into a plain gate report.
chk("no 'domain-gap' prefix leaks into the report",
    "domain-gap" not in (res_pass.report or "").lower())

# ---------------------------------------------------------------------------
# 4. The single canonical classifier still lives in squad.
# ---------------------------------------------------------------------------
import orchestrator.squad as _squad  # noqa: E402

chk("squad.detect_domain_gap is the single canonical classifier",
    callable(getattr(_squad, "detect_domain_gap", None)))

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
passed = [r for r in results if r[0]]
failed = [r for r in results if not r[0]]
print(f"\n{'='*60}")
print(f"gate_domain_gap_test: {len(passed)}/{len(results)} passed", flush=True)
if failed:
    for _, label in failed:
        print(f"  FAIL: {label}", flush=True)
    sys.exit(1)
