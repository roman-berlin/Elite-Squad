"""EU-804 hex sweep: deploy-strip and chat-bubble surfaces carry zero raw hex literals.

Replaces hardcoded hex in two regions of cockpit_views:
  - Deploy strip  : .qa-strip and .deploybar .dmsg  (#cfe0ff → var(--ink))
  - Chat bubbles  : .msg.you .bub                   (#1e3a5f/#eaf1fb → var(--accentbg)/var(--ink))
                  : .aim                            (#9be7bd → var(--ok))

We assert the *skin contract* — swept outputs reference tokens via var(--…) and carry
zero bare hex literals in their CSS declarations, and that every token consumed passes
WCAG AA ≥4.5:1 against its background in light mode.

Test strategy: read the cockpit_views module SOURCE (the CSS is baked into f-string
templates inside the module), not render-and-fetch — same approach as dark_hex_token_sweep_test.
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

# ── Helpers ───────────────────────────────────────────────────────────────

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# WCAG 2.1 helpers (mirrors eu792_faint_contrast_test)
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


# A bare colour hex literal (#rgb / #rrggbb / #rrggbbaa).
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")

# Read the ENTIRE module source — deploy-strip CSS lives inside f-string templates
# (_TEMPLATE blocks), so rendering would lose structural context.
import inspect
cockpit_source = inspect.getsource(V)

# ── AC 1: Deploy strip — no raw hex in .qa-strip / .deploybar .dmsg ──────

# NOTE: cockpit_views CSS inside templates uses {{ }} (f-string escaping).
# Match both single-brace (raw Python) and double-brace (escaped) forms.
def _braces(pattern: str) -> str:
    """Replace literal '{' with '[{]+' and '}' with '}+' so the regex matches {{}} and {}."""
    return pattern.replace("{", "[{]+").replace("}", "}+")

deploy_region_re = re.compile(_braces(
    r'.qa-strip{[^}]*}|.deploybar\s*{[^}]*}|.deploybar\s+\.\w*{[^}]*}'
))
deploy_hits = deploy_region_re.findall(cockpit_source)
deploy_css = "\n".join(deploy_hits)

chk("deploy strip: zero bare hex in .qa-strip selector",
    not HEX.search(deploy_css), f".qa-strip region: {deploy_css!r}")
chk("deploy strip: retired #cfe0ff gone from .qa-strip",
    "#cfe0ff" not in deploy_css, f".qa-strip region: {deploy_css!r}")

# Also check the specific dmsg rule (its parent selector is .deploybar)
dmsg_re = re.compile(_braces(r'.deploybar\s+\.dmsg{[^}]*}'))
dmsg_match = dmsg_re.search(cockpit_source)
if dmsg_match:
    dmsg_css = dmsg_match.group(0)
    chk("deploy strip: zero bare hex in .deploybar .dmsg selector",
        not HEX.search(dmsg_css), f".dmsg region: {dmsg_css!r}")
    chk("deploy strip: .deploybar .dmsg uses a token for color",
        "color:var(--" in dmsg_css or "color:none" in dmsg_css,
        f".dmsg region: {dmsg_css!r}")
else:
    chk("deploy strip: .deploybar .dmsg rule exists in source", False, ".dmsg rule not found by regex")

# Verify the deployed text color IS var(--ink)
chk("deploy strip: .qa-strip text color uses var(--ink)",
    re.search(_braces(r'.qa-strip{[^}]*color:var\(--ink\)'), deploy_css),
    f".qa-strip region: {deploy_css!r}")
chk("deploy strip: .deploybar .dmsg text color uses var(--ink)",
    dmsg_match and "color:var(--ink)" in dmsg_match.group(0),
    f".dmsg region: {dmsg_match.group(0) if dmsg_match else 'NOT FOUND'}")

# ── AC 2: Deploy-strip token contrast ≥4.5:1 on --accentbg (light) ──────

fallback = V._TOKENS_FALLBACK

# Extract light theme overrides
light_m = re.search(r":root\[data-theme=light\]\{[^}]*\}", fallback)
assert light_m, "Could not extract light :root from _TOKENS_FALLBACK"
light_block = light_m.group(0)

ink_val = _extract(light_block, "--ink")
accentbg_val = _extract(light_block, "--accentbg")
assert ink_val and accentbg_val, f"Missing light tokens: --ink={ink_val}, --accentbg={accentbg_val}"

ink_lum = _rel_luminance(ink_val)
accentbg_lum = _rel_luminance(accentbg_val)
accent_ratio = _contrast(ink_lum, accentbg_lum)

chk(f"--ink:{ink_val} on --accentbg:{accentbg_val} → {accent_ratio}:1 ≥ 4.5 (light)",
    accent_ratio >= 4.5, f"ratio={accent_ratio}:1, ink={ink_val}, accentbg={accentbg_val}")

# ── AC 3: Chat bubbles — no raw hex in _CHAT_STYLE ──────────────────────

chat_style = V._CHAT_STYLE

# Check .msg.you .bub specifically
you_bub_re = re.compile(r'\.msg\.you\s+\.bub\{[^}]*\}')
you_bub_match = you_bub_re.search(chat_style)
assert you_bub_match, ".msg.you .bub rule not found in _CHAT_STYLE"
you_bub_css = you_bub_match.group(0)

chk("chat bubble (.msg.you .bub): zero bare hex in background",
    not re.search(r"background:#[0-9a-fA-F]{3,8}\b", you_bub_css),
    f".msg.you .bub: {you_bub_css!r}")
chk("chat bubble (.msg.you .bub): zero bare hex in color",
    not re.search(r"color:#[0-9a-fA-F]{3,8}\b", you_bub_css),
    f".msg.you .bub: {you_bub_css!r}")
chk("chat bubble (.msg.you .bub): retired #1e3a5f gone",
    "#1e3a5f" not in you_bub_css, f".msg.you .bub: {you_bub_css!r}")
chk("chat bubble (.msg.you .bub): retired #eaf1fb gone",
    "#eaf1fb" not in you_bub_css, f".msg.you .bub: {you_bub_css!r}")
chk("chat bubble (.msg.you .bub): background uses var(--accentbg)",
    "background:var(--accentbg)" in you_bub_css,
    f".msg.you .bub: {you_bub_css!r}")
chk("chat bubble (.msg.you .bub): color uses var(--ink)",
    "color:var(--ink)" in you_bub_css,
    f".msg.you .bub: {you_bub_css!r}")

# Check .aim
aim_re = re.compile(r'\.aim\{[^}]*\}')
aim_match = aim_re.search(chat_style)
assert aim_match, ".aim rule not found in _CHAT_STYLE"
aim_css = aim_match.group(0)

chk("chat bubble (.aim): zero bare hex in color",
    not re.search(r"color:#[0-9a-fA-F]{3,8}\b", aim_css),
    f".aim: {aim_css!r}")
chk("chat bubble (.aim): retired #9be7bd gone",
    "#9be7bd" not in aim_css, f".aim: {aim_css!r}")
chk("chat bubble (.aim): color uses var(--ok)",
    "color:var(--ok)" in aim_css,
    f".aim: {aim_css!r}")

# ── Summary ───────────────────────────────────────────────────────────────

print("\n=============== EU-804 HEX DEPLOY+CHAT SWEEP QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
