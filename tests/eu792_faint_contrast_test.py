"""EU-792: --faint must pass WCAG AA ≥4.5:1 against --bg in both themes.

Reads --faint and --bg hex values from BOTH warroom._PAGE and
cockpit_views._TOKENS_FALLBACK (the two mirrored source blocks) so every
acceptance criterion is exercised together.

Acceptance criteria verified here:
  1. Dark --faint/#a0aab8 vs --bg/#080a0f → ≈8.43:1 ≥ 4.5
  2. Light --faint/#626978 vs --bg/#eef1f6 → ≈4.87:1 ≥ 4.5
  3. --faint is byte-identical between _PAGE and _TOKENS_FALLBACK for each theme.
"""
import re
import sys
import types

sys.path.insert(0, ".")
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


_sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = _sdk

from orchestrator import cockpit_views as V
from orchestrator import warroom

# ── WCAG 2.1 helpers ────────────────────────────────────────────────────────


def _srgb_to_linear(c8: int) -> float:
    s = c8 / 255.0
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4


def _rel_luminance(hex_color: str) -> float:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    return 0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g) + 0.0722 * _srgb_to_linear(b)


def _contrast(l1: float, l2: float) -> float:
    lighter, darker = max(l1, l2), min(l1, l2)
    return round((lighter + 0.05) / (darker + 0.05), 2)


def _extract(block: str, token: str) -> str | None:
    m = re.search(re.escape(token) + r":([^;}\n]+)", block)
    return m.group(1).strip() if m else None


page = warroom._PAGE
fallback = V._TOKENS_FALLBACK
results: list[tuple[str, bool, str]] = []
chk = lambda n, c, d="": results.append((n, bool(c), d))

# ── Extract from dark :root{…} blocks ──────────────────────────────────────

dark_page = re.search(r":root\{[^}]*\}", page)
dark_fb = re.search(r":root\{[^}]*\}", fallback)

dark_page_faint = _extract(dark_page.group(0), "--faint") if dark_page else None
dark_page_bg = _extract(dark_page.group(0), "--bg") if dark_page else None
dark_fb_faint = _extract(dark_fb.group(0), "--faint") if dark_fb else None
dark_fb_bg = _extract(dark_fb.group(0), "--bg") if dark_fb else None

assert dark_page_faint and dark_page_bg, "Could not extract dark tokens from _PAGE"
assert dark_fb_faint and dark_fb_bg, "Could not extract dark tokens from fallback"

# ── Extract from light :root[data-theme=light]{…} blocks ──────────────────

light_page_m = re.search(r":root\[data-theme=light\]\{[^}]*\}", page)
light_fb_m = re.search(r":root\[data-theme=light\]\{[^}]*\}", fallback)

light_page_faint = _extract(light_page_m.group(0), "--faint") if light_page_m else None
light_page_bg = _extract(light_page_m.group(0), "--bg") if light_page_m else None
light_fb_faint = _extract(light_fb_m.group(0), "--faint") if light_fb_m else None
light_fb_bg = _extract(light_fb_m.group(0), "--bg") if light_fb_m else None

assert light_page_faint and light_page_bg, "Could not extract light tokens from _PAGE"
assert light_fb_faint and light_fb_bg, "Could not extract light tokens from fallback"

# ── AC 1: Dark --faint contrast ≥4.5:1 ─────────────────────────────────────

dark_l_faint = _rel_luminance(dark_page_faint)
dark_l_bg = _rel_luminance(dark_page_bg)
dark_ratio = _contrast(dark_l_faint, dark_l_bg)
chk(f"Dark --faint:{dark_page_faint} vs --bg:{dark_page_bg} → {dark_ratio}:1 ≥ 4.5",
    dark_ratio >= 4.5, f"ratio={dark_ratio}:1")

# ── AC 2: Light --faint contrast ≥4.5:1 ─────────────────────────────────────

light_l_faint = _rel_luminance(light_page_faint)
light_l_bg = _rel_luminance(light_page_bg)
light_ratio = _contrast(light_l_faint, light_l_bg)
chk(f"Light --faint:{light_page_faint} vs --bg:{light_page_bg} → {light_ratio}:1 ≥ 4.5",
    light_ratio >= 4.5, f"ratio={light_ratio}:1")

# ── AC 3: Mirror identity — dark==dark, light==light ───────────────────────

chk("--faint dark identical between _PAGE and fallback",
    dark_page_faint == dark_fb_faint,
    f"page={dark_page_faint!r} fallback={dark_fb_faint!r}")
chk("--faint light identical between _PAGE and fallback",
    light_page_faint == light_fb_faint,
    f"page={light_page_faint!r} fallback={light_fb_faint!r}")

print("\n========== EU-792 FAINT CONTRAST QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
