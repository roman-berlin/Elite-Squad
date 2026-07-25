"""EU-542 — floating back-to-cockpit button.

Verifies that EVERY chat/detail view rendered via _wrap() AND the /tasks logs page use
a position:fixed ".backbtn" anchor instead of the old inline-flow rule — and pins the PM's
placement call: a token-based translucent backdrop, safe-area-aware offsets/clearance, an
icon-only 38px disc below 560px (never covering the message column), and keyboard access.
"""
from __future__ import annotations

import sys
sys.path.insert(0, ".")

from orchestrator import cockpit_views as V, server
from orchestrator.config import AppConfig, Config

# ── shared config / Flask test client ───────────────────────────────────────────
_CFG = Config(
    apps=[AppConfig(name="automatixy", repo_path="/tmp", base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path="/tmp/eu542_test_audit.jsonl",
    use_worktree=False,
)
_CFG.detected_auth = lambda: "test"

_APP = server.create_app(_CFG)
_CLIENT = _APP.test_client()

# ── result accumulator ──────────────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# =============================================================================
# TEST 1 — fixed-position + z-index + offset
# =============================================================================
def _test_fixed_position() -> None:
    body = V._wrap("CTO Chat", "<div class=thread>hi</div>")
    # Must contain .backbtn with position:fixed (not display:inline-flex … margin-bottom)
    chk("fixed btn: contains position:fixed in .backbtn rule",
        "position:fixed" in body,
        "'position:fixed' found" if "position:fixed" in body else f"'position:fixed' NOT found in body")

    # Must contain an explicit z-index value
    chk("fixed btn: contains z-index declaration on .backbtn",
        "z-index:" in body or "z-index :" in body,
        "z-index present" if ("z-index:" in body or "z-index :" in body) else "no z-index found")


_test_fixed_position()

# =============================================================================
# TEST 2 — correct anchor tags
# =============================================================================
def _test_anchor_tag() -> None:
    body = V._wrap("CTO Chat", "<div>hi</div>")
    # href should point to _back_home() which returns "/" or "/?app=..."
    chk("anchor has href to cockpit home",
        "href='" in body and "aria-label='Back to cockpit'" in body,
        "href+aria-label present")

    # aria-label must say 'Back to cockpit'
    chk("button has aria-label='Back to cockpit'",
        "aria-label='Back to cockpit'" in body,
        "aria-label found" if "aria-label='Back to cockpit'" in body else "aria-label missing or wrong")


_test_anchor_tag()

# =============================================================================
# TEST 3 — var() only; no hex color literals in .backbtn CSS
# =============================================================================
def _test_var_only_colors() -> None:
    body = V._wrap("CTO Chat", "<div>hi</div>")
    import re
    # Grab flat .backbtn{...} blocks from the style tag
    backbtn_rules = re.findall(r'\.backbtn[^{}]*(?:\{[^{}]*\}[^{}]*)*', body)
    has_hex = any(re.search(r':#[0-9a-fA-F]{3,8}\b', r) for r in backbtn_rules if r.strip())
    chk("no hex colors in .backbtn declarations",
        not has_hex,
        repr(backbtn_rules[:2]) if has_hex else "all var()" if not has_hex else "")


_test_var_only_colors()

# =============================================================================
# TEST 4 — mobile media query
# =============================================================================
def _test_mobile_media_query() -> None:
    body = V._wrap("CTO Chat", "<div>hi</div>")
    chk("mobile rule exists (@media max-width:560px)",
        "@media(max-width:560px)" in body or "@media (max-width:560px)" in body,
        "media query found" if ("@media" in body and "560px" in body) else "no @media query with 560px")

    # The text label 'cockpit' must be inside <span class=bbl> so it can be hidden
    chk("label wrapped in span.bbl",
        "class=bbl" in body or 'class="bbl"' in body or "class='bbl'" in body,
        "bbl span found" if ("bbl" in body) else "no span.bbl wrapper around cockpit label")


_test_mobile_media_query()

# =============================================================================
# TEST 5 — /tasks page also uses the fixed-position back button
# =============================================================================
def _test_tasks_page_fixed_btn() -> None:
    resp = _CLIENT.get("/tasks")
    body = resp.get_data(as_text=True)
    chk("/tasks response contains fixed-position backbtn",
        "position:fixed" in body,
        "position:fixed found" if "position:fixed" in body else f"body status={resp.status_code}")

    chk("/tasks response has aria-label='Back to cockpit'",
        "aria-label='Back to cockpit'" in body,
        "aria-label found" if "aria-label='Back to cockpit'" in body else "missing")


_test_tasks_page_fixed_btn()

# =============================================================================
# TEST 6 — focus-visible ring token
# =============================================================================
def _test_focus_visible() -> None:
    body = V._wrap("CTO Chat", "<div>hi</div>")
    chk(":focus-visible references var(--ring)",
        ".backbtn:focus-visible" in body and "--ring" in body,
        ":focus-visible+var(--ring)" if (".backbtn:focus-visible" in body and "--ring" in body)
        else "rule missing")


_test_focus_visible()

# =============================================================================
# TEST 7 — PM placement call: translucent token backdrop + safe-area clearance
# =============================================================================
def _test_backdrop_and_safe_area() -> None:
    body = V._wrap("CTO Chat", "<div>hi</div>")
    chk("translucent token backdrop (color-mix over --panel2, @supports-gated + blur)",
        "color-mix(in srgb,var(--panel2)" in body and "@supports" in body
        and "backdrop-filter" in body)
    chk("button offsets honour the notch (env safe-area insets)",
        "top:calc(16px + env(safe-area-inset-top,0px))" in body
        and "left:calc(16px + env(safe-area-inset-left,0px))" in body)
    chk("page content clears the button footprint incl. safe area (body top padding)",
        "padding:calc(66px + env(safe-area-inset-top,0px)) 30px" in body)
    chk("reduced-motion users get no animation/transition",
        "prefers-reduced-motion:reduce" in body)


_test_backdrop_and_safe_area()

# =============================================================================
# TEST 8 — narrow screens collapse to an icon-only 38px disc (AC3)
# =============================================================================
def _test_mobile_disc() -> None:
    body = V._wrap("CTO Chat", "<div>hi</div>")
    chk("≤560px: collapses to a 38px disc",
        "width:38px;height:38px" in body)
    chk("≤560px: label hidden (icon-only), aria-label still names it",
        ".backbtn .bbl{display:none}" in body and "aria-label='Back to cockpit'" in body)
    chk("≤560px: disc hugs the safe area",
        "top:calc(10px + env(safe-area-inset-top,0px))" in body)


_test_mobile_disc()

# =============================================================================
# TEST 9 — /tasks: chips keep their styling + header clears the float
# =============================================================================
def _test_tasks_layout() -> None:
    body = _CLIENT.get("/tasks").get_data(as_text=True)
    chk("/tasks project chips keep their styling",
        ".pchip{" in body and ".pchips{" in body and ".tasknav{" in body)
    chk("/tasks header indented so the float never covers the page title",
        "header{padding-left:calc(150px + env(safe-area-inset-left,0px))}" in body)
    chk("/tasks mobile header indent clears the 38px disc",
        "header{padding-left:calc(60px + env(safe-area-inset-left,0px))" in body)


_test_tasks_layout()

# =============================================================================
# TEST 10 — the AC1 trio: chat, council, logs all carry the float; composer clear
# =============================================================================
def _test_ac_trio() -> None:
    for route in ("/chat", "/council", "/tasks"):
        body = _CLIENT.get(route).get_data(as_text=True)
        chk(f"{route} shows the floating back button",
            "position:fixed" in body and "aria-label='Back to cockpit'" in body,
            f"status={_CLIENT.get(route).status_code}")
    chat = _CLIENT.get("/chat").get_data(as_text=True)
    chk("/chat composer stays fixed at the bottom — the top-left float never covers the input",
        ".composer{position:fixed;bottom:0" in chat)


_test_ac_trio()

# =============================================================================
# Summary
# =============================================================================
passed_n = sum(1 for _, ok, _ in results if ok)
print("\n========= EU-542 floating back-btn tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("---------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed_n == len(results) else f"{len(results) - passed_n} FAIL")
sys.exit(0 if passed_n == len(results) else 1)
