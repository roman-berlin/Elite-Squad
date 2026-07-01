"""EU-122: Dual-provider budget monitor (Claude + GLM) — test core logic in isolation.

Tests GLM quota detection, dual_provider_budget_status(), pre_flight_check(),
graceful_stop_check(), and config integration.
"""
import sys, types, tempfile
from pathlib import Path

# Mock SDK
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
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Setup fresh ledger
tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"
usage.configure(str(audit))

# Record some Claude usage
usage.record("claude-opus-4-8", 5000, 3000, 0.0, "builder")
usage.record("claude-sonnet-4-6", 2000, 1000, 0.0, "reviewer")

# Record some GLM usage (simulated with "glm" in model name)
usage.record("glm-4", 3000, 2000, 0.0, "builder")
usage.record("glm-4-flash", 1500, 500, 0.0, "soldier·test")

cfg_dual = Config(
    apps=[],
    audit_path=str(audit),
    daily_token_budget=100_000,
    glm_daily_token_budget=50_000_000,
    claude_low_watermark_tokens=50_000,
    glm_low_watermark_tokens=25_000_000,
    use_worktree=False,
)

# Test GLM budget status
glm_stat = usage.glm_budget_status(cfg_dual)
chk("GLM budget status is on", glm_stat["on"] is True)
chk("GLM used tokens match ledger", glm_stat["used"] == 3000 + 2000 + 1500 + 500, str(glm_stat["used"]))
chk("GLM remaining is cap minus used", glm_stat["remaining"] == 50_000_000 - 7000, str(glm_stat["remaining"]))
chk("GLM not over budget", glm_stat["over"] is False)
chk("GLM not at low watermark", glm_stat["low"] is False)

# Test Claude detailed budget status
claude_stat = usage.claude_budget_status_detailed(cfg_dual)
chk("Claude budget status is on", claude_stat["on"] is True)
chk("Claude used tokens match ledger", claude_stat["used"] == 5000 + 3000 + 2000 + 1000, str(claude_stat["used"]))
chk("Claude remaining is cap minus used", claude_stat["remaining"] == 100_000 - 11000, str(claude_stat["remaining"]))
chk("Claude not at low watermark", claude_stat["low"] is False)

# Test dual provider budget status
dual_stat = usage.dual_provider_budget_status(cfg_dual)
chk("Dual status includes Claude", "claude" in dual_stat)
chk("Dual status includes GLM", "glm" in dual_stat)
chk("Dual status has active_provider", "active_provider" in dual_stat)
chk("Dual status active_provider defaults to Claude", dual_stat["active_provider"] == "claude")
chk("Dual status can_pick_ticket when healthy", dual_stat["can_pick_ticket"] is True)

# Test pre-flight check with healthy budget
pre = usage.pre_flight_check(cfg_dual)
chk("Pre-flight check passes with healthy budget", pre["go"] is True)
chk("Pre-flight includes provider", pre["provider"] == "claude")
chk("Pre-flight includes remaining", pre["remaining"] > 0)

# Test pre-flight check when budget is low (simulate by setting low cap)
cfg_low = Config(
    apps=[],
    audit_path=str(audit),
    daily_token_budget=12_000,  # Only 1k remaining after 11k used
    claude_low_watermark_tokens=50_000,
    use_worktree=False,
)
pre_low = usage.pre_flight_check(cfg_low)
chk("Pre-flight blocks when budget below margin", pre_low["go"] is False)
chk("Pre-flight reason explains why", "below safety margin" in pre_low["reason"].lower())

# Test pre-flight check when budget is exhausted
cfg_exhausted = Config(
    apps=[],
    audit_path=str(audit),
    daily_token_budget=10_000,  # Less than 11k used
    claude_low_watermark_tokens=50_000,
    use_worktree=False,
)
pre_exhausted = usage.pre_flight_check(cfg_exhausted)
chk("Pre-flight blocks when budget exhausted", pre_exhausted["go"] is False)
chk("Pre-flight reason mentions exhausted", "exhausted" in pre_exhausted["reason"].lower())

# Test graceful stop check when healthy
grace = usage.graceful_stop_check(cfg_dual)
chk("Graceful stop check doesn't trigger when healthy", grace["should_stop"] is False)

# Test graceful stop check when at low watermark
cfg_at_low = Config(
    apps=[],
    audit_path=str(audit),
    daily_token_budget=11_000,  # Exactly at used amount
    claude_low_watermark_tokens=50_000,
    use_worktree=False,
)
grace_low = usage.graceful_stop_check(cfg_at_low)
chk("Graceful stop triggers when at low watermark", grace_low["should_stop"] is True)
chk("Graceful stop reason explains low watermark", "low-watermark" in grace_low["reason"].lower())

# Test graceful stop crossing detection (prior remaining > watermark, now <=)
grace_crossed = usage.graceful_stop_check(cfg_at_low, prior_remaining=100_000)
chk("Graceful stop detects crossing", grace_crossed["crossed"] is True)
chk("Graceful stop crossed reason mentions crossing", "crossed" in grace_crossed["reason"].lower())

# Test GLM-specific watermarks from percentage
cfg_pct = Config(
    apps=[],
    audit_path=str(audit),
    daily_token_budget=100_000,
    glm_daily_token_budget=1_000_000,
    claude_low_watermark_pct=0.10,  # 10% = 10k tokens
    glm_low_watermark_pct=0.05,     # 5% = 50k tokens
    use_worktree=False,
)
# Use some GLM quota
usage.record("glm-4", 100_000, 50_000, 0.0, "builder")  # 150k used of 1M
glm_pct_stat = usage.glm_budget_status(cfg_pct)
chk("GLM percentage watermark computed correctly", glm_pct_stat["low"] is False)  # 850k remaining > 50k watermark

# Test with no config (should be safe/graceful)
pre_no_cfg = usage.pre_flight_check(None)
chk("Pre-flight with no config defaults to go", pre_no_cfg["go"] is True)
grace_no_cfg = usage.graceful_stop_check(None)
chk("Graceful stop with no config doesn't trigger", grace_no_cfg["should_stop"] is False)

print("\n=============== EU-122: DUAL-PROVIDER BUDGET MONITOR ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
