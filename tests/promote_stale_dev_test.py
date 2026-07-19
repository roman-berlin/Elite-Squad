"""EU-367 / EU-335: the cockpit Deploy (dev->main) must refuse to ship a STALE local dev.

The cockpit can be running behind origin/dev — autopull's fast-forward is blocked whenever the
unit's own runtime files keep the tree dirty (the "0 shipped" daily-brief incident). Pre-fix,
`promote()` pushed the LOCAL dev ref to origin/main without fetching, so a stale checkout shipped
OLD code to production while reporting success. The fix fetches origin/dev and refuses unless the
local dev ref is EXACTLY origin/dev.

Cases pinned here:
  • local dev BEHIND origin/dev  -> refuse ("STALE"), nothing pushed, origin/main untouched;
  • local dev AHEAD of origin/dev -> refuse ("push dev first"), nothing pushed;
  • local dev == origin/dev       -> proceeds and ships (the happy path still works).
"""
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

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)


tmp = Path(tempfile.mkdtemp())
origin = tmp / "origin.git"
subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], capture_output=True)

# seed origin: main + dev at c0
git(tmp, "clone", str(origin), "seed"); seed = tmp / "seed"
git(seed, "config", "user.email", "t@t"); git(seed, "config", "user.name", "t")
(seed / "README.md").write_text("v0"); git(seed, "add", "."); git(seed, "commit", "-m", "c0")
git(seed, "push", "origin", "HEAD:main")
git(seed, "checkout", "-b", "dev")
# put a couple commits on dev so it is ahead of main (so, absent the staleness guard, promote WOULD push)
for i in (1, 2):
    (seed / f"d{i}.txt").write_text(str(i)); git(seed, "add", "."); git(seed, "commit", "-m", f"d{i}")
git(seed, "push", "origin", "dev")

# the cockpit's own clone — starts current with origin/dev
mac = tmp / "mac"; git(tmp, "clone", str(origin), "mac")
git(mac, "config", "user.email", "m@m"); git(mac, "config", "user.name", "mac")
git(mac, "checkout", "dev")

cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(mac), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(mac / "audit.jsonl"), use_worktree=False)
os.environ["GENERAL_COCKPIT_PROMOTE"] = "1"

origin_main_at_start = git(origin, "rev-parse", "main").stdout.strip()

# ---- Case 1: local dev BEHIND origin/dev (the stale-cockpit case) ----
# advance origin/dev from an independent clone; the mac clone does NOT pull it.
other = tmp / "other"; git(tmp, "clone", str(origin), "other")
git(other, "config", "user.email", "o@o"); git(other, "config", "user.name", "o")
git(other, "checkout", "dev")
(other / "newer.txt").write_text("newer"); git(other, "add", "."); git(other, "commit", "-m", "d3 newer")
git(other, "push", "origin", "dev")

r_behind = sync.promote(cfg)
chk("stale (behind): promote refuses", not r_behind["ok"] and not r_behind["pushed"], str(r_behind))
chk("stale (behind): error says STALE/behind", "stale" in (r_behind.get("error") or "").lower(),
    r_behind.get("error"))
chk("stale (behind): origin/main was NOT advanced",
    git(origin, "rev-parse", "main").stdout.strip() == origin_main_at_start)

# ---- Case 2: local dev AHEAD of origin/dev (unpushed local commit) ----
# bring mac current first, then add a LOCAL commit that is not pushed.
git(mac, "fetch", "origin", "dev"); git(mac, "reset", "--hard", "origin/dev")
(mac / "local_only.txt").write_text("x"); git(mac, "add", "."); git(mac, "commit", "-m", "local only")
r_ahead = sync.promote(cfg)
chk("ahead: promote refuses", not r_ahead["ok"] and not r_ahead["pushed"], str(r_ahead))
chk("ahead: error says push dev first", "push dev first" in (r_ahead.get("error") or "").lower(),
    r_ahead.get("error"))
chk("ahead: origin/main still not advanced",
    git(origin, "rev-parse", "main").stdout.strip() == origin_main_at_start)

# ---- Case 3: local dev == origin/dev (happy path still ships) ----
git(mac, "reset", "--hard", "origin/dev")   # discard the local-only commit; now current
r_ok = sync.promote(cfg)
chk("current: promote ships (ok + pushed)", r_ok["ok"] and r_ok["pushed"], str(r_ok))
git(mac, "fetch", "origin", "main")
chk("current: origin/main fast-forwarded to dev",
    git(mac, "rev-parse", "origin/main").stdout.strip() == git(mac, "rev-parse", "dev").stdout.strip())

print("\n============ EU-367 PROMOTE STALE-DEV GUARD QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
