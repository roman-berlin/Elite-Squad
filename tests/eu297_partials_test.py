"""EU-297 (EU-285b): reusable card/button/KPI-metric partials — the first slice that actually
*uses* the EU-296 foundation tokens (--s-*/--t-*/--surface/--r-xl) instead of just defining them.

Guards:
  1. ``cockpit_views._card`` / ``_kpi_metric`` / ``_btn`` are named, reusable functions (not
     copy-pasted markup per call site).
  2. ``_kpi_metric``'s own output carries ``var(--t-2xl)``/``var(--t-xl)`` for the numeral and NO
     raw 40px/30px font-size literal.
  3. ``_card``'s own output carries ``var(--r-xl)``, ``var(--surface)`` and an ``--s-*`` spacing
     token; the EU-296 foundation tokens stay mirrored between ``warroom._PAGE``'s ``:root``
     block and ``cockpit_views._TOKENS_FALLBACK``.
  4. ``_btn``'s own output carries ``var(--r-xl)`` and ``--s-*`` padding tokens, and the
     toolbar-button live call site (``_actbtn``/``_actbar``) renders through it.
  5. ``warroom._kpi_html`` routes numerals through ``cockpit_views._kpi_metric`` rather than
     duplicating the ``<div class=kv>``/``<div class=kl>`` markup inline, and rendering a real
     war-room board (``render_board``) produces a live call site for both ``_kpi_metric`` and
     ``_card`` (the KPI grid + the "Talk to the unit" panel).
"""
import json
import re
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
sys.path.insert(0, ".")

from orchestrator import cockpit_views as V
from orchestrator import warroom
from orchestrator.config import AppConfig, Config

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ── 1) named, reusable partial functions exist ────────────────────────────────────────────
chk("cockpit_views defines _card", callable(getattr(V, "_card", None)))
chk("cockpit_views defines _kpi_metric", callable(getattr(V, "_kpi_metric", None)))
chk("cockpit_views defines _btn", callable(getattr(V, "_btn", None)))

# ── 2) _kpi_metric: carries the label/value, consumes --t-2xl/--t-xl, no raw 30/40px ──────
km = V._kpi_metric("12", "Merged→DEV")
chk("_kpi_metric output contains the value", "12" in km, km)
chk("_kpi_metric output contains the label", "Merged" in km, km)
chk("_kpi_metric numeral consumes a type-scale token",
    "var(--t-2xl)" in km or "var(--t-xl)" in km, km)
chk("_kpi_metric introduces no raw 40px font-size literal", "font-size:40px" not in km, km)
chk("_kpi_metric introduces no raw 30px font-size literal", "font-size:30px" not in km, km)

# ── 3) _card: consumes --r-xl / --surface / an --s-* spacing token ────────────────────────
card = V._card("Talk to the unit", "<p>body</p>")
chk("_card output contains var(--r-xl)", "var(--r-xl)" in card, card)
chk("_card output contains var(--surface)", "var(--surface)" in card, card)
chk("_card output contains an --s-* spacing token", bool(re.search(r"var\(--s-\d\)", card)), card)
chk("_card keeps the title", "Talk to the unit" in card, card)
chk("_card keeps the trusted body HTML", "<p>body</p>" in card, card)

# the EU-296 foundation tokens stay mirrored between _PAGE's :root block and _TOKENS_FALLBACK
m_page = re.search(r":root\{[^}]*\}", warroom._PAGE)
for tok in ("--t-xl", "--t-2xl", "--s-1", "--s-4", "--surface"):
    chk(f"{tok} defined in warroom._PAGE :root", bool(m_page) and f"{tok}:" in m_page.group(0), tok)
    chk(f"{tok} defined in cockpit_views._TOKENS_FALLBACK", f"{tok}:" in V._TOKENS_FALLBACK, tok)

# ── 4) _btn: consumes --r-xl / --s-* padding, and the toolbar call site renders through it ──
btn = V._btn("Run drill", cls="actbtn")
chk("_btn output contains var(--r-xl)", "var(--r-xl)" in btn, btn)
chk("_btn output contains an --s-* padding token", bool(re.search(r"var\(--s-\d\)", btn)), btn)
chk("_btn keeps the label", "Run drill" in btn, btn)

ab = V._actbar(V._actbtn("/api/drill", "Run drill"))
chk("_actbar's button renders via the shared .btn base class", 'class="btn actbtn"' in ab, ab)
chk("_actbar's button carries the --r-xl radius token", "var(--r-xl)" in ab, ab)

# ── 5) warroom._kpi_html routes numerals through cockpit_views._kpi_metric ────────────────
non_security_html = warroom._kpi_html([{"label": "Merged → DEV today", "value": 5, "hint": "x",
                                         "href": "/merge-stats"}])
chk("warroom._kpi_html routes non-security cards through the kpim marker (not inline)",
    "kpim" in non_security_html, non_security_html)
chk("warroom._kpi_html numeral consumes --t-2xl", "var(--t-2xl)" in non_security_html,
    non_security_html)
chk("warroom._kpi_html introduces no raw 30px font-size literal at this call site",
    "font-size:30px" not in non_security_html, non_security_html)

# security cards are unaffected (still <details>, unchanged branch)
sec_html = warroom._kpi_html([{"label": "Security blocks", "value": 0, "hint": "x", "tone": "bad",
                                "href": "/forensics?cat=security_block",
                                "security_block_findings": []}])
chk("warroom._kpi_html security branch stays a <details> card (unchanged)",
    "<details" in sec_html, sec_html)

# ── live war-room integration: rendering a real board produces a real _kpi_metric AND _card
#    call site (EU-297 proof-of-integration) ──────────────────────────────────────────────
tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"
with audit.open("a") as f:
    f.write(json.dumps({"event": "ticket_start", "ticket_id": "AUTO-1", "app": "automatixy",
                         "ts": "2026-07-14T09:00:00"}) + "\n")
    f.write(json.dumps({"event": "merged", "ticket_id": "AUTO-1", "app": "automatixy",
                         "ts": "2026-07-14T09:01:00"}) + "\n")

cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                              protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(audit), use_worktree=False)
board = warroom.render_board(cfg, "automatixy", {})
chk("live board render includes a _kpi_metric call site (kpim marker)", "kpim" in board,
    board[:2000])
chk("live board render includes a _card call site (--surface + --r-xl tokens)",
    "class=card style=\"background:var(--surface)" in board, board[:2000])
chk("live board still shows 'Talk to the unit'", "Talk to the unit" in board)

print("\n=============== EU-297 REUSABLE PARTIALS QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det[:200]})" if det and not ok else ""))
print("-------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
