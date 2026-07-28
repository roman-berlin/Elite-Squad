"""EU-778 (EU-555c) QA: the four view files outside server.py consume the design tokens —
no bare dark hex left in the swept regions, the dead rules are gone, and /tasks follows
the theme toggle.

Swept surfaces:
  - cockpit_views._working()  — spinner / progress bar (was #232936 / #3b6cff / #1a1f29 …)
  - roster.html_view()        — the roster page's emitted <style> block (was #c3cad6 / #12161f …)
  - dashboard.render_html()   — _TEMPLATE's hardcoded `:root{color-scheme:dark}` (broke light mode)
  - warroom health banner     — .healthbar.bad gradient + border hexes, and the DEAD .ok rule
    (the banner only ever renders class="healthbar bad" — a healthy unit renders no banner at all)
  - server.py page routes (EU-780) — /usage /budget /onboard /forensics /memory /standup
    /council /ship-preview (~33 inline dark hexes → tokens; every page already injects the
    shared token block, so the substitution is what makes light mode render)

We assert the *skin contract* — swept outputs reference the single-source-of-truth tokens via
``var(--…)`` and carry zero bare hex literals — and that every token the sweep uses is actually
DEFINED in the token source of truth (an undefined var() silently falls back to inherit, so a
typo'd token would pass a pure "contains var(--" check while rendering wrong).
"""
import os
import re
import subprocess
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

# ── server.py surfaces (EU-780): every standalone page route rides the tokens ──
# /usage, /budget, /onboard, /forensics, /memory, /standup, /council, /ship-preview
# used to inline ~30 dark hexes (dark cards + unreadable grey text in light mode).
src_server = (Path(__file__).parent.parent / "orchestrator" / "server.py").read_text(encoding="utf-8")
RETIRED_SERVER_HEX = (
    "#8a909c", "#8a929f", "#c4c9d2", "#e9ecf1", "#5c6573", "#6b7480", "#c3cad6", "#aab2c0",
    "#b9a6e6", "#7aa2ff", "#12161f", "#1b2230", "#0d1119", "#1a1f2a", "#101620", "#171226",
    "#2c2148", "#232936", "#222a38", "#1f6f43", "#3b6cff", "#d99a2b", "#f0676b", "#3fb950",
    "#3fb961", "#f0a93f", "#2b5cff", "#2350e6", "#7c3aed", "#6d28d9", "#161b25", "#16203a",
    "#2a3343",
)
chk("server.py: zero retired dark hex literals left in the source",
    not any(h in src_server for h in RETIRED_SERVER_HEX),
    str([h for h in RETIRED_SERVER_HEX if h in src_server]))

from orchestrator import server  # noqa: E402 — imported late: only this section needs the app

tmp2 = Path(tempfile.mkdtemp())


def G2(*a):
    subprocess.run(["git", *a], cwd=tmp2, check=True, capture_output=True, text=True)


subprocess.run(["git", "init", str(tmp2)], check=True, capture_output=True)
G2("config", "user.email", "t@t"); G2("config", "user.name", "t")
G2("checkout", "-b", "DEV")
(tmp2 / "a.txt").write_text("x\n"); G2("add", "-A"); G2("commit", "-m", "base")
G2("branch", "MAIN", "DEV")
(tmp2 / "b.txt").write_text("y\n"); G2("add", "-A"); G2("commit", "-m", "AUTO-9: a change")

app2 = AppConfig(name="automatixy", repo_path=str(tmp2), base_branch="DEV", protected_branch="MAIN",
                 backlog_backend="jira", backlog={"base_url": "https://acme.atlassian.net"})
cfg2 = Config(apps=[app2], use_worktree=False)
cfg2.detected_auth = lambda: "test"
os.environ["GENERAL_COCKPIT_PROMOTE"] = "1"
client = server.create_app(cfg2).test_client()

# #fff is allowed: white on a token accent fill (primary buttons) is correct in BOTH themes.
# The injected token block is EXCLUDED — the palette hexes legitimately live THERE (that's
# the single source every page-side style below must read via var()).
def _bare(page: str) -> list[str]:
    page = page.split("/* END THEME TOKENS */</style>", 1)[-1]
    css = "".join(re.findall(r"<style>(.*?)</style>", page, re.S))
    return [h for h in HEX.findall(css) if h.lower() != "#fff"]


for route in ("/usage", "/budget", "/onboard", "/forensics", "/memory", "/standup", "/council",
              "/ship-preview"):
    body = client.get(f"{route}?app=automatixy").get_data(as_text=True)
    chk(f"{route}: renders with the shared token block (dark + light override)",
        ":root{color-scheme:dark" in body and "data-theme=light" in body)
    chk(f"{route}: <style> blocks carry zero bare hex (except #fff fills)",
        not _bare(body), str(_bare(body)[:6]))
    chk(f"{route}: page consumes var() tokens", "var(--ink)" in body)

# ── every token the sweep consumes is actually defined ────────────────────────
# (var(--undefined) falls back to inherit — looks tokenised, renders wrong)
used = ("--line", "--accent", "--panel2", "--info", "--dim", "--text",
        "--panel", "--badbg", "--badline", "--bad", "--faint", "--ink", "--well",
        "--accentbg", "--accentline", "--brand", "--ok", "--warn", "--okline")
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
