"""Mac<->server state sync — real git round-trip over a bare origin.

Proves: a 'mac' clone publishes its audit to the orphan unit-state branch; a 'server' clone pulls it
and its cockpit view (dashboard.audit_lines / load_tasks) now includes the Mac's builds; a host's own
events are de-duplicated; and a repo with no origin fails soft instead of crashing.
"""
import os, sys, tempfile, subprocess, types
from pathlib import Path

# stub the SDK so importing orchestrator.* is cheap + offline
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import sync, dashboard
from orchestrator.config import Config, AppConfig

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)

tmp = Path(tempfile.mkdtemp())
origin = tmp / "origin.git"
subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], capture_output=True)

# seed origin with one commit so it has a default branch to clone
git(tmp, "clone", str(origin), "seed")
seed = tmp / "seed"
git(seed, "config", "user.email", "t@t"); git(seed, "config", "user.name", "t")
(seed / "README.md").write_text("seed")
git(seed, "add", "."); git(seed, "commit", "-m", "init"); git(seed, "push", "origin", "HEAD:main")

def mkcfg(repo: Path) -> Config:
    return Config(
        apps=[AppConfig(name="automatixy", repo_path=str(repo), base_branch="DEV",
                        protected_branch="MAIN", backlog_backend="none")],
        audit_path=str(repo / "audit.jsonl"), use_worktree=False)

# ---- MAC: two build events, first sync (bootstraps the orphan unit-state branch) ----
git(tmp, "clone", str(origin), "mac")
mac = tmp / "mac"
(mac / "audit.jsonl").write_text(
    '{"event":"ticket_start","ticket_id":"AUTO-1","ts":"2026-06-18T10:00:00"}\n'
    '{"event":"merged","ticket_id":"AUTO-1","ts":"2026-06-18T10:05:00"}\n')
os.environ["GENERAL_HOST_ID"] = "mac"
cfg_mac = mkcfg(mac)

# SANDBOX GUARD (2026-07-05): this harness runs the REAL git_sync — fetch, publish, commit, PUSH.
# A brief _repo_root regression that day resolved every cfg to the ACTUAL repo root, so a failing
# run of this test overwrote and pushed fixture data over the real shared .unit-state channel.
# Abort loudly BEFORE any sync if the state dir would land outside our tmp sandbox — a broken
# path derivation must fail this harness, never touch the live channel.
_resolved_sd = sync.state_dir(cfg_mac).resolve()
assert str(_resolved_sd).startswith(str(tmp.resolve())), (
    f"REFUSING TO SYNC: state_dir resolves OUTSIDE the tmp sandbox ({_resolved_sd}) — "
    "a _repo_root/state_dir regression would corrupt the real .unit-state channel.")

r1 = sync.git_sync(cfg_mac)
check("mac sync bootstraps state clone + pushes", r1["pushed"] and (mac / ".unit-state/.git").exists(), str(r1))
check("mac published shared/mac.jsonl", (mac / ".unit-state/shared/mac.jsonl").exists())

# ---- SERVER: one council event, first sync (pulls the Mac's published audit) ----
git(tmp, "clone", str(origin), "server")
server = tmp / "server"
(server / "audit.jsonl").write_text('{"event":"council","ts":"2026-06-18T06:30:00"}\n')
os.environ["GENERAL_HOST_ID"] = "server"
cfg_srv = mkcfg(server)
r2 = sync.git_sync(cfg_srv)
check("server pulled unit-state (sees mac.jsonl)", r2["pulled"] and (server / ".unit-state/shared/mac.jsonl").exists(), str(r2))
check("server sync reports both hosts", set(r2["hosts"]) >= {"mac", "server"}, str(r2.get("hosts")))

# ---- the payoff: the SERVER's cockpit view now includes the Mac's builds ----
joined = "\n".join(dashboard.audit_lines(cfg_srv.audit_path))
check("server view includes mac's AUTO-1 build", "AUTO-1" in joined)
check("server view keeps its own council", '"event": "council"' in joined or '"event":"council"' in joined)
ids = {t["ticket_id"] for t in dashboard.load_tasks(cfg_srv.audit_path)}
check("load_tasks on server surfaces mac's ticket", "AUTO-1" in ids, str(ids))

# ---- dedup: mac re-syncs (pulls server.jsonl); its own events stay single ----
os.environ["GENERAL_HOST_ID"] = "mac"
r3 = sync.git_sync(cfg_mac)
check("mac re-sync pulls server.jsonl", "server" in r3["hosts"], str(r3.get("hosts")))
mac_lines = dashboard.audit_lines(cfg_mac.audit_path)
dup = sum(1 for L in mac_lines if "AUTO-1" in L and "ticket_start" in L)
check("mac's own start event is de-duplicated (appears once)", dup == 1, f"count={dup}")
check("mac view now includes the server council", any("council" in L for L in mac_lines))

# ---- best-effort: no origin -> fails soft, audit_lines still works locally ----
noremote = tmp / "noremote"
subprocess.run(["git", "init", "-b", "main", str(noremote)], capture_output=True)
(noremote / "audit.jsonl").write_text('{"event":"x","ts":"2026-06-18T01:00:00"}\n')
os.environ["GENERAL_HOST_ID"] = "lonely"
r4 = sync.git_sync(mkcfg(noremote))
check("no-origin sync fails soft (error set, no push, no crash)", bool(r4["error"]) and not r4["pushed"], str(r4))
check("audit_lines works with no state clone", len(dashboard.audit_lines(str(noremote / "audit.jsonl"))) == 1)

# ---- pull-only consumer (read-only box like the real VPS): pulls peers, never pushes ----
git(tmp, "clone", str(origin), "reader")
reader = tmp / "reader"
(reader / "audit.jsonl").write_text('{"event":"council","ts":"2026-06-18T07:00:00"}\n')
os.environ["GENERAL_HOST_ID"] = "reader"
os.environ["GENERAL_SYNC_PULL_ONLY"] = "1"
r5 = sync.git_sync(mkcfg(reader))
os.environ.pop("GENERAL_SYNC_PULL_ONLY")
check("pull-only: pulled peers, push not attempted (no error)", r5["pulled"] and r5["pushed"] is None and not r5["error"], str(r5))
check("pull-only: sees mac WITHOUT publishing its own file", "mac" in r5["hosts"] and not (reader / ".unit-state/shared/reader.jsonl").exists(), str(r5.get("hosts")))
check("pull-only: reader's cockpit view still includes mac's build", "AUTO-1" in "\n".join(dashboard.audit_lines(str(reader / "audit.jsonl"))))

print("\n================ MAC<->SERVER STATE SYNC QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
