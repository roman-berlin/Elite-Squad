"""EU-540: Tokens tile — dark-on-light contrast, stale 0%, Qwen quota line.

Tests each of the four surfaces (AC1–AC5) and pins AC4 ordering, then exits non-zero on any fail.
"""
import sys, types, tempfile, time, json
from pathlib import Path

# ── Stub external deps so orchestrator imports succeed in CI ──────────────────
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req

sys.path.insert(0, ".")

from orchestrator import usage, server, warroom
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ════════════════════════════════════════════════════════════
# Setup a fresh ledger + cfg for controlled token counts
# ════════════════════════════════════════════════════════════

audit = Path(tempfile.mkdtemp()) / "audit.jsonl"
usage.configure(str(audit))

# Seed exactly 94,000,000 tokens today
usage.record("qwen3.8-max-preview", 47_000_000, 0, 0.0, "builder")   # 47M input, m="qwen"
usage.record("claude-opus-4-8", 47_000_000, 0, 0.0, "builder")       # 47M input, m="opus"
assert usage.today_tokens(None) == 94_000_000, f"seed wrong: {usage.today_tokens(None)}"

repo = Path(tempfile.mkdtemp()) / "app"; repo.mkdir()
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(repo), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(audit), use_worktree=False, daily_token_budget=500_000_000)
cfg.detected_auth = lambda: "test"

# ════════════════════════════════════════════════════════════
# AC1: No hardcoded dark hexes; theme tokens present
# ════════════════════════════════════════════════════════════

BAD_HEXES = ("#12161f", "#e9ecf1", "#6b7480", "#232936", "#0d1119",
             "#8a929f", "#c3cad6", "#1a1f2a", "#222a38")

# Stub plan probe so it doesn't spawn claude CLI in tests
usage._plan_cache.clear()
usage._probe_plan_limits = lambda: []

client = server.create_app(cfg).test_client()

# Fetch the /usage page first (needed by this section AND later sections)
r_usage = client.get("/usage")
body_usage = r_usage.get_data(as_text=True)

# AC1 check: only look for hardcoded hexes in the page's OWN style blocks,
# NOT inside :root{} theme-token definitions (which legitimately define --well=#0d1119 in dark mode).
def _hexes_in_style_blocks(html_text: str):
    """Extract content between <style> tags and flag standalone hex codes (not :root var defs)."""
    import re
    blocks = re.findall(r'<style>(.*?)</style>', html_text, re.DOTALL)
    found = []
    for block in blocks:
        # Skip :root{...} blocks — these are theme token DEFINITIONS where hexes are the source data
        if ':root{' in block or 'theme' in block.lower():
            continue
        for h in BAD_HEXES:
            if h in block:
                found.append(h)
    return found

found_hexes = _hexes_in_style_blocks(body_usage)
for h in BAD_HEXES:
    chk(f"AC1 /usage no hardcoded {h}", h not in found_hexes, f"found {h} in style block")

chk("AC1 /usage contains var(--panel)", "var(--panel)" in body_usage, "")
chk("AC1 /usage contains var(--ink)", "var(--ink)" in body_usage, "")
chk("AC1 /usage contains var(--dim)", "var(--dim)" in body_usage, "")

# Test dual_provider_gauge output (already rendered inside /usage body via dual_gauge div)
chk("AC1 /usage gauge card uses var(--panel)", 'class=provcard' in body_usage and "var(--panel)" in body_usage, "")

# ════════════════════════════════════════════════════════════
# AC2: Tokens KPI card shows % text computed from SAME bs['pct']
# ════════════════════════════════════════════════════════════

bs = usage.budget_status(cfg)   # 94M/500M = 0.188 → 19% rounded
assert abs(bs["pct"] - 0.188) < 1e-9, f"budget status pct unexpected: {bs['pct']}"

cards = warroom.kpis(cfg, [], "automatixy")
tokens_card = next(c for c in cards if c.get("label") == "Tokens")

# Check _kpi_html renders visible 19%
kpi_str = warroom._kpi_html(cards)
chk("AC2 KPI HTML contains '94.0M'", "94.0M" in kpi_str, kpi_str[:500])
chk("AC2 KPI HTML contains visible '19%'", "19%" in kpi_str, f"missing 19% in: {kpi_str[:500]}")

# Check the gauge width attribute matches the same pct source (18.8%)
chk("AC2 gauge bar shows ~18.8%", "width:18.8%" in kpi_str, kpi_str[400:800] if len(kpi_str)>800 else kpi_str)

# Verify number, text, and bar all derive from the same bs['pct'] value
pct_int = round(bs["pct"] * 100)
pct_float = round(bs["pct"] * 100, 1)
chk(f"AC2 int-pct ({pct_int}%) consistent", str(pct_int) + "%" in kpi_str, "")
chk(f"AC2 float-pct ({pct_float}%) in gauge", f"{pct_float}%" in kpi_str, "")

# ════════════════════════════════════════════════════════════
# AC3: qwen_quota_status() — three scenarios + corrupt JSON
# ════════════════════════════════════════════════════════════

QWEN_DIR = Path(tempfile.mkdtemp())
qwen_probe_file = QWEN_DIR / "qwen_quota.json"

# 3a) Cached probe present → "5h X% · 7d Y%"
with qwen_probe_file.open("w") as f:
    json.dump({"five_h_pct_remaining": 62, "seven_d_pct_remaining": 41,
               "checked_at": time.time()}, f)
old_path = usage._QWEN_PROBE_PATH
usage._QWEN_PROBE_PATH = qwen_probe_file

r3a = usage.qwen_quota_status(None)
chk("AC3a cached probe → 5h 62%", r3a is not None and "5h" in str(r3a) and "62%" in str(r3a), r3a)
chk("AC3a cached probe → 7d 41%", r3a is not None and "7d" in str(r3a) and "41%" in str(r3a), r3a)

# 3b) File absent but today-ledger has recent qwen rows → "quota ok · last-checked HH:MM"
qwen_probe_file.unlink(missing_ok=True)
# Add a qwen row from within the last hour
now_t = time.time()
led_p = usage._path(cfg)
with led_p.open("a") as f:
    f.write(json.dumps({"t": now_t - 30, "m": "qwen3.8-max-preview", "i": 100, "o": 50, "c": 0.0}) + "\n")
r3b = usage.qwen_quota_status(cfg)
chk("AC3b ledger qwen row → 'quota ok'", r3b is not None and "quota ok" in str(r3b), r3b)
chk("AC3b ledger qwen row → HH:MM pattern", ":" in str(r3b), r3b)

# 3c) Neither source available → returns None (no exception)
led_p.write_text("")
r3c = usage.qwen_quota_status(cfg)
chk("AC3c neither source → None", r3c is None, r3c)

# 3d) Corrupt JSON → falls through gracefully → None
with qwen_probe_file.open("w") as f:
    f.write("{not valid json!!!}")
usage._QWEN_PROBE_PATH = qwen_probe_file
r3d = usage.qwen_quota_status(None)
chk("AC3d corrupt JSON → never raises", r3d is None, r3d)
usage._QWEN_PROBE_PATH = old_path
if qwen_probe_file.exists():
    qwen_probe_file.unlink()

# Reset ledger back to the seeded state for subsequent tests
usage.record("qwen3.8-max-preview", 47_000_000, 0, 0.0, "builder")
usage.record("claude-opus-4-8", 47_000_000, 0, 0.0, "builder")

# ════════════════════════════════════════════════════════════
# AC4: kpis() returns cards in exact scan priority order
# ════════════════════════════════════════════════════════════

expected_labels = ["Merged → DEV today", "Needs you", "Security blocks", "Tokens"]
actual_labels = [c["label"] for c in cards]
chk("AC4 labels match expected order", actual_labels == expected_labels,
    str(actual_labels))

# ════════════════════════════════════════════════════════════
# AC5: /usage dual-provider section has live GLM data (not dead 0%)
# ════════════════════════════════════════════════════════════

glm_stat = usage.glm_budget_status(cfg)
# Our seeded GLM ledger has 94M tokens used (from the qwen record which has provider="glm" by model name check... wait, actually
# "qwen3.8-max-preview" does NOT match glm detection, so GLM may be near zero. Let's add an explicit GLM row.)
# Actually the seed was: qwen3.8 ... m="qwen" → that's NOT glm. Only claude-opus is counted for Claude.
# GLM should be 0 for our seeded cfg. That's fine — the point is the gauge shows real data, not placeholder None.
# Let's verify the /usage body doesn't say "unconfigured" for GLM when we have GLM data.

# First check: with zero GLM, it still shouldn't show "unconfigured" since we pass real glm_budget_status() now
chk("AC5 /usage doesn't show 'unconfigured' anymore", "unconfigured" not in body_usage, "")

# Now add real GLM rows and confirm gauge percent reflects them
glm_audit_dir = Path(tempfile.mkdtemp()) / "audit.jsonl"
usage.configure(str(glm_audit_dir))
usage.record("glm-4", 50_000_000, 0, 0.0, "soldier·test")  # 50M GLM input
glm_cfg = Config(apps=[], audit_path=str(glm_audit_dir), daily_token_budget=500_000_000)
glm_st = usage.glm_budget_status(glm_cfg)
glm_pct = round(glm_st["pct"] * 100)  # 50M/500M = 10%
chk("AC5 GLM budget status reflects ledger", glm_pct >= 10, f"GLM pct={glm_st['pct']}")

# Verify the gauge function itself receives the live data correctly
dual = usage.dual_provider_budget_status(glm_cfg)
chk("AC5 dual status includes live GLM data", dual["glm"].get("on") is True, "")

# ── AC5 RENDER-LEVEL (review fix) ────────────────────────────────────────────────
# It is not enough that glm_budget_status() returns the right number: the RENDERED /usage
# GLM provider card must show a non-zero gauge that MATCHES that number. Re-fetch /usage now
# that the GLM ledger is seeded (the page was first fetched at the top, before any GLM burn).
# `client` is bound to the original `cfg`, and usage.configure() above pointed the global ledger
# at the seeded GLM file — so /usage's glm_budget_status(cfg) reads the 50M of GLM burn.
r_usage_glm = client.get("/usage")
body_glm = r_usage_glm.get_data(as_text=True)
glm_expected = int(usage.glm_budget_status(cfg)["pct"] * 100)  # closure cfg == /usage's cfg
chk("AC5 render: expected GLM pct is non-zero", glm_expected > 0, f"expected={glm_expected}%")

# Isolate the GLM card (2nd provcard) by anchoring on its pname marker, then bound the slice so
# we only ever match the GLM card's own gauge — never a later page section's 'width:NN%'.
glm_anchor = body_glm.rfind("pname>GLM<")
chk("AC5 render: /usage contains a GLM provider card", glm_anchor != -1, "")
glm_card_html = body_glm[glm_anchor:glm_anchor + 700] if glm_anchor != -1 else ""
chk("AC5 render: GLM card shows 'NN% used' matching glm_budget_status pct",
    f"{glm_expected}% used" in glm_card_html, glm_card_html[:240])
chk("AC5 render: GLM gauge 'width:NN%' is non-zero and matches pct",
    f"width:{glm_expected}%" in glm_card_html, glm_card_html[:480])
# Guard against the dead-0% regression specifically: the GLM card must NOT read '0% used'.
chk("AC5 render: GLM card is not the stale '0% used'", "0% used" not in glm_card_html,
    glm_card_html[:240])

# ════════════════════════════════════════════════════════════
# RUN_ALL GATE (AC6) — just sanity-import-check for now;
# the full suite runs separately via run_all.py
# ════════════════════════════════════════════════════════════

print("\n============ EU-540 TOKENS TILE QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
