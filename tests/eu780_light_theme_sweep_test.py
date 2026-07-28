"""EU-780 (EU-555 close) QA: the light-theme sweep is complete and cannot drift back.

Covers what the sibling pieces (EU-778 view-file sweep, EU-792 --faint contrast) don't
each cover on their own:

  1. server.py surfaces — the semantic mappings landed: /usage fills ride the
     accent/warn/bad/ok roles, /forensics + /onboard + /ship-preview read from
     panel/well/brand/info tokens (the per-route bare-hex sweep itself lives in
     dark_hex_token_sweep_test.py).
  2. The warroom leftovers (.secbtn:hover, .stopbtn:hover, .synced) and the cockpit
     chrome (qa-strip, deploy msg, plan-limit banner) are tokenised; the banner's
     doubled ⚠ is gone (exactly one warning glyph).
  3. ONE token source: ``cockpit_views._TOKENS_FALLBACK`` is byte-for-byte the same
     region ``_token_css()`` slices out of ``warroom._PAGE`` (no regex scraping), so
     the main page and every sub-page emit byte-identical :root blocks and the two
     copies can never silently diverge (--brand + the light shadow/ring overrides
     ride along).
  4. --faint passes WCAG AA ≥4.5:1 against --bg in BOTH themes, in BOTH source blocks
     (the EU-792 fix, re-asserted here so the sweep's identity test can't regress it).
  5. Theme boot defers to the OS prefers-color-scheme — 'dark' is only the last-resort
     fallback, never an unconditional default (main page AND sub-page boot snippets).
"""
import inspect
import os
import re
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import cockpit_views as V
from orchestrator import server, warroom
from orchestrator.config import AppConfig, Config

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# A bare colour hex literal — only ever run against CSS text (HTML entities like &#9888;
# would false-positive), so every check extracts <style> blocks first. #fff is allowed:
# white on a token accent fill (primary buttons) is correct in BOTH themes.
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def bare_hex(css: str) -> list[str]:
    return [h for h in HEX.findall(css) if h.lower() != "#fff"]


# ── fixture: a configured project so every page route renders ─────────────────
tmp = Path(tempfile.mkdtemp())


def G(*a):
    subprocess.run(["git", *a], cwd=tmp, check=True, capture_output=True, text=True)


subprocess.run(["git", "init", str(tmp)], check=True, capture_output=True)
G("config", "user.email", "t@t"); G("config", "user.name", "t")
G("checkout", "-b", "DEV")
(tmp / "a.txt").write_text("x\n"); G("add", "-A"); G("commit", "-m", "base")
G("branch", "MAIN", "DEV")
(tmp / "b.txt").write_text("y\n"); G("add", "-A"); G("commit", "-m", "AUTO-9: a change")

app = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV", protected_branch="MAIN",
                backlog_backend="jira", backlog={"base_url": "https://acme.atlassian.net"})
cfg = Config(apps=[app], use_worktree=False)
cfg.detected_auth = lambda: "test"
os.environ["GENERAL_COCKPIT_PROMOTE"] = "1"
client = server.create_app(cfg).test_client()

# ── 1) server.py surfaces: the semantic mappings actually landed on the pages ──
# (the full per-route bare-hex sweep lives in dark_hex_token_sweep_test.py; here we
# pin that the specific EU-555 bypass colours now read from the right token ROLES.)
usage_body = client.get("/usage?app=automatixy").get_data(as_text=True)
chk("usage: bar fills ride the accent/warn/bad/ok tokens",
    all(t in usage_body for t in ("background:var(--accent)", "background:var(--warn)",
                                  "background:var(--bad)", "background:var(--ok)")))
fx_body = client.get("/forensics?app=automatixy").get_data(as_text=True)
chk("forensics: rows/bars tokenised (panel/line2/well/accent)",
    all(t in fx_body for t in ("background:var(--panel)", "var(--line2)",
                               "background:var(--well)", "background:var(--accent)")))
ob_body = client.get("/onboard?app=automatixy").get_data(as_text=True)
chk("onboard: form/chips tokenised (panel/panel2/accentbg/faint)",
    all(t in ob_body for t in ("background:var(--panel)", "background:var(--panel2)",
                               "background:var(--accentbg)", "color:var(--faint)")))
ship_body = client.get("/ship-preview?app=automatixy").get_data(as_text=True)
chk("ship-preview: header + go button ride brand/info/accentline tokens",
    all(t in ship_body for t in ("background:var(--brand)", "color:var(--info)",
                                 "border:1px solid var(--accentline)")))

# ── 2) warroom leftovers + cockpit chrome ─────────────────────────────────────
src_views = (Path(__file__).parent.parent / "orchestrator" / "cockpit_views.py").read_text(encoding="utf-8")
chk("warroom: .secbtn:hover / .stopbtn:hover / .synced tokenised",
    ".secbtn:hover{background:var(--accent-hover)}" in warroom._PAGE
    and ".stopbtn:hover{background:var(--badline)}" in warroom._PAGE
    and "color:var(--dim);letter-spacing:.02em" in warroom._PAGE)
chk("warroom: retired hover/status hexes gone from _PAGE",
    not any(h in warroom._PAGE for h in ("#3b5ecc", "#3a181b", "#5b6b86")))
chk("cockpit_views: qa-strip + deploy msg ride --info (deploy strip was ~1.1:1 on light accentbg)",
    "color:var(--info);font-size:13px;font-weight:650}}" in src_views
    and "#cfe0ff" not in src_views)

banner = V._plan_limit_banner({"plan_limit_hit": True,
                               "plan_limit_reset_at": time.time() + 3600}, cfg=None)
chk("plan-limit banner renders", "plan limit reached" in banner)
# hex-check the inline style values only — HTML entities (&#9888;) would false-positive
banner_css = "".join(re.findall(r"style=['\"]([^'\"]*)['\"]", banner))
chk("plan-limit banner: zero bare hex (rides badbg/badline/bad/ink tokens)",
    not bare_hex(banner_css), str(bare_hex(banner_css)[:6]))
chk("plan-limit banner: tokenised via the danger roles",
    all(t in banner for t in ("var(--badbg)", "var(--badline)", "var(--bad)", "var(--ink)")))
chk("plan-limit banner: exactly ONE warning glyph (the doubled ⚠ is gone)",
    banner.count("&#9888;") == 1, f"count={banner.count('&#9888;')}")

# ── 3) one token source — byte-identical main page / sub-page :root blocks ────
page = warroom._PAGE
s = page.index(":root{color-scheme:dark;")
e = page.index("/* END THEME TOKENS */") + len("/* END THEME TOKENS */")
main_region = page[s:e]

_token_css_src = inspect.getsource(V._token_css)
chk("cockpit_views no longer regex-scrapes the token block (index-sliced)",
    "re.search" not in _token_css_src and ".index(" in _token_css_src
    and 'r":root' not in src_views)
chk("_TOKENS_FALLBACK is byte-for-byte the warroom._PAGE token region",
    V._TOKENS_FALLBACK == main_region,
    f"lens {len(V._TOKENS_FALLBACK)} vs {len(main_region)}")
chk("fallback carries the tokens the old copy was missing (--brand + light shadow/ring)",
    "--brand:#ff7a59" in V._TOKENS_FALLBACK and "--brand:#e8590c" in V._TOKENS_FALLBACK
    and "--shadow-1:0 1px 2px rgba(23,32,54,.08)" in V._TOKENS_FALLBACK
    and "--ring:0 0 0 2px var(--bg),0 0 0 4px rgba(59,98,217,.5)" in V._TOKENS_FALLBACK)
chk("live _token_css() emits <style> + that exact region + boot",
    V._token_css() == "<style>" + main_region + "</style>" + V._THEME_BOOT)

sub = V._wrap("EU-780 probe", "<p>x</p>")
sub_region = sub.split("<style>", 1)[1].split("/* END THEME TOKENS */", 1)[0] + "/* END THEME TOKENS */"
chk("sub-page :root blocks are byte-identical to the main page's",
    sub_region == main_region,
    f"lens {len(sub_region)} vs {len(main_region)}")

# ── 4) --faint contrast ≥4.5:1 in BOTH themes, BOTH source blocks ─────────────


def _lin(c8: int) -> float:
    s = c8 / 255.0
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4


def _lum(hexcolor: str) -> float:
    h = hexcolor.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _ratio(a: str, b: str) -> float:
    l1, l2 = _lum(a), _lum(b)
    lighter, darker = max(l1, l2), min(l1, l2)
    return round((lighter + 0.05) / (darker + 0.05), 2)


def _of(block: str, tok: str) -> str:
    return re.search(re.escape(tok) + r":([^;}\n]+)", block).group(1).strip()


for name, source in (("warroom._PAGE", page), ("cockpit_views._TOKENS_FALLBACK", V._TOKENS_FALLBACK)):
    dark = re.search(r":root\{[^}]*\}", source).group(0)
    light = re.search(r":root\[data-theme=light\]\{[^}]*\}", source).group(0)
    dr = _ratio(_of(dark, "--faint"), _of(dark, "--bg"))
    lr = _ratio(_of(light, "--faint"), _of(light, "--bg"))
    chk(f"{name}: dark --faint:{_of(dark, '--faint')} vs --bg:{_of(dark, '--bg')} → {dr}:1 ≥ 4.5",
        dr >= 4.5, f"ratio={dr}")
    chk(f"{name}: light --faint:{_of(light, '--faint')} vs --bg:{_of(light, '--bg')} → {lr}:1 ≥ 4.5",
        lr >= 4.5, f"ratio={lr}")

# ── 5) theme boot: OS preference first, dark only as last resort ──────────────
boot = V._THEME_BOOT.replace('"', "'")
chk("_THEME_BOOT probes prefers-color-scheme before any dark default",
    "matchMedia('(prefers-color-scheme:light)')" in boot
    and "if(!t&&window.matchMedia" in boot)
chk("_THEME_BOOT: no unconditional localStorage||'dark' fallback remains",
    "localStorage.getItem('ui.theme')||'dark'" not in boot
    and "dataset.theme=t||'dark'" in boot)
page_boot = page.replace('"', "'")
chk("warroom main-page boot is matchMedia-aware (no hardcoded dark default)",
    "matchMedia('(prefers-color-scheme:light)')" in page_boot
    and "localStorage.getItem('ui.theme')||'dark'" not in page_boot)

print("\n=============== EU-780 LIGHT-THEME SWEEP QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
