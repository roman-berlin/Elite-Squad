"""EU-207: Remove dead promote_status helper code - fail-first tests."""
import os, sys, tempfile, subprocess, types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")
os.environ["GENERAL_COCKPIT_PROMOTE"] = "1"

from orchestrator import sync
from orchestrator.config import Config

def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Setup test repo
tmp = Path(tempfile.mkdtemp())
origin = tmp / "origin.git"
subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], capture_output=True)

git(tmp, "clone", str(origin), "seed"); seed = tmp / "seed"
git(seed, "config", "user.email", "t@t"); git(seed, "config", "user.name", "t")
(seed / "README.md").write_text("v0"); git(seed, "add", "."); git(seed, "commit", "-m", "c0")
git(seed, "push", "origin", "HEAD:main")
git(seed, "checkout", "-b", "dev"); git(seed, "push", "origin", "dev")

mac = tmp / "mac"; git(tmp, "clone", str(origin), "mac")
git(mac, "config", "user.email", "m@m"); git(mac, "config", "user.name", "mac")
git(mac, "checkout", "main"); git(mac, "checkout", "dev")
for i in (1, 2):
    (mac / f"f{i}.txt").write_text(str(i)); git(mac, "add", "."); git(mac, "commit", "-m", f"c{i}")
git(mac, "push", "origin", "dev")

cfg = Config(apps=[], audit_path=str(mac / "audit.jsonl"))

# Test 1: promote_status function should NOT exist (will FAIL initially)
chk("promote_status function does not exist in sync module",
    not hasattr(sync, 'promote_status'),
    f"hasattr(sync, 'promote_status') = {hasattr(sync, 'promote_status')}")

# Test 2: promote() should still return ahead_before
r = sync.promote(cfg)
chk("promote() still returns ahead_before in result dict",
    "ahead_before" in r and isinstance(r.get("ahead_before"), int),
    f"result keys: {list(r.keys())}, ahead_before={r.get('ahead_before')}")

# Test 3: promote() ahead_before should be 2 (our test repo has 2 commits ahead)
chk("promote() ahead_before is 2 (correct count from inlined logic)",
    r.get("ahead_before") == 2,
    f"ahead_before={r.get('ahead_before')}")

# Test 4: No code computing '_ahead' status between dev and main remains in cockpit_views.py
cockpit_views_code = Path("orchestrator/cockpit_views.py").read_text()
chk("No _ahead computation in cockpit_views.py",
    "_sync.promote_status" not in cockpit_views_code,
    "_sync.promote_status found in cockpit_views.py")

print("\n================== EU-207 ACCEPTANCE TESTS ==================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
