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
    async def runner(prompt, options, tag="", **kw):
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

# ---- EU-105: _parse_security_sections (new in EU-105 build) ----
# Happy path: all three §1/§2/§3 sections present and filled.
FULL_REPORT = """\
No secrets found.

§1 SECRETS: diff grep output confirmed zero credentials
§2 AUTHZ: /api/foo guarded by require_auth() at line 42
§3 INJECTION: cursor.execute(sql, params) used throughout

SECURITY GATE: PASS
"""
s1, s2, s3 = provost._parse_security_sections(FULL_REPORT)
chk("_parse_security_sections: §1 extracted correctly",
    s1 == "diff grep output confirmed zero credentials")
chk("_parse_security_sections: §2 extracted correctly",
    s2 == "/api/foo guarded by require_auth() at line 42")
chk("_parse_security_sections: §3 extracted correctly",
    s3 == "cursor.execute(sql, params) used throughout")

# Missing §2 → empty string for that slot.
MISSING_S2 = "§1 SECRETS: none found\n§3 INJECTION: parameterised\nSECURITY GATE: PASS\n"
s1, s2, s3 = provost._parse_security_sections(MISSING_S2)
chk("_parse_security_sections: missing §2 returns empty string", s2 == "")
chk("_parse_security_sections: §1 still extracted when §2 absent", s1 == "none found")
chk("_parse_security_sections: §3 still extracted when §2 absent", s3 == "parameterised")

# Completely empty report → all three return empty strings.
s1, s2, s3 = provost._parse_security_sections("")
chk("_parse_security_sections: empty report returns three empty strings",
    s1 == "" and s2 == "" and s3 == "")

# Multi-line §3 content (DOTALL) terminated by SECURITY GATE:.
MULTI = "§1 SECRETS: none\n§2 AUTHZ: none\n§3 INJECTION: line one\nline two\nSECURITY GATE: PASS\n"
s1, s2, s3 = provost._parse_security_sections(MULTI)
chk("_parse_security_sections: multi-line §3 content captured",
    "line one" in s3 and "line two" in s3)

# ---- EU-105: _publish_artifact (new in EU-105 build) ----
# No-op when store is None — must not raise.
try:
    provost._publish_artifact(None, "s1", "s2", "s3", signed=True)
    chk("_publish_artifact: no-op when store=None (no exception)", True)
except Exception as exc:
    chk("_publish_artifact: no-op when store=None (no exception)", False, str(exc))

# store provided → puts artifact with the correct field values.
from orchestrator.contracts import PerTicketArtifactStore, SecurityArtifact
_store = PerTicketArtifactStore()
provost._publish_artifact(_store, "sec1", "sec2", "sec3", signed=True)
_sa = _store.get_security()
chk("_publish_artifact: security artifact placed in store", _sa is not None)
chk("_publish_artifact: s1_secrets set correctly", _sa is not None and _sa.s1_secrets == "sec1")
chk("_publish_artifact: s2_authz set correctly", _sa is not None and _sa.s2_authz == "sec2")
chk("_publish_artifact: s3_injection set correctly", _sa is not None and _sa.s3_injection == "sec3")
chk("_publish_artifact: signed=True propagated", _sa is not None and _sa.signed is True)

# signed=False propagates too.
_store2 = PerTicketArtifactStore()
provost._publish_artifact(_store2, "a", "b", "c", signed=False)
_sa2 = _store2.get_security()
chk("_publish_artifact: signed=False propagated", _sa2 is not None and _sa2.signed is False)

# ---- EU-105: gate() end-to-end with §1/§2/§3 in the Security Engineer report ----
# When gate() receives a PASS report with properly-filled sections, the store must hold
# a signed artifact whose is_signed() returns True — the countersig gate's green path.
_PASS_WITH_SECS = (
    "§1 SECRETS: diff grep output confirmed zero credentials\n"
    "§2 AUTHZ: all new routes behind require_auth()\n"
    "§3 INJECTION: all queries use parameterised execute()\n"
    "SECURITY GATE: PASS\n"
)

def run_gate_with_store(reply_or_exc):
    """Like run_gate() above but also captures the store state."""
    store = PerTicketArtifactStore()
    async def runner(prompt, options, tag="", **kw):
        if isinstance(reply_or_exc, Exception):
            raise reply_or_exc
        return FakeRun(reply_or_exc)
    provost.run_agent = runner
    ok, rep = asyncio.run(provost.gate(Cfg(), App(), "diff --git a b", store=store))
    return ok, rep, store

ok, rep, store = run_gate_with_store(_PASS_WITH_SECS)
_sa = store.get_security()
chk("gate() PASS+sections → store has SecurityArtifact", _sa is not None)
chk("gate() PASS+sections → artifact.is_signed() True", _sa is not None and _sa.is_signed())
chk("gate() PASS+sections → s1_secrets populated",
    _sa is not None and "zero credentials" in _sa.s1_secrets)

# A BLOCK report → store gets artifact with signed=False (is_signed() False).
ok, rep, store_b = run_gate_with_store("§1 SECRETS: found key!\n§2 AUTHZ: none\n§3 INJECTION: none\nSECURITY GATE: BLOCK\n")
_sa_b = store_b.get_security()
chk("gate() BLOCK → store artifact is unsigned (is_signed() False)",
    _sa_b is not None and not _sa_b.is_signed())

# Exception inside gate() → store gets empty unsigned artifact (published in except branch).
ok, rep, store_exc = run_gate_with_store(RuntimeError("boom"))
_sa_exc = store_exc.get_security()
chk("gate() exception → store has an unsigned SecurityArtifact",
    _sa_exc is not None and not _sa_exc.is_signed())

print("\n================ PROVOST GATE (fail-closed) QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
