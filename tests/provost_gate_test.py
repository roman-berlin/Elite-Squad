"""Provost security gate fails CLOSED (EU-1 / F2).

The gate is the boundary we rely on when `security_gate: true`. It must be the mirror of the
Reviewer — pass ONLY on an explicit `SECURITY GATE: PASS`; treat absence / BLOCK / empty /
parse-uncertainty / a raised exception as a BLOCK (the safe direction → PR, not landed on DEV).
"""
import sys, types, asyncio

# Stub the SDK so importing provost (which imports ClaudeAgentOptions) is cheap and offline.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import provost, memory
memory.preamble = lambda: ""   # avoid touching real memory files

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ---- _gate_passed (pure verdict parse) ----
chk("neither marker -> blocked (passed False)", provost._gate_passed("looks fine to me") is False)
chk("explicit PASS -> passed True", provost._gate_passed("findings...\nSECURITY GATE: PASS") is True)
chk("explicit BLOCK -> passed False", provost._gate_passed("HIGH leak\nSECURITY GATE: BLOCK") is False)
chk("empty report -> blocked", provost._gate_passed("") is False)
chk("PASS + BLOCK both present -> blocked (uncertainty)",
    provost._gate_passed("SECURITY GATE: PASS\nSECURITY GATE: BLOCK") is False)
chk("case-insensitive PASS -> passed", provost._gate_passed("security gate: pass") is True)

# ---- gate() wrapper: route canned agent replies, then a raising runner ----
class FakeRun:
    def __init__(s, final): s.final = final; s.text = final
class App:
    name = "automatixy"; repo_path = "."; workdir = None; base_branch = "DEV"
class Cfg:
    reviewer_model = "m"

def run_gate(reply_or_exc):
    async def runner(prompt, options, tag=""):
        if isinstance(reply_or_exc, Exception):
            raise reply_or_exc
        return FakeRun(reply_or_exc)
    provost.run_agent = runner
    return asyncio.run(provost.gate(Cfg(), App(), "diff --git a b"))

# case: PASS
ok, rep = run_gate("clean\nSECURITY GATE: PASS")
chk("gate() PASS reply -> passed True", ok is True, rep)

# case: BLOCK
ok, rep = run_gate("CRITICAL secret\nSECURITY GATE: BLOCK")
chk("gate() BLOCK reply -> passed False", ok is False, rep)

# case: empty / missing marker
ok, rep = run_gate("")
chk("gate() empty reply -> passed False", ok is False, rep)
chk("gate() empty reply -> reason mentions failing closed", "failing closed" in rep.lower(), rep)

ok, rep = run_gate("the diff is fine, nothing to flag")  # no marker at all
chk("gate() missing marker -> passed False", ok is False, rep)

# case: exception inside gate() -> blocked, reason carries the error (logged to audit by caller)
ok, rep = run_gate(RuntimeError("provost boom"))
chk("gate() exception -> passed False (blocked)", ok is False, rep)
chk("gate() exception -> reason names the error", "provost boom" in rep and "RuntimeError" in rep, rep)
chk("gate() exception -> verdict reads BLOCK", "SECURITY GATE: BLOCK" in rep, rep)

print("\n================ PROVOST GATE (fail-closed) QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
