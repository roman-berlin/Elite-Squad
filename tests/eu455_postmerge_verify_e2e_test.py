"""EU-455 — post-merge dev-HEAD re-verification END-TO-END QA (the verify-child that closes EU-447).

The three sibling pieces each landed and each tested IN ISOLATION, stubbing the others out:
  • EU-452 engine (``postmerge_verify.verify``) — its harness stubs ``gate.run_commands``.
  • EU-453 wiring (``_postmerge_verify_flag``, called from ``_land``) — its harness stubs
    ``postmerge_verify.verify``.
  • EU-454 cache eviction (``evict_base_green``) — tested standalone.
The ONE coverage none of them provides — exactly what this verify-child exists for — is the REAL
engine running through the REAL ``_land`` wiring against a real merged dev HEAD, including the real
``publish_base_green`` → ``evict_base_green`` sequence on red. So NOTHING on the target path is
stubbed here: ``loop._land`` is the live function, ``postmerge_verify.verify`` is the live engine,
and the gate suite is a REAL trivial subprocess that genuinely exits 0 (green) or non-zero (red).
Only the NON-target side-channels are stubbed (the pre-land ``run_gate``, ``_notify``,
``_record_changelog``, ``devstate``, and the sentinel/smoke/ci ``should_run`` gates) — exactly the
set eu453's ``_LandGit`` harness stubs, so the post-merge re-verification is the ONLY behaviour
under test.

This is the integration check the individual pieces don't each cover: a clean combine that breaks
dev's real state (a red merged tip) is caught end-to-end, flagged for revert/escalation (audit +
Telegram + Needs Human + a comment carrying the failure evidence), the merge STANDS (it never
auto-reverts — that's the Commander's call / the next pick's base gate), and the FALSE-GREEN
base-cache entry the land just published (``publish_base_green``) is EVICTED (``evict_base_green``)
so the next pick's ``base_gate_check`` MISSES and re-verifies against the real (red) dev instead of
silently trusting the stale green. The wrong-tree guard is exercised too: when dev's HEAD is not the
merged tip, the engine green-SKIPS without ever spawning the gate subprocess.
"""
import os
import shlex
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

# Stub the Agent SDK exactly like the sibling harnesses (no network, no real models).
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): pass

    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import loop                              # noqa: E402
from orchestrator import postmerge_verify as pmv          # noqa: E402
from orchestrator import gate, sentinel, smoke, ci_conclusion, devstate  # noqa: E402
from orchestrator.config import AppConfig, Config          # noqa: E402
from orchestrator.contracts import Outcome                 # noqa: E402

ns = SimpleNamespace
results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


MERGE = "abc123def4567890abcdef1234567890abcdef12"
WRONG = "deadbeefcafebabe0000000000000000deadbeef"   # a dev HEAD that is NOT the merged tip

# A REAL trivial gate suite: a genuine subprocess that prints a marker and exits 0 (green) or 1
# (red). The -c body is single-quoted inside a shell double-quoted arg, so it survives /bin/sh
# parsing untouched. The pinned venv interpreter (sys.executable) is shlex-quoted so a path with a
# space still parses — we never invent a bare ``python3`` that dies outside the venv.
_PY = shlex.quote(sys.executable)
_GREEN_BODY = "print('eu455_green_gate passed')"
_RED_BODY = ("print('FAILED eu455_red_gate - AssertionError: post-merge broke dev'); "
             "raise SystemExit(1)")
PASS_CMD = f'{_PY} -c "{_GREEN_BODY}"'
FAIL_CMD = f'{_PY} -c "{_RED_BODY}"'


# --- shared fakes (reused from eu453's harness shape) ------------------------ #
class Audit:
    def __init__(self):
        self.events = []

    def record(self, kind, **kw):
        self.events.append((kind, kw))

    def kinds(self):
        return [k for k, _ in self.events]


class FakeBacklog:
    def __init__(self):
        self.statuses = []    # list of (ticket, status)
        self.comments = []    # list of (ticket, body)

    def set_status(self, ticket, status):
        self.statuses.append((ticket, status))

    def add_comment(self, ticket, body):
        self.comments.append((ticket, body))


class LandGit:
    """Spy for the live-merge path ``_land`` drives (eu453's ``_LandGit`` shape), with a
    controllable ``current_sha()``. ``loop._land`` captures ``merge_sha = git.current_sha()`` once
    (loop.py:3026); the post-merge engine then calls ``git.current_sha()`` AGAIN inside
    ``postmerge_verify.verify``. So call #1 is the merge_sha capture and call #2 is the engine's
    wrong-tree check — when ``other`` is set, call #2 returns a DIFFERENT sha so the real wrong-tree
    guard engages (current_sha ≠ merge_sha -> green-skip, no suite run). Records every mutating
    method so a RED can prove the merge stood (land_trial happened, NO revert followed)."""

    def __init__(self, sha=MERGE, other=None):
        self._sha = sha
        self._other = other
        self._n = 0
        self.calls = []

    def commit_all(self, *a, **k):
        return "feat_sha"

    def trial_merge(self, *a, **k):
        self.calls.append("trial_merge")
        return True

    def changed_paths(self, *a, **k):
        return []

    def current_sha(self, *a, **k):
        self._n += 1
        if self._other is not None and self._n >= 2:
            return self._other          # the engine's check sees a dev HEAD ≠ merge_sha
        return self._sha                # call #1: the merge_sha _land captures

    def land_trial(self, temp):
        self.calls.append("land_trial")

    def delete_local_branch(self, b):
        self.calls.append("delete_local_branch")

    def delete_remote_branch(self, b):
        self.calls.append("delete_remote_branch")

    def sync_main_base(self):
        self.calls.append("sync_main_base")
        return "synced"

    def abandon_trial(self, *a, **k):
        pass


def _scenario(gate_cmds, sha=MERGE, other=None):
    """One fully-isolated REAL ``_land`` run: a fresh temp workdir + temp audit/cache paths, a real
    AppConfig armed for post-merge verify, a fresh spy git / backlog / audit, and the gate-subprocess
    counter zeroed. Returns everything the ACs assert over."""
    d = Path(tempfile.mkdtemp(prefix="eu455_run_"))
    app = AppConfig(name="Elite-Unit", repo_path=str(d), workdir=str(d),
                    base_branch="dev", protected_branch="main", branch_prefix="autodev",
                    backlog_backend="none", gate_commands=list(gate_cmds),
                    postmerge_verify=True, gate_env={}, gate_timeout_sec=60)
    cfg = Config(apps=[], audit_path=str(d / "audit.jsonl"), postmerge_verify=True,
                 dry_run=False, sync_base_after_merge=False, mark_done_on_merge=False)
    git = LandGit(sha=sha, other=other)
    backlog = FakeBacklog()
    audit = Audit()
    _rc_calls["n"] = 0
    ticket = ns(id="EU-455", key="EU-455", summary="e2e post-merge dev-HEAD verify",
                description="", ephemeral=False)
    report = loop._land(ticket, app, cfg, git, backlog, audit, branch="autodev/EU-455",
                        iteration=1, cost=0.0, build=ns(summary="built it"),
                        review=ns(summary="reviewed it", unverifiable_gaps=None))
    return report, audit, backlog, git, app, cfg, (d / "red_base_cache.json")


# ============================================================================ #
# AC#6 (first) — EU-355: redirect GENERAL_AUDIT_PATH / GENERAL_PID_FILE to temp so
# running this harness NEVER writes to the live state dir. (run_all.py already points the whole
# suite's child env here; this makes the harness correct standalone too.)
# ============================================================================ #
_state_tmp = Path(tempfile.mkdtemp(prefix="eu455_state_"))
_prev_audit = os.environ.get("GENERAL_AUDIT_PATH")
_prev_pid = os.environ.get("GENERAL_PID_FILE")
os.environ["GENERAL_AUDIT_PATH"] = str(_state_tmp / "audit.jsonl")
os.environ["GENERAL_PID_FILE"] = str(_state_tmp / "autopilot.pid")
chk("AC#6: GENERAL_AUDIT_PATH redirected to a temp path (not live state/)",
    "eu455_state_" in os.environ["GENERAL_AUDIT_PATH"], os.environ.get("GENERAL_AUDIT_PATH"))
chk("AC#6: GENERAL_PID_FILE redirected to a temp path (not live /tmp/general-autopilot.pid)",
    "eu455_state_" in os.environ["GENERAL_PID_FILE"], os.environ.get("GENERAL_PID_FILE"))


# ============================================================================ #
# Stub ONLY the non-target side-channels (exactly eu453's _LandGit harness set) so the REAL
# post-merge verify is the only behaviour under test. gate.run_commands is WRAPPED (not faked): the
# wrapper counts calls but delegates to the REAL subprocess runner, so the gate genuinely runs.
# ============================================================================ #
_rc_calls = {"n": 0}
_orig = {
    "run_gate": loop.run_gate,
    "_notify": loop._notify,
    "_record_changelog": loop._record_changelog,
    "devstate_refresh": devstate.refresh,
    "sentinel_should_run": sentinel.should_run,
    "smoke_should_run": smoke.should_run,
    "ci_should_run": ci_conclusion.should_run,
    "gate_run_commands": gate.run_commands,
}
_real_run_commands = gate.run_commands


def _counting_run_commands(app_arg, cmds, cwd=None):
    _rc_calls["n"] += 1
    return _real_run_commands(app_arg, cmds, cwd=cwd)


loop.run_gate = lambda *a, **k: ns(passed=True, report="ok")
loop._notify = lambda *a, **k: None
loop._record_changelog = lambda *a, **k: None
devstate.refresh = lambda *a, **k: None
sentinel.should_run = lambda *a, **k: False
smoke.should_run = lambda *a, **k: False
ci_conclusion.should_run = lambda *a, **k: False
gate.run_commands = _counting_run_commands

try:
    # ========================================================================== #
    # AC#1 — GREEN: real PASSING gate subprocess -> _land MERGED, with BOTH the
    #         engine event (postmerge_verify_green) AND the wiring event
    #         (postmerge_verify_pass); never Needs Human. publish_base_green also wrote
    #         merge_sha into the cache (the GREEN-stays fact that gives AC#4 its teeth).
    # ========================================================================== #
    rep, au, bl, g, app, cfg, cache = _scenario([PASS_CMD])
    chk("AC#1 GREEN: _land outcome is MERGED",
        rep.outcome == Outcome.MERGED, str(getattr(rep, "outcome", None)))
    chk("AC#1 GREEN: engine recorded postmerge_verify_green",
        "postmerge_verify_green" in au.kinds(), str(au.kinds()))
    chk("AC#1 GREEN: wiring recorded postmerge_verify_pass",
        "postmerge_verify_pass" in au.kinds(), str(au.kinds()))
    chk("AC#1 GREEN: NO 'Needs Human' status set",
        "Needs Human" not in [s for _, s in bl.statuses], str(bl.statuses))
    chk("AC#1 GREEN: the REAL gate subprocess ran exactly once",
        _rc_calls["n"] == 1, str(_rc_calls))
    _green_key = f"{app.repo_path}@{MERGE}"
    chk("AC#1 GREEN: publish_base_green wrote merge_sha into the cache (proves publish ran)",
        cache.exists() and _green_key in cache.read_text(encoding="utf-8", errors="replace"),
        f"{cache} exists={cache.exists()}")

    # ========================================================================== #
    # AC#2 — RED: real FAILING gate (subprocess exits non-zero) -> _land ESCALATED,
    #         engine postmerge_verify_red + wiring postmerge_verify_fail, Needs Human set,
    #         and a ticket comment whose body carries the failure evidence.
    # ========================================================================== #
    rep, au, bl, g, app, cfg, cache = _scenario([FAIL_CMD])
    chk("AC#2 RED: _land outcome is ESCALATED",
        rep.outcome == Outcome.ESCALATED, str(getattr(rep, "outcome", None)))
    chk("AC#2 RED: engine recorded postmerge_verify_red",
        "postmerge_verify_red" in au.kinds(), str(au.kinds()))
    chk("AC#2 RED: wiring recorded postmerge_verify_fail",
        "postmerge_verify_fail" in au.kinds(), str(au.kinds()))
    chk("AC#2 RED: backlog.set_status(ticket,'Needs Human')",
        "Needs Human" in [s for _, s in bl.statuses], str(bl.statuses))
    chk("AC#2 RED: a comment body carries the failure evidence (eu455_red_gate)",
        any("eu455_red_gate" in body for _, body in bl.comments), str(bl.comments))
    chk("AC#2 RED: the REAL gate subprocess ran exactly once",
        _rc_calls["n"] == 1, str(_rc_calls))

    # ========================================================================== #
    # AC#3 — on RED the merge STANDS: land_trial was invoked and NO git method whose name
    #         contains 'revert' was called (the verify flag never touches git's mutating methods).
    # ========================================================================== #
    chk("AC#3 RED: the merge already happened (land_trial invoked)",
        "land_trial" in g.calls, str(g.calls))
    _red_reverts = [c for c in g.calls if "revert" in str(c)]
    chk("AC#3 RED: NO revert method invoked (the merge stands)", not _red_reverts, str(g.calls))

    # ========================================================================== #
    # AC#4 — on RED the false-green base cache entry is EVICTED. publish_base_green (run earlier
    #         in _land) wrote merge_sha into the temp cache; the red verify's evict_base_green
    #         then removed it — so merge_sha is NOT in the cache after _land returns. (AC#1's
    #         GREEN-stays check above proves publish really writes, so this is non-vacuous.)
    # ========================================================================== #
    _red_key = f"{app.repo_path}@{MERGE}"
    _evicted = (not cache.exists()) or (_red_key not in cache.read_text(encoding="utf-8", errors="replace"))
    chk("AC#4 RED: the false-green cache entry for merge_sha was EVICTED", _evicted,
        f"{cache} exists={cache.exists()}")

    # ========================================================================== #
    # AC#5 — WRONG-TREE green-skip through the REAL engine: when the spy git's current_sha()
    #         differs from merge_sha, postmerge_verify.verify returns a green-skip WITHOUT ever
    #         spawning the gate subprocess (count 0), so _land proceeds to MERGED and no
    #         postmerge_verify_fail event is recorded.
    # ========================================================================== #
    rep, au, bl, g, app, cfg, cache = _scenario([PASS_CMD], sha=MERGE, other=WRONG)
    chk("AC#5 WRONG-TREE: _land outcome is MERGED (green-skip proceeds)",
        rep.outcome == Outcome.MERGED, str(getattr(rep, "outcome", None)))
    chk("AC#5 WRONG-TREE: the gate subprocess was NEVER invoked (count 0)",
        _rc_calls["n"] == 0, str(_rc_calls))
    chk("AC#5 WRONG-TREE: NO postmerge_verify_fail event (not a red)",
        "postmerge_verify_fail" not in au.kinds(), str(au.kinds()))
    chk("AC#5 WRONG-TREE: engine recorded postmerge_verify_skip (the wrong-tree skip)",
        "postmerge_verify_skip" in au.kinds(), str(au.kinds()))
    chk("AC#5 WRONG-TREE: wiring recorded postmerge_verify_pass (green-skip is quiet)",
        "postmerge_verify_pass" in au.kinds(), str(au.kinds()))
finally:
    # Restore every stub so this harness leaves the orchestrator modules pristine for the rest
    # of the suite (each harness runs in its own subprocess, but restoring is still honest).
    loop.run_gate = _orig["run_gate"]
    loop._notify = _orig["_notify"]
    loop._record_changelog = _orig["_record_changelog"]
    devstate.refresh = _orig["devstate_refresh"]
    sentinel.should_run = _orig["sentinel_should_run"]
    smoke.should_run = _orig["smoke_should_run"]
    ci_conclusion.should_run = _orig["ci_should_run"]
    gate.run_commands = _orig["gate_run_commands"]
    for _k, _v in (("GENERAL_AUDIT_PATH", _prev_audit), ("GENERAL_PID_FILE", _prev_pid)):
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v


print("\n=============== EU-455 POST-MERGE VERIFY END-TO-END QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
