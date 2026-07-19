"""EU-296 (EU-285a): design-token foundation — spacing scale, type scale, semantic
color-role aliases, added ADDITIVELY to the single-source ``:root`` block.

Guards:
  1. every new token name appears in BOTH ``warroom._PAGE``'s ``:root`` block and
     ``cockpit_views._TOKENS_FALLBACK`` (the mirror ``_token_css()`` falls back to);
  2. the VALUE assigned to each new token is byte-identical between the two blocks
     (mirror stays in sync);
  3. ``_token_css()`` still extracts a single, valid ``:root{…}`` block via the
     ``:root\\{[^}]*\\}`` regex — i.e. no nested ``{}`` was introduced;
  4. the semantic-role tokens resolve to EXISTING palette vars (no new raw hex);
  5. no pre-existing selector/declaration was touched — this file only asserts on
     additions, and a separate check confirms the pre-EU-296 token set is untouched.
"""
import re
import sys
import types

sys.path.insert(0, ".")
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


_sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = _sdk

from orchestrator import cockpit_views as V
from orchestrator import warroom

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


SPACING = [f"--s-{i}" for i in range(1, 7)]
TYPE = ["--t-xs", "--t-sm", "--t-md", "--t-lg", "--t-xl", "--t-2xl"]
SEMANTIC = ["--surface", "--border", "--text", "--positive", "--critical"]
NEW_TOKENS = SPACING + TYPE + SEMANTIC

page = warroom._PAGE
fallback = V._TOKENS_FALLBACK

m_page = re.search(r":root\{[^}]*\}", page)
m_fb = re.search(r":root\{[^}]*\}", fallback if fallback.startswith(":root{") else fallback)

# ── 1) every new token name is present in BOTH blocks ────────────────────────────
for tok in NEW_TOKENS:
    chk(f"{tok} present in warroom._PAGE :root", m_page and f"{tok}:" in m_page.group(0), tok)
    chk(f"{tok} present in cockpit_views._TOKENS_FALLBACK", f"{tok}:" in fallback, tok)


def _value_of(block, tok):
    mm = re.search(re.escape(tok) + r":([^;}]+)", block)
    return mm.group(1) if mm else None


# ── 2) values are byte-identical between the two blocks ─────────────────────────
for tok in NEW_TOKENS:
    v_page = _value_of(m_page.group(0), tok) if m_page else None
    v_fb = _value_of(fallback, tok)
    chk(f"{tok} value matches between _PAGE and fallback", v_page is not None and v_page == v_fb,
        f"page={v_page!r} fallback={v_fb!r}")

# ── 3) the :root{…} regex still extracts ONE valid, non-nested block ────────────
chk("warroom._PAGE :root block still scrapes cleanly (no nested braces)",
    m_page is not None and "{" not in m_page.group(0)[len(":root{"):-1])
chk("_token_css() returns the token style block (+ light override + boot, 2026-07-19)",
    V._token_css().startswith("<style>:root{") and "data-theme=light" in V._token_css()
    and "</style>" in V._token_css())

# ── 4) semantic-role tokens resolve to EXISTING palette vars, not new raw hex ────
SEMANTIC_TARGETS = {
    "--surface": "--panel",
    "--border": "--line",
    "--text": "--ink",
    "--positive": "--ok",
    "--critical": "--bad",
}
for tok, target in SEMANTIC_TARGETS.items():
    v = _value_of(m_page.group(0), tok) if m_page else None
    chk(f"{tok} resolves to existing palette var var({target})", v == f"var({target})", v)
    chk(f"{tok} introduces no raw hex", v is not None and "#" not in v, v)

# ── 5) the pre-existing (EU-39) token set is untouched — additive only ──────────
PRE_EXISTING = ("--bg:", "--panel:", "--panel2:", "--line:", "--line2:", "--ink:", "--dim:",
                 "--faint:", "--ok:", "--okbg:", "--okline:", "--warn:", "--warnbg:", "--warnline:",
                 "--bad:", "--badbg:", "--badline:", "--info:", "--infobg:", "--infoline:",
                 "--accent:", "--accentbg:", "--accentline:", "--mono:",
                 "--r-sm:", "--r-md:", "--r-lg:", "--r-xl:", "--r-pill:",
                 "--shadow-1:", "--shadow-2:", "--shadow-3:", "--ring:", "--t-fast:")
for tok in PRE_EXISTING:
    chk(f"pre-existing token {tok.rstrip(':')} still declared in _PAGE", tok in (m_page.group(0) if m_page else ""))
    chk(f"pre-existing token {tok.rstrip(':')} still declared in fallback", tok in fallback)

print("\n=============== EU-296 DESIGN-TOKEN FOUNDATION QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    if not ok:
        print(f"  [FAIL] {n}" + (f"  ({det})" if det else ""))
print("-------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
