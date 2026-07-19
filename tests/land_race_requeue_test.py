"""EU-259: a land-time non-ff race requeues cleanly — it never lands ungated, never error-strikes.

git_lifecycle_test.py pins the git-level contract (land_trial raises LandRaceError and lands
nothing on a base-advanced race). This harness pins the LOOP half: loop._land, on that
LandRaceError, must

  • return Outcome.REQUEUED (so the ticket is retried next drain) — NOT ERRORED (which ticks the
    EU-219 consecutive-error counter toward park) and NOT ESCALATED (which parks on the Commander);
  • leave the Jira ticket UNTOUCHED — no set_status, no add_comment — because nothing merged and the
    status change only happens after a successful land;
  • record a distinct `land_race_requeue` audit event so forensics can see the real reason;
  • carry notes that make the resume behaviour explicit (gate re-runs next drain).

Contrast with the happy path (land_trial succeeds) → MERGED + the Jira status moves to QA.
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
from orchestrator.git_ops import LandRaceError
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, Outcome

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


tmp = Path(tempfile.mkdtemp())
app = AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev",
                protected_branch="main", backlog_backend="none")
BRANCH = "autodev/EU-259"


def cfg() -> Config:
    return Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), dry_run=False,
                  sync_base_after_merge=True)


class _RaceGit:
    """A Git whose land_trial always loses the push race (raises LandRaceError), after cleaning up
    exactly as the real land_trial does."""
    def __init__(self):
        self.calls = []
    def commit_all(self, *a, **k): return "feat_sha"
    def trial_merge(self, *a, **k): self.calls.append("trial_merge"); return True
    def changed_paths(self, *a, **k): return []
    def current_sha(self, *a, **k): return "merge_sha"
    def land_trial(self, temp):
        self.calls.append("land_trial")
        raise LandRaceError("land_trial: push to origin/dev rejected — the base advanced under us.")
    def abandon_trial(self, *a, **k): self.calls.append("abandon_trial")
    def delete_local_branch(self, *a, **k): self.calls.append("delete_local_branch")
    def delete_remote_branch(self, *a, **k): self.calls.append("delete_remote_branch")
    def sync_main_base(self, *a, **k): self.calls.append("sync_main_base"); return ""


class _WinGit(_RaceGit):
    """The happy path — land_trial succeeds, so the ticket merges and its status moves."""
    def land_trial(self, temp): self.calls.append("land_trial")   # no raise → success


class _Backlog:
    def __init__(self): self.status_calls = []; self.comment_calls = []
    def set_status(self, ticket, status): self.status_calls.append(status)
    def add_comment(self, *a, **k): self.comment_calls.append(a)


class _Audit:
    def __init__(self): self.events = []
    def record(self, event, **k): self.events.append((event, k))


tkt = Ticket(id="EU-259", key="EU-259", summary="land race", description="")
bld = SimpleNamespace(summary="built it")
rev = SimpleNamespace(summary="reviewed it")

orig_gate, orig_notify, orig_changelog = loop.run_gate, loop._notify, loop._record_changelog
loop.run_gate = lambda *a, **k: SimpleNamespace(passed=True, report="ok")
loop._notify = lambda *a, **k: None
loop._record_changelog = lambda *a, **k: None


def land(git, backlog, audit):
    return loop._land(tkt, app, cfg(), git, backlog, audit,
                      branch=BRANCH, iteration=1, cost=0.0, build=bld, review=rev)


try:
    # 1) The race path.
    g, bl, au = _RaceGit(), _Backlog(), _Audit()
    rep = land(g, bl, au)
    chk("a land-race returns REQUEUED (not ERRORED/ESCALATED)",
        rep.outcome == Outcome.REQUEUED, str(rep.outcome))
    chk("REQUEUED is not the error-striking ERRORED outcome", rep.outcome != Outcome.ERRORED)
    chk("REQUEUED is not the parking ESCALATED outcome", rep.outcome != Outcome.ESCALATED)
    chk("the Jira ticket status is left UNTOUCHED (nothing merged)",
        bl.status_calls == [], str(bl.status_calls))
    chk("no Jira comment is posted on the race (no merge to announce)",
        bl.comment_calls == [], str(bl.comment_calls))
    chk("a land_race_requeue audit event is recorded",
        any(e == "land_race_requeue" for e, _ in au.events), str([e for e, _ in au.events]))
    chk("NO merged_to_dev audit event was recorded (nothing landed)",
        not any(e == Outcome.MERGED.audit_event for e, _ in au.events), str([e for e, _ in au.events]))
    chk("the notes explain the re-trial", "re-trial" in (rep.notes or "").lower(), rep.notes)

    # 2) The happy path still merges and moves the ticket — the fix didn't break a normal land.
    g2, bl2, au2 = _WinGit(), _Backlog(), _Audit()
    rep2 = land(g2, bl2, au2)
    chk("the happy path still reports MERGED", rep2.outcome == Outcome.MERGED, str(rep2.outcome))
    chk("the happy path moves the ticket status (QA/Done)", bl2.status_calls != [], str(bl2.status_calls))
    chk("the happy path records merged_to_dev",
        any(e == Outcome.MERGED.audit_event for e, _ in au2.events), str([e for e, _ in au2.events]))
    chk("the happy path records NO land_race_requeue",
        not any(e == "land_race_requeue" for e, _ in au2.events))
finally:
    loop.run_gate, loop._notify, loop._record_changelog = orig_gate, orig_notify, orig_changelog

print("\n================= EU-259 LAND-RACE REQUEUE QA =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
