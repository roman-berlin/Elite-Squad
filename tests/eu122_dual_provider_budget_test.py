"""EU-122: Dual-provider budget monitor tests.

Tests the GLM budget status, dual-provider status, pre-flight checks,
and graceful stop checks.
"""
import sys, types, tempfile, time, json
from pathlib import Path

# Mock the Claude Agent SDK
sdk = types.ModuleType("claude_agent_sdk")
class ClaudeAgentOptions:
    def __init__(self, **kw): self.__dict__.update(kw)
sdk.ClaudeAgentOptions = ClaudeAgentOptions
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import usage
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Setup test environment
tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"
usage.configure(str(audit))

# Test 1: glm_budget_status when quota is disabled (glm_quota_tokens = 0)
cfg_no_quota = Config(
    apps=[],
    audit_path=str(audit),
    glm_quota_tokens=0
)
glm_st = usage.glm_budget_status(cfg_no_quota)
chk("GLM budget status OFF when quota is 0",
    glm_st["on"] is False and glm_st["cap"] == 0,
    f"got {glm_st}")

# Test 2: glm_budget_status with quota and no usage yet
cfg_quota = Config(
    apps=[],
    audit_path=str(audit),
    glm_quota_tokens=100_000,
    budget_alert_pct=0.8,
    budget_bad_threshold=0.95
)
glm_st = usage.glm_budget_status(cfg_quota)
chk("GLM budget status ON with quota, no usage yet",
    glm_st["on"] is True and glm_st["cap"] == 100_000 and glm_st["used"] == 0,
    f"got {glm_st}")

# Test 3: glm_budget_status with usage recorded
# Record some GLM usage with tag "glm"
usage.record("glm-model", 10000, 5000, 0.0, "glm")
usage.record("glm-model", 20000, 3000, 0.0, "glm-ticket")
glm_st = usage.glm_budget_status(cfg_quota)
expected_used = 10000 + 5000 + 20000 + 3000
chk("GLM budget status calculates usage from ledger correctly",
    glm_st["used"] == expected_used,
    f"expected {expected_used}, got {glm_st['used']}")
pct = expected_used / 100_000
chk("GLM budget status calculates percentage correctly",
    abs(glm_st["pct"] - pct) < 0.001,
    f"expected {pct:.4f}, got {glm_st['pct']:.4f}")

# Test 4: glm_budget_status alert and bad thresholds
# Use fresh ledger to avoid contamination from previous tests
ledger_alert = Path(tempfile.mkdtemp()) / "audit.jsonl"
usage.configure(str(ledger_alert))
cfg_alert = Config(
    apps=[],
    audit_path=str(ledger_alert),
    glm_quota_tokens=100_000,
    budget_alert_pct=0.8,
    budget_bad_threshold=0.95
)
# Record enough usage to cross alert threshold (80%) but not bad threshold (95%)
usage.record("glm-model", 85000, 0, 0.0, "glm")  # 85% - crosses alert but not bad
glm_st = usage.glm_budget_status(cfg_alert)
chk("GLM budget status alert fires at 80%",
    glm_st["alert"] is True and glm_st["bad"] is False,
    f"alert={glm_st['alert']}, bad={glm_st['bad']}, pct={glm_st['pct']:.2%}")

# Test 5: glm_budget_status bad threshold
# Record enough to cross bad threshold (95%)
usage.record("glm-model", 10000, 0, 0.0, "glm")
glm_st = usage.glm_budget_status(cfg_quota)
chk("GLM budget status bad fires at 95%",
    glm_st["bad"] is True,
    f"bad={glm_st['bad']}, pct={glm_st['pct']:.2%}")

# Test 6: dual_provider_budget_status with Claude unavailable
dual_st = usage.dual_provider_budget_status(cfg_quota)
chk("Dual provider status includes Claude and GLM",
    "claude" in dual_st and "glm" in dual_st,
    str(dual_st.keys()))
chk("Dual provider status GLM data matches glm_budget_status",
    dual_st["glm"]["used"] == glm_st["used"],
    "")
chk("Dual provider healthy flag is False when GLM is bad",
    dual_st["healthy"] is False and dual_st["bad_provider"] == "glm",
    f"healthy={dual_st['healthy']}, bad_provider={dual_st['bad_provider']}")

# Test 7: pre_flight_check with healthy provider
fresh_ledger = Path(tempfile.mkdtemp()) / "audit.jsonl"
usage.configure(str(fresh_ledger))
cfg_fresh = Config(
    apps=[],
    audit_path=str(fresh_ledger),
    glm_quota_tokens=100_000,
    budget_bad_threshold=0.95
)
# 2026-07-10: budget gates judge the ACTIVE provider only (GLM at 97.8% must not hold an Opus
# drain — the 18:05 incident). These GLM-exhaustion checks therefore make GLM the active backend.
from orchestrator import backend_pref as _bp
_bp.set_active("glm", cfg_fresh)
pre = usage.pre_flight_check(cfg_fresh, ticket_estimate_pct=0.08)
chk("Pre-flight check allows start when budget is healthy",
    pre["should_skip"] is False,
    f"got {pre}")

# Test 8: pre_flight_check with GLM near exhaustion
usage.record("glm", 92000, 0, 0.0, "glm")  # 92% used, crosses 95% - 8% = 87%
pre = usage.pre_flight_check(cfg_fresh, ticket_estimate_pct=0.08)
chk("Pre-flight check skips when GLM near exhaustion",
    pre["should_skip"] is True and pre["provider"] == "glm",
    f"got {pre}")

# Test 9: pre_flight_check ticket estimate affects threshold
cfg_custom = Config(
    apps=[],
    audit_path=str(fresh_ledger),
    glm_quota_tokens=100_000,
    budget_bad_threshold=0.95
)
# At 92%, with 20% ticket estimate, 95% - 20% = 75%, so we SHOULD skip (92% >= 75%)
pre = usage.pre_flight_check(cfg_custom, ticket_estimate_pct=0.20)
chk("Pre-flight check with larger ticket estimate still skips when near limit",
    pre["should_skip"] is True,
    f"should_skip={pre['should_skip']}, pct=92%, estimate=20%")

# Test 9b: With a smaller ticket estimate and lower usage, we should NOT skip
# Create a fresh ledger with only 70% usage
ledger_estimate = Path(tempfile.mkdtemp()) / "audit.jsonl"
usage.configure(str(ledger_estimate))
cfg_estimate = Config(
    apps=[],
    glm_low_watermark_tokens=1_000,   # toy 100k quota: keep the watermark below test headroom
    audit_path=str(ledger_estimate),
    glm_quota_tokens=100_000,
    budget_bad_threshold=0.95
)
_bp.set_active("glm", cfg_estimate)   # GLM is the active backend for this GLM-headroom check
usage.record("glm", 70000, 0, 0.0, "glm")  # 70% used
pre = usage.pre_flight_check(cfg_estimate, ticket_estimate_pct=0.20)
# 70% < 95% - 20% = 75%, so should NOT skip
chk("Pre-flight check allows start with enough headroom",
    pre["should_skip"] is False,
    f"should_skip={pre['should_skip']}, pct=70%, estimate=20%")

# Test 10: graceful_stop_check with healthy providers
grace = usage.graceful_stop_check(cfg_fresh)
# Create a fresh ledger for this test
very_fresh_ledger = Path(tempfile.mkdtemp()) / "audit.jsonl"
usage.configure(str(very_fresh_ledger))
cfg_very_fresh = Config(
    apps=[],
    glm_low_watermark_tokens=1_000,   # toy 100k quota: keep the watermark below test headroom
    audit_path=str(very_fresh_ledger),
    glm_quota_tokens=100_000,
    budget_bad_threshold=0.95
)
_bp.set_active("glm", cfg_very_fresh)   # GLM active — its bad-threshold crossing must stop THIS drain
grace = usage.graceful_stop_check(cfg_very_fresh)
chk("Graceful stop check allows continuation when healthy",
    grace["should_stop"] is False,
    f"got {grace}")

# Test 11: graceful_stop_check with GLM in bad state
usage.record("glm", 98000, 0, 0.0, "glm")  # 98% used, crosses 95%
grace = usage.graceful_stop_check(cfg_very_fresh)
chk("Graceful stop check triggers when GLM crosses bad threshold",
    grace["should_stop"] is True and grace["critical_provider"] == "glm",
    f"got {grace}")
chk("Graceful stop check includes reason",
    len(grace["reason"]) > 0,
    f"reason={grace['reason']}")

# Test 12: graceful_stop_check status includes dual provider data
status = grace["status"]
chk("Graceful stop check status includes dual provider readings",
    "claude" in status and "glm" in status and "healthy" in status,
    str(status.keys()))

# Test 13: Config has budget_bad_threshold field
cfg_check = Config(apps=[], audit_path=str(audit))
chk("Config has budget_bad_threshold with default value",
    hasattr(cfg_check, "budget_bad_threshold") and cfg_check.budget_bad_threshold == 0.95,
    f"budget_bad_threshold={getattr(cfg_check, 'budget_bad_threshold', None)}")

# Test 14: Config has glm_quota_tokens field
chk("Config has glm_quota_tokens field",
    hasattr(cfg_check, "glm_quota_tokens") and isinstance(cfg_check.glm_quota_tokens, int),
    f"glm_quota_tokens={getattr(cfg_check, 'glm_quota_tokens', None)}")

# Test 15: Verify tokens_today_for_tag filters by tag prefix
ledger_tag = Path(tempfile.mkdtemp()) / "audit.jsonl"
usage.configure(str(ledger_tag))
cfg_tag = Config(
    apps=[],
    audit_path=str(ledger_tag),
    glm_quota_tokens=100_000,
    budget_bad_threshold=0.95
)
usage.record("some-model", 1000, 100, 0.0, "other-tag")  # Not GLM
usage.record("glm-model", 2000, 200, 0.0, "glm-build")  # GLM
usage.record("glm-model", 3000, 300, 0.0, "glm-review")  # GLM
glm_today = usage.tokens_today_for_tag(cfg_tag, "glm")
expected_glm = 2000 + 200 + 3000 + 300
chk("tokens_today_for_tag filters by tag prefix correctly",
    glm_today == expected_glm,
    f"expected {expected_glm}, got {glm_today}")

print("\n=============== EU-122 DUAL-PROVIDER BUDGET MONITOR QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
