"""EU-453 post-merge dev-HEAD verify WIRING QA.

The EU-452 engine (``postmerge_verify.py``) only returns a ``(bool, str)`` verdict. EU-453 is the
WIRING: it calls that engine from ``_land``'s post-merge block and, on a TRUE red, FLAGS the merge
(audit event + Telegram + ticket comment + Needs Human) and returns the ticket as ESCALATED —
flag-only, the merge STANDS, NEVER auto-reverts (mirrors smoke; obeys the unit's hardest lesson:
nothing post-merge may auto-park the queue). This harness pins that wiring contract two ways:

  1. HELPER level — ``loop._postmerge_verify_flag`` is the testable unit (the factored decision the
     design brief extracts so AC#1-5 are testable without the heavy real-git ``_land`` harness). We
     stub ``postmerge_verify.should_run`` / ``verify`` + a fake backlog/audit + monkeypatch
     ``loop._notify`` and assert the exact side-effects of red / green / not-armed / green-skip /
     ephemeral.
  2. ``_land`` level — drive the real ``_land`` over a spy git to confirm the inline wiring: a red
     post-merge verify short-circuits to ESCALATED (skipping smoke/ci/MERGED), a green / not-armed
     one falls through to MERGED, and a not-armed app never even calls ``verify``.

Both halves mirror the engine contract: the merge already happened, so the flag must NEVER raise into
the loop and NEVER touch git's mutating methods.
"""
import sys
import types
from types import SimpleNamespace

# Stub the Agent SDK exactly like the sibling harnesses (no network, no real models).
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): pass

    def __call__(self, *a, **k): return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import loop
from orchestrator import postmerge_verify as pmv
from orchestrator import sentinel, smoke, ci_conclusion, devstate
from orchestrator.config import Config
from orchestrator.contracts import Outcome, TicketReport

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


MERGE = "abc123def4567890abcdef1234567890abcdef12"
BRANCH = "autodev/EU-453"

ns = SimpleNamespace


# --- shared fakes ------------------------------------------------------------ #
class Audit:
    def __init__(self):
        self.events = []

    def record(self, kind, **kw):
        self.events.append((kind, kw))

    def kinds(self):
        return [k for k, _ in self.events]

    def evt(self, kind):
        return [kw for k, kw in self.events if k == kind]


class FakeBacklog:
    def __init__(self):
        self.statuses = []   # list of (ticket, status)
        self.comments = []   # list of (ticket, body)

    def set_status(self, ticket, status):
        self.statuses.append((ticket, status))

    def add_comment(self, ticket, body):
        self.comments.append((ticket, body))


class _RaiseBacklog(FakeBacklog):
    """A backlog whose set_status/add_comment raise — proves the red path swallows it and still
    returns ESCALATED (the merge already stands; a tracker outage must not crash the loop)."""

    def set_status(self, ticket, status):
        raise RuntimeError("tracker set_status down")

    def add_comment(self, ticket, body):
        raise RuntimeError("tracker add_comment down")


class SpyGit:
    """Records every method touched so the red path can prove it calls NO git mutating method
    (no revert, no land_trial, no delete/sync/push/abandon) — the merge stands, untouched."""

    MUTATING = ("revert", "land_trial", "abandon_trial", "delete_local_branch",
                "delete_remote_branch", "sync_main_base", "push", "force_push")

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _rec(*a, **k):
            self.calls.append(name)
            return None
        return _rec


def ticket(ephemeral=False):
    return ns(id="EU-453", key="EU-453", summary="post-merge verify wiring",
              description="", ephemeral=ephemeral)


def app():
    return ns(name="Elite-Unit", base_branch="dev", postmerge_verify=True,
              gate_commands=["python tests/run_all.py"], branch_prefix="autodev")


cfg_on = Config(apps=[], audit_path="/tmp/x.jsonl", postmerge_verify=True)


def reset_pmv(should_run=True, verdict=(True, "green")):
    """Arm the engine stubs: should_run + a verdict tuple, with a canary counting verify() calls."""
    calls = {"n": 0}

    def _should_run(c, a):
        return should_run

    def _verify(c, a, g, sha, au=None):
        calls["n"] += 1
        return verdict

    pmv.should_run = _should_run
    pmv.verify = _verify
    return calls


# ============================================================================ #
# AC#1 — RED: flag-only (audit fail + Needs Human + comment + Telegram + ESCALATED),
#         merge STANDS (no git revert/land), and a raising backlog/notify never propagates.
# ============================================================================ #
sent_notify = []


def _cap_notify(cfg, text):
    sent_notify.append(text)


orig_notify = loop._notify
loop._notify = _cap_notify

verify_calls = reset_pmv(should_run=True, verdict=(False, "FAILED eu_harness - AssertionError: x"))
au = Audit()
bl = FakeBacklog()
git = SpyGit()
t = ticket()
rep = loop._postmerge_verify_flag(cfg_on, app(), t, git, MERGE, au, bl, 1, 0.0, BRANCH)

chk("RED -> returns a TicketReport (not None)", isinstance(rep, TicketReport), repr(rep))
chk("RED -> outcome is ESCALATED", rep is not None and rep.outcome == Outcome.ESCALATED,
    str(getattr(rep, "outcome", None)))
chk("RED -> audit postmerge_verify_fail recorded",
    "postmerge_verify_fail" in au.kinds(), str(au.kinds()))
_fail_evt = au.evt("postmerge_verify_fail")[0] if au.evt("postmerge_verify_fail") else {}
chk("RED -> fail event carries ticket_id", _fail_evt.get("ticket_id") == "EU-453", str(_fail_evt))
chk("RED -> fail event carries app", _fail_evt.get("app") == "Elite-Unit", str(_fail_evt))
chk("RED -> fail event carries merge_sha", _fail_evt.get("merge_sha") == MERGE, str(_fail_evt))
chk("RED -> backlog.set_status(ticket,'Needs Human')",
    ("Needs Human" in [s for _, s in bl.statuses]) and any(tt is t for tt, _ in bl.statuses),
    str(bl.statuses))
_chk_comment = any(("EU-453" in body and "AssertionError" in body) for _, body in bl.comments)
chk("RED -> backlog.add_comment names the ticket + evidence", _chk_comment, str(bl.comments))
_chk_notify = any(("EU-453" in m and "Post-merge dev-HEAD verify" in m) for m in sent_notify)
chk("RED -> _notify Telegram names the ticket + 'Post-merge dev-HEAD verify'", _chk_notify,
    str(sent_notify))
chk("RED -> _notify Telegram says DEV may be silently red / needs revert",
    any("silently red" in m and "revert" in m for m in sent_notify), str(sent_notify))
chk("RED -> verify WAS invoked exactly once", verify_calls["n"] == 1, str(verify_calls))
_red_mutating = [c for c in git.calls if any(c.startswith(m) or m in c for m in SpyGit.MUTATING)]
chk("RED -> NO git revert/land/mutating method invoked (merge stands)", not _red_mutating,
    str(git.calls))

# Raising backlog/notify still returns ESCALATED and never propagates (the merge stands).
sent_notify.clear()
loop._notify = lambda cfg, text: (_ for _ in ()).throw(RuntimeError("telegram down"))
au_r = Audit()
bl_r = _RaiseBacklog()
raised = False
try:
    rep_r = loop._postmerge_verify_flag(cfg_on, app(), ticket(), SpyGit(), MERGE, au_r, bl_r, 1, 0.0, BRANCH)
except Exception as exc:  # noqa: BLE001 — the WHOLE point is it must not raise
    raised = True
    rep_r = None
chk("RED -> raising backlog/_notify swallowed (never propagates)", not raised)
chk("RED -> raising backlog/_notify still returns ESCALATED",
    isinstance(rep_r, TicketReport) and rep_r.outcome == Outcome.ESCALATED, repr(rep_r))
loop._notify = _cap_notify

# ============================================================================ #
# AC#2 — GREEN: audit postmerge_verify_pass, quiet (no status/comment/notify), returns None.
# ============================================================================ #
sent_notify.clear()
verify_calls = reset_pmv(should_run=True, verdict=(True, "post-merge dev-HEAD verify green"))
au2 = Audit()
bl2 = FakeBacklog()
ret2 = loop._postmerge_verify_flag(cfg_on, app(), ticket(), SpyGit(), MERGE, au2, bl2, 1, 0.0, BRANCH)
chk("GREEN -> returns None (proceed to MERGED)", ret2 is None, repr(ret2))
chk("GREEN -> audit postmerge_verify_pass recorded",
    "postmerge_verify_pass" in au2.kinds(), str(au2.kinds()))
_pass_evt = au2.evt("postmerge_verify_pass")[0] if au2.evt("postmerge_verify_pass") else {}
chk("GREEN -> pass event carries ticket_id + merge_sha",
    _pass_evt.get("ticket_id") == "EU-453" and _pass_evt.get("merge_sha") == MERGE, str(_pass_evt))
chk("GREEN -> NO backlog.set_status", not bl2.statuses, str(bl2.statuses))
chk("GREEN -> NO backlog.add_comment", not bl2.comments, str(bl2.comments))
chk("GREEN -> NO _notify", not sent_notify, str(sent_notify))

# ============================================================================ #
# AC#3 — NOT-ARMED: should_run False -> verify NEVER called, zero side-effects, returns None.
# ============================================================================ #
sent_notify.clear()
verify_calls = reset_pmv(should_run=False, verdict=(False, "would-be-red-but-skipped"))
au3 = Audit()
bl3 = FakeBacklog()
ret3 = loop._postmerge_verify_flag(cfg_on, app(), ticket(), SpyGit(), MERGE, au3, bl3, 1, 0.0, BRANCH)
chk("NOT-ARMED -> returns None", ret3 is None, repr(ret3))
chk("NOT-ARMED -> verify NEVER called", verify_calls["n"] == 0, str(verify_calls))
chk("NOT-ARMED -> NO audit events", not au3.events, str(au3.events))
chk("NOT-ARMED -> NO backlog.set_status", not bl3.statuses, str(bl3.statuses))
chk("NOT-ARMED -> NO backlog.add_comment", not bl3.comments, str(bl3.comments))
chk("NOT-ARMED -> NO _notify", not sent_notify, str(sent_notify))

# ============================================================================ #
# AC#4 — engine green-SKIP (sha-mismatch / runner-error -> (True,'skipped')) is NOT a red:
#         no fail event, never escalates, never Needs Human, returns None.
# ============================================================================ #
sent_notify.clear()
verify_calls = reset_pmv(should_run=True, verdict=(True, "dev HEAD ≠ merge_sha; skipped"))
au4 = Audit()
bl4 = FakeBacklog()
ret4 = loop._postmerge_verify_flag(cfg_on, app(), ticket(), SpyGit(), MERGE, au4, bl4, 1, 0.0, BRANCH)
chk("green-SKIP -> returns None (not a red)", ret4 is None, repr(ret4))
chk("green-SKIP -> NO postmerge_verify_fail event",
    "postmerge_verify_fail" not in au4.kinds(), str(au4.kinds()))
chk("green-SKIP -> NO backlog.set_status (never Needs Human)", not bl4.statuses, str(bl4.statuses))
chk("green-SKIP -> NO _notify", not sent_notify, str(sent_notify))

# ============================================================================ #
# AC#5 — EPHEMERAL: ticket.ephemeral=True on RED still records the fail + notifies, but does NOT
#         write the tracker (no set_status / no comment), and still returns ESCALATED.
# ============================================================================ #
sent_notify.clear()
verify_calls = reset_pmv(should_run=True, verdict=(False, "FAILED ephemeral harness"))
au5 = Audit()
bl5 = FakeBacklog()
t_eph = ticket(ephemeral=True)
rep5 = loop._postmerge_verify_flag(cfg_on, app(), t_eph, SpyGit(), MERGE, au5, bl5, 1, 0.0, BRANCH)
chk("EPHEMERAL RED -> returns ESCALATED",
    isinstance(rep5, TicketReport) and rep5.outcome == Outcome.ESCALATED, repr(rep5))
chk("EPHEMERAL RED -> audit postmerge_verify_fail STILL recorded",
    "postmerge_verify_fail" in au5.kinds(), str(au5.kinds()))
chk("EPHEMERAL RED -> _notify STILL sent", len(sent_notify) == 1, str(sent_notify))
chk("EPHEMERAL RED -> NO backlog.set_status (ephemeral skips tracker)",
    not bl5.statuses, str(bl5.statuses))
chk("EPHEMERAL RED -> NO backlog.add_comment (ephemeral skips tracker)",
    not bl5.comments, str(bl5.comments))

loop._notify = orig_notify


# ============================================================================ #
# _land WIRING — drive the real _land over a spy git: RED short-circuits to ESCALATED
#                (smoke/ci/MERGED skipped), GREEN + NOT-ARMED fall through to MERGED, and
#                a not-armed app never calls verify().
# ============================================================================ #
class _LandGit:
    """Minimal spy for the live-merge path _land drives (eu81 shape). Records mutating calls so a
    RED can prove the merge stood (land_trial happened, NO revert followed)."""

    def __init__(self):
        self.calls = []

    def commit_all(self, *a, **k):
        return "feat_sha"

    def trial_merge(self, *a, **k):
        self.calls.append("trial_merge")
        return True

    def changed_paths(self, *a, **k):
        return []

    def current_sha(self, *a, **k):
        return MERGE

    def land_trial(self, temp):
        self.calls.append("land_trial")

    def delete_local_branch(self, b):
        self.calls.append(("delete_local_branch", b))

    def delete_remote_branch(self, b):
        self.calls.append(("delete_remote_branch", b))

    def sync_main_base(self):
        self.calls.append("sync_main_base")
        return "synced"

    def abandon_trial(self, *a, **k):
        pass


from pathlib import Path  # noqa: E402
import tempfile  # noqa: E402

_tmp = Path(tempfile.mkdtemp())
_land_app = type("A", (), {
    "name": "Elite-Unit", "repo_path": str(_tmp), "base_branch": "dev",
    "protected_branch": "main", "branch_prefix": "autodev", "backlog_backend": "none",
    "postmerge_commands": [], "smoke_command": None,
    "gate_commands": ["python tests/run_all.py"], "postmerge_verify": True,
})()


def _land_cfg():
    return Config(apps=[], audit_path=str(_tmp / "a.jsonl"), dry_run=False,
                  sync_base_after_merge=False, mark_done_on_merge=False)


_tkt = ns(id="EU-453", key="EU-453", summary="wiring", description="", ephemeral=False)
_bld = ns(summary="built it")
_rev = ns(summary="reviewed it", unverifiable_gaps=None)

# Stub every _land side-channel so only the EU-453 wiring is under test.
_orig = {
    "run_gate": loop.run_gate,
    "_notify": loop._notify,
    "_record_changelog": loop._record_changelog,
    "devstate_refresh": devstate.refresh,
    "sentinel_should_run": sentinel.should_run,
    "smoke_should_run": smoke.should_run,
    "ci_should_run": ci_conclusion.should_run,
    "pmv_should_run": pmv.should_run,
    "pmv_verify": pmv.verify,
}
loop.run_gate = lambda *a, **k: ns(passed=True, report="ok")
loop._notify = lambda *a, **k: None
loop._record_changelog = lambda *a, **k: None
devstate.refresh = lambda c: None
sentinel.should_run = lambda *a, **k: False
smoke.should_run = lambda *a, **k: False
ci_conclusion.should_run = lambda *a, **k: False

_land_verify_calls = {"n": 0}


def _land_verify(c, a, g, sha, au=None):
    _land_verify_calls["n"] += 1
    return _land_verify_calls["verdict"]


pmv.verify = _land_verify

try:
    # RED via _land: post-merge verify red -> _land returns ESCALATED (NOT MERGED), and the merge
    # already happened (land_trial) with NO revert after it (merge stands).
    pmv.should_run = lambda *a, **k: True
    _land_verify_calls["verdict"] = (False, "FAILED land harness - AssertionError")
    _land_verify_calls["n"] = 0
    g_red = _LandGit()
    rep_red = loop._land(_tkt, _land_app, _land_cfg(), g_red, FakeBacklog(), Audit(),
                         branch=BRANCH, iteration=1, cost=0.0, build=_bld, review=_rev)
    chk("_land RED -> outcome ESCALATED (not MERGED)",
        rep_red.outcome == Outcome.ESCALATED, str(rep_red.outcome))
    chk("_land RED -> merge already happened (land_trial called)", "land_trial" in g_red.calls,
        str(g_red.calls))
    _red_revert = [c for c in g_red.calls if "revert" in (c if isinstance(c, str) else c[0])]
    chk("_land RED -> NO revert after the merge (merge stands)", not _red_revert, str(g_red.calls))

    # GREEN via _land: proceeds to MERGED.
    pmv.should_run = lambda *a, **k: True
    _land_verify_calls["verdict"] = (True, "post-merge dev-HEAD verify green")
    _land_verify_calls["n"] = 0
    g_green = _LandGit()
    rep_green = loop._land(_tkt, _land_app, _land_cfg(), g_green, FakeBacklog(), Audit(),
                           branch=BRANCH, iteration=1, cost=0.0, build=_bld, review=_rev)
    chk("_land GREEN -> outcome MERGED", rep_green.outcome == Outcome.MERGED,
        str(rep_green.outcome))
    chk("_land GREEN -> verify WAS called", _land_verify_calls["n"] == 1, str(_land_verify_calls))

    # NOT-ARMED via _land: verify never called, proceeds to MERGED (zero behavior change).
    pmv.should_run = lambda *a, **k: False
    _land_verify_calls["verdict"] = (False, "would-be-red-but-not-armed")
    _land_verify_calls["n"] = 0
    g_off = _LandGit()
    rep_off = loop._land(_tkt, _land_app, _land_cfg(), g_off, FakeBacklog(), Audit(),
                         branch=BRANCH, iteration=1, cost=0.0, build=_bld, review=_rev)
    chk("_land NOT-ARMED -> verify NEVER called", _land_verify_calls["n"] == 0, str(_land_verify_calls))
    chk("_land NOT-ARMED -> outcome MERGED (zero behavior change)",
        rep_off.outcome == Outcome.MERGED, str(rep_off.outcome))
finally:
    loop.run_gate = _orig["run_gate"]
    loop._notify = _orig["_notify"]
    loop._record_changelog = _orig["_record_changelog"]
    devstate.refresh = _orig["devstate_refresh"]
    sentinel.should_run = _orig["sentinel_should_run"]
    smoke.should_run = _orig["smoke_should_run"]
    ci_conclusion.should_run = _orig["ci_should_run"]
    pmv.should_run = _orig["pmv_should_run"]
    pmv.verify = _orig["pmv_verify"]


print("\n================= EU-453 POST-MERGE VERIFY WIRING QA =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
