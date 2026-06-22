"""EU-8 (F3) regression: the build pipeline must actually CATCH a runtime-broken change, not just
typecheck it. Exercises the REAL (un-mocked) gate + Sentinel with bounded subprocesses and proves:

  • a deliberately runtime-broken change fails the pre-land gate (run_gate), AND
  • if it slips past the gate, an armed Sentinel reverts it post-merge and hands the ticket back, AND
  • both honour the RAM cap (gate_env passthrough) and gate_timeout_sec (no machine freeze).
"""
import sys, types
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import sentinel, gate
from orchestrator.config import AppConfig, Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace
sentinel.notify.send = lambda *a, **k: None   # silence Telegram

class Audit:
    def __init__(s): s.events = []
    def record(s, kind, **kw): s.events.append((kind, kw))

class FakeGit:
    def __init__(s): s.reverted = None
    def revert_merge_on_base(s, sha): s.reverted = sha; return True

def app(cmds=None, post=None, timeout=30, env=None):
    return AppConfig(name="automatixy", repo_path="/tmp", base_branch="DEV", workdir="/tmp",
                     gate_commands=list(cmds or []), postmerge_commands=list(post or []),
                     gate_timeout_sec=timeout, gate_env=dict(env or {}))

cfg = Config(apps=[], audit_path="/tmp/x.jsonl")

# --- the framework is ARMED by default (F3 fix) — no per-deploy flag needed, just a suite ---
chk("sentinel_enabled defaults to True (armed)", cfg.sentinel_enabled is True)
chk("armed + suite -> Sentinel runs", sentinel.should_run(cfg, app(post=["true"])))
chk("armed but NO suite -> still a no-op", not sentinel.should_run(cfg, app(post=[])))

# --- ACCEPTANCE path A: a deliberately runtime-broken change FAILS the pre-land gate ---
broken = gate.run_gate(app(cmds=["sh -c 'echo boom; exit 7'"]))
chk("runtime-broken gate FAILS pre-land", not broken.passed)
chk("gate report carries the failure", "exit 7" in (broken.report or ""))
chk("clean gate passes", gate.run_gate(app(cmds=["true"])).passed)
chk("empty gate is a pass (unchanged)", gate.run_gate(app(cmds=[])).passed)

# --- ACCEPTANCE path B: if it slips the gate, an armed Sentinel reverts it post-merge ---
g, au = FakeGit(), Audit()
ok, note = sentinel.guard(cfg, app(post=["sh -c 'exit 1'"]),
                          ns(id="AUTO-8", ephemeral=False), g, "sha_broken", au)
chk("real runtime-broken post-merge suite -> guard False", not ok)
chk("real red -> reverts the exact merge sha", g.reverted == "sha_broken", str(g.reverted))
chk("real red -> audit sentinel_revert", any(k == "sentinel_revert" for k, _ in au.events))

g2, au2 = FakeGit(), Audit()
ok2, _ = sentinel.guard(cfg, app(post=["true"]), ns(id="AUTO-8b", ephemeral=False), g2, "sha_ok", au2)
chk("real green post-merge suite -> guard True, no revert", ok2 and g2.reverted is None)

# --- ACCEPTANCE: respects gate_timeout_sec (a hung run is killed, not allowed to freeze the box) ---
timed = gate.run_commands(app(timeout=1), ["sleep 30"])
chk("a hung command is killed at gate_timeout_sec", not timed.passed)
chk("timeout is reported (not a freeze)", "timed out" in (timed.report or "").lower())

# --- ACCEPTANCE: respects the RAM cap — gate_env is passed into the suite's subprocess ---
env_cmd = "sh -c '[ \"$NODE_OPTIONS\" = \"--max-old-space-size=3072\" ]'"
capped = gate.run_commands(app(env={"NODE_OPTIONS": "--max-old-space-size=3072"}), [env_cmd])
chk("gate_env (RAM cap) reaches the suite subprocess", capped.passed, capped.report)

print("\n================= GATE / SENTINEL ARMING QA =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
