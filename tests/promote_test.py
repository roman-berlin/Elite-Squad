"""Promote QA: dev->main (and app DEV->MAIN) promote on the REMOTE without touching the working tree —
a DIRTY tree (the unit's runtime files) must never block a deploy, ff-only, never forced. Regression
test for the 'Update unit stuck spinner' caused by the old checkout-based promote choking on a dirty tree."""
import sys, types, tempfile, subprocess, os
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
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

def sh(cwd, *a): subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)
def sha(cwd, ref): return subprocess.run(["git", "rev-parse", ref], cwd=cwd, capture_output=True, text=True).stdout.strip()

def make_repo(devname, mainname):
    root = Path(tempfile.mkdtemp())
    origin = root / "o.git"; subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    work = root / "w"; subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    sh(work, "config", "user.email", "t@t"); sh(work, "config", "user.name", "t")
    sh(work, "checkout", "-b", mainname)
    (work / "base.txt").write_text("base\n"); sh(work, "add", "-A"); sh(work, "commit", "-m", "baseline")
    sh(work, "push", "-u", "origin", mainname)
    sh(work, "checkout", "-b", devname)
    (work / "f.txt").write_text("feature\n"); sh(work, "add", "-A"); sh(work, "commit", "-m", "AUTO-9: ship it")
    sh(work, "push", "-u", "origin", devname)
    # make the working tree DIRTY (a tracked file the unit would keep rewriting)
    (work / "base.txt").write_text("base + runtime churn\n")
    return work

# --- promote() : dev -> main, dirty tree present ---
work = make_repo("dev", "main")
cfg = Config(apps=[], audit_path=str(work / "audit.jsonl"))
chk("setup: tree is dirty + dev ahead of main",
    subprocess.run(["git", "status", "--porcelain"], cwd=work, capture_output=True, text=True).stdout.strip() != ""
    and sync.promote_status(cfg)["ahead"] == 1)
dev_sha = sha(work, "dev")
r = sync.promote(cfg)
chk("promote ok despite dirty tree (the stuck-spinner bug)", r.get("ok"), str(r))
chk("origin/main advanced to dev", sha(work, "origin/main") == dev_sha)
chk("local main ref advanced too", sha(work, "main") == dev_sha)
chk("HEAD never left dev (no checkout)", subprocess.run(["git","rev-parse","--abbrev-ref","HEAD"],cwd=work,capture_output=True,text=True).stdout.strip() == "dev")
chk("working tree still dirty (untouched by promote)",
    subprocess.run(["git","status","--porcelain"],cwd=work,capture_output=True,text=True).stdout.strip() != "")
chk("nothing left to promote afterwards", sync.promote_status(cfg)["ahead"] == 0)

# --- promote() : already in sync -> ok, no-op ---
r2 = sync.promote(cfg)
chk("re-promote when in sync -> ok, nothing to do", r2.get("ok") and r2.get("ahead_before") == 0)

# --- promote() : diverged -> rejected, not forced ---
work2 = make_repo("dev", "main")
# add a commit to origin/main that dev doesn't have (divergence)
sh(work2, "checkout", "main"); (work2/"x").write_text("only-on-main"); sh(work2,"add","-A"); sh(work2,"commit","-m","hotfix on main"); sh(work2,"push","origin","main"); sh(work2,"checkout","dev")
cfg2 = Config(apps=[], audit_path=str(work2 / "audit.jsonl"))
rd = sync.promote(cfg2)
chk("diverged -> NOT ok (push rejected, never forced)", not rd.get("ok"))
chk("diverged -> clear error", "diverg" in (rd.get("error") or "").lower(), rd.get("error"))

# --- promote_app() : app DEV -> MAIN, dirty tree ---
appwork = make_repo("DEV", "MAIN")
app = AppConfig(name="automatixy", repo_path=str(appwork), base_branch="DEV", protected_branch="MAIN",
                backlog_backend="none")
adev = sha(appwork, "DEV")
ra = sync.promote_app(app)
chk("promote_app ok despite dirty tree", ra.get("ok"), str(ra))
chk("app origin/MAIN advanced to DEV", sha(appwork, "origin/MAIN") == adev)
chk("app HEAD stayed on DEV", subprocess.run(["git","rev-parse","--abbrev-ref","HEAD"],cwd=appwork,capture_output=True,text=True).stdout.strip() == "DEV")

print("\n================== PROMOTE QA ==================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
