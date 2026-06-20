"""New-product onboarding QA: detect branches from a repo, preview vs write, dup-guard + non-git errors,
a SURGICAL insert under apps: that preserves comments, optional Jira-connection attach, and the cockpit
/onboard page + endpoint."""
import sys, types, tempfile, subprocess, shutil, os
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
class _Sess:
    def __init__(s): s.auth = None; s.headers = types.SimpleNamespace(update=lambda *a, **k: None)
req.Session = lambda: _Sess()
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import onboarding, connections

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

_HAVE_GIT = shutil.which("git") is not None

def _repo(branches, default=None):
    """Make a throwaway git repo with the given branches; returns its path (or a fake-.git dir if no git)."""
    d = Path(tempfile.mkdtemp(prefix="obrepo-"))
    if not _HAVE_GIT:
        (d / ".git").mkdir()
        return d
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    run = lambda *a: subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, env=env)
    subprocess.run(["git", "init", str(d)], capture_output=True, text=True, env=env)
    run("config", "user.email", "t@t.dev"); run("config", "user.name", "t")
    first = default or branches[0]
    run("checkout", "-b", first)
    run("commit", "--allow-empty", "-m", "init")
    for b in branches:
        if b != first:
            run("branch", b)
    return d

# --- git repo detection + branch suggestion ---
repo = _repo(["main", "dev"], default="main")
chk("is_git_repo true for a real repo", onboarding.is_git_repo(repo))
chk("is_git_repo false for a plain dir", not onboarding.is_git_repo(Path(tempfile.mkdtemp())))
if _HAVE_GIT:
    base, prot = onboarding.suggest_branches(repo)
    chk("suggest: base = dev (found)", base == "dev", base)
    chk("suggest: protected = main (found)", prot == "main", prot)
    chk("suggest: base != protected", base != prot)
    repo_caps = _repo(["MAIN", "DEV"], default="MAIN")
    b2, p2 = onboarding.suggest_branches(repo_caps)
    chk("suggest: honors DEV/MAIN casing (Automatixy-style)", b2 == "DEV" and p2 == "MAIN", f"{b2}/{p2}")
else:
    for n in ("suggest: base = dev (found)", "suggest: protected = main (found)", "suggest: base != protected",
              "suggest: honors DEV/MAIN casing (Automatixy-style)"):
        chk(n + " [skipped: no git]", True)

# --- build_block shape ---
blk = onboarding.build_block(name="signaldesk", repo_path="/x/SignalDesk", base="dev", protected="main")
chk("block: 2-space sequence indent", blk.startswith("  - name: signaldesk"))
chk("block: backlog none -> empty backlog", "backlog_backend: \"none\"" in blk and "backlog: {}" in blk)
blkj = onboarding.build_block(name="x", repo_path="/x", base="dev", protected="main", backlog="jira")
chk("block: backlog jira -> ready_status/label scaffold", "backlog_backend: \"jira\"" in blkj and "ready_status" in blkj)

# --- a config.yaml with a comment + one app ---
cfgp = Path(tempfile.mkdtemp()) / "config.yaml"
cfgp.write_text(
    'builder_model: "claude-opus-4-8"\n'
    "# KEEP THIS COMMENT — onboarding must not rewrite the file\n"
    "apps:\n"
    "  - name: automatixy\n"
    '    repo_path: "/tmp/auto"\n'
    '    base_branch: "DEV"\n'
    '    protected_branch: "MAIN"\n'
    '    backlog_backend: "none"\n'
    "    backlog: {}\n"
    'audit_path: "./audit.jsonl"\n', encoding="utf-8")
chk("app_names reads existing apps", onboarding.app_names(cfgp) == ["automatixy"])

# --- preview (write=False) doesn't touch disk ---
before = cfgp.read_text()
pv = onboarding.scaffold(cfgp, name="signaldesk", repo_path=str(repo))
chk("preview: ok", pv["ok"])
chk("preview: returns the block", pv["block"].startswith("  - name: signaldesk"))
chk("preview: NOT written", pv["written"] is False and cfgp.read_text() == before)

# --- errors: non-git, missing name, dup, base==protected ---
chk("error: not a git repo", onboarding.scaffold(cfgp, name="x", repo_path=tempfile.mkdtemp())["ok"] is False)
chk("error: missing name", onboarding.scaffold(cfgp, name="", repo_path=str(repo))["ok"] is False)
chk("error: duplicate name", onboarding.scaffold(cfgp, name="automatixy", repo_path=str(repo))["ok"] is False)
chk("error: base == protected", onboarding.scaffold(cfgp, name="x", repo_path=str(repo),
                                                    base="dev", protected="dev")["ok"] is False)

# --- write inserts under apps:, preserves the comment, makes a .bak ---
connections._file = lambda cfg=None: Path(tempfile.mkdtemp()) / "jc.json"   # isolate the connection store
wr = onboarding.scaffold(cfgp, name="signaldesk", repo_path=str(repo), base="dev", protected="main", write=True)
after = cfgp.read_text()
chk("write: ok + written", wr["ok"] and wr["written"])
chk("write: comment preserved", "KEEP THIS COMMENT" in after)
chk("write: automatixy still there", "name: automatixy" in after)
chk("write: signaldesk added under apps", "name: signaldesk" in after and onboarding.app_names(cfgp) == ["signaldesk", "automatixy"])
chk("write: backup created", cfgp.with_name("config.yaml.bak").exists())
chk("write: backup is the pre-write content", cfgp.with_name("config.yaml.bak").read_text() == before)

# --- Jira attach assigns the connection to the new project ---
cfgp2 = Path(tempfile.mkdtemp()) / "config.yaml"
cfgp2.write_text("apps:\n  - name: a\n    repo_path: /tmp/a\n    backlog_backend: none\n", encoding="utf-8")
store = Path(tempfile.mkdtemp()) / "jc.json"
connections._file = lambda cfg=None: store
cid = connections.add(None, name="SignalDesk Jira", base_url="https://sd.atlassian.net", email="me@sd.com", token="TOK12345")
wj = onboarding.scaffold(cfgp2, name="signaldesk", repo_path=str(repo), base="dev", protected="main",
                         backlog="jira", connection_id=cid, write=True)
chk("jira attach: ok, backlog=jira", wj["ok"] and wj["backlog"] == "jira")
chk("jira attach: connection assigned to the new project", connections.assigned_id(None, "signaldesk") == cid)
chk("jira attach: adapter would resolve it", (connections.for_app("signaldesk") or {}).get("token") == "TOK12345")

# --- cockpit /onboard page + endpoint ---
from orchestrator import server
from orchestrator.config import Config, AppConfig
scfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(repo), base_branch="dev",
                              protected_branch="main", backlog_backend="none")],
              audit_path=str(Path(tempfile.mkdtemp()) / "audit.jsonl"), use_worktree=False)
scfg.detected_auth = lambda: "test"
cfg_for_cockpit = Path(tempfile.mkdtemp()) / "config.yaml"
cfg_for_cockpit.write_text("apps:\n  - name: automatixy\n    repo_path: /tmp/a\n    backlog_backend: none\n", encoding="utf-8")
scfg._source_path = str(cfg_for_cockpit)
client = server.create_app(scfg).test_client()

r = client.get("/onboard"); body = r.get_data(as_text=True)
chk("/onboard returns 200", r.status_code == 200, str(r.status_code))
chk("/onboard has the product form", "Product name" in body and "Repo path" in body and "Add product" in body)
chk("/onboard lists current products", "automatixy" in body)
chk("/onboard offers saved Jira connections", "SignalDesk Jira" in body)

newrepo = _repo(["main", "dev"], default="main")
r2 = client.post("/api/onboard", data={"name": "robot", "repo_path": str(newrepo), "base": "dev",
                                       "protected": "main", "jira": ""})
ok_body = r2.get_data(as_text=True)
chk("/api/onboard succeeds", r2.status_code == 200 and "Added" in ok_body)
chk("/api/onboard wrote robot into the cockpit's config", "name: robot" in cfg_for_cockpit.read_text())
# duplicate via the endpoint -> friendly error, no crash
r3 = client.post("/api/onboard", data={"name": "robot", "repo_path": str(newrepo)})
chk("/api/onboard duplicate -> error shown", "already in config" in r3.get_data(as_text=True))

print("\n=============== ONBOARDING QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
