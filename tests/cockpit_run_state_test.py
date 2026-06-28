"""EU-64: per-project run state retires the single global run-lock.

Proves distinct projects claim runs INDEPENDENTLY (true parallel), that two near-simultaneous
claims for the SAME project can't both win (the per-app TOCTOU guard), that the optional
max-parallel-runs cap is honoured across projects, and that the legacy single-context names
(``_state`` / ``_run_lock``) stay valid as the default-key state/lock."""
import sys
import threading

sys.path.insert(0, ".")

import orchestrator.cockpit_state as cs


def setup_function(_fn):
    cs.set_max_parallel_runs(0)
    cs.reset_run_state()


def test_distinct_projects_run_in_parallel():
    assert cs.claim_run("alpha") is True
    assert cs.claim_run("beta") is True            # NOT blocked by alpha's run
    assert cs.is_active("alpha") and cs.is_active("beta")
    assert cs.active_run_count() == 2
    cs.release_run("alpha")
    assert not cs.is_active("alpha") and cs.is_active("beta")


def test_same_project_cannot_double_start():
    assert cs.claim_run("alpha") is True
    assert cs.claim_run("alpha") is False          # already in flight
    cs.release_run("alpha")
    assert cs.claim_run("alpha") is True            # claimable again after release


def test_concurrent_claims_one_winner():
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(cs.claim_run("alpha"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1                 # exactly one claim wins the race


def test_max_parallel_cap():
    cs.set_max_parallel_runs(2)
    assert cs.claim_run("alpha") is True
    assert cs.claim_run("beta") is True
    assert cs.claim_run("gamma") is False           # cap reached
    cs.release_run("alpha")
    assert cs.claim_run("gamma") is True            # slot freed


def test_env_overrides_cap(monkeypatch=None):
    import os
    os.environ["EU_MAX_PARALLEL_RUNS"] = "1"
    try:
        assert cs.max_parallel_runs() == 1
        assert cs.claim_run("alpha") is True
        assert cs.claim_run("beta") is False
    finally:
        del os.environ["EU_MAX_PARALLEL_RUNS"]


def test_log_seq_bumps_shared_and_per_app():
    before_shared = cs.shared_log_seq()
    before_app = cs.get_state("alpha")["log_seq"]
    cs.bump_log_seq("alpha")
    assert cs.shared_log_seq() == before_shared + 1
    assert cs.get_state("alpha")["log_seq"] == before_app + 1


def test_legacy_names_alias_default_key():
    # _state IS the default-key (None) state; _run_lock IS its lock.
    assert cs.get_state(None) is cs._state
    assert cs.run_lock_for(None) is cs._run_lock
    assert cs.claim_run(None) is True
    assert cs._state["active"] is True
    cs.release_run(None)
    assert cs._state["active"] is False


def test_release_run_clears_liveness_but_preserves_error():
    """EU-104 (iter-3): release_run clears the liveness fields but PRESERVES last_msg.

    A finished run must never ghost as 'Working' on the cockpit tab, so the terminal release
    zeroes the run-slot + liveness fields (active, autopilot_on, last_activity).  But it must NOT
    touch ``last_msg``: the run's _bg writes the failure reason there and release_run runs in the
    SAME ``finally`` immediately after — clearing it would silently swallow every run error before
    the operator could read why the run failed.  (Clearing the transient 'stopping…' note on a
    CLEAN outcome is the _bg's job — see tests/eu104_run_error_survives_test.py.)
    """
    cs.claim_run("alpha")
    st = cs.get_state("alpha")
    # Simulate in-flight display fields + a run error the operator must still be able to read.
    st["autopilot_on"] = True
    st["last_msg"] = "build failed: boom at the gate"
    import time as _time
    st["last_activity"] = _time.time()

    cs.release_run("alpha")

    assert not cs.is_active("alpha"), "active must be False after release_run"
    assert st["autopilot_on"] is False, (
        "autopilot_on must be zeroed by release_run (EU-104 — ghost autopilot badge)"
    )
    assert st["last_activity"] is None, (
        "last_activity must be cleared by release_run (EU-104 — stale heartbeat timestamp)"
    )
    assert st["last_msg"] == "build failed: boom at the gate", (
        "release_run must NOT clear last_msg — the operator must still see why the run failed "
        "(EU-104 iter-3: release_run clobbering run errors was the review rejection)"
    )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            setup_function(fn)
            fn()
            print(f"ok  {name}")
    print("all cockpit_run_state tests passed")
