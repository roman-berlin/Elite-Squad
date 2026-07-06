"""EU-47: prove the hard guardrail is ACTUALLY wired onto the read-only recon officers and the provost
security gate — behaviourally, not just by source-grep.

The risk EU-47 closes: provost/scout/quartermaster recon and the provost diff-gate run
``permission_mode="bypassPermissions"`` with ``Bash`` allowed (for npm/bun audit), but had NO
``hooks=guard.hooks_config()``, so the F1/F2 deny-by-content denylist (cat .env / printenv / exfil) was
inert exactly where an attacker-influenceable diff/comment is read. These tests construct the real
``ClaudeAgentOptions`` those code paths build and assert the guard hook rides along — so deleting the
wiring (or guard.hooks_config() going stale) flips them RED instead of passing silently.
"""
import sys, types, asyncio

# --- Stub the Agent SDK so importing the orchestrator needs no real models/network. Unlike a throwaway
#     mock, ClaudeAgentOptions here CAPTURES its kwargs so we can inspect what each officer wired. ---
sdk = types.ModuleType("claude_agent_sdk")


class HookMatcher:
    def __init__(self, **k):
        self.__dict__.update(k)


class ClaudeAgentOptions:
    def __init__(self, **k):
        # record everything the officer passed so the test can assert on hooks/allowed_tools/etc.
        self.__dict__.update(k)


sdk.HookMatcher = HookMatcher
sdk.ClaudeAgentOptions = ClaudeAgentOptions


class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self


# anything else the SDK exposes (ClaudeSDKClient, query, …) is a harmless dummy
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import guard, recon, provost  # noqa: E402

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


def _matcher_names(opts):
    """The PreToolUse matcher pattern attached to an options object ('' if no guard)."""
    hc = getattr(opts, "hooks", None)
    if not isinstance(hc, dict) or "PreToolUse" not in hc:
        return None
    return hc["PreToolUse"][0].matcher


# ============================ recon._opts (provost/scout/quartermaster recon) ============================
SOLDIER_TOOLS = ["Read", "Grep", "Glob", "Bash"]

opts = recon._opts("SYS", "/work", "model-x", SOLDIER_TOOLS, 14, "high")
chk("recon._opts runs under bypassPermissions", opts.permission_mode == "bypassPermissions")
chk("recon._opts keeps Bash (deny-by-content, not tool removal)", "Bash" in opts.allowed_tools)
chk("recon._opts still denies the write tools", set(opts.disallowed_tools) >= {"Write", "Edit", "NotebookEdit"})
# the heart of EU-47: a real PreToolUse hook is attached, and it names Read|Bash so the deny actually fires
m = _matcher_names(opts)
chk("recon._opts ATTACHES the guard hook (not None)", m is not None, str(getattr(opts, "hooks", None)))
chk("recon guard matcher fires on Bash (exfil shell)", bool(m) and "Bash" in m, str(m))
chk("recon guard matcher fires on Read (secret read)", bool(m) and "Read" in m, str(m))

# planning/lead pass is read-only (Read/Grep/Glob) but MUST still carry the guard
opts_plan = recon._opts("SYS", "/work", "model-x", SOLDIER_TOOLS, 12, "medium", planning=True)
chk("recon planning pass drops Bash from allow-list", "Bash" not in opts_plan.allowed_tools)
chk("recon planning pass STILL attaches the guard", _matcher_names(opts_plan) is not None)

# Drift/regression: _opts must read guard.hooks_config() LIVE (not a stale constant) — if the guard
# vanishes (old SDK → hooks_config() returns None), the attached hooks must follow to None, and the
# officer's warn_if_absent is what then fires loud. Proves the wiring is the function, not a snapshot.
_orig_hc = guard.hooks_config
guard.hooks_config = lambda: None
try:
    opts_absent = recon._opts("SYS", "/work", "model-x", SOLDIER_TOOLS, 14, "high")
    chk("recon._opts forwards a vanished guard as None (warn path engages)", opts_absent.hooks is None)
finally:
    guard.hooks_config = _orig_hc


# ---- run_officer must shout warn_if_absent on entry (the loud signal when the guard is gone) ----
class _FakeRun:
    final = "RECON OK"
    text = "RECON OK"


async def _fake_run_agent(task, options, tag=None):  # noqa: ANN001
    _fake_run_agent.captured = options
    return _FakeRun()


class _Cfg:
    delegation_enabled = False


_warns = []
_orig_warn = guard.warn_if_absent
guard.warn_if_absent = lambda officer="officer": _warns.append(officer) or False
_orig_recon_run = recon.run_agent
recon.run_agent = _fake_run_agent
try:
    report = asyncio.run(recon.run_officer(
        officer="scout", label="Recon Scout", system="SYS", task="inspect",
        cfg=_Cfg(), cwd="/work", model="m", soldier_tools=SOLDIER_TOOLS, max_turns=10, effort="high"))
    chk("run_officer returns the report string", report == "RECON OK", report)
    chk("run_officer calls warn_if_absent for the officer", "scout" in _warns, str(_warns))
    # and the solo run it dispatched carried the guard hook end-to-end
    chk("run_officer's dispatched options carry the guard",
        _matcher_names(_fake_run_agent.captured) is not None)
finally:
    guard.warn_if_absent = _orig_warn
    recon.run_agent = _orig_recon_run


# Phase-2 §2 (2026-07-06): the provost per-diff GATE (provost.gate) was DELETED — its attacker-diff
# guard-wiring pins went with it. The read-only recon path above (recon._opts) still carries the
# guard and is the surviving EU-47 coverage; the deterministic gate.py scan replaced the gate.
chk("provost.gate is gone (Phase-2 §2 — recon path above is the surviving guard coverage)",
    not hasattr(provost, "gate"))


print("\n============== EU-47 RECON/GATE GUARD WIRING ==============")
passed_n = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed_n == len(results) else f"{len(results)-passed_n} FAIL")
sys.exit(0 if passed_n == len(results) else 1)
