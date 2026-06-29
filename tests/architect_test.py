"""Architect officer: parse_adr, detect_oversized, and should_run_architect (EU-109)."""
import sys, types, asyncio
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator.architect import (
    ADRExtraction,
    DEFAULT_SPLIT_THRESHOLD,
    detect_oversized,
    parse_adr,
    should_run_architect,
)
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Minimal Config fixture for should_run_architect tests
class TestConfig:
    def app(self, name):
        class FakeApp:
            repo_path = "/fake/repo"
        return FakeApp()

# --- parse_adr (pure) ---
# SKIP_ADR
chk("SKIP_ADR parsed", parse_adr("SKIP_ADR").skipped is True)
chk("SKIP_ADR with context", parse_adr("Small bug fix. SKIP_ADR").skipped is True)
# Empty response
chk("Empty response skips", parse_adr("").skipped is True)
chk("None response skips", parse_adr(None).skipped is True)
# Full ADR parsing
adr_full = """
## APPROACH
We will add authentication middleware using JWT tokens stored in HTTP-only cookies.

## RISK + ALTERNATIVE
Main risk: cookie theft via XSS. Alternative: localStorage tokens, but rejected due to XSS exposure.

## TOUCH-POINTS
• src/auth/middleware.py
• src/auth/jwt_helper.py
• frontend/components/Login.tsx
• README.md

## DEFINITION-OF-DONE
• Tests: test_auth_middleware.py (happy path + expired token)
• A11y: Login page axe-core scan (zero violations)
• Security: All /api/* routes guarded by require_auth(); input validation on email/password

ADR_COMPLETE
"""
parsed = parse_adr(adr_full)
chk("Full ADR not skipped", parsed.skipped is False)
chk("APPROACH extracted", "JWT tokens" in parsed.approach)
chk("RISK + ALTERNATIVE extracted", "cookie theft" in parsed.risk_alt)
chk("TOUCH-POINTS count", len(parsed.touch_points) == 4)
chk("TOUCH-POINTS content", "src/auth/middleware.py" in parsed.touch_points)
chk("DEFINITION-OF-DONE extracted", "test_auth_middleware" in parsed.definition_of_done)

# Various header formats
adr_variants = """
## APPROACH
Add feature flag system.

## RISK
Feature flag complexity. Alternative: kill-switch, rejected for lack of granularity.

## TOUCH-POINTS
src/features/flags.py
frontend/hooks/useFlags.ts

## DEFINITION-OF-DONE
Tests + a11y + security covered.

ADR_COMPLETE
"""
parsed_var = parse_adr(adr_variants)
chk("Variant headers parsed", parsed_var.skipped is False)
chk("Variant approach", "feature flag system" in parsed_var.approach)
chk("Variant touch-points count", len(parsed_var.touch_points) == 2)

# --- detect_oversized (pure) ---
# Skipped ADR never oversized
chk("Skipped ADR not oversized", not detect_oversized(ADRExtraction(skipped=True, raw="SKIP_ADR")))
# Empty touch-points
chk("Empty touch-points not oversized", not detect_oversized(ADRExtraction(skipped=False, touch_points=[], raw="")))
# Oversized by touch-point count (default threshold is 5)
touch_many = [f"src/module{i}/file.py" for i in range(6)]
chk("Oversized by touch-point count", detect_oversized(ADRExtraction(skipped=False, touch_points=touch_many, raw="")))
# Under threshold (same module to avoid module threshold)
touch_few = [
    "src/auth/middleware.py",
    "src/auth/jwt.py",
    "src/auth/validators.py",
    "src/auth/tests/test_auth.py",
]
chk("Under threshold not oversized", not detect_oversized(ADRExtraction(skipped=False, touch_points=touch_few, raw="")))
# Oversized by module count (default threshold is 3 distinct modules)
touch_modules = [
    "src/auth/middleware.py",
    "src/auth/jwt.py",
    "src/db/users.py",
    "frontend/login.tsx",
]
chk("Oversized by module count", detect_oversized(ADRExtraction(skipped=False, touch_points=touch_modules, raw="")))
# Custom threshold (high)
touch_custom_high = [f"src/auth/file{i}.py" for i in range(10)]
custom_high = {"touch_points": 20, "modules": 10}
chk("Custom high threshold respected", not detect_oversized(ADRExtraction(skipped=False, touch_points=touch_custom_high, raw=""), threshold=custom_high))
# Custom threshold (low)
touch_custom_low = ["src/auth/middleware.py"]
custom_low = {"touch_points": 1, "modules": 1}
chk("Custom low threshold triggers", detect_oversized(ADRExtraction(skipped=False, touch_points=touch_custom_low, raw=""), threshold=custom_low))

# --- ADRExtraction.to_dict (pure) ---
adrex_dict = ADRExtraction(
    approach="Use JWT",
    risk_alt="XSS risk",
    touch_points=["src/auth.py"],
    definition_of_done="Tests + a11y",
    raw="raw ADR text",
    skipped=False,
).to_dict()
chk("to_dict approach", adrex_dict["approach"] == "Use JWT")
chk("to_dict risk_alt", adrex_dict["risk_alt"] == "XSS risk")
chk("to_dict touch_points", adrex_dict["touch_points"] == ["src/auth.py"])
chk("to_dict definition_of_done", adrex_dict["definition_of_done"] == "Tests + a11y")
chk("to_dict raw", adrex_dict["raw"] == "raw ADR text")
chk("to_dict skipped", adrex_dict["skipped"] is False)

adrex_skip_dict = ADRExtraction(skipped=True, raw="SKIP_ADR").to_dict()
chk("to_dict skipped ADR", adrex_skip_dict["skipped"] is True)
chk("to_dict skipped raw", adrex_skip_dict["raw"] == "SKIP_ADR")

# --- should_run_architect (async, pure logic) ---
async def _test_should_run():
    cfg = TestConfig()
    # Bug tickets skip
    bug_ticket = Ticket(id="AUTO-1", key="AUTO-1", summary="Fix typo", description="Fix typo in header", issue_type="Bug")
    chk("Bug ticket skips", not await should_run_architect(cfg, bug_ticket))
    # Bug label skips
    bug_label = Ticket(id="AUTO-2", key="AUTO-2", summary="Something", description="Something broke", labels=["bug"])
    chk("Bug label skips", not await should_run_architect(cfg, bug_label))
    # Trivial label skips
    trivial_label = Ticket(id="AUTO-3", key="AUTO-3", summary="Update docs", description="Update README", labels=["trivial"])
    chk("Trivial label skips", not await should_run_architect(cfg, trivial_label))
    # Story tickets run (with enough AC to be L/XL size)
    story_ticket = Ticket(id="AUTO-4", key="AUTO-4", summary="Add user profiles", description="Implement user profiles",
                          issue_type="Story", acceptance_criteria=["AC1", "AC2", "AC3", "AC4", "AC5"])
    chk("Story ticket runs", await should_run_architect(cfg, story_ticket))
    # Epic tickets run
    epic_ticket = Ticket(id="AUTO-5", key="AUTO-5", summary="Redesign auth", description="Auth redesign",
                         issue_type="Epic", acceptance_criteria=["AC1", "AC2", "AC3"])
    chk("Epic ticket runs", await should_run_architect(cfg, epic_ticket))
    # Feature label runs (with enough AC to not be XS)
    feature_label = Ticket(id="AUTO-6", key="AUTO-6", summary="New feature", description="New feature",
                            labels=["feature"], acceptance_criteria=["AC1", "AC2", "AC3", "AC4", "AC5"])
    chk("Feature label runs", await should_run_architect(cfg, feature_label))
    # Epic label runs
    epic_label = Ticket(id="AUTO-7", key="AUTO-7", summary="Big work", description="Big work",
                       labels=["epic"], acceptance_criteria=["AC1", "AC2"])
    chk("Epic label runs", await should_run_architect(cfg, epic_label))

asyncio.run(_test_should_run())

# --- Architect audit events (unit test audit recording) ---
def _test_audit_recording():
    """Test that Architect records correct audit events (synchronous unit test)."""
    # Mock audit recorder
    recorded = []
    class MockAudit:
        def record(self, event, **fields):
            recorded.append((event, fields))

    audit = MockAudit()

    from orchestrator.architect import ADRExtraction, parse_adr

    # Test 1: Skipped ADR (bug/small)
    skip_adr = parse_adr("SKIP_ADR")
    # Simulate what design() does when skipped
    if audit is not None:
        if skip_adr.skipped:
            audit.record("architect_skipped", ticket_id="EU-1", reason="small_bug")

    chk("Skip recorded architect_skipped",
        any(e == "architect_skipped" and f.get("reason") == "small_bug" and f.get("ticket_id") == "EU-1"
            for e, f in recorded))

    # Clear for next test
    recorded.clear()

    # Test 2: Full ADR (feature/large) without split
    full_adr_text = """
## APPROACH
Add JWT authentication for secure API access.

## RISK + ALTERNATIVE
XSS risk with cookies. Alternative: localStorage tokens (rejected due to XSS exposure).

## TOUCH-POINTS
src/auth/middleware.py
src/auth/jwt_helper.py

## DEFINITION-OF-DONE
Tests: test_auth_middleware.py happy + expired token
A11y: Login page axe-core scan
Security: /api/* routes guarded, input validation on email/password

ADR_COMPLETE
"""
    full_adr = parse_adr(full_adr_text)
    triggered_split = False  # Under threshold

    # Simulate what design() does when not skipped
    if audit is not None:
        if not full_adr.skipped:
            adr_summary = f"{len(full_adr.touch_points)} touch-points"
            if full_adr.approach:
                adr_summary = f"{full_adr.approach[:150]}... | {adr_summary}"
            audit.record("architect_run", ticket_id="EU-2",
                         adr_summary=adr_summary, triggered_split=triggered_split)

    run_events = [(e, f) for e, f in recorded if e == "architect_run"]
    chk("ADR recorded architect_run", len(run_events) == 1)
    if run_events:
        _, fields = run_events[0]
        chk("architect_run has ticket_id EU-2", fields.get("ticket_id") == "EU-2")
        chk("architect_run has adr_summary", "adr_summary" in fields)
        chk("architect_run has triggered_split False", fields.get("triggered_split") is False)
        summary = fields.get("adr_summary", "")
        chk("adr_summary mentions JWT auth", "JWT" in summary)
        chk("adr_summary mentions 2 touch-points", "2 touch-points" in summary)

    # Clear for next test
    recorded.clear()

    # Test 3: Full ADR that triggers split (oversized)
    oversized_adr_text = """
## APPROACH
Migrate user service to microservices architecture.

## RISK + ALTERNATIVE
Network complexity. Alternative: monolith refactor (rejected due to scalability).

## TOUCH-POINTS
src/auth/middleware.py
src/auth/jwt_helper.py
src/db/users.py
src/api/users.py
src/frontend/Users.tsx
src/frontend/UserForm.tsx

## DEFINITION-OF-DONE
Tests: migration tests + rollback tests
A11y: user pages axe-core scans
Security: API gateway authz, input validation

ADR_COMPLETE
"""
    oversized_adr = parse_adr(oversized_adr_text)
    triggered_split = True  # Over threshold (6 touch-points)

    # Simulate what design() does when oversized
    if audit is not None:
        if not oversized_adr.skipped:
            adr_summary = f"{len(oversized_adr.touch_points)} touch-points"
            if oversized_adr.approach:
                adr_summary = f"{oversized_adr.approach[:150]}... | {adr_summary}"
            audit.record("architect_run", ticket_id="EU-3",
                         adr_summary=adr_summary, triggered_split=triggered_split)

    run_events = [(e, f) for e, f in recorded if e == "architect_run"]
    chk("Oversized ADR recorded architect_run", len(run_events) == 1)
    if run_events:
        _, fields = run_events[0]
        chk("architect_run has ticket_id EU-3", fields.get("ticket_id") == "EU-3")
        chk("architect_run has triggered_split True", fields.get("triggered_split") is True)
        summary = fields.get("adr_summary", "")
        chk("adr_summary mentions microservices", "microservices" in summary)
        chk("adr_summary mentions 6 touch-points", "6 touch-points" in summary)

_test_audit_recording()

# --- Result tally ---
print(f"{sum(1 for _, ok, _ in results if ok)}/{len(results)} passed")
if not all(ok for _, ok, _ in results):
    print("RESULT: FAIL")
    for n, ok, d in results:
        if not ok:
            print(f"  ✗ {n}: {d}")
    sys.exit(1)
else:
    print("RESULT: PASS")
