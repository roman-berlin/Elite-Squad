"""EU-334: reap_stale_worktrees must never self-reap the worktree it's running in, must survive a
git-subprocess OSError/FileNotFoundError without propagating, and must never point a mutating git
call's cwd at a path the same loop iteration may have just removed.

Regression coverage for the false "red base" halt: the base-gate ran `tests/run_all.py` from
INSIDE a `.general-worktrees/<app>` worktree whose HEAD was exactly the base tip — the reaper
classified that worktree as merged+dead, `git worktree remove`d the directory the process was
running in, then `git worktree prune` crashed with FileNotFoundError (stale cwd) and that
exception blew straight out of autopilot(), through the harness, and read as a genuine
assertion-red on `dev`.
"""
import sys, types, os, tempfile, subprocess
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.modules["requests"] = types.ModuleType("requests")
sys.path.insert(0, ".")

from orchestrator.config import Config, AppConfig
from orchestrator import git_ops

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


def setup_test_repo():
    d = Path(tempfile.mkdtemp())
    repo = d / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True)
    # CI runners have no global git identity — commits crash without a repo-local one.
    subprocess.run(["git", "config", "user.email", "unit@test"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Elite Unit test"], cwd=str(repo), check=True)
    (repo / "file").write_text("1")
    subprocess.run(["git", "add", "file"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True)
    subprocess.run(["git", "branch", "-m", "dev"], cwd=str(repo), check=True)
    origin = d / "origin"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=str(repo), check=True)
    subprocess.run(["git", "push", "-u", "origin", "dev"], cwd=str(repo), check=True)
    return d, repo


# --- AC1: reap_stale_worktrees, run with os.getcwd() INSIDE a .general-worktrees/<app> worktree
# whose HEAD equals the base tip, must complete without raising and must leave that worktree
# present on disk. This is byte-for-byte the EU-334 shape: the daemon's own base worktree, idle
# (free flock) and detached at the base tip — indistinguishable from an orphan by state alone. ---
def test_self_reap_safety():
    d, repo = setup_test_repo()

    self_wt = repo / ".general-worktrees" / "Elite-Unit"
    self_wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", "--detach", str(self_wt), "dev"], cwd=str(repo), check=True)
    (repo / ".general-worktrees" / "Elite-Unit.lock").write_text("")   # idle: free flock

    # The app config's repo_path IS the worktree itself — exactly the shape that let `repo` (the
    # reap loop's local var) alias the very directory being classified for removal.
    app = AppConfig(name="eu", repo_path=str(self_wt), backlog_backend="none",
                     base_branch="dev", branch_prefix="feat/")
    cfg = Config(apps=[app], audit_path="/tmp/x.jsonl", use_worktree=True)

    prev_cwd = os.getcwd()
    os.chdir(str(self_wt))
    raised = None
    try:
        git_ops.reap_stale_worktrees(cfg)
    except Exception as exc:  # noqa: BLE001 — we want to SEE any exception the fix must prevent
        raised = exc
    finally:
        os.chdir(prev_cwd)

    chk("self-reap: reap_stale_worktrees does not raise when run from inside the base worktree",
        raised is None, str(raised))
    chk("self-reap: the current worktree is left on disk", self_wt.exists())
    out = subprocess.run(["git", "worktree", "list"], cwd=str(repo), capture_output=True, text=True).stdout
    chk("self-reap: the current worktree is still a registered git worktree", "Elite-Unit" in out, out)

test_self_reap_safety()


# --- AC2: a simulated FileNotFoundError/OSError from a `git worktree …` subprocess inside the
# reaper is swallowed (logged, continue) and never propagates to the caller. ---
def test_oserror_swallowed():
    d, repo = setup_test_repo()

    dead_wt = repo / ".general-worktrees" / "dead-one"
    dead_wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", "--detach", str(dead_wt), "dev"], cwd=str(repo), check=True)
    (repo / ".general-worktrees" / "dead-one.lock").write_text("")

    app = AppConfig(name="eu", repo_path=str(repo), backlog_backend="none",
                     base_branch="dev", branch_prefix="feat/")
    cfg = Config(apps=[app], audit_path="/tmp/x.jsonl", use_worktree=True)

    real_run = subprocess.run
    def _boom(cmd, *a, **k):
        if isinstance(cmd, list) and "worktree" in cmd and ("remove" in cmd or "prune" in cmd):
            raise FileNotFoundError(2, "No such file or directory", cmd[-1] if cmd else "")
        return real_run(cmd, *a, **k)

    git_ops.subprocess.run = _boom
    raised = None
    try:
        git_ops.reap_stale_worktrees(cfg)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    finally:
        git_ops.subprocess.run = real_run

    chk("OSError from a worktree remove/prune subprocess never propagates out of the reaper",
        raised is None, str(raised))

test_oserror_swallowed()


# --- AC3: every `git worktree remove|prune|unlock` and `git branch -D` the reaper issues is
# invoked with cwd = an existing primary-worktree/repo-root path — never the wt_path being
# removed (a path the SAME loop iteration may have just deleted).
#
# Deliberately points app.repo_path AT the worktree being reaped (so the pre-fix code's
# `cwd=str(repo)` equals the wt_path itself — the exact aliasing that produced the traceback)
# while leaving the real process cwd elsewhere, so this isolates the stable-cwd fix from the
# self-exclusion guard covered by test_self_reap_safety above: neither `current_wt_root` nor the
# porcelain-derived `stable_cwd` (the true primary worktree) equals this wt_path, so the reap
# actually proceeds — and that is exactly the case that must use a cwd guaranteed to survive it. ---
def test_stable_cwd():
    d, repo = setup_test_repo()

    merged_wt = repo / ".general-worktrees" / "merged-one"
    merged_wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", "--detach", str(merged_wt), "dev"], cwd=str(repo), check=True)
    (repo / ".general-worktrees" / "merged-one.lock").write_text("")
    merged_wt_resolved = str(merged_wt.resolve())   # capture BEFORE the reaper (may) removes it

    app = AppConfig(name="eu", repo_path=str(merged_wt), backlog_backend="none",
                     base_branch="dev", branch_prefix="feat/")
    cfg = Config(apps=[app], audit_path="/tmp/x.jsonl", use_worktree=True)

    real_run = subprocess.run
    calls = []
    def _record(cmd, *a, **k):
        calls.append((cmd, k.get("cwd")))
        return real_run(cmd, *a, **k)

    git_ops.subprocess.run = _record
    raised = None
    try:
        git_ops.reap_stale_worktrees(cfg)
    except Exception as exc:  # noqa: BLE001 — pre-fix this aliasing raises; capture, don't crash the harness
        raised = exc
    finally:
        git_ops.subprocess.run = real_run

    chk("stable cwd: reap_stale_worktrees does not raise when repo_path aliases the reaped worktree",
        raised is None, str(raised))
    mutating = [(cmd, cwd) for cmd, cwd in calls
                if len(cmd) >= 3 and cmd[0] == "git" and
                ((cmd[1] == "worktree" and cmd[2] in ("remove", "prune", "unlock")) or
                 (cmd[1] == "branch" and cmd[2] == "-D"))]
    chk("stable cwd: the reaper actually issued mutating worktree/branch calls to check", len(mutating) > 0)
    chk("stable cwd: every mutating call's cwd exists on disk",
        all(cwd and Path(cwd).exists() for _, cwd in mutating), str(mutating))
    chk("stable cwd: no mutating call's cwd is the wt_path being removed",
        all(cwd != str(merged_wt) and cwd != merged_wt_resolved for _, cwd in mutating), str(mutating))

test_stable_cwd()


print("\n============ REAPER SELF-REAP / STABLE-CWD QA (EU-334) ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
