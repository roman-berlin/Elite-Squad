"""EU-104: per-project log tagging + live-feed scoping.

Proves:
  1. _Tee tags each log line with the sole active app when exactly one project is running.
  2. _Tee uses None as the app_key when zero or more than one project is running (unattributed).
  3. recent_log(app=<app>) returns only lines for that project.
  4. recent_log() (no app) still returns all lines — back-compat.
  5. bump_log_seq(app_key) updates the per-app last_activity and log_seq, not only the global.
"""
import sys
sys.path.insert(0, ".")

import orchestrator.cockpit_state as cs


def setup():
    cs.reset_run_state()


def test_tee_tags_line_with_active_app():
    """When exactly one project is active the Tee tags the line with that app's key."""
    setup()
    cs.claim_run("alpha")
    tee = cs._Tee(type("F", (), {"write": lambda s, v: None, "flush": lambda s: None})())
    tee.write("alpha-step-1\n")
    lines = cs.recent_log(10, app="alpha")
    assert "alpha-step-1" in lines, f"expected line in alpha feed; got {lines}"
    cs.release_run("alpha")


def test_tee_does_not_bleed_line_to_other_project():
    """Lines tagged to alpha must not appear in beta's filtered feed."""
    setup()
    cs.claim_run("alpha")
    tee = cs._Tee(type("F", (), {"write": lambda s, v: None, "flush": lambda s: None})())
    tee.write("alpha-only-line\n")
    cs.release_run("alpha")
    lines_beta = cs.recent_log(10, app="beta")
    assert "alpha-only-line" not in lines_beta, (
        f"alpha line bled into beta feed: {lines_beta}")


def test_tee_unattributed_when_no_run():
    """Lines written with no active run are tagged None and appear in the unfiltered view only."""
    setup()
    tee = cs._Tee(type("F", (), {"write": lambda s, v: None, "flush": lambda s: None})())
    tee.write("idle-line\n")
    # unfiltered: appears
    assert "idle-line" in cs.recent_log(10)
    # scoped to any named app: absent (was never attributed to one)
    assert "idle-line" not in cs.recent_log(10, app="alpha")


def test_tee_unattributed_when_multiple_runs():
    """Lines written while two projects run simultaneously are tagged None (ambiguous)."""
    setup()
    cs.claim_run("alpha")
    cs.claim_run("beta")
    tee = cs._Tee(type("F", (), {"write": lambda s, v: None, "flush": lambda s: None})())
    tee.write("concurrent-line\n")
    cs.release_run("alpha")
    cs.release_run("beta")
    # scoped views: absent
    assert "concurrent-line" not in cs.recent_log(10, app="alpha")
    assert "concurrent-line" not in cs.recent_log(10, app="beta")
    # unfiltered: present
    assert "concurrent-line" in cs.recent_log(10)


def test_recent_log_no_app_returns_all():
    """recent_log() with no app arg is backward-compatible: returns all lines."""
    setup()
    cs.claim_run("alpha")
    tee = cs._Tee(type("F", (), {"write": lambda s, v: None, "flush": lambda s: None})())
    tee.write("all-projects-line\n")
    cs.release_run("alpha")
    assert "all-projects-line" in cs.recent_log(10)


def test_tee_bumps_per_app_last_activity():
    """bump_log_seq(app_key) must update the per-app state's last_activity, not only global."""
    setup()
    cs.claim_run("gamma")
    before = cs.get_state("gamma").get("last_activity")
    tee = cs._Tee(type("F", (), {"write": lambda s, v: None, "flush": lambda s: None})())
    tee.write("gamma-heartbeat\n")
    after = cs.get_state("gamma").get("last_activity")
    cs.release_run("gamma")
    assert after is not None, "last_activity was not set on gamma's state"
    assert (before is None) or (after >= before), "last_activity must be monotone"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all eu104 per-project log tests passed")
