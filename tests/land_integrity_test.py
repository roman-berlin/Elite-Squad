"""EU-367: land-path integrity — post-git tracker writes are guarded, worktree isolation is not
silently downgraded for the unit's own repo.

Two seams pinned here (the third, the promote stale-dev guard, is promote_stale_dev_test.py):

EU-367.1 — a tracker outage AFTER an irreversible git effect must not raise out of _land:
  · SRE-revert path: sentinel already reverted DEV; if set_status/add_comment then fail, _land must
    still return ESCALATED (not a ticket_exception) and record `tracker_reconcile_needed` so the
    operator is told to fix the status by hand;
  · PR-opened path: the PR is already created; a failing comment/attach must not raise — the PR url
    is preserved in a `tracker_reconcile_needed` event and the ticket still reports PR_OPENED.

EU-367.2 — _make_git must NOT silently build in the main checkout when worktree isolation fails:
  · the orchestrator's OWN repo -> REFUSE (raise) rather than edit the running unit in-tree;
  · a product repo -> in-tree fallback still allowed, but LOUD (a Telegram notify fires).
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
from orchestrator import sentinel as sentinel_mod
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, Outcome
from orchestrator.git_ops import GitError

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


tmp = Path(tempfile.mkdtemp())
app = AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev",
                protected_branch="main", backlog_backend="none")
BRANCH = "autodev/EU-367"


def cfg(**kw) -> Config:
    base = dict(apps=[app], audit_path=str(tmp / "audit.jsonl"), dry_run=False,
                sync_base_after_merge=False, open_pr_on_block=True)
    base.update(kw)
    return Config(**base)


class _LandGit:
    """A Git that lands cleanly; the SRE/PR behaviour is driven by monkeypatching sentinel + the gate."""
    def commit_all(self, *a, **k): return "feat_sha"
    def trial_merge(self, *a, **k): return True
    def changed_paths(self, *a, **k): return []
    def current_sha(self, *a, **k): return "merge_sha"
    def land_trial(self, temp): pass
    def delete_local_branch(self, *a, **k): pass
    def delete_remote_branch(self, *a, **k): pass
    def sync_main_base(self, *a, **k): return ""
    def abandon_trial(self, *a, **k): pass
    def push(self, *a, **k): pass
    def open_pr(self, *a, **k): return "https://example/pr/1"
    def attach_pr(self, *a, **k): pass


class _Audit:
    def __init__(self): self.events = []
    def record(self, event, **k): self.events.append((event, k))


class _RaisingBacklog:
    """Every tracker write fails — simulates a Jira outage right after the git side-effect."""
    def set_status(self, *a, **k): raise RuntimeError("jira 503")
    def add_comment(self, *a, **k): raise RuntimeError("jira 503")
    def attach_pr(self, *a, **k): raise RuntimeError("jira 503")


tkt = Ticket(id="EU-367", key="EU-367", summary="land integrity", description="")
bld = SimpleNamespace(summary="built it")
rev = SimpleNamespace(summary="reviewed it", unverifiable_gaps=None)

orig = dict(gate=loop.run_gate, notify=loop._notify, changelog=loop._record_changelog,
            should_run=sentinel_mod.should_run, guard=sentinel_mod.guard,
            Git=loop.Git)
loop._notify = lambda *a, **k: None
loop._record_changelog = lambda *a, **k: None

try:
    # ---- EU-367.1a: SRE reverted, then the tracker write fails -> still ESCALATED, reconcilable ----
    loop.run_gate = lambda *a, **k: SimpleNamespace(passed=True, report="ok")
    sentinel_mod.should_run = lambda cfg, app: True
    sentinel_mod.guard = lambda *a, **k: (False, "post-merge suite failed on dev")
    au = _Audit()
    rep = loop._land(tkt, app, cfg(), _LandGit(), _RaisingBacklog(), au,
                     branch=BRANCH, iteration=1, cost=0.0, build=bld, review=rev)
    chk("SRE-revert + tracker outage still returns ESCALATED (not a crash/ticket_exception)",
        rep.outcome == Outcome.ESCALATED, str(rep.outcome))
    chk("SRE-revert tracker failure records tracker_reconcile_needed",
        any(e == "tracker_reconcile_needed" for e, _ in au.events), str([e for e, _ in au.events]))

    # ---- EU-367.1b: gate RED -> PR opened, then the tracker write fails -> still PR_OPENED ----
    loop.run_gate = lambda *a, **k: SimpleNamespace(passed=False, report="1 failing test")
    sentinel_mod.should_run = lambda cfg, app: False
    au2 = _Audit()
    rep2 = loop._land(tkt, app, cfg(), _LandGit(), _RaisingBacklog(), au2,
                      branch=BRANCH, iteration=1, cost=0.0, build=bld, review=rev)
    chk("PR-opened + tracker outage still returns PR_OPENED (PR not lost)",
        rep2.outcome == Outcome.PR_OPENED, str(rep2.outcome))
    chk("PR-opened tracker failure records tracker_reconcile_needed with the PR url",
        any(e == "tracker_reconcile_needed" and k.get("pr_url") for e, k in au2.events),
        str([(e, k.get("pr_url")) for e, k in au2.events]))
    chk("PR-opened still records the PR_OPENED audit event",
        any(e == Outcome.PR_OPENED.audit_event for e, _ in au2.events))
finally:
    loop.run_gate, loop._notify, loop._record_changelog = orig["gate"], orig["notify"], orig["changelog"]
    sentinel_mod.should_run, sentinel_mod.guard = orig["should_run"], orig["guard"]

# ---- EU-367.2: worktree isolation failure — refuse for self-repo, loud fallback for product ----
SELF_ROOT = Path(loop.__file__).resolve().parent.parent


class _IsoFailGit:
    """setup() fails only in the isolated (worktree) attempt — the in-tree fallback constructs
    without a worktree_path and never calls setup()."""
    def __init__(self, repo_path, base, protected, worktree_path=None):
        self.repo_path, self.worktree_path = repo_path, worktree_path
        self.workdir = repo_path
        self.isolated = worktree_path is not None
    def setup(self):
        if self.worktree_path is not None:
            raise GitError("origin/dev does not resolve (no origin remote)")
        return False


notify_calls = []
try:
    loop.Git = _IsoFailGit
    loop._notify = lambda cfg, text: notify_calls.append(text)

    # self-repo: isolation failure must REFUSE (raise), never build the unit in-tree
    self_app = AppConfig(name="Elite-Unit", repo_path=str(SELF_ROOT), base_branch="dev",
                         protected_branch="main", backlog_backend="none")
    self_cfg = Config(apps=[self_app], audit_path=str(tmp / "a.jsonl"), use_worktree=True)
    raised = False
    try:
        loop._make_git(self_cfg, self_app)
    except GitError as e:
        raised = "self-edit guard" in str(e) or "own repo" in str(e)
    chk("self-repo: worktree failure REFUSES (raises), never downgrades to in-tree", raised)

    # product repo: isolation failure still falls back to in-tree, but LOUDLY (a notify fires)
    notify_calls.clear()
    prod_app = AppConfig(name="automatixy", repo_path=str(tmp / "prod"), base_branch="dev",
                         protected_branch="main", backlog_backend="none")
    prod_cfg = Config(apps=[prod_app], audit_path=str(tmp / "a.jsonl"), use_worktree=True)
    g = loop._make_git(prod_cfg, prod_app)
    chk("product repo: worktree failure falls back to in-tree (returns a Git)", g is not None)
    chk("product repo: the in-tree downgrade is LOUD (a Telegram notify fired)",
        any("worktree isolation unavailable" in t for t in notify_calls), str(notify_calls))
finally:
    loop.Git = orig["Git"]
    loop._notify = orig["notify"]

print("\n================= EU-367 LAND-INTEGRITY QA =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
