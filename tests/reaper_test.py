"""EU-117: Reaper cleans up merged worktrees with dead PIDs."""
import sys, types, os, tempfile, subprocess
from pathlib import Path

# mock sdk
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
from orchestrator import loop

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
    # create origin/dev
    origin = d / "origin"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=str(repo), check=True)
    subprocess.run(["git", "push", "-u", "origin", "dev"], cwd=str(repo), check=True)
    return d, repo

def test_reaper():
    d, repo = setup_test_repo()
    
    app = AppConfig(
        name="testapp",
        repo_path=str(repo),
        backlog_backend="none",
        base_branch="dev",
        branch_prefix="feat/"
    )
    cfg = Config(apps=[app], audit_path="/tmp/x.jsonl", use_worktree=True)
    
    # 1. Create a merged fake worktree
    # branch is merged into dev
    merged_wt = repo / ".general-worktrees" / "Elite-Unit"
    merged_wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", "-b", "br-merged", str(merged_wt), "dev"], cwd=str(repo), check=True)
    
    # Lock it with a dead PID via loop._worktree_lock
    lock_file = repo / ".general-worktrees" / "Elite-Unit.lock"
    lock_file.write_text("999999\n")
    
    # 2. Create an unmerged worktree
    unmerged_wt = repo / ".claude" / "worktrees" / "agent-123"
    unmerged_wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", "-b", "br-unmerged", str(unmerged_wt), "dev"], cwd=str(repo), check=True)
    # make it unmerged
    (unmerged_wt / "file").write_text("unmerged")
    subprocess.run(["git", "commit", "-am", "unmerged"], cwd=str(unmerged_wt), check=True)
    # lock it via git worktree lock with a dead PID
    subprocess.run(["git", "worktree", "lock", "--reason", "pid 999999", str(unmerged_wt)], cwd=str(repo), check=True)

    # 3. Create a merged, but ALIVE worktree
    alive_wt = repo / ".claude" / "worktrees" / "agent-alive"
    subprocess.run(["git", "worktree", "add", "-b", "br-alive", str(alive_wt), "dev"], cwd=str(repo), check=True)
    subprocess.run(["git", "worktree", "lock", "--reason", f"pid {os.getpid()}", str(alive_wt)], cwd=str(repo), check=True)

    # 4. QW7 (2026-07-05): a DETACHED merged worktree with a dead owner — the ONLY shape production
    # actually creates (git_ops adds every .general-worktrees worktree with `worktree add --detach`).
    # This is byte-for-byte the Jul-1 crash orphan (.general-worktrees/Elite-Unit, detached @ a sha
    # on dev, stale flock with a dead PID) that the old `if not branch_ref: continue` skipped.
    detached_wt = repo / ".general-worktrees" / "Elite-Unit-detached"
    subprocess.run(["git", "worktree", "add", "--detach", str(detached_wt), "dev"], cwd=str(repo), check=True)
    (repo / ".general-worktrees" / "Elite-Unit-detached.lock").write_text("999999\n")

    # 5. QW7 guard: a DETACHED .claude agent worktree keeps the skip (foreign provenance) even
    # when merged — only .general-worktrees detached worktrees are ours to classify by sha.
    foreign_wt = repo / ".claude" / "worktrees" / "agent-detached"
    subprocess.run(["git", "worktree", "add", "--detach", str(foreign_wt), "dev"], cwd=str(repo), check=True)

    # 6. Review fix (2026-07-05): the CANONICAL persistent per-app worktree
    # (loop._worktree_path = <repo_parent>/.general-worktrees/<app.name>) is parked detached at
    # base with a free flock whenever it is idle — state-identical to an orphan. The reaper must
    # NEVER touch it (Git.setup() self-heals it); reaping it would re-run worktree_setup_cmd on
    # every autopilot restart (EU-54 run-ONCE violation).
    canonical_wt = d / ".general-worktrees" / "testapp"
    canonical_wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", "--detach", str(canonical_wt), "dev"], cwd=str(repo), check=True)
    (d / ".general-worktrees" / "testapp.lock").write_text("999999\n")   # idle: free flock, dead pid

    # Verify pre-state
    out = subprocess.run(["git", "worktree", "list"], cwd=str(repo), capture_output=True, text=True).stdout
    chk("pre: br-merged exists", "br-merged" in out)
    chk("pre: br-unmerged exists", "br-unmerged" in out)
    chk("pre: br-alive exists", "br-alive" in out)
    chk("pre: detached production worktree exists", "Elite-Unit-detached" in out)
    chk("pre: detached foreign agent worktree exists", "agent-detached" in out)

    # Run reaper
    git_ops.reap_stale_worktrees(cfg)

    out = subprocess.run(["git", "worktree", "list"], cwd=str(repo), capture_output=True, text=True).stdout
    chk("merged + dead IS reaped", "br-merged" not in out)
    chk("unmerged + dead IS LEFT untouched", "br-unmerged" in out)
    chk("merged + alive IS LEFT untouched", "br-alive" in out)
    chk("QW7: DETACHED merged + dead .general-worktrees IS reaped (the Jul-1 orphan shape)",
        "Elite-Unit-detached" not in out, out)
    chk("QW7: DETACHED .claude agent worktree keeps the skip (foreign provenance)",
        "agent-detached" in out)
    chk("QW7: the dead session's flock sidecar is removed with its worktree",
        not (repo / ".general-worktrees" / "Elite-Unit-detached.lock").exists())
    chk("review fix: the canonical idle per-app worktree is LEFT untouched",
        "general-worktrees/testapp" in out, out)
    chk("review fix: the canonical worktree's lock sidecar survives too",
        (d / ".general-worktrees" / "testapp.lock").exists())

test_reaper()

# --- Print results ---
fails = [n for n, c, _ in results if not c]
for n, c, d in results:
    print(f"[{'PASS' if c else 'FAIL'}] {n}" + (f" ({d})" if not c and d else ""))
if fails:
    sys.exit(1)
