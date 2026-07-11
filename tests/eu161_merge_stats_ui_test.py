"""EU-161 — Modernize merge statistics page UI/UX.

Covers the ticket's six testable acceptance criteria (static-markup checks — this repo's test
harness has no browser/JS runtime, so client-side behaviour is verified by asserting the required
DOM elements/attributes/CSS are present in the server-rendered markup and that the shipped inline
script actually wires them up):

  1. Stats sit in a responsive container (grid) with a mobile-width media query that collapses to
     a single column.
  2. Semantic structure: a <main>, section headings, and a text label associated with each stat
     value (via aria-describedby) — colours pulled from design-token vars, not raw hex.
  3. A loading placeholder (aria-busy) is present in the initial markup, hidden by default.
  4. An error state (role=alert) with a retry affordance is present in the initial markup, and the
     client JS actually shows it on a failed fetch / wires the retry button.
  5. Stat values carry a CSS transition using var(--t-fast), and the client JS toggles a
     transition/fade class on them when a range switch resolves.
  6. Interactive controls are real <button> elements (so the existing global :focus-visible/
     var(--ring) rule applies) and stat text/background pairs use design tokens (contrast).
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import time
import types
from pathlib import Path

# ── minimal SDK / requests stubs so the orchestrator can import without real deps ──
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import server
from orchestrator.config import AppConfig, Config

# ── shared config / audit path / Flask test client ─────────────────────────────
_TMP = Path(tempfile.mkdtemp())
_AUDIT = _TMP / "audit.jsonl"
_CFG = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_TMP), base_branch="DEV",
                     protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(_AUDIT),
    use_worktree=False,
)
_CFG.detected_auth = lambda: "test"

_APP = server.create_app(_CFG)
_CLIENT = _APP.test_client()

# ── result accumulator ──────────────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


def _reset_audit() -> None:
    _AUDIT.parent.mkdir(parents=True, exist_ok=True)
    _AUDIT.write_text("", encoding="utf-8")


def _write_event(event: str, ts_epoch: float, seq: list[int]) -> None:
    seq[0] += 1
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts_epoch)), "event": event,
           "ticket_id": f"TEST-{seq[0]}"}
    with _AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _mscss(body: str) -> str:
    """Slice out just the merge-stats page's own <style> block (identified by a selector unique
    to this page) so token/hex assertions don't get confused by the shared chrome CSS that _wrap
    injects (which legitimately DEFINES the hex values behind the vars). Splits on each literal
    '<style>' rather than a single regex so it can't accidentally span across earlier/later
    <style>...</style> blocks (e.g. the :root token block) to reach '.msgrid'."""
    for chunk in body.split("<style>")[1:]:
        block = chunk.split("</style>", 1)[0]
        if ".msgrid" in block:
            return block
    return ""


# =============================================================================
# (1) responsive container: grid + mobile-width media query collapsing to 1 column
# =============================================================================

def _test_responsive_grid_with_mobile_breakpoint() -> None:
    _reset_audit()
    for r in ("today", "week", "month", "all"):
        resp = _CLIENT.get(f"/merge-stats?time_range={r}")
        chk(f"GET /merge-stats?time_range={r}: HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
    body = _CLIENT.get("/merge-stats").get_data(as_text=True)
    css = _mscss(body)
    chk("stats container is a CSS grid", "display:grid" in css, css[:400])
    chk("has a mobile-width media query", "@media" in css and "max-width" in css, css[:600])
    m = re.search(r"@media[^{]*\{[^}]*\.msgrid\{[^}]*grid-template-columns:1fr", css)
    chk("media query collapses .msgrid to a single column", m is not None, css[:800])


_test_responsive_grid_with_mobile_breakpoint()

# =============================================================================
# (2) semantic structure + label-per-stat + design tokens (not raw hex)
# =============================================================================

def _test_semantic_structure_and_tokens() -> None:
    _reset_audit()
    body = _CLIENT.get("/merge-stats").get_data(as_text=True)
    chk("page has a <main> landmark", "<main" in body, body[:400])
    chk("page has section heading(s) (h1/h2)", "<h1" in body or "<h2" in body)
    chk("each stat value is associated with a text label via aria-describedby",
        "aria-describedby=ms-total-label" in body and "aria-describedby=ms-pr-label" in body
        and "aria-describedby=ms-sr-label" in body, body[-1200:])
    chk("the referenced labels exist with matching ids",
        "id=ms-total-label" in body and "id=ms-pr-label" in body and "id=ms-sr-label" in body)
    css = _mscss(body)
    chk("stat card background pulls from var(--panel)", "background:var(--panel)" in css, css[:400])
    chk("stat value colour pulls from var(--ink)", "color:var(--ink)" in css, css[:400])
    hexes = set(re.findall(r"#[0-9a-fA-F]{3,8}", css))
    chk("no raw hex colour literals in the page's own style block (besides the #fff-on-accent "
        "convention used elsewhere in this app)", hexes <= {"#fff"}, str(hexes))


_test_semantic_structure_and_tokens()

# =============================================================================
# (3) loading placeholder present in initial markup, hidden by default
# =============================================================================

def _test_loading_placeholder_in_initial_markup() -> None:
    _reset_audit()
    body = _CLIENT.get("/merge-stats").get_data(as_text=True)
    chk("a loading element with an id is present", "id=ms-loading" in body, body[-1200:])
    m = re.search(r"<[^>]*id=ms-loading[^>]*>", body)
    chk("loading element found as a single tag", m is not None, body[-1200:])
    if m:
        tag = m.group(0)
        chk("loading element has aria-busy", "aria-busy" in tag, tag)
        chk("loading element is hidden by default (not shown on initial server-rendered load)",
            "hidden" in tag, tag)


_test_loading_placeholder_in_initial_markup()

# =============================================================================
# (4) error state (role=alert) + retry, present in markup and wired by the client JS
# =============================================================================

def _test_error_state_with_retry() -> None:
    _reset_audit()
    body = _CLIENT.get("/merge-stats").get_data(as_text=True)
    m = re.search(r"<[^>]*id=ms-error[^>]*>", body)
    chk("an error container with an id is present", m is not None, body[-1200:])
    if m:
        chk("error container has role=alert", "role=alert" in m.group(0), m.group(0))
        chk("error container is hidden by default", "hidden" in m.group(0), m.group(0))
    chk("a retry control is present", "id=ms-retry" in body, body[-1200:])
    retry_tag = re.search(r"<button[^>]*id=ms-retry[^>]*>", body)
    chk("retry control is a real <button>", retry_tag is not None, body[-1200:])
    chk("client JS handles a failed fetch (.catch) and reveals the error state",
        ".catch(" in body and "ms-error" in body.split("<script>", 1)[-1], body[-1500:])
    chk("client JS wires the retry button to re-attempt the load",
        "ms-retry" in body.split("<script>", 1)[-1] and "addEventListener" in body, body[-1500:])


_test_error_state_with_retry()

# =============================================================================
# (5) transition on stat values using var(--t-fast), toggled by the client JS
# =============================================================================

def _test_transition_on_range_switch() -> None:
    _reset_audit()
    body = _CLIENT.get("/merge-stats").get_data(as_text=True)
    css = _mscss(body)
    m = re.search(r"\.msbig\{[^}]*transition:[^}]*var\(--t-fast\)", css)
    chk(".msbig has a CSS transition using var(--t-fast)", m is not None, css[:600])
    script = body.split("<script>", 1)[-1]
    chk("client JS toggles a fade/transition class on the stat elements after a range switch",
        "classList" in script and ("msfade" in script or "fade" in script), script[-1500:])
    chk("no full page reload on range switch (still uses the JSON API + pushState, not location.href)",
        "fetch('/api/merge-stats?time_range='" in body and "history.pushState" in body
        and "location.href" not in script and "location.reload" not in script, script[-800:])


_test_transition_on_range_switch()

# =============================================================================
# (6) keyboard focus ring on interactive controls + token-based contrast
# =============================================================================

def _test_focus_ring_and_contrast() -> None:
    _reset_audit()
    body = _CLIENT.get("/merge-stats").get_data(as_text=True)
    chk("the shared :focus-visible / var(--ring) rule (from _wrap) is present",
        "focus-visible" in body and "var(--ring)" in body)
    range_tag = re.search(r"<button[^>]*class='msrange[^']*'[^>]*>", body)
    chk("the time-range control is a real <button> (inherits the global focus-visible ring)",
        range_tag is not None, body[:800])
    retry_tag = re.search(r"<button[^>]*id=ms-retry[^>]*>", body)
    chk("the retry control is a real <button> (inherits the global focus-visible ring)",
        retry_tag is not None, body[-1200:])
    css = _mscss(body)
    chk("stat label text uses a design-token colour (contrast pairing with var(--panel))",
        "color:var(--dim)" in css or "color:var(--ink)" in css, css[:600])


_test_focus_ring_and_contrast()

# =============================================================================
# Summary
# =============================================================================
passed_n = sum(1 for _, ok, _ in results if ok)
print("\n========= EU-161 merge-stats UI/UX modernization tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("---------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
