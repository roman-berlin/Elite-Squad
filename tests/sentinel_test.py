"""Sentinel QA: gating, post-merge guard (pass = keep / red = revert + hand back / revert-fail = needs
you), and a REAL git revert round-trip proving a landed --no-ff merge is rolled back forward-only."""
import sys, types, tempfile, subprocess
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import sentinel, gate
from orchestrator.config import Config
from orchestrator.contracts import GateResult
from orchestrator.git_ops import Git

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace
sentinel.notify.send = lambda *a, **k: None   # silence Telegram

class Audit:
    def __init__(s): s.events = []
    def record(s, kind, **kw): s.events.append((kind, kw))

def app(cmds):
    return ns(name="automatixy", base_branch="DEV", postmerge_commands=list(cmds),
              workdir="/tmp", repo_path="/tmp", gate_timeout_sec=30, gate_env={})

on = Config(apps=[], audit_path="/tmp/x.jsonl", sentinel_enabled=True)
off = Config(apps=[], audit_path="/tmp/x.jsonl", sentinel_enabled=False)

# --- gating ---
chk("should_run OFF when sentinel disabled", not sentinel.should_run(off, app(["echo ok"])))
chk("should_run OFF when no post-merge suite", not sentinel.should_run(on, app([])))
chk("should_run ON when enabled + post-merge suite", sentinel.should_run(on, app(["echo ok"])))

class FakeGit:
    def __init__(s): s.reverted = None
    def revert_merge_on_base(s, sha): s.reverted = sha; return True

# --- guard PASS -> keep, no revert ---
sentinel.gate.run_commands = lambda a, c: GateResult(passed=True, report="green")
g, au = FakeGit(), Audit()
ok, note = sentinel.guard(on, app(["echo ok"]), ns(id="AUTO-1", ephemeral=False), g, "sha_pass", au)
chk("guard pass -> healthy True", ok)
chk("guard pass -> NO revert", g.reverted is None)
chk("guard pass -> audit sentinel_pass", any(k == "sentinel_pass" for k, _ in au.events))

# --- guard RED -> revert the exact merge sha + hand back ---
sentinel.gate.run_commands = lambda a, c: GateResult(passed=False, report="FAIL: integration tests broke")
g2, au2 = FakeGit(), Audit()
ok2, note2 = sentinel.guard(on, app(["bun run e2e"]), ns(id="AUTO-2", ephemeral=False), g2, "sha_red", au2)
chk("guard red -> healthy False", not ok2)
chk("guard red -> reverts the SAME merge sha", g2.reverted == "sha_red", str(g2.reverted))
chk("guard red -> audit sentinel_revert", any(k == "sentinel_revert" for k, _ in au2.events))
chk("guard red -> note says reverted", "revert" in note2.lower())

# --- guard RED + revert FAILS -> needs you ---
class FailGit:
    def revert_merge_on_base(s, sha): return False
ok3, note3 = sentinel.guard(on, app(["x"]), ns(id="AUTO-3", ephemeral=False), FailGit(), "s", Audit())
chk("revert-fail -> False + manual rollback note", not ok3 and "manual" in note3.lower())

# --- a runner that throws is treated as red (still rolls back) ---
def _boom(a, c): raise RuntimeError("runner exploded")
sentinel.gate.run_commands = _boom
g4 = FakeGit()
ok4, _ = sentinel.guard(on, app(["x"]), ns(id="AUTO-4", ephemeral=False), g4, "sha_boom", Audit())
chk("runner exception -> treated as red, reverts", (not ok4) and g4.reverted == "sha_boom")

# --- guard RED due to misconfigured (missing script) -> no revert, log misconfigured ---
# EU-359: fed the block shape gate.run_commands really emits ("$ <cmd>\n(exit <code>)\n<tail>").
# The old synthetic free-text report was a shape the runner never produces, and matching it anywhere
# in the body is what let genuine reds through as "misconfigured".
sentinel.gate.run_commands = lambda a, c: GateResult(
    passed=False, report="$ bun run two_tenant_smoke\n(exit 127)\nsh: 1: two_tenant_smoke.py: not found")
g5, au5 = FakeGit(), Audit()
ok5, note5 = sentinel.guard(on, app(["bun run two_tenant_smoke"]), ns(id="AUTO-5", ephemeral=False), g5, "sha_misconfigured", au5)
chk("misconfigured (missing script) -> healthy True", ok5)
chk("misconfigured (missing script) -> NO revert", g5.reverted is None)
chk("misconfigured (missing script) -> audit contains misconfigured", any("misconfigured" in str(v) for _, ev in au5.events for v in ev.values()))
chk("misconfigured (missing script) -> note says misconfigured", "misconfigured" in note5.lower())

# --- missing env is caught BEFORE the suite runs -> no revert, log misconfigured ---
# EU-359 moved this protection earlier: the requirement is derived from the app's own command
# ($SENTINEL_ABSENT_EMAIL), so the gate never runs at all rather than being reverse-engineered from
# whatever the script printed.
sentinel.gate.run_commands = lambda a, c: GateResult(passed=True, report="should not run")
g6, au6 = FakeGit(), Audit()
ok6, note6 = sentinel.guard(on, app(["./two_tenant_smoke.py --email $SENTINEL_ABSENT_EMAIL"]),
                            ns(id="AUTO-6", ephemeral=False), g6, "sha_env_missing", au6)
chk("misconfigured (missing env) -> healthy True", ok6)
chk("misconfigured (missing env) -> NO revert", g6.reverted is None)
chk("misconfigured (missing env) -> audit contains misconfigured", any("missing env" in str(v) for _, ev in au6.events for v in ev.values()))
chk("misconfigured (missing env) -> note says misconfigured", "misconfigured" in note6.lower())

# --- EU-359: a suite that RAN and printed env/file words is a genuine RED -> revert ---
# The 2026-07-16 audit's headline false-skip: a real product regression whose output happens to
# contain "not set" / "No such file or directory" must never be waved onto DEV.
sentinel.gate.run_commands = lambda a, c: GateResult(
    passed=False, report="$ bun run two_tenant_smoke\n(exit 1)\nERROR: TENANT_A_EMAIL not set. TENANT_B_EMAIL not set.")
g7, au7 = FakeGit(), Audit()
ok7, note7 = sentinel.guard(on, app(["bun run two_tenant_smoke"]), ns(id="AUTO-7", ephemeral=False), g7, "sha_ran_red", au7)
chk("suite ran + env text in output -> healthy False", not ok7)
chk("suite ran + env text in output -> reverts", g7.reverted == "sha_ran_red", str(g7.reverted))
chk("suite ran + env text in output -> not called misconfigured", "misconfigured" not in note7.lower())

# --- REAL git: a landed --no-ff merge is reverted forward-only and pushed ---
tmp = Path(tempfile.mkdtemp())
def G(cwd, *a): subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)
origin = tmp / "origin.git"; subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
work = tmp / "work"; subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
G(work, "config", "user.email", "t@t"); G(work, "config", "user.name", "t")
G(work, "checkout", "-b", "dev")
(work / "base.txt").write_text("base\n"); G(work, "add", "-A"); G(work, "commit", "-m", "base")
G(work, "push", "-u", "origin", "dev")
G(work, "checkout", "-b", "feat")
(work / "feature.txt").write_text("risky feature\n"); G(work, "add", "-A"); G(work, "commit", "-m", "feat")
G(work, "checkout", "dev")
G(work, "merge", "--no-ff", "-m", "Merge feat into dev (AUTO-9)", "feat")
merge_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True).stdout.strip()
G(work, "push", "origin", "dev")
chk("setup: feature landed on dev", (work / "feature.txt").exists())

g_real = Git(str(work), "dev", "main")   # in-tree mode (no worktree)
reverted = g_real.revert_merge_on_base(merge_sha)
chk("real revert returns True", reverted)
G(work, "checkout", "dev")
subprocess.run(["git", "pull", "--ff-only"], cwd=work, capture_output=True)
chk("real revert removed the feature file from dev", not (work / "feature.txt").exists())
chk("real revert kept base.txt", (work / "base.txt").exists())
olog = subprocess.run(["git", "log", "--oneline", "origin/dev"], cwd=work, capture_output=True, text=True).stdout
chk("revert commit pushed to origin/dev (forward-only)", "Revert" in olog)
chk("merge commit still in history (no force-rewrite)", "AUTO-9" in olog)

print("\n================= SENTINEL QA =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
