"""EU-105 regression: unsigned security artifact blocks the pipeline from reaching Land.

Three check groups:

  A. SecurityArtifact.is_signed() — blank / placeholder §-fields → False
  B. SecurityArtifact.is_signed() — fully-filled + signed=True → True
  C. Pipeline gate: when store.security is None or unsigned after provost.gate() the loop
     records a security_block audit event and _land is called with security_block set
     (i.e. the merge/land path is blocked); when the artifact is properly signed _land
     receives security_block=None (happy path).
"""
import sys, types, asyncio

# ── Stub the Claude SDK before any orchestrator import ───────────────────────
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

results = []


def chk(name, cond, detail=""):
    """Record a test result; print nothing here — summary printed at the end."""
    results.append((name, bool(cond), detail))


# ── Imports ───────────────────────────────────────────────────────────────────
from orchestrator.contracts import (
    SecurityArtifact,
    PerTicketArtifactStore,
    BuildArtifact,
    BuildResult,
    ReviewResult,
    ReviewVerdict,
    TestEngineerResult,
    SpecArtifact,
    Ticket,
    TicketReport,
    Outcome,
    GateResult,
    Verdict,
)


# ============================================================================ #
# A. is_signed() returns False for blank / placeholder §-fields                #
# ============================================================================ #

# Any empty field → False (signed flag is irrelevant)
for field_name, kw in [
    ("s1_secrets blank", dict(s1_secrets="",   s2_authz="ok", s3_injection="ok")),
    ("s2_authz blank",   dict(s1_secrets="ok", s2_authz="",   s3_injection="ok")),
    ("s3_injection blank", dict(s1_secrets="ok", s2_authz="ok", s3_injection="")),
]:
    a = SecurityArtifact(**kw, signed=True)
    chk(f"is_signed False when {field_name}", a.is_signed() is False)

# Whitespace-only counts as blank
ws = SecurityArtifact(s1_secrets="   ", s2_authz="ok", s3_injection="ok", signed=True)
chk("is_signed False when s1_secrets is whitespace-only", ws.is_signed() is False)

# Angle-bracket placeholder strings → False
for ph in [
    "<describe secrets here>",
    "<§1 fill-in text>",
    "<no credentials found or list them>",
    "  <placeholder>  ",
]:
    a = SecurityArtifact(s1_secrets=ph, s2_authz="ok", s3_injection="ok", signed=True)
    chk(f"is_signed False for placeholder {ph!r}", a.is_signed() is False)

# signed=False even with real prose → False
not_signed = SecurityArtifact(
    s1_secrets="No secrets found.",
    s2_authz="Routes guarded.",
    s3_injection="Parameterised.",
    signed=False,
)
chk("is_signed False when signed=False despite real prose", not_signed.is_signed() is False)

# ============================================================================ #
# B. is_signed() returns True for a fully-filled + signed=True artifact        #
# ============================================================================ #

good = SecurityArtifact(
    s1_secrets="No hardcoded credentials found in diff.",
    s2_authz="All new routes protected by require_auth middleware.",
    s3_injection="All DB calls use parameterised queries.",
    signed=True,
)
chk("is_signed True when all fields filled and signed=True", good.is_signed() is True)

# Prose that happens to contain '<…>' sub-strings is NOT a bare placeholder → True
embedded = SecurityArtifact(
    s1_secrets="Checked for <token> patterns; none found in changed files.",
    s2_authz="Route /api/foo guarded by check_auth() at line 42.",
    s3_injection="cursor.execute(sql, params) used throughout.",
    signed=True,
)
chk("is_signed True for prose containing '<…>' but not a bare placeholder",
    embedded.is_signed() is True)

# ============================================================================ #
# C. Pipeline regression: unsigned artifact / missing artifact blocks Land     #
# ============================================================================ #

import orchestrator.loop as loop
import orchestrator.provost as provost_mod


# Minimal stubs the loop needs
class Audit:
    def __init__(self):
        self.events = []

    def record(self, event, **kw):
        self.events.append({"event": event, **kw})


class Backlog:
    def add_comment(self, *a): pass
    def set_status(self, *a): pass


class Git:
    def has_changes(self): return True
    def diff_against_base(self): return "diff --git a/x b/x\n+added_line"
    def changed_paths(self): return ["orchestrator/contracts.py"]


def mkcfg(**kw):
    from orchestrator.config import Config, AppConfig
    return Config(
        apps=[AppConfig(name="automatixy", repo_path=".",
                        base_branch="DEV", protected_branch="MAIN",
                        backlog_backend="none")],
        audit_path="/tmp/eu105_test.jsonl",
        use_worktree=False,
        security_gate=True,   # ← enable the security gate
        pm_enabled=False,
        **kw,
    )


# Suppress notification side-effects
loop._notify = lambda c, t: None
loop._route_out_of_scope = lambda *a, **k: None
loop.run_gate = lambda app, changed=None, **_: GateResult(passed=True, report="")


# ── Officer stubs: build → pass, review → ship-ready ─────────────────────────
class StubBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket):
        return ("low", "sized")

    @staticmethod
    async def build(req, app, cfg, audit=None, store=None, spec=None):
        if store is not None:
            store.put(BuildArtifact(
                files_changed=[],
                diff_digest="added the feature",
                decisions=["simple approach"],
                open_questions=[],
            ))
        return BuildResult(ok=True, summary="added the feature",
                           cost_usd=0.0, num_turns=1, raw="", tools=[])


class StubTE:
    @staticmethod
    async def ensure_coverage(ticket, app, cfg, store=None, build_artifact=None):
        return TestEngineerResult(ok=True, coverage="80→90%")


class StubReviewer:
    @staticmethod
    async def review(diff, ticket, app, cfg, iteration=1, store=None, build_artifact=None):
        if store is not None:
            store.put(ReviewVerdict(verdict=Verdict.PASS, blocking=[], notes=[]))
        return ReviewResult(verdict=Verdict.PASS, spec_met=True, cost_usd=0.0)


loop.builder_mod = StubBuilder
loop.test_engineer_mod = StubTE
loop.reviewer_mod = StubReviewer


def _run_attempt_with_gate(gate_fn):
    """Run _attempt() with a custom provost.gate mock; return (report, audit)."""
    cfg = mkcfg(max_iterations=1)
    app = cfg.app("automatixy")
    ticket = Ticket(
        id="EU-105", key="EU-105",
        summary="security countersig gate regression",
        description="verify countersig gate",
        ephemeral=True,
        app="automatixy",
    )
    audit = Audit()

    # Capture _land invocations
    land_calls = []

    def fake_land(tk, app, cfg, git, backlog, audit, branch, iteration, cost, build, review,
                  security_block=None, coverage=""):
        land_calls.append({"security_block": security_block})
        return TicketReport(tk.id, Outcome.MERGED, iteration, cost, app.name, branch)

    loop._land = fake_land
    provost_mod.gate = gate_fn

    asyncio.run(loop._attempt(ticket, app, cfg, Git(), Backlog(), audit,
                              loop.Budget(0), "autodev/EU-105"))
    return land_calls, audit.events


# ── Scenario 1: gate() PASS but store.security is None (artifact never published) ─
async def _gate_pass_no_artifact(cfg, app, diff, store=None):
    """Gate claims PASS but never publishes a SecurityArtifact — store.security stays None."""
    # Do NOT call store.put() — simulate a Security Engineer that passed the verdict
    # but didn't produce the mandatory §1/§2/§3 countersig artifact.
    return (True, "SECURITY GATE: PASS")


land_calls_1, events_1 = _run_attempt_with_gate(_gate_pass_no_artifact)

chk(
    "Pipeline: gate PASS + store.security=None → _land called with security_block set",
    land_calls_1 and land_calls_1[0]["security_block"] is not None,
)
chk(
    "Pipeline: gate PASS + store.security=None → security_block recorded in audit",
    any(e["event"] == "security_block" for e in events_1),
)

# ── Scenario 2: gate() PASS but artifact is unsigned (signed=False) ───────────
async def _gate_pass_unsigned_artifact(cfg, app, diff, store=None):
    """Gate claims PASS but publishes an unsigned artifact (signed=False)."""
    if store is not None:
        store.put(SecurityArtifact(
            s1_secrets="No secrets found.",
            s2_authz="Routes guarded.",
            s3_injection="Parameterised.",
            signed=False,   # ← countersignature missing
        ))
    return (True, "SECURITY GATE: PASS")


land_calls_2, events_2 = _run_attempt_with_gate(_gate_pass_unsigned_artifact)

chk(
    "Pipeline: gate PASS + unsigned artifact → _land called with security_block set",
    land_calls_2 and land_calls_2[0]["security_block"] is not None,
)
chk(
    "Pipeline: gate PASS + unsigned artifact → security_block recorded in audit",
    any(e["event"] == "security_block" for e in events_2),
)

# ── Scenario 3: gate() PASS and artifact is fully signed (happy path) ─────────
async def _gate_pass_signed_artifact(cfg, app, diff, store=None):
    """Gate passes AND publishes a fully-signed SecurityArtifact — green path."""
    if store is not None:
        store.put(SecurityArtifact(
            s1_secrets="No hardcoded credentials in diff.",
            s2_authz="All routes behind require_auth().",
            s3_injection="All queries parameterised.",
            signed=True,
        ))
    return (True, "SECURITY GATE: PASS")


land_calls_3, events_3 = _run_attempt_with_gate(_gate_pass_signed_artifact)

chk(
    "Pipeline: gate PASS + fully-signed artifact → _land called with security_block=None (green path)",
    land_calls_3 and land_calls_3[0]["security_block"] is None,
)
chk(
    "Pipeline: gate PASS + fully-signed artifact → no security_block audit event",
    not any(e["event"] == "security_block" for e in events_3),
)

# ── Scenario 4: gate() BLOCK outright (no artifact check needed) ──────────────
async def _gate_block(cfg, app, diff, store=None):
    """Gate itself returns BLOCK — countersig check is moot but block must still fire."""
    if store is not None:
        store.put(SecurityArtifact(s1_secrets="", s2_authz="", s3_injection="", signed=False))
    return (False, "SECURITY GATE: BLOCK — critical finding in diff")


land_calls_4, events_4 = _run_attempt_with_gate(_gate_block)

chk(
    "Pipeline: gate BLOCK → _land called with security_block set",
    land_calls_4 and land_calls_4[0]["security_block"] is not None,
)
chk(
    "Pipeline: gate BLOCK → security_block recorded in audit",
    any(e["event"] == "security_block" for e in events_4),
)

# ============================================================================ #
# Report                                                                        #
# ============================================================================ #
print("\n============ EU-105 SECURITY COUNTERSIG REGRESSION ============")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    status = "PASS" if ok else "FAIL"
    suffix = f"  ({detail})" if detail and not ok else ""
    print(f"  [{status}] {name}{suffix}")
print("---------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
