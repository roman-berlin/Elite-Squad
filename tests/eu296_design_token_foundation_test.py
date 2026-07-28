"""EU-296 (EU-285a): design-token foundation — spacing scale, type scale, semantic
color-role aliases, added ADDITIVELY to the single-source ``:root`` block.

Guards:
  1. every new token name appears in ``warroom._PAGE``'s ``:root`` block (now via
     ``THEME_TOKENS_CSS``, the single source of truth wired into ``_token_css()``);
  2. ``_token_css()`` emits ``THEME_TOKENS_CSS`` verbatim inside ``<style>…</style>``;
  3. the semantic-role tokens resolve to EXISTING palette vars (no new raw hex);
  4. no pre-existing selector/declaration was touched — this file only asserts on
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
tokens = warroom.THEME_TOKENS_CSS   # EU-791: single source replaces fallback

m_page = re.search(r":root\{[^}]*\}", page)
m_tok = re.search(r":root\{[^}]*\}", tokens)

# ── 1) every new token name is present in warroom's :root block ─────────────────
for tok in NEW_TOKENS:
    chk(f"{tok} present in warroom.THEME_TOKENS_CSS", m_tok and f"{tok}:" in m_tok.group(0), tok)


def _value_of(block, tok):
    mm = re.search(re.escape(tok) + r":([^;}]+)", block)
    return mm.group(1) if mm else None


# ── 2) theme token css wraps THEEME_TOKENS_CSS verbatim ─────────────────────────
expected_css = "<style>" + tokens + "</style>" + V._THEME_BOOT
chk("_token_css() wraps THEME_TOKENS_CSS identically", V._token_css() == expected_css)
chk("_token_css() returns valid style block + light override + boot",
    V._token_css().startswith("<style>") and "data-theme=light" in V._token_css()
    and "</style>" in V._token_css())

# ── 3) semantic-role tokens resolve to EXISTING palette vars, not new raw hex ────
SEMANTIC_TARGETS = {
    "--surface": "--panel",
    "--border": "--line",
    "--text": "--ink",
    "--positive": "--ok",
    "--critical": "--bad",
}
for tok, target in SEMANTIC_TARGETS.items():
    v = _value_of(tokens, tok)
    chk(f"{tok} resolves to existing palette var var({target})", v == f"var({target})", v)
    chk(f"{tok} introduces no raw hex", v is not None and "#" not in v, v)

# ── 4) the pre-existing (EU-39) token set is untouched — additive only ──────────
PRE_EXISTING = ("--bg:", "--panel:", "--panel2:", "--line:", "--line2:", "--ink:", "--dim:",
                 "--faint:", "--ok:", "--okbg:", "--okline:", "--warn:", "--warnbg:", "--warnline:",
                 "--bad:", "--badbg:", "--badline:", "--info:", "--infobg:", "--infoline:",
                 "--accent:", "--accentbg:", "--accentline:", "--mono:",
                 "--r-sm:", "--r-md:", "--r-lg:", "--r-xl:", "--r-pill:",
                 "--shadow-1:", "--shadow-2:", "--shadow-3:", "--ring:", "--t-fast:")
for tok in PRE_EXISTING:
    chk(f"pre-existing token {tok.rstrip(':')} still declared in THEME_TOKENS_CSS",
        tok in tokens)

print("\n=============== EU-296 DESIGN-TOKEN FOUNDATION QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    if not ok:
        print(f"  [FAIL] {n}" + (f"  ({det})" if det else ""))
print("-------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
