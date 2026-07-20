"""EU-335: the changelog append must not re-dirty the primary checkout — AND must never do so by
creating a LOCAL commit that DIVERGES from origin (iteration 1 was rejected for exactly that: an
unconditional local commit on a stale base blocked the very ff-only autopull this fix unblocks, so
the branch couldn't land cleanly).

loop._record_changelog appends a line to Documentation/Development_Status.md on every successful LIVE
land (EU-41). This asserts it now, when the target lives inside a real git work tree ON THE BASE
BRANCH, first fast-forwards onto origin/<base> and only then best-effort commits (+ pushes) its own
append — so `git status --porcelain` goes back to empty WITHOUT introducing divergence — while staying
a total no-op (no exception, write still succeeds) off the base branch or outside a repo, and
swallowing any commit/push failure so the land is never broken."""
import sys, types, tempfile, subprocess
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import loop
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

def run(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)

def new_repo(*, bare=False):
    p = Path(tempfile.mkdtemp())
    if bare:
        run(p, "init", "--bare", "-q")
    else:
        run(p, "init", "-q")
    run(p, "symbolic-ref", "HEAD", "refs/heads/dev")  # force base branch = dev, git-version-agnostic
    run(p, "config", "user.email", "test@example.com")
    run(p, "config", "user.name", "Test")
    return p

app = AppConfig(name="Elite-Unit", repo_path=".", base_branch="dev",
                protected_branch="main", backlog_backend="none")
live = Config(apps=[app], audit_path="/tmp/eu335-audit.jsonl", dry_run=False)
tkt = Ticket(id="EU-335", key="EU-335", summary="commit the changelog append", description="")

# --- 1) target INSIDE a real base-branch work tree (no origin): append committed, tree goes clean ---
repo = new_repo()
(repo / "README.md").write_text("seed\n", encoding="utf-8")
(repo / ".gitignore").write_text("*.md.lock\n", encoding="utf-8")  # matches this repo's own ignore
run(repo, "add", "README.md", ".gitignore")
run(repo, "commit", "-q", "-m", "seed")

doc = repo / "Documentation" / "Development_Status.md"
wrote = loop._record_changelog(live, tkt, app, "committed the changelog append",
                               "https://dev.example.com/eu335", today="2026-07-17", path=str(doc))
chk("returned True", wrote)
status = run(repo, "status", "--porcelain").stdout
chk("git status --porcelain empty right after the land (no dangling append)", status.strip() == "", repr(status))
log = run(repo, "log", "--oneline", "-1").stdout
chk("HEAD is a NEW commit (not the seed)", "seed" not in log, log)
show = run(repo, "show", "--stat", "HEAD").stdout
chk("the changelog file is part of that commit", "Development_Status.md" in show, show)
chk("the entry text is actually IN the commit",
    "committed the changelog append" in run(repo, "show", "HEAD").stdout)

# second land on the same repo: still clean, history intact
wrote2 = loop._record_changelog(live, tkt, app, "second commit", "", today="2026-07-18", path=str(doc))
status2 = run(repo, "status", "--porcelain").stdout
chk("second land returns True", wrote2)
chk("tree clean again after a second land", status2.strip() == "", repr(status2))
chk("both entries preserved across commits",
    doc.read_text(encoding="utf-8").count("- 2026-07-1") == 2)

# --- 2) THE AC#1 DIVERGENCE GUARANTEE: origin advances behind our back, then we land ---------------
# Seed a bare origin, clone it, advance origin via a SECOND clone (simulating a worktree-based land
# pushing commits our primary checkout hasn't seen), THEN call _record_changelog in the (now-behind)
# primary checkout. It must (a) not raise, and (b) leave the checkout able to ff onto origin/dev —
# i.e. introduce NO divergence. Iteration 1 failed (b): it committed on the stale base -> divergence.
origin = new_repo(bare=True)
local = new_repo()
run(local, "remote", "add", "origin", str(origin))
(local / "README.md").write_text("seed\n", encoding="utf-8")
(local / ".gitignore").write_text("*.md.lock\n", encoding="utf-8")
run(local, "add", "README.md", ".gitignore")
run(local, "commit", "-q", "-m", "seed")
run(local, "push", "-q", "origin", "dev")

lander = Path(tempfile.mkdtemp())
run(lander, "clone", "-q", str(origin), ".")
run(lander, "config", "user.email", "lander@example.com")
run(lander, "config", "user.name", "Lander")
(lander / "landed.txt").write_text("a worktree land origin now has\n", encoding="utf-8")
run(lander, "add", "landed.txt")
run(lander, "commit", "-q", "-m", "EU-999 landed via worktree")
run(lander, "push", "-q", "origin", "dev")   # origin/dev now AHEAD of `local`

doc_l = local / "Documentation" / "Development_Status.md"
raised = False
try:
    out = loop._record_changelog(live, tkt, app, "land while origin moved", "",
                                 today="2026-07-19", path=str(doc_l))
except Exception:
    raised = True
    out = None
chk("origin-moved case: no exception", not raised)
chk("origin-moved case: returns True", out is True)
# (b) the actual guarantee: the checkout can still ff onto origin/dev — no divergence was introduced.
run(local, "fetch", "-q", "origin", "dev")
ff = run(local, "merge", "--ff-only", "origin/dev")
chk("origin-moved case: `git merge --ff-only origin/dev` STILL succeeds (no divergence)",
    ff.returncode == 0, ff.stderr)
chk("origin-moved case: origin's land commit is now present locally (we ff'd first)",
    "EU-999 landed via worktree" in run(local, "log", "--oneline").stdout)
chk("origin-moved case: our append is in the history",
    "land while origin moved" in run(local, "log", "-p", "-1").stdout
    or "land while origin moved" in doc_l.read_text(encoding="utf-8"))

# --- 3) branch guard: NOT on the base branch -> no-op (no commit), never raises --------------------
side = new_repo()
(side / "README.md").write_text("seed\n", encoding="utf-8")
run(side, "add", "README.md")
run(side, "commit", "-q", "-m", "seed")
run(side, "checkout", "-q", "-b", "feature/x")
doc_s = side / "Documentation" / "Development_Status.md"
raised3 = False
try:
    out3 = loop._record_changelog(live, tkt, app, "on a feature branch", "",
                                  today="2026-07-17", path=str(doc_s))
except Exception:
    raised3 = True
    out3 = None
chk("off-base branch: no exception", not raised3)
chk("off-base branch: write still returns True", out3 is True)
chk("off-base branch: NO changelog commit was made (guard held)",
    "on a feature branch" not in run(side, "log", "-p").stdout)

# --- 3b) 2026-07-20 (caught live on the AUTO-59 land): the branch guard is case-INSENSITIVE —
# --- automatixy's configured base is 'DEV' while git reports 'dev'; exact-match skipped every
# --- one of that repo's lands from the changelog. Same branch, different case → commit happens.
from dataclasses import replace as _dc_replace
case_repo = new_repo()
(case_repo / "README.md").write_text("seed\n", encoding="utf-8")
run(case_repo, "add", "README.md")
run(case_repo, "commit", "-q", "-m", "seed")          # HEAD is 'dev' (new_repo forces it)
app_uc = _dc_replace(app, base_branch="DEV")
doc_c = case_repo / "Documentation" / "Development_Status.md"
out3b = loop._record_changelog(Config(apps=[app_uc], audit_path="/tmp/eu335-audit.jsonl", dry_run=False),
                               tkt, app_uc, "case-insensitive base match", "",
                               today="2026-07-20", path=str(doc_c))
chk("case-mismatched base ('dev' vs 'DEV'): the changelog commit IS made",
    out3b is True and "case-insensitive base match" in run(case_repo, "log", "-p").stdout)

# --- 4) target NOT inside a git repo (arbitrary tmp path, as the eu41 tests use): clean no-op ------
plain = Path(tempfile.mkdtemp())
doc_plain = plain / "Documentation" / "Development_Status.md"
raised4 = False
try:
    out4 = loop._record_changelog(live, tkt, app, "no repo here", "", today="2026-07-17",
                                  path=str(doc_plain))
except Exception:
    raised4 = True
    out4 = None
chk("non-repo target: no exception", not raised4)
chk("non-repo target: write still succeeds (returns True)", out4 is True)
chk("non-repo target: file was written to disk", doc_plain.exists())
chk("non-repo target: no stray .git created as a side effect", not (plain / ".git").exists())

print("\n=============== EU-335 CHANGELOG COMMIT QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
