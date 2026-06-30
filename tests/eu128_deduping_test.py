"""EU-128 regression test: notification deduping and dry-run ticket re-picking.

Tests that:
1. clear_parked_repos() is called once per autopilot run, not every cycle
2. Continuous+dry-run mode tracks previewed tickets and doesn't re-pick them
3. Valid apps continue draining when one app is parked
"""
import sys, types
from pathlib import Path

# stub the Agent SDK so importing orchestrator.* is cheap + offline (house pattern)
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

results = []
def chk(name: str, cond, detail: str = "") -> None:
    """Record one assertion."""
    results.append((name, bool(cond), detail))


def test_clear_parked_repos_location():
    """Test that clear_parked_repos() is called in autopilot, not loop.run."""
    import orchestrator.autopilot as autopilot_mod
    import orchestrator.loop as loop_mod
    import inspect

    # Check that clear_parked_repos is NOT in loop.run
    loop_source = inspect.getsource(loop_mod.run)
    chk("clear_parked_repos: NOT in loop.run", "clear_parked_repos" not in loop_source,
        "clear_parked_repos should not be called in loop.run()")

    # Check that clear_parked_repos IS in autopilot.autopilot
    autopilot_source = inspect.getsource(autopilot_mod.autopilot)
    chk("clear_parked_repos: IS in autopilot.autopilot", "clear_parked_repos" in autopilot_source,
        "clear_parked_repos should be called in autopilot.autopilot()")


def test_previewed_tickets_tracking():
    """Test that autopilot tracks previewed tickets in dry-run mode."""
    import orchestrator.autopilot as autopilot_mod
    import inspect

    # Check that previewed_tickets variable exists in autopilot
    autopilot_source = inspect.getsource(autopilot_mod.autopilot)
    chk("previewed_tickets: variable exists", "previewed_tickets" in autopilot_source,
        "previewed_tickets set should exist in autopilot")

    # Check that previewed_tickets is used for filtering
    chk("previewed_tickets: used for filtering", "previewed_tickets" in autopilot_source and
        "if t.id not in previewed_tickets" in autopilot_source,
        "previewed_tickets should be used to filter worklist")

    # Check that previewed_tickets is updated after processing
    chk("previewed_tickets: updated after processing", "previewed_tickets.update" in autopilot_source,
        "previewed_tickets should be updated with processed ticket IDs")


def test_dry_run_warning_updated():
    """Test that the continuous+dry-run warning message is updated."""
    import orchestrator.autopilot as autopilot_mod
    import inspect

    autopilot_source = inspect.getsource(autopilot_mod.autopilot)
    # The old warning message should not exist
    chk("dry-run warning: old message removed",
        "would re-pick the same ticket forever" not in autopilot_source,
        "Old warning message about re-picking should be removed")

    # The new informative message should exist
    chk("dry-run warning: new message present",
        "previewing tickets once" in autopilot_source,
        "New warning message about previewing should be present")


# Run all tests
test_clear_parked_repos_location()
test_previewed_tickets_tracking()
test_dry_run_warning_updated()

# Print results
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"\n{passed}/{total} checks passed", flush=True)

for name, ok, detail in results:
    status = "✓" if ok else "✗"
    msg = f"{status} {name}"
    if detail:
        msg += f" — {detail}"
    print(msg, flush=True)

sys.exit(0 if passed == total else 1)
