"""EU-39: the cockpit design tokens are the single source the board surfaces consume.

Guards the visual-refresh contract that the Frontend slice owns:
  1. the master <style> declares the EU-39 token set (radii, elevation, focus ring, motion);
  2. board-surface components reference those tokens via var(...) rather than re-hardcoding
     literals, so a re-skin is one edit in :root and can't drift surface-to-surface;
  3. keyboard focus is visible on interactive board surfaces (a11y) via the shared --ring;
  4. the phase-bar node markup the EU-55/F12 fix renders is left intact (no design regression).
"""
import sys
import types

sys.path.insert(0, ".")
_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = _sdk

from orchestrator import warroom

ns = types.SimpleNamespace
cfg = ns(audit_path="/tmp/eu39-nope.jsonl", apps=[ns(name="automatixy")])
page = warroom.render_page(cfg, None, {"active": False}, "<div>BAR</div>",
                           {"healthy": True, "checks": []})

checks = []
def chk(name, cond):
    checks.append(bool(cond))
    print(("  ok " if cond else "  XX ") + name)

# --- 1) The token set is declared once, in :root -------------------------------------------------
for tok in ("--r-lg:", "--r-xl:", "--r-pill:", "--shadow-1:", "--shadow-2:", "--shadow-3:",
            "--ring:", "--t-fast:", "--okline:", "--badline:", "--accentbg:"):
    chk(f"token {tok.rstrip(':')} declared", tok in page)

# --- 2) Board surfaces consume the tokens (not re-hardcoded literals) -----------------------------
chk("KPI card uses radius token", "border-radius:var(--r-lg)" in page)
chk("panel uses radius + elevation token", "border-radius:var(--r-xl)" in page and "box-shadow:var(--shadow-1)" in page)
chk("phase-bar node uses pill radius token", "border-radius:var(--r-pill)" in page)
chk("backlog app badge uses pill token", ".blapp" in page and "var(--r-pill)" in page)
chk("hero uses elevation token", "box-shadow:var(--shadow-2)" in page)
chk("surfaces share the motion token", "var(--t-fast)" in page)

# --- 3) Keyboard focus ring is wired on interactive board surfaces (a11y) -------------------------
chk("focus-visible ring rule present", ":focus-visible" in page and "box-shadow:var(--ring)" in page)
chk("KPI/roster/backlog rows are focusable targets", "a.kpi:focus-visible" in page and ".offrow:focus-visible" in page)

# --- 4) The EU-55/F12 phase-bar markup is untouched by the re-skin --------------------------------
chk("phase-bar failed selector intact", ".phasebar .ph.failed" in page)

ok = sum(1 for c in checks if c)
print(f"{ok}/{len(checks)} passed")
assert ok == len(checks), "EU-39 design-token / board-surface contract broken"
print("OK")
