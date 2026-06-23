"""Project switcher: Recent projects (record/dedup/cap/filter) + nearby-repo discovery + switcher groups."""
import sys, types, tempfile
from pathlib import Path
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import projects, warroom
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
# a configured app whose repo lives in tmp (so discovery scans tmp)
(tmp / "automatixy" / ".git").mkdir(parents=True)
(tmp / "zelmero" / ".git").mkdir(parents=True)
cfg = Config(apps=[
    AppConfig(name="automatixy", repo_path=str(tmp / "automatixy"), base_branch="DEV", protected_branch="MAIN", backlog_backend="none"),
    AppConfig(name="zelmero", repo_path=str(tmp / "zelmero"), base_branch="DEV", protected_branch="MAIN", backlog_backend="none"),
], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)

# --- recents: record / move-to-front / dedup ---
chk("recents empty initially", projects.recents(cfg) == [])
projects.record_recent(cfg, "automatixy")
projects.record_recent(cfg, "zelmero")
projects.record_recent(cfg, "automatixy")   # re-open -> back to front
chk("recents newest-first, deduped", projects.recents(cfg) == ["automatixy", "zelmero"], str(projects.recents(cfg)))
projects.record_recent(cfg, "*")            # all-projects view -> ignored
projects.record_recent(cfg, "ghost")        # unknown app -> ignored
chk("recents ignores '*' and unknown apps", projects.recents(cfg) == ["automatixy", "zelmero"], str(projects.recents(cfg)))

# --- recents filtered to apps that still exist ---
cfg2 = Config(apps=[cfg.apps[0]], audit_path=cfg.audit_path, use_worktree=False)   # drop zelmero
chk("recents filtered to existing apps", projects.recents(cfg2) == ["automatixy"], str(projects.recents(cfg2)))

# --- discovery: nearby git repos that aren't configured ---
(tmp / "repoA" / ".git").mkdir(parents=True)
(tmp / "repoB" / ".git").mkdir(parents=True)
(tmp / "plaindir").mkdir()                   # not a git repo -> ignored
disc = projects.discover_repos(cfg, parents=[tmp])
names = {r["name"] for r in disc}
chk("discovery finds nearby git repos", {"repoA", "repoB"} <= names, str(names))
chk("discovery excludes configured repos", "automatixy" not in names and "zelmero" not in names)
chk("discovery excludes non-git dirs", "plaindir" not in names)

# --- switcher shows Recent + Projects (+ discovered) ---
selhtml = warroom.project_selector(cfg, "automatixy")
chk("switcher has a Recent group", "Recent" in selhtml and "&#9733;" in selhtml)
chk("switcher lists the configured projects", "automatixy" in selhtml and "zelmero" in selhtml)
chk("switcher surfaces nearby repos", "Found nearby" in selhtml and "repoA" in selhtml, "")
# EU-34: a disabled <option> can't navigate, so the dropdown must not promise "click to onboard".
chk("switcher drops the dead click-to-onboard promise", "click to onboard" not in selhtml.lower(),
    selhtml)

print("\n================ PROJECT SWITCHER QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
