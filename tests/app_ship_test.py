"""Cockpit 'Ship → MAIN': promote the current APP's DEV→MAIN (production), gated to the Mac, ff-only."""
import sys, os, types, tempfile, subprocess
from pathlib import Path
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import sync, server
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))
def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)

tmp = Path(tempfile.mkdtemp())
origin = tmp / "origin.git"
subprocess.run(["git", "init", "--bare", "-b", "MAIN", str(origin)], capture_output=True)
# seed MAIN + DEV (DEV will be 2 ahead)
git(tmp, "clone", str(origin), "seed"); seed = tmp / "seed"
git(seed, "config", "user.email", "t@t"); git(seed, "config", "user.name", "t")
(seed / "README.md").write_text("v0"); git(seed, "add", "."); git(seed, "commit", "-m", "c0")
git(seed, "push", "origin", "HEAD:MAIN"); git(seed, "checkout", "-b", "DEV"); git(seed, "push", "origin", "DEV")

repo = tmp / "app"; git(tmp, "clone", str(origin), "app")
git(repo, "config", "user.email", "m@m"); git(repo, "config", "user.name", "m")
git(repo, "checkout", "MAIN"); git(repo, "checkout", "DEV")
for i in (1, 2):
    (repo / f"f{i}.txt").write_text(str(i)); git(repo, "add", "."); git(repo, "commit", "-m", f"c{i}")
git(repo, "push", "origin", "DEV")

app = AppConfig(name="automatixy", repo_path=str(repo), base_branch="DEV", protected_branch="MAIN", backlog_backend="none")
cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)

# --- status: DEV is 2 ahead of MAIN ---
st = sync.app_promote_status(app)
chk("app_promote_status: DEV 2 ahead of MAIN", st["ahead"] == 2 and st["base"] == "DEV" and st["prot"] == "MAIN", str(st))

# --- gate: refuses without the Mac flag ---
os.environ.pop("GENERAL_COCKPIT_PROMOTE", None)
chk("ship refused off-Mac (no flag)", not sync.promote_app(app)["ok"] and "disabled" in (sync.promote_app(app)["error"] or ""))
os.environ["GENERAL_COCKPIT_PROMOTE"] = "1"

# --- the cockpit Ship button renders (Mac, app ahead) ---
bar = server._control_bar(cfg, "automatixy", True)
chk("Ship button present for the current app", "/ship-preview?app=automatixy" in bar and "Ship automatixy" in bar, "")
chk("Ship button shows the ahead count", "<span class=cbadge>2</span>" in bar)

# --- ship: ff MAIN to DEV, push, return to DEV ---
r = sync.promote_app(app)
chk("ship ok + pushed (2 commits)", r["ok"] and r["pushed"] and r["ahead_before"] == 2, str(r))
chk("tree returned to DEV", git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "DEV")
chk("DEV no longer ahead of MAIN", sync.app_promote_status(app)["ahead"] == 0, str(sync.app_promote_status(app)))
git(repo, "fetch", "origin", "MAIN")
chk("origin/MAIN fast-forwarded to DEV", git(repo, "rev-parse", "origin/MAIN").stdout.strip() == git(repo, "rev-parse", "DEV").stdout.strip())

# --- idempotent: nothing ahead -> clean no-op ---
chk("ship no-op when already shipped", sync.promote_app(app)["ok"] and sync.promote_app(app)["ahead_before"] == 0)

print("\n================ SHIP → MAIN (APP) QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
