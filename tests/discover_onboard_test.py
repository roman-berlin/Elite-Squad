"""Runnable discovered repos: a git repo found beside a configured one shows up on /onboard as a
click-to-onboard chip, and clicking it pre-fills the form (name + path + auto-detected branches)."""
import sys, types, tempfile, subprocess, shutil, os
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import projects, server
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

_HAVE_GIT = shutil.which("git") is not None

def _repo(path, branches=("main", "dev")):
    path.mkdir(parents=True, exist_ok=True)
    if not _HAVE_GIT:
        (path / ".git").mkdir()
        return
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], capture_output=True, text=True, env=env)
    subprocess.run(["git", "init", str(path)], capture_output=True, text=True, env=env)
    run("config", "user.email", "t@t.dev"); run("config", "user.name", "t")
    run("checkout", "-b", branches[0]); run("commit", "--allow-empty", "-m", "init")
    for b in branches[1:]:
        run("branch", b)

# a parent dir with a CONFIGURED repo + an UNCONFIGURED one beside it
parent = Path(tempfile.mkdtemp()) / "Projects"
_repo(parent / "automatixy")
_repo(parent / "signaldesk", branches=("main", "dev"))   # the nearby repo to discover

scfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(parent / "automatixy"),
                              base_branch="main", protected_branch="dev", backlog_backend="none")],
              audit_path=str(Path(tempfile.mkdtemp()) / "audit.jsonl"), use_worktree=False)
scfg.detected_auth = lambda: "test"
scfg._source_path = str(Path(tempfile.mkdtemp()) / "config.yaml")

# discovery itself finds the nearby repo and excludes the configured one
disc = projects.discover_repos(scfg)
names = {r["name"] for r in disc}
chk("discover: finds the nearby repo", "signaldesk" in names, str(names))
chk("discover: excludes the configured repo", "automatixy" not in names)

client = server.create_app(scfg).test_client()

# /onboard lists it as a click-to-onboard chip pointing back into the form
r = client.get("/onboard"); body = r.get_data(as_text=True)
chk("/onboard shows Found nearby", "Found nearby" in body)
chk("/onboard chip links to a pre-fill URL", "/onboard?name=signaldesk&repo=" in body)
chk("/onboard chip shows the repo name", "signaldesk" in body)

# clicking the chip pre-fills name + repo_path (+ detected branches if git is present)
r2 = client.get("/onboard?name=signaldesk&repo=" + str(parent / "signaldesk"))
b2 = r2.get_data(as_text=True)
chk("pre-fill: product name filled", "value='signaldesk'" in b2)
chk("pre-fill: repo path filled", f"value='{parent / 'signaldesk'}'" in b2)
if _HAVE_GIT:
    chk("pre-fill: base branch auto-detected (dev)", "value='dev'" in b2)
    chk("pre-fill: protected branch auto-detected (main)", "value='main'" in b2)
else:
    chk("pre-fill: base branch auto-detected (dev) [skipped: no git]", True)
    chk("pre-fill: protected branch auto-detected (main) [skipped: no git]", True)

# and the end-to-end onboard still works from the pre-filled values
Path(scfg._source_path).write_text("apps:\n  - name: automatixy\n    repo_path: /x\n    backlog_backend: none\n", encoding="utf-8")
client.post("/api/onboard", data={"name": "signaldesk", "repo_path": str(parent / "signaldesk"),
                                  "base": "dev", "protected": "main", "jira": ""})
chk("onboard from a discovered repo writes the entry",
    "name: signaldesk" in Path(scfg._source_path).read_text())

print("\n========== DISCOVERED-REPO ONBOARD QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
