"""EU-778 (EU-555c) QA: the four view files outside server.py consume the design tokens —
no bare dark hex left in the swept regions, the dead rules are gone, and /tasks follows
the theme toggle.

Swept surfaces:
  - cockpit_views._working()  — spinner / progress bar (was #232936 / #3b6cff / #1a1f29 …)
  - roster.html_view()        — the roster page's emitted <style> block (was #c3cad6 / #12161f …)
  - dashboard.render_html()   — _TEMPLATE's hardcoded `:root{color-scheme:dark}` (broke light mode)
  - warroom health banner     — .healthbar.bad gradient + border hexes, and the DEAD .ok rule
    (the banner only ever renders class="healthbar bad" — a healthy unit renders no banner at all)

We assert the *skin contract* — swept outputs reference the single-source-of-truth tokens via
``var(--…)`` and carry zero bare hex literals — and that every token the sweep uses is actually
DEFINED in the token source of truth (an undefined var() silently falls back to inherit, so a
typo'd token would pass a pure "contains var(--" check while rendering wrong).
"""
import re
import sys
import tempfile
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import cockpit_views as V
from orchestrator import dashboard, roster, warroom
from orchestrator.config import AppConfig, Config

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# A bare colour hex literal (#rgb / #rrggbb / #rrggbbaa). Only ever run against CSS text —
# HTML entities like &#8594; would false-positive, so every check below extracts the
# <style> block (or a CSS region) first.
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")

# ── cockpit_views._working spinner: dark hex → tokens ─────────────────────────
working = V._working("Building…", 5)
wk_style = working.split("<style>", 1)[1].split("</style>", 1)[0]
chk("spinner: zero bare hex in the _working style block", not HEX.search(wk_style), wk_style)
chk("spinner: skinned via tokens",
    all(t in wk_style for t in ("var(--line)", "var(--accent)", "var(--panel2)", "var(--info)", "var(--dim)")))
chk("spinner: retired dark hexes gone",
    not any(h in working for h in ("#232936", "#3b6cff", "#8a909c", "#1a1f29", "#2b5cff", "#6aa9ff")))

# ── roster.html_view: template CSS strings dark hex → tokens ──────────────────
tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="testapp", repo_path=str(tmp), base_branch="dev",
                            protected_branch="main", backlog_backend="none")],
             use_worktree=False)
rpage = roster.html_view(cfg, "")
rstyle = rpage.split("<style>", 1)[1].split("</style>", 1)[0]
chk("roster: zero bare hex in the emitted style block", not HEX.search(rstyle), rstyle)
chk("roster: skinned via tokens",
    all(t in rstyle for t in ("var(--text)", "var(--panel)", "var(--line)", "var(--dim)", "var(--accent)")))
chk("roster: retired dark hexes gone",
    not any(h in rpage for h in ("#c3cad6", "#12161f", "#232936", "#e9ecf1", "#7aa2ff", "#8a929f", "#6b7480", "#1a1f2a")))

# ── dashboard: no hardcoded color-scheme; the page follows the theme toggle ───
chk("dashboard: _TEMPLATE carries no hardcoded color-scheme rule",
    "color-scheme" not in dashboard._TEMPLATE)
dpage = dashboard.render_html([], show_cost=False)
chk("dashboard: rendered page is theme-aware (light override rides along)",
    "data-theme=light" in dpage and "color-scheme:light" in dpage)
chk("dashboard: page body skinned via tokens",
    "background:var(--bg)" in dpage and "color:var(--ink)" in dpage)

# ── warroom health banner: .bad tokenised, dead .ok rule deleted ──────────────
chk("warroom: a healthy unit renders NO banner (the .ok variant was never emitted)",
    warroom.health_banner({"healthy": True, "checks": []}) == "")
bad = warroom.health_banner({"healthy": False,
                             "checks": [{"status": "bad", "name": "disk", "detail": "full"}]})
chk("warroom: unhealthy banner uses the bad class", 'class="healthbar bad"' in bad)
chk("warroom: dead .healthbar.ok CSS rule deleted", ".healthbar.ok" not in warroom._PAGE)
hb_css = warroom._PAGE.split("/* health banner */", 1)[1].split(".hbactions", 1)[0]
chk("warroom: health-banner CSS carries zero bare hex", not HEX.search(hb_css), hb_css)
chk("warroom: .healthbar.bad skinned via the danger tokens",
    "var(--badbg)" in hb_css and "var(--badline)" in hb_css)

# ── every token the sweep consumes is actually defined ────────────────────────
# (var(--undefined) falls back to inherit — looks tokenised, renders wrong)
used = ("--line", "--accent", "--panel2", "--info", "--dim", "--text",
        "--panel", "--badbg", "--badline", "--bad")
defined_in = V._TOKENS_FALLBACK + warroom._PAGE
missing = [t for t in used if f"{t}:" not in defined_in]
chk("every token the sweep uses is defined in the token source of truth",
    not missing, f"missing: {missing}")

print("\n=============== DARK-HEX TOKEN SWEEP QA (EU-778 / EU-555c) ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
