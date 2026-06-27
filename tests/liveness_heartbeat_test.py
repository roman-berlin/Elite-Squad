"""EU-84: usage.record() bumps last_activity for active runs.

Regression guard: a run in a long soldier-build with recent model calls must
render "working …", never "stuck".  Previously last_activity was only bumped by
bump_log_seq (stdout), which the soldier sub-agent's stdout bypasses — so the
cockpit chip false-alarmed after ~6 min of delegation.

Tests 1-5: verify usage.record() propagates the heartbeat via last_activity.
Tests 6-7: verify the _liveness() chip renders "working", never "stuck", when
           last_activity is fresh — closing the end-to-end acceptance criterion.
"""
import sys
import types
import tempfile
import time
from pathlib import Path

# ------------------------------------------------------------------
# Stub claude_agent_sdk so the orchestrator modules import cleanly
# without a real SDK present.
# ------------------------------------------------------------------
_sdk = types.ModuleType("claude_agent_sdk")
class _CAO:
    def __init__(self, **kw): self.__dict__.update(kw)
_sdk.ClaudeAgentOptions = _CAO
_sdk.__getattr__ = lambda n: type(n, (), {})
sys.modules.setdefault("claude_agent_sdk", _sdk)

sys.path.insert(0, ".")

from orchestrator import usage          # noqa: E402
from orchestrator import cockpit_state  # noqa: E402
from orchestrator.warroom import _liveness  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# ── helpers ────────────────────────────────────────────────────────
def _fresh_ledger() -> Path:
    tmp = Path(tempfile.mkdtemp())
    ledger = tmp / "usage_ledger.jsonl"
    usage.configure(str(tmp / "audit.jsonl"))
    return ledger


# ── 1. No active run → last_activity is untouched ──────────────────
cockpit_state.reset_run_state()
_fresh_ledger()

usage.record("claude-sonnet-4-6", 100, 10, 0.0, "test")
chk(
    "no active run → default last_activity stays None",
    cockpit_state.get_state(None)["last_activity"] is None,
)

# ── 2. Active run → record() bumps last_activity ───────────────────
cockpit_state.reset_run_state()
_fresh_ledger()
cockpit_state.claim_run(None)
cockpit_state.get_state(None)["last_activity"] = None  # reset to None explicitly

before = time.time()
usage.record("claude-sonnet-4-6", 200, 20, 0.0, "builder")
la = cockpit_state.get_state(None)["last_activity"]
chk("record() bumps last_activity for the active run", la is not None and la >= before, str(la))
chk("bumped last_activity is within the last second", la is not None and (time.time() - la) < 1.0,
    str(la))

# ── 3. Soldier-build scenario: last_activity was frozen 15 min ago ─
# Simulates: parent delegated at -900s; autopilot loop hasn't re-cycled yet;
# soldier is calling the model every few seconds.
cockpit_state.reset_run_state()
_fresh_ledger()
cockpit_state.claim_run(None)
FIFTEEN_MIN_AGO = time.time() - 900          # well past the 360s "stuck" threshold
cockpit_state.get_state(None)["last_activity"] = FIFTEEN_MIN_AGO

# Model call arrives (soldier is still working) — must reset the clock
usage.record("claude-opus-4-8", 500, 50, 0.0, "soldier·ordnance-be")
la2 = cockpit_state.get_state(None)["last_activity"]
chk(
    "model call resets last_activity past the 15-min-ago frozen value",
    la2 is not None and la2 > FIFTEEN_MIN_AGO,
    str(la2),
)
idle_after_call = time.time() - la2
chk(
    "idle after model call is < 5 s (well under 360 s stuck threshold)",
    idle_after_call < 5.0,
    f"{idle_after_call:.2f}s",
)

# ── 4. Per-app active run also gets the heartbeat ──────────────────
cockpit_state.reset_run_state()
_fresh_ledger()
cockpit_state.claim_run("automatixy")
cockpit_state.get_state("automatixy")["last_activity"] = None

usage.record("claude-sonnet-4-6", 100, 10, 0.0, "builder")
la3 = cockpit_state.get_state("automatixy")["last_activity"]
chk("per-app active run has last_activity bumped by model call", la3 is not None, str(la3))
chk("default (None) key untouched when only named app is active",
    cockpit_state.get_state(None)["last_activity"] is None)

# ── 5. record() is still a safe no-op when unconfigured ─────────────
cockpit_state.reset_run_state()
cockpit_state.claim_run(None)
old_path = usage._PATH
usage._PATH = None
try:
    usage.record("m", 1, 1, 0.0, "x")
    chk("record() is a safe no-op when unconfigured", True)
except Exception as exc:  # noqa: BLE001
    chk("record() is a safe no-op when unconfigured", False, str(exc))
finally:
    usage._PATH = old_path

# ── 6. _liveness chip: frozen last_activity → "stuck"; bumped → "working" ──
# This is the end-to-end regression guard required by the acceptance criterion:
# "a run in a long soldier_build with recent model calls renders 'working', never 'stuck'".
frozen_state = {"last_activity": time.time() - 900}   # 15 min ago — above 360s threshold
chip_stuck = _liveness(frozen_state, active=True)
chk(
    "_liveness shows 'stuck' when last_activity frozen 15 min ago (baseline)",
    "stuck" in chip_stuck,
    chip_stuck,
)

fresh_state = {"last_activity": time.time()}           # just bumped by usage.record()
chip_working = _liveness(fresh_state, active=True)
chk(
    "_liveness shows 'working' when last_activity is fresh (post-model-call)",
    "work" in chip_working and "stuck" not in chip_working,
    chip_working,
)

# ── 7. End-to-end: simulate the exact EU-84 scenario through the chip ──
# Parent delegated 15 min ago; soldier calls model; chip must flip from stuck → working.
cockpit_state.reset_run_state()
_fresh_ledger()
cockpit_state.claim_run(None)
cockpit_state.get_state(None)["last_activity"] = time.time() - 900  # frozen at delegation

# Confirm the chip is "stuck" before the model call
state_before = cockpit_state.get_state(None)
chk(
    "e2e: chip reads 'stuck' before soldier model call",
    "stuck" in _liveness(state_before, active=True),
)

# Soldier calls the model — usage.record() should bump last_activity
usage.record("claude-opus-4-8", 1000, 100, 0.0, "soldier·ordnance-be")

# Now the chip must read "working"
state_after = cockpit_state.get_state(None)
chip_after = _liveness(state_after, active=True)
chk(
    "e2e: chip reads 'working' immediately after soldier model call (EU-84 fix)",
    "work" in chip_after and "stuck" not in chip_after,
    chip_after,
)

# ── 8. Two simultaneous active runs → KNOWN cross-run contamination ──
# EU-84 review (iter 2): the ledger choke-point carries no per-run app attribution, so a single
# model call bumps last_activity for EVERY active run, not just the one that made the call. This
# test PINS that current behaviour so the limitation is documented and any future change to it
# (e.g. threading app_key from run_agent) is a deliberate, reviewed one. See the KNOWN LIMITATION
# note in usage.record().
cockpit_state.reset_run_state()
_fresh_ledger()
cockpit_state.claim_run(None)          # run A (default key)
cockpit_state.claim_run("automatixy")  # run B (named app), genuinely idle
FROZEN_B = time.time() - 900           # B has been idle 15 min (would read "stuck")
cockpit_state.get_state(None)["last_activity"] = None
cockpit_state.get_state("automatixy")["last_activity"] = FROZEN_B

# A single model call (attributed to no specific run) fires while A works.
usage.record("claude-opus-4-8", 300, 30, 0.0, "builder")

la_a = cockpit_state.get_state(None)["last_activity"]
la_b = cockpit_state.get_state("automatixy")["last_activity"]
chk("two active runs: the call bumps run A (default key)", la_a is not None, str(la_a))
chk(
    "two active runs: same call ALSO bumps run B (documented cross-run contamination)",
    la_b is not None and la_b > FROZEN_B,
    str(la_b),
)

# ── report ──────────────────────────────────────────────────────────
print("\n=============== LIVENESS HEARTBEAT EU-84 ===============")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
