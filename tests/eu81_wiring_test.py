"""EU-81 loop wiring QA — pins the git-lifecycle contract `loop._land` must honour after a land.

`git_lifecycle_test.py` proves the REAL Git class fast-forwards the Mac checkout and retires the
feature branch (merge→push→ff-sync with bare repos). This harness pins the OTHER half: that
`_land` actually *invokes* that lifecycle in the right order and respects its knobs — the exact
wiring whose regression (an unconditional `sync_main_base` / `delete_remote_branch` that ignored
`cfg.sync_base_after_merge`) broke three sibling harnesses.

It drives the real `_land` over a SPY git that records every lifecycle call and asserts:
  • a live land retires the branch (local + remote autodev/* ref) AND syncs the Mac base, in order;
  • `cfg.sync_base_after_merge=False` skips the Mac sync but STILL retires the branch;
  • post-land housekeeping is best-effort — a raising `delete_remote_branch` or `sync_main_base`
    NEVER unwinds an already-successful land (definition of done = commit on remote <base>).
"""
import sys, types, tempfile
from pathlib import Path
from types import SimpleNamespace

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import loop
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, Outcome

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
app = AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev",
                protected_branch="main", backlog_backend="none")  # no postmerge/smoke -> SRE+smoke skipped
BRANCH = "autodev/EU-81"


def cfg(sync: bool) -> Config:
    return Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), dry_run=False,
                  sync_base_after_merge=sync)


class _SpyGit:
    """Records every lifecycle call _land makes on the live-merge path. Optionally raises from a
    named post-land step to exercise the best-effort guard."""
    def __init__(self, raise_on=None):
        self.calls = []
        self.raise_on = raise_on
    def commit_all(self, *a, **k): return "feat_sha"
    def trial_merge(self, *a, **k): self.calls.append("trial_merge"); return True
    def changed_paths(self, *a, **k): return []
    def current_sha(self, *a, **k): return "merge_sha"
    def land_trial(self, temp): self.calls.append("land_trial")
    def delete_local_branch(self, branch): self.calls.append(("delete_local_branch", branch))
    def delete_remote_branch(self, branch):
        self.calls.append(("delete_remote_branch", branch))
        if self.raise_on == "delete_remote_branch":
            raise RuntimeError("remote rejected the delete")
    def sync_main_base(self):
        self.calls.append("sync_main_base")
        if self.raise_on == "sync_main_base":
            raise RuntimeError("ff-only refused")
        return "dev fast-forwarded in your checkout — ready to QA"
    def abandon_trial(self, *a, **k): pass


class _Backlog:
    def set_status(self, *a, **k): pass
    def add_comment(self, *a, **k): pass

class _Audit:
    def record(self, *a, **k): pass

tkt = Ticket(id="EU-81", key="EU-81", summary="git lifecycle", description="")
bld = SimpleNamespace(summary="built it")
rev = SimpleNamespace(summary="reviewed it")

orig_gate, orig_notify, orig_changelog = loop.run_gate, loop._notify, loop._record_changelog
loop.run_gate = lambda *a, **k: SimpleNamespace(passed=True, report="ok")
loop._notify = lambda *a, **k: None
loop._record_changelog = lambda *a, **k: None


def land(git, sync):
    return loop._land(tkt, app, cfg(sync), git, _Backlog(), _Audit(),
                      branch=BRANCH, iteration=1, cost=0.0, build=bld, review=rev)


try:
    # 1) Live land, sync ON: retires branch (local+remote) AND syncs the Mac base, in order.
    g1 = _SpyGit()
    rep1 = land(g1, sync=True)
    chk("live land reports MERGED", rep1.outcome == Outcome.MERGED, str(rep1.outcome))
    chk("retires the LOCAL feature branch by name", ("delete_local_branch", BRANCH) in g1.calls, str(g1.calls))
    chk("retires the REMOTE autodev/* ref by name", ("delete_remote_branch", BRANCH) in g1.calls, str(g1.calls))
    chk("fast-forwards the Mac base (sync_main_base called)", "sync_main_base" in g1.calls, str(g1.calls))
    order = [c if isinstance(c, str) else c[0] for c in g1.calls]
    chk("order: land_trial -> delete_local -> delete_remote -> sync_main_base",
        order.index("land_trial") < order.index("delete_local_branch")
        < order.index("delete_remote_branch") < order.index("sync_main_base"), str(order))

    # 2) Live land, sync OFF: branch still retired, but the Mac sync is SKIPPED (honours the knob).
    g2 = _SpyGit()
    rep2 = land(g2, sync=False)
    chk("sync OFF still reports MERGED", rep2.outcome == Outcome.MERGED, str(rep2.outcome))
    chk("sync OFF still retires the remote branch", ("delete_remote_branch", BRANCH) in g2.calls, str(g2.calls))
    chk("sync OFF SKIPS sync_main_base (honours cfg.sync_base_after_merge)",
        "sync_main_base" not in g2.calls, str(g2.calls))

    # 3) Best-effort: a raising delete_remote_branch must NOT unwind the land — still MERGED, and
    #    the Mac sync still runs (cleanup failure is isolated from the sync step).
    g3 = _SpyGit(raise_on="delete_remote_branch")
    rep3 = land(g3, sync=True)
    chk("delete_remote_branch raising -> land still MERGED (cleanup never fails a landed ticket)",
        rep3.outcome == Outcome.MERGED, str(rep3.outcome))
    chk("delete_remote_branch raising -> Mac sync still happens", "sync_main_base" in g3.calls, str(g3.calls))

    # 4) Best-effort: a raising sync_main_base (sync ON) must NOT unwind the land either.
    g4 = _SpyGit(raise_on="sync_main_base")
    rep4 = land(g4, sync=True)
    chk("sync_main_base raising -> land still MERGED (sync is convenience, not custody)",
        rep4.outcome == Outcome.MERGED, str(rep4.outcome))
finally:
    loop.run_gate, loop._notify, loop._record_changelog = orig_gate, orig_notify, orig_changelog

print("\n================= EU-81 LAND WIRING QA =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
