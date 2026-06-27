"""EU-75 QA: 'Tokens today' KPI card — gauge, paused state, hint content, and href."""
import sys, types, tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import usage, warroom
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ── helpers ─────────────────────────────────────────────────────────────────

tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"

def _cfg(**kw):
    """Build a minimal Config with audit_path wired to our temp ledger."""
    return Config(apps=[], audit_path=str(audit), **kw)

def _tok_card(cfg):
    """Return the 'Tokens today' card dict from warroom.kpis(), or None if absent."""
    cards = warroom.kpis(cfg, [], None)
    return next((c for c in cards if c.get("label") == "Tokens today"), None)

# ── (a) normal state: hint contains 'resets at local midnight' + pct string ─

usage.configure(str(audit))
usage.record("claude-sonnet-4-6", 500, 100, 0.0, "builder")   # 600 tokens today

cfg_normal = _cfg(daily_token_budget=10_000)
card = _tok_card(cfg_normal)
chk("normal: card is present", card is not None)
hint = (card or {}).get("hint", "")
chk("normal: hint contains 'resets at local midnight'", "resets at local midnight" in hint, repr(hint))
# pct should be ~6 % of 10 000 → "6%"
chk("normal: hint contains a pct string (e.g. '6%')", "%" in hint, repr(hint))

# ── (b) over-budget state: card value is '⛔ paused — budget hit' ────────────

cfg_tiny = _cfg(daily_token_budget=1)   # 1-token cap → immediately over
card_over = _tok_card(cfg_tiny)
chk("over-budget: card is present", card_over is not None)
val = (card_over or {}).get("value", "")
chk("over-budget: value is '⛔ paused — budget hit'",
    val == "⛔ paused — budget hit", repr(val))

# ── (c) gauge field is a 0-1 float when budget is on ────────────────────────

gauge = (card or {}).get("gauge")
chk("normal: gauge is a float", isinstance(gauge, float), repr(gauge))
chk("normal: gauge is in [0, 1]", gauge is not None and 0.0 <= gauge <= 1.0, repr(gauge))

gauge_over = (card_over or {}).get("gauge")
chk("over-budget: gauge is a float", isinstance(gauge_over, float), repr(gauge_over))
chk("over-budget: gauge is a non-negative float (may exceed 1 when over cap)",
    gauge_over is not None and gauge_over >= 0.0, repr(gauge_over))

# ── (d) card href is '/usage' ────────────────────────────────────────────────

chk("normal: href is '/usage'", (card or {}).get("href") == "/usage",
    repr((card or {}).get("href")))
chk("over-budget: href is '/usage'", (card_over or {}).get("href") == "/usage",
    repr((card_over or {}).get("href")))

# ── (e) tone colours: None under alert, 'warn' in alert zone, 'bad' when over ─

# tone None: 600 tokens of 10 000 = 6 % → green, no tone
chk("normal: tone is None (green — no alarm)", (card or {}).get("tone") is None,
    repr((card or {}).get("tone")))

# tone "bad": cap=1 → over budget
chk("over-budget: tone is 'bad'", (card_over or {}).get("tone") == "bad",
    repr((card_over or {}).get("tone")))

# tone "warn": burn past alert_pct (80%) but not yet over cap.
# usage._PATH (the global choke-point) takes priority over cfg, so we must
# configure() to point at the alert ledger during this block, then restore.
import json as _json
usage_alert_dir = Path(tempfile.mkdtemp())
audit_alert = usage_alert_dir / "audit.jsonl"
audit_alert.write_text("", encoding="utf-8")
ledger_alert = usage_alert_dir / "usage_ledger.jsonl"
ts_today = round(usage._day_start() + 60, 1)
ledger_alert.write_text(
    _json.dumps({"t": ts_today, "m": "sonnet", "i": 85_000, "o": 0, "c": 0.0, "g": "builder"}) + "\n",
    encoding="utf-8",
)
# Temporarily redirect the global ledger so _path() resolves to our alert file
usage.configure(str(audit_alert))
cfg_alert = Config(apps=[], audit_path=str(audit_alert), daily_token_budget=100_000, budget_alert_pct=0.8)
card_alert = next(
    (c for c in warroom.kpis(cfg_alert, [], None) if c.get("label") == "Tokens today"), None
)
# Restore the original ledger for subsequent tests
usage.configure(str(audit))
chk("alert-state: card present", card_alert is not None)
chk("alert-state: tone is 'warn'",
    (card_alert or {}).get("tone") == "warn", repr((card_alert or {}).get("tone")))
chk("alert-state: gauge ≥ 0.8",
    (card_alert or {}).get("gauge") is not None and (card_alert or {}).get("gauge", 0) >= 0.8,
    str((card_alert or {}).get("gauge")))

# ── (f) budget OFF (cap=0): no gauge, no tone, hint says 'no daily cap' ────

cfg_off = _cfg(daily_token_budget=0)
card_off = _tok_card(cfg_off)
chk("budget-off: card is present", card_off is not None)
chk("budget-off: gauge is None (no bar)", (card_off or {}).get("gauge") is None)
chk("budget-off: tone is None", (card_off or {}).get("tone") is None)
chk("budget-off: hint says 'no daily cap'",
    "no daily cap" in (card_off or {}).get("hint", "").lower(),
    repr((card_off or {}).get("hint", "")))

# ── (g) 'Tokens this week' card always present ────────────────────────────

week_card_normal = next(
    (c for c in warroom.kpis(cfg_normal, [], None) if c.get("label") == "Tokens this week"), None
)
chk("week-card: present alongside 'Tokens today'", week_card_normal is not None)
chk("week-card: href is '/usage'", (week_card_normal or {}).get("href") == "/usage")
chk("week-card: hint mentions '7-day rolling'",
    "7-day" in (week_card_normal or {}).get("hint", ""),
    repr((week_card_normal or {}).get("hint", "")))

# ── (h) _fmt_tokens helper: compact human-readable formatting ───────────────

chk("_fmt_tokens(0) → '0'", warroom._fmt_tokens(0) == "0", warroom._fmt_tokens(0))
chk("_fmt_tokens(999) → '999'", warroom._fmt_tokens(999) == "999", warroom._fmt_tokens(999))
chk("_fmt_tokens(1000) → '1k'", warroom._fmt_tokens(1_000) == "1k", warroom._fmt_tokens(1_000))
chk("_fmt_tokens(144_000_000) → '144.0M'",
    warroom._fmt_tokens(144_000_000) == "144.0M", warroom._fmt_tokens(144_000_000))
chk("_fmt_tokens(1_000_000) → '1.0M'",
    warroom._fmt_tokens(1_000_000) == "1.0M", warroom._fmt_tokens(1_000_000))

# ── (i) _kpi_html: gauge bar rendered/omitted and colour tracks tone ────────

# No gauge field → no gauge bar HTML
html_no_gauge = warroom._kpi_html([{"label": "Merged total", "value": 5, "hint": "all", "tone": "ok"}])
chk("_kpi_html: no gauge-bar when gauge=None", "height:3px" not in html_no_gauge)

# gauge=0.5, tone absent → bar uses --ok colour
html_half = warroom._kpi_html(
    [{"label": "Tokens today", "value": "5k · 50%", "hint": "cap 10k", "gauge": 0.5}]
)
chk("_kpi_html: gauge bar emitted when gauge=0.5", "height:3px" in html_half)
chk("_kpi_html: fill width is 50.0%", "width:50.0%" in html_half, html_half[:400])
chk("_kpi_html: neutral tone → --ok colour", "var(--ok)" in html_half)

# warn tone → --warn colour
html_warn = warroom._kpi_html(
    [{"label": "Tokens today", "value": "85k · 85%", "hint": "cap 100k", "gauge": 0.85, "tone": "warn"}]
)
chk("_kpi_html: warn tone → --warn gauge colour", "var(--warn)" in html_warn)

# bad tone → --bad colour
html_bad = warroom._kpi_html(
    [{"label": "Tokens today", "value": "⛔ paused", "hint": "cap hit", "gauge": 1.0, "tone": "bad"}]
)
chk("_kpi_html: bad tone → --bad gauge colour", "var(--bad)" in html_bad)

# Gauge values outside [0,1] are clamped cleanly
html_clamp_hi = warroom._kpi_html([{"label": "T", "value": "x", "hint": "", "gauge": 1.5}])
chk("_kpi_html: gauge > 1 clamped to 100%", "width:100.0%" in html_clamp_hi, html_clamp_hi[:300])
html_clamp_lo = warroom._kpi_html([{"label": "T", "value": "x", "hint": "", "gauge": -0.5}])
chk("_kpi_html: gauge < 0 clamped to 0%", "width:0.0%" in html_clamp_lo, html_clamp_lo[:300])

# ── (j) render_board integration: gauge + deeplink appear in full board HTML ─

try:
    board_html = warroom.render_board(cfg_normal, None, {"active": False})
    chk("render_board: 'Tokens today' label in full board HTML",
        "Tokens today" in board_html)
    chk("render_board: gauge bar emitted in board HTML",
        "height:3px" in board_html)
    chk("render_board: /usage deeplink wired through in board",
        'href="/usage"' in board_html)
except Exception as exc:  # noqa: BLE001
    chk("render_board: no crash with token gauge", False, str(exc))

# ── summary ──────────────────────────────────────────────────────────────────

print("\n============ EU-75 TOKEN GAUGE QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
