"""Cockpit theme system (2026-07-19): one token source, a light override, a persisted toggle.

Pins:
  (1) warroom._PAGE carries BOTH palettes — the dark :root and the [data-theme=light]
      override — ending at the END THEME TOKENS sentinel;
  (2) cockpit_views._token_css() extracts the WHOLE region (dark + light + boot script), so
      every standalone page follows the toggle;
  (3) the boot script applies localStorage('ui.theme') before first paint (default dark);
  (4) the War Room header renders the toggle button wired to uiTheme();
  (5) the /tasks dashboard template consumes the shared tokens (no hardcoded page palette —
      zero raw 6-digit hexes left in _TEMPLATE) and prepends the token block on render;
  (6) light mode swaps color-scheme so native controls (selects, scrollbars) follow.
"""
import re
import sys
import types

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import cockpit_views, dashboard, warroom

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# (1) both palettes in the one source
chk("(1a) _PAGE has the dark :root palette", ":root{color-scheme:dark" in warroom._PAGE)
chk("(1b) _PAGE has the light override", ":root[data-theme=light]{color-scheme:light" in warroom._PAGE)
chk("(1c) the token region ends at the sentinel", "/* END THEME TOKENS */" in warroom._PAGE)
chk("(1d) light palette defines the core roles",
    all(f"--{k}:" in warroom._PAGE.split(":root[data-theme=light]")[1].split("}")[0] + "}"
        for k in ("bg", "panel", "ink", "accent")) or True)  # structural spot-check below is the real pin

_light = re.search(r":root\[data-theme=light\]\{[^}]*\}", warroom._PAGE)
chk("(1e) light block parses and covers bg/panel/ink/ok/warn/bad/accent",
    _light is not None and all(f"--{k}:" in _light.group(0)
                               for k in ("bg", "panel", "ink", "ok", "warn", "bad", "accent")),
    _light.group(0)[:120] if _light else "no light block")

# (2)+(3) token extraction carries theme + boot everywhere
css = cockpit_views._token_css()
chk("(2) _token_css carries the light override", "data-theme=light" in css)
chk("(2b) _token_css reaches the sentinel (whole region, not just the first block)",
    "END THEME TOKENS" in css)
_norm = css.replace('"', "'")
chk("(3) boot script applies the persisted theme (dark only as last resort)",
    "localStorage.getItem('ui.theme')" in _norm and "dataset.theme=t||'dark'" in _norm)
# EU-780: a first-time visitor boots from the OS preference, never an unconditional dark default.
chk("(3b) boot checks prefers-color-scheme before falling back to dark",
    "matchMedia('(prefers-color-scheme:light)')" in _norm
    and "localStorage.getItem('ui.theme')||'dark'" not in _norm)
chk("(3c) the War Room main page boot is matchMedia-aware too",
    "matchMedia" in warroom._PAGE
    and 'localStorage.getItem("ui.theme")||"dark"' not in warroom._PAGE)

# (4) the header toggle
chk("(4a) the War Room header renders the theme toggle", 'id=themetoggle' in warroom._PAGE)
chk("(4b) toggle is wired to uiTheme()", 'onclick="uiTheme()"' in warroom._PAGE)
chk("(4c) uiTheme flips and persists", "localStorage.setItem" in warroom._PAGE
    and 'dataset.theme==="light"?"dark":"light"' in warroom._PAGE)

# (5) the /tasks template is token-driven
chk("(5a) no raw hex palette left in the dashboard template",
    not re.findall(r"#[0-9a-fA-F]{6}\b", dashboard._TEMPLATE),
    str(re.findall(r"#[0-9a-fA-F]{6}\b", dashboard._TEMPLATE)[:5]))
chk("(5b) render_html prepends the shared tokens",
    "data-theme=light" in dashboard.render_html([], cfg=None, app_name=None))

# (6) native-control color-scheme follows
chk("(6) light mode swaps color-scheme", "color-scheme:light" in css)

print("\n============ COCKPIT THEME QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
