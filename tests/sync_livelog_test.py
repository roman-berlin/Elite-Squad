"""Server->Mac living-log bridge: Mac pulls the server's UNIT.live.md over SSH (server stays read-only)."""
import sys, os, types, tempfile
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

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)

# 1) no server host -> not attempted (stand-alone Mac)
os.environ.pop("GENERAL_SERVER_SSH", None); os.environ.pop("GENERAL_SYNC_PULL_ONLY", None)
chk("no GENERAL_SERVER_SSH -> not attempted", not sync.pull_server_state(cfg)["attempted"])

# 2) the server never SSHes to itself
os.environ["GENERAL_SERVER_SSH"] = "ubuntu@host"; os.environ["GENERAL_SYNC_PULL_ONLY"] = "1"
chk("server (pull-only) does not pull from itself", not sync.pull_server_state(cfg)["attempted"])
os.environ.pop("GENERAL_SYNC_PULL_ONLY")

# 3) host set -> scp the server's live log down, to the Mac's memory/UNIT.live.md
captured = {}
class FakeProc:
    def __init__(s, rc, err=""): s.returncode = rc; s.stderr = err; s.stdout = ""
def fake_ok(args, **kw):
    captured["args"] = args
    return FakeProc(0)
sync.subprocess.run = fake_ok
os.environ["GENERAL_SERVER_REPO"] = "General"
r = sync.pull_server_state(cfg)
chk("pull attempted + ok", r["attempted"] and r["pulled"], str(r))
chk("scp source = server's UNIT.live.md", "ubuntu@host:General/memory/UNIT.live.md" in captured["args"], str(captured.get("args")))
chk("scp dest = Mac's memory/UNIT.live.md", any(a.endswith("/memory/UNIT.live.md") and "ubuntu@host" not in a for a in captured["args"]))
chk("scp uses BatchMode (no interactive password)", "BatchMode=yes" in captured["args"])

# 4) ssh/scp failure -> reported, never fatal
def fake_fail(args, **kw):
    return FakeProc(1, "Permission denied (publickey).")
sync.subprocess.run = fake_fail
r = sync.pull_server_state(cfg)
chk("scp failure -> pulled False + error (best-effort)", r["attempted"] and not r["pulled"] and "Permission denied" in (r["error"] or ""), str(r))

print("\n================ SERVER->MAC LIVING-LOG BRIDGE QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
