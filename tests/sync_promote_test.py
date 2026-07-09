"""Cockpit Deploy DEV -> main (promote): gating, ahead-count, ff-merge+push, return-to-dev, idempotency."""
import os, sys, tempfile, subprocess, types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import sync
from orchestrator.config import Config, AppConfig

def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
origin = tmp / "origin.git"
subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], capture_output=True)

# seed origin with main + dev at the same commit
git(tmp, "clone", str(origin), "seed"); seed = tmp / "seed"
git(seed, "config", "user.email", "t@t"); git(seed, "config", "user.name", "t")
(seed / "README.md").write_text("v0"); git(seed, "add", "."); git(seed, "commit", "-m", "c0")
git(seed, "push", "origin", "HEAD:main")
git(seed, "checkout", "-b", "dev"); git(seed, "push", "origin", "dev")

# mac clone with both local branches; dev gets 2 commits ahead of main
mac = tmp / "mac"; git(tmp, "clone", str(origin), "mac")
git(mac, "config", "user.email", "m@m"); git(mac, "config", "user.name", "mac")
git(mac, "checkout", "main"); git(mac, "checkout", "dev")
for i in (1, 2):
    (mac / f"f{i}.txt").write_text(str(i)); git(mac, "add", "."); git(mac, "commit", "-m", f"c{i}")
git(mac, "push", "origin", "dev")

cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(mac), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(mac / "audit.jsonl"), use_worktree=False)

# --- gate ---
os.environ.pop("GENERAL_COCKPIT_PROMOTE", None)
chk("can_promote() false without the flag", not sync.can_promote())
os.environ["GENERAL_COCKPIT_PROMOTE"] = "1"
chk("can_promote() true with the flag", sync.can_promote())

# --- status: dev is 2 ahead of main (checked via promote()'s ahead_before) ---
# We can't just call promote() because it will consume the state.
# Instead, we'll verify ahead_before by doing the actual promotion and checking it worked.
# First, verify the promotion will push 2 commits by checking ahead_before in the result
r = sync.promote(cfg)
chk("promote ahead_before == 2 (dev has 2 commits ahead)", r["ahead_before"] == 2, str(r))
# AND it should have pushed since there were commits ahead
chk("promote pushed when commits were ahead", r["ok"] and r["pushed"], str(r))
chk("working tree left on dev", git(mac, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "dev")
# Verify dev no longer ahead of main via ahead_before
after_promote = sync.promote(cfg)
chk("dev no longer ahead of main", after_promote.get("ahead_before") == 0, str(after_promote))
git(mac, "fetch", "origin", "main")
om = git(mac, "rev-parse", "origin/main").stdout.strip()
dv = git(mac, "rev-parse", "dev").stdout.strip()
chk("origin/main fast-forwarded to dev", om == dv, f"{om[:7]} vs {dv[:7]}")

# --- idempotent: promoting again when in sync is a clean no-op ---
r2 = sync.promote(cfg)
chk("promote no-op when already in sync", r2["ok"] and r2["ahead_before"] == 0, str(r2))

print("\n================ DEPLOY DEV->MAIN (PROMOTE) QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
