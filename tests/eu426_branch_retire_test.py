"""EU-426: retire abandoned autodev/* branches once their TICKET is Done.

A merged ticket already retires its branch (loop._land deletes local + remote). But a ticket that
never merges — escalated, Planner-CLOSE-parked, or a killed run — keeps its branch forever, and
nothing collects it once the TICKET itself closes. reap_closed_branches is the sweep that does:
prune an unmerged autodev/<KEY>-* branch ONLY when its ticket's statusCategory is 'done'.

Conservative by design (a deleted branch holding the only copy of someone's work is a disaster):
  · Done + unmerged              → tag attic/<KEY>-<shortsha>, delete local + remote, audit branch_retired
  · Done + already merged into base → delete local + remote, NO tag (commits live on base), audit branch_retired
  · Blocked / To Do / In Progress / QA → untouched (statusCategory != 'done')
  · checked out in any worktree  → untouched, even if Done
  · backlog unreachable OR an unknown/None status → prune NOTHING that cycle (fail closed)
  · not a ticket branch (adhoc-*, _trial, …) → untouched (no ticket to be Done)

This harness builds a hermetic temp git repo (bare origin + clone, real git, no mocks) with one
branch per case and a stubbed backlog mapping ticket keys → statusCategory, then pins every AC.
"""
import sys, types, tempfile, subprocess
from pathlib import Path

# stub the Agent SDK so importing orchestrator.* is cheap + offline (house pattern)
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator.config import Config, AppConfig
from orchestrator import git_ops

results = []
def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))

def G(cwd: Path, *args: str) -> None:
    """Run a git command; raise on failure (harness setup only)."""
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)

def out(cwd: Path, *args: str) -> str:
    """Run a git command and return trimmed stdout (never raises)."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    ).stdout.strip()

def branch_exists(repo: Path, name: str) -> bool:
    return bool(out(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"))

def tag_exists(repo: Path, pattern: str) -> bool:
    return bool(out(repo, "tag", "--list", pattern))


class StubBacklog:
    """Minimal backlog stub: maps ticket key -> statusCategory ('new'|'indeterminate'|'done').
    A key absent from the map returns None (status could not be determined)."""
    def __init__(self, cats: dict):
        self.cats = dict(cats)
        self.queries = []
    def status_category(self, key: str):
        self.cats.get  # touch
        self.queries.append(key)
        return self.cats.get(key)


class FakeAudit:
    def __init__(self):
        self.events = []
    def record(self, event: str, **kw):
        self.events.append((event, kw))
    def retired(self, key: str):
        return [kw for ev, kw in self.events if ev == "branch_retired" and kw.get("key") == key]


tmp = Path(tempfile.mkdtemp())


def seed(name: str):
    """Bare origin 'dev' + a clone 'work' with base branch dev. Returns (origin, work)."""
    origin = tmp / f"{name}.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    work = tmp / f"{name}_work"
    G(tmp, "clone", str(origin), str(work))
    G(work, "config", "user.email", "t@t")
    G(work, "config", "user.name", "EU426 test")
    (work / "seed.txt").write_text("baseline\n")
    G(work, "add", "-A"); G(work, "commit", "-m", "baseline")
    G(work, "branch", "-m", "dev")            # rename default 'main' -> 'dev' (the base)
    G(work, "push", "origin", "dev")
    return origin, work


def mk_unmerged(work: Path, branch: str, content: str) -> str:
    """Create an autodev branch off dev with one unique commit (unmerged). Returns its tip sha."""
    G(work, "checkout", "-b", branch, "dev")
    (work / f"{branch.replace('/', '_')}.txt").write_text(content)
    G(work, "add", "-A"); G(work, "commit", "-m", branch)
    G(work, "checkout", "dev")
    return out(work, "rev-parse", branch)


# ============================================================================ #
# SCENARIO A — the full matrix in one repo / one stubbed backlog                #
# ============================================================================ #
originA, workA = seed("A")

# statusCategory map: 'done' reaps; everything else (incl. None) leaves the branch.
cats = {
    "EU-1001": "done",          # unmerged  -> tag + delete + audit        (AC1)
    "EU-1002": "done",          # merged    -> delete, no tag              (AC1 merged clause)
    "EU-1003": "indeterminate", # In Progress -> untouched                 (AC2)
    "EU-1004": "new",           # To Do       -> untouched                 (AC2)
    "EU-1005": "indeterminate", # Blocked     -> untouched                 (AC2)
    "EU-1006": "indeterminate", # QA          -> untouched                 (AC2)
    "EU-1007": "done",          # done BUT checked out in a worktree       -> untouched (AC3)
    "EU-1008": None,            # backlog couldn't classify this key       -> untouched (AC4 fail-closed)
    # EU-1009 not in map at all -> None -> untouched (fail closed)
}
backlogA = StubBacklog(cats)
auditA = FakeAudit()

sha_1001 = mk_unmerged(workA, "autodev/EU-1001-done", "one")
sha_1002 = mk_unmerged(workA, "autodev/EU-1002-merged", "two")
mk_unmerged(workA, "autodev/EU-1003-inprog", "three")
mk_unmerged(workA, "autodev/EU-1004-todo", "four")
mk_unmerged(workA, "autodev/EU-1005-blocked", "five")
mk_unmerged(workA, "autodev/EU-1006-qa", "six")
mk_unmerged(workA, "autodev/EU-1007-wt", "seven")
mk_unmerged(workA, "autodev/EU-1008-unknown", "eight")
mk_unmerged(workA, "autodev/EU-1009-absent", "nine")
mk_unmerged(workA, "autodev/adhoc-misc", "adhoc")     # not a ticket key -> untouched

# EU-1002 is the MERGED case: merge its branch into dev and push dev so origin/dev carries it
# (the sweep checks origin/<base> for "already merged into base").
G(workA, "checkout", "dev")
G(workA, "merge", "--no-ff", "-m", "merge EU-1002", "autodev/EU-1002-merged")
G(workA, "push", "origin", "dev")

# EU-1001 gets a REMOTE ref so we can prove the sweep deletes origin's copy too.
G(workA, "push", "origin", "autodev/EU-1001-done")

# EU-1007 is checked out in a linked worktree -> must be skipped even though Done.
wt1007 = tmp / "A_wt1007"
G(workA, "worktree", "add", str(wt1007), "autodev/EU-1007-wt")

appA = AppConfig(name="eu", repo_path=str(workA), backlog_backend="none", base_branch="dev")
cfgA = Config(apps=[appA], audit_path=str(tmp / "A_audit.jsonl"))

sha_1002 = out(workA, "rev-parse", "autodev/EU-1002-merged")
sha_1007 = out(workA, "rev-parse", "autodev/EU-1007-wt")

git_ops.reap_closed_branches(cfgA, {"eu": backlogA}, auditA)

# --- AC1: Done + unmerged -> tagged then deleted, audited (key, sha, tag) ---
chk("AC1 unmerged: local branch deleted", not branch_exists(workA, "autodev/EU-1001-done"))
chk("AC1 unmerged: attic tag created", tag_exists(workA, f"attic/EU-1001-{sha_1001[:12]}"))
chk("AC1 unmerged: attic tag points at the original tip",
    out(workA, "rev-list", "-n", "1", f"attic/EU-1001-{sha_1001[:12]}") == sha_1001)
chk("AC1 unmerged: remote ref deleted", not out(originA, "rev-parse", "--verify", "--quiet",
    "refs/heads/autodev/EU-1001-done"))
r1 = auditA.retired("EU-1001")
chk("AC1 unmerged: audited as branch_retired", len(r1) == 1, str(r1))
if r1:
    chk("AC1 unmerged: audit carries key/sha/tag",
        r1[0].get("key") == "EU-1001" and r1[0].get("sha") == sha_1001 and
        r1[0].get("tag") == f"attic/EU-1001-{sha_1001[:12]}", str(r1[0]))

# --- AC1 merged clause: Done + merged into base -> deleted, NO tag, audited ---
chk("AC1 merged: local branch deleted", not branch_exists(workA, "autodev/EU-1002-merged"))
chk("AC1 merged: NO attic tag (work lives on base)", not tag_exists(workA, "attic/EU-1002-*"))
r2 = auditA.retired("EU-1002")
chk("AC1 merged: audited as branch_retired", len(r2) == 1, str(r2))
if r2:
    chk("AC1 merged: audit tag is None (merged -> no archive needed)",
        r2[0].get("tag") in (None, ""), str(r2[0]))

# --- AC2: Blocked / To Do / In Progress / QA -> untouched (pin EACH status) ---
chk("AC2 In Progress (indeterminate) untouched", branch_exists(workA, "autodev/EU-1003-inprog"))
chk("AC2 To Do (new) untouched", branch_exists(workA, "autodev/EU-1004-todo"))
chk("AC2 Blocked (indeterminate) untouched", branch_exists(workA, "autodev/EU-1005-blocked"))
chk("AC2 QA (indeterminate) untouched", branch_exists(workA, "autodev/EU-1006-qa"))
chk("AC2: none of the open-ticket branches got an attic tag",
    not tag_exists(workA, "attic/EU-1003-*") and not tag_exists(workA, "attic/EU-1004-*") and
    not tag_exists(workA, "attic/EU-1005-*") and not tag_exists(workA, "attic/EU-1006-*"))
chk("AC2: no branch_retired audit for any open-ticket key",
    not auditA.retired("EU-1003") and not auditA.retired("EU-1004") and
    not auditA.retired("EU-1005") and not auditA.retired("EU-1006"))

# --- AC3: branch checked out in a worktree -> untouched even if Done ---
chk("AC3 worktree: Done branch checked out elsewhere is left in place",
    branch_exists(workA, "autodev/EU-1007-wt"))
chk("AC3 worktree: no attic tag for the worktree branch", not tag_exists(workA, "attic/EU-1007-*"))
chk("AC3 worktree: no branch_retired audit for the worktree key", not auditA.retired("EU-1007"))

# --- AC4: backlog unreachable / unknown status -> prune nothing (fail closed) ---
chk("AC4 unknown-status key (None) untouched", branch_exists(workA, "autodev/EU-1008-unknown"))
chk("AC4 absent key (not in map -> None) untouched", branch_exists(workA, "autodev/EU-1009-absent"))
chk("AC4: no attic tag for the unclassifiable keys",
    not tag_exists(workA, "attic/EU-1008-*") and not tag_exists(workA, "attic/EU-1009-*"))
chk("AC4: no branch_retired audit for unclassifiable keys",
    not auditA.retired("EU-1008") and not auditA.retired("EU-1009"))

# --- non-ticket branch (adhoc) is never a prune candidate ---
chk("adhoc (non-key) branch untouched", branch_exists(workA, "autodev/adhoc-misc"))
chk("adhoc: no branch_retired audit", not auditA.retired("adhoc"))


# ============================================================================ #
# SCENARIO B — the WHOLE backlog is unreachable: prune NOTHING that cycle        #
# (backlogs dict has no adapter for the app -> every key fails closed)           #
# ============================================================================ #
originB, workB = seed("B")
mk_unmerged(workB, "autodev/EU-2001-done", "b-one")
mk_unmerged(workB, "autodev/EU-2002-inprog", "b-two")
appB = AppConfig(name="eu", repo_path=str(workB), backlog_backend="none", base_branch="dev")
cfgB = Config(apps=[appB], audit_path=str(tmp / "B_audit.jsonl"))
auditB = FakeAudit()

git_ops.reap_closed_branches(cfgB, {}, auditB)   # no adapter -> board unreachable

chk("AC4 board-unreachable: Done-looking branch left in place (no status source)",
    branch_exists(workB, "autodev/EU-2001-done"))
chk("AC4 board-unreachable: In-Progress branch left in place",
    branch_exists(workB, "autodev/EU-2002-inprog"))
chk("AC4 board-unreachable: NO attic tags created", not tag_exists(workB, "attic/*"))
chk("AC4 board-unreachable: NO branch_retired audit at all", not auditB.events, str(auditB.events))


# ============================================================================ #
# SCENARIO C — the REAL JiraAdapter.status_category (fake session, eu374 pattern)#
# Pins the parsing the sweep depends on: statusCategory.key carried through, and #
# every failure mode reads as None (fail closed -> the sweep prunes nothing).    #
# Imported lazily so the core sweep ACs above never depend on `requests`.        #
# ============================================================================ #
from types import SimpleNamespace as _NS  # noqa: E402
from orchestrator.backlog import jira as _jira  # noqa: E402
import requests as _requests  # noqa: E402


class _Resp:
    def __init__(self, payload=None, auth_denied=False):
        self._p = payload or {}
        self.headers = ({"X-Seraph-LoginReason": "AUTHENTICATION_DENIED"} if auth_denied else {})
    def raise_for_status(self): pass
    def json(self): return self._p


def _status_payload(cat_key):
    return {"fields": {"status": {"name": "X", "statusCategory": {"key": cat_key}}}}


class _StatusSess:
    """GET issue/<key>?fields=status serves a canned payload (or raises to模拟 a network blip)."""
    def __init__(self, by_key, raise_for=None):
        self.by_key = by_key
        self.raise_for = raise_for or set()
    def get(self, url, params=None, **k):
        key = url.rstrip("/").split("/issue/")[-1].split("?")[0]
        if key in self.raise_for:
            raise _requests.RequestException("network down")
        return self.by_key.get(key, _Resp({"fields": {"status": {}}}))


def _jira_adapter(session):
    a = _NS(app_name="eu", base_url="https://x.atlassian.net", project="EU", session=session)
    a._url = lambda p: f"https://x.atlassian.net/rest/api/3/{p.lstrip('/')}"
    # bind the REAL auth-blind-200 guard so the denied case is exercised, not stubbed away
    a._raise_if_unauthenticated = types.MethodType(
        _jira.JiraAdapter._raise_if_unauthenticated, a)
    return a


sessC = _StatusSess(by_key={
    "EU-3001": _Resp(_status_payload("done")),
    "EU-3002": _Resp(_status_payload("indeterminate")),
    "EU-3003": _Resp(_status_payload("new")),
    "EU-3004": _Resp({"fields": {"status": {"name": "Weird"}}}),  # no statusCategory at all
    "EU-3005": _Resp(_status_payload("done"), auth_denied=True),  # auth-blind 200
}, raise_for={"EU-3006"})
adC = _jira_adapter(sessC)

chk("Jira status_category: done key -> 'done'",
    _jira.JiraAdapter.status_category(adC, "EU-3001") == "done")
chk("Jira status_category: In Progress -> 'indeterminate'",
    _jira.JiraAdapter.status_category(adC, "EU-3002") == "indeterminate")
chk("Jira status_category: To Do -> 'new'",
    _jira.JiraAdapter.status_category(adC, "EU-3003") == "new")
chk("Jira status_category: missing statusCategory -> None (fail closed)",
    _jira.JiraAdapter.status_category(adC, "EU-3004") is None)
chk("Jira status_category: auth-blind 200 -> None (fail closed, never 'looks Done')",
    _jira.JiraAdapter.status_category(adC, "EU-3005") is None)
chk("Jira status_category: network RequestException -> None (fail closed)",
    _jira.JiraAdapter.status_category(adC, "EU-3006") is None)


# ============================================================================ #
# Results                                                                       #
# ============================================================================ #
print("\n================ EU-426 BRANCH-RETIRE SWEEP QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    suffix = f"  ({detail})" if detail and not ok else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{suffix}")
print("---------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
