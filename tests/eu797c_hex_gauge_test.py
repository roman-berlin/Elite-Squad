"""EU-797c: Dual-provider gauge must contain zero raw hex colour literals.

Substituted from EU-797: every fill/stroke in the _dual_provider_gauge inline
<style> now uses var(--ok) / var(--warn) / var(--bad) instead of bare #RRGGBB
so the gauge renders correctly in both light and dark modes.
"""
import re
import sys
import types
from pathlib import Path

# Stub the Agent SDK (transitively imported) so no real model is needed.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.config import Config
from orchestrator.cockpit_views import _dual_provider_gauge

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Build usage payloads that trigger real gauge cards with each tone:
#   • Claude → bad tone  (utilization > 0.95 → red via .pgfill.r)
#   • Secondary → warn tone  (utilization ~0.85 → amber via .pgfill.a)
cfg = Config(apps=[], audit_path="/dev/null", glm_quota_tokens=100_000)

claude_usage = {
    "available": True,
    "limits": [{"utilization": 0.97, "label": "Max subscription"}],
}
glm_usage = {"pct": 0.85, "cap": 100_000, "used": 85_000}

html = _dual_provider_gauge(cfg, claude_usage, glm_usage)

# ---- assertions -----------------------------------------------------------
chk("gauge renders non-empty string", len(html) > 0)

# Strip HTML numeric entities (&#...;) before looking for hex — &#9888; etc. are icon codes.
_cleaned = re.sub(r"&#(?:[xX][0-9a-fA-F]+|[0-9]+);", "", html)

chk("no raw hex in gauge block",
    not re.search(r"#(?:[0-9a-fA-F]{3}\b|[0-9a-fA-F]{6}\b)", _cleaned))

if re.search(r"#(?:[0-9a-fA-F]{3}\b|[0-9a-fA-F]{6}\b)", _cleaned):
    for m in re.finditer(r"(?<!&)#\b[0-9a-fA-F]{3}\b(?!\)|;)"
                         r"|(?<!&)#\b[0-9a-fA-F]{6}\b", html):
        ctx = html[max(0,m.start()-10):m.end()+10]
        print(f"  HEX LEAK: `{m.group()}` → ...{ctx!r}...")

# Verify semantic tokens are present (proves substitution happened, not deletion).
chk("uses var(--ok)", "var(--ok)" in html)
chk("uses var(--warn)", "var(--warn)" in html)
chk("uses var(--bad)", "var(--bad)" in html)

# Verify class names survived intact (tone-to-color mapping preserved).
chk(".picon.ok class present", ".picon.ok" in html)
chk(".picon.warn class present", ".picon.warn" in html)
chk(".picon.bad class present", ".picon.bad" in html)
chk(".pgfill.g class present", ".pgfill.g" in html)
chk(".pgfill.a class present", ".pgfill.a" in html)
chk(".pgfill.r class present", ".pgfill.r" in html)

# Verify provider card markup structure (glance check).
chk("contains dualprov wrapper", '<div class=dualprov>' in html)
chk("contains provcard", "provcard" in html)

# ---- report ----------------------------------------------------------------
print("\n===================== EU-797C HEX SWEEP QA =====================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-" * 40)
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
