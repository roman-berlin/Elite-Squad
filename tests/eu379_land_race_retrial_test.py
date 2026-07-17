"""EU-379 — a lost land race re-trials IN-PROCESS (re-merge + re-gate), not via a full rebuild.

Before: LandRaceError → REQUEUED → the next drain re-runs planner+builder from scratch (~6M tokens
mean) even though the feature branch is intact — land_trial only deletes the throwaway temp. The
requeue path has never fired in production (land_race_requeue: 0 events all-time at N=1), but a
concurrent drain (EU-380) makes races ~14-27% of lands; at rebuild prices that would ADD ~36%
tokens at N=3. The re-trial makes a lost race cost one re-merge + one gate run instead.

Correctness invariant preserved: every re-trial re-merges onto the NEW tip and re-runs the full
gate before pushing — skipping the gate would land an ungated combined tree, the exact thing
EU-259 rejected rebase-and-push for.

Pins (driving the real loop._land with the land_race_requeue_test harness pattern):
  (1) race once → in-process re-trial: re-merge + re-gate + land; outcome MERGED, no requeue;
  (2) the re-gate is REAL: a red re-gate on the new tip falls back to REQUEUED (never lands red);
  (3) a re-merge that no longer applies cleanly falls back to REQUEUED;
  (4) attempts are bounded: a race that never resolves → REQUEUED after 3 land attempts (the
      pre-EU-379 contract, still pinned by land_race_requeue_test);
  (5) the published base-green (EU-376) uses the FINAL merge sha — the one that actually landed,
      not the pre-race one;
  (6) audit: each re-trial records land_race_retrial with the attempt number.
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

sdk = types.ModuleType("claude_agent_sdk")
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", sdk)

sys.path.insert(0, ".")

from orchestrator import loop  # noqa: E402
from orchestrator.git_ops import LandRaceError  # noqa: E402
from orchestrator.config import Config, AppConfig  # noqa: E402
from orchestrator.contracts import Ticket, Outcome  # noqa: E402

checks = 0


def chk(n, c, d=""):
    global checks
    checks += 1
    if not c:
        print(f"  ✗ {n}  {d}")
        sys.exit(1)
    print(f"  ✓ {n}")


_tmp = Path(tempfile.mkdtemp())


def cfg() -> Config:
    c = Config(apps=[], audit_path=str(_tmp / "audit.jsonl"))
    c.merge_to_dev = True
    c.dry_run = False
    c.mark_done_on_merge = False
    return c


app = AppConfig(name="EU", repo_path=str(_tmp), base_branch="dev",
                protected_branch="main", backlog_backend="none")
BRANCH = "autodev/EU-379"


class _RaceOnceGit:
    """Loses the push race exactly once, then wins — the N=2 concurrent-drain common case."""
    def __init__(self):
        self.calls = []
        self.sha_seq = iter(["merge_sha_1", "merge_sha_2"])
        self._raced = False

    def commit_all(self, *a, **k):
        return "feat_sha"

    def trial_merge(self, *a, **k):
        self.calls.append("trial_merge")
        return True

    def changed_paths(self, *a, **k):
        return []

    def current_sha(self, *a, **k):
        return next(self.sha_seq, "merge_sha_2")

    def land_trial(self, temp):
        self.calls.append("land_trial")
        if not self._raced:
            self._raced = True
            raise LandRaceError("push to origin/dev rejected — the base advanced under us.")

    def abandon_trial(self, *a, **k): self.calls.append("abandon_trial")
    def delete_local_branch(self, *a, **k): pass
    def delete_remote_branch(self, *a, **k): pass
    def sync_main_base(self, *a, **k): return ""


class _Backlog:
    def set_status(self, *a, **k): pass
    def add_comment(self, *a, **k): pass


class _Audit:
    def __init__(self): self.events = []
    def record(self, event, **k): self.events.append((event, k))


tkt = Ticket(id="EU-379", key="EU-379", summary="race retrial", description="")
bld = SimpleNamespace(summary="built")
rev = SimpleNamespace(summary="reviewed")

gates_run = []
published = []
_orig = (loop.run_gate, loop._notify, loop._record_changelog, loop.publish_base_green)
loop.run_gate = lambda *a, **k: (gates_run.append(1) or SimpleNamespace(passed=True, report="ok"))
loop._notify = lambda *a, **k: None
loop._record_changelog = lambda *a, **k: None
loop.publish_base_green = lambda a, c, sha: published.append(sha)


def land(git, audit):
    return loop._land(tkt, app, cfg(), git, _Backlog(), audit,
                      branch=BRANCH, iteration=1, cost=0.0, build=bld, review=rev)


try:
    # (1) race once → re-trial in-process and MERGE
    g, au = _RaceOnceGit(), _Audit()
    gates_run.clear(); published.clear()
    rep = land(g, au)
    chk("(1) a single race re-trials in-process and MERGES", rep.outcome == Outcome.MERGED,
        str(rep.outcome))
    chk("(1b) the re-trial re-merged onto the new tip",
        g.calls.count("trial_merge") == 2, str(g.calls))
    chk("(1c) the gate ran AGAIN for the re-trial (never land ungated)",
        len(gates_run) == 2, f"gates={len(gates_run)}")
    chk("(1d) no land_race_requeue was recorded (it landed)",
        not any(e == "land_race_requeue" for e, _ in au.events), str([e for e, _ in au.events]))

    # (6) the re-trial is auditable
    retrials = [k for e, k in au.events if e == "land_race_retrial"]
    chk("(6) land_race_retrial recorded with the attempt number",
        len(retrials) == 1 and retrials[0].get("attempt") == 1, str(retrials))

    # (5) EU-376 publishes the FINAL sha — the commit that actually landed
    chk("(5) base-green published for the post-re-trial sha, not the pre-race one",
        published == ["merge_sha_2"], str(published))

    # (2) a red re-gate on the new tip must NOT land — falls back to REQUEUED
    g2, au2 = _RaceOnceGit(), _Audit()
    gates_run.clear()
    _seq = iter([SimpleNamespace(passed=True, report="ok"),      # the original dev_gate
                 SimpleNamespace(passed=False, report="FAILED: x_test.py")])  # the re-gate
    loop.run_gate = lambda *a, **k: next(_seq)
    rep2 = land(g2, au2)
    chk("(2) a red re-gate falls back to REQUEUED (never lands red)",
        rep2.outcome == Outcome.REQUEUED, str(rep2.outcome))
    chk("(2b) nothing was pushed after the red re-gate",
        g2.calls.count("land_trial") == 1, str(g2.calls))
    loop.run_gate = lambda *a, **k: (gates_run.append(1) or SimpleNamespace(passed=True, report="ok"))

    # (3) a dirty re-merge falls back to REQUEUED
    g3, au3 = _RaceOnceGit(), _Audit()
    _tm = g3.trial_merge
    g3.trial_merge = lambda *a, **k: (g3.calls.append("trial_merge") or False) \
        if g3.calls.count("land_trial") else _tm(*a, **k)
    rep3 = land(g3, au3)
    chk("(3) a re-merge that no longer applies cleanly → REQUEUED",
        rep3.outcome == Outcome.REQUEUED, str(rep3.outcome))

    # (4) bounded: the permanent-race case still exhausts to REQUEUED (old contract)
    class _AlwaysRace(_RaceOnceGit):
        def land_trial(self, temp):
            self.calls.append("land_trial")
            raise LandRaceError("still racing")
    g4, au4 = _AlwaysRace(), _Audit()
    rep4 = land(g4, au4)
    chk("(4) a never-resolving race exhausts 3 attempts → REQUEUED",
        rep4.outcome == Outcome.REQUEUED and g4.calls.count("land_trial") == 3,
        f"{rep4.outcome} land_trials={g4.calls.count('land_trial')}")
finally:
    loop.run_gate, loop._notify, loop._record_changelog, loop.publish_base_green = _orig

print(f"\n{checks}/{checks} passed")
