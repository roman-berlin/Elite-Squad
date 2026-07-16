"""EU-299 (EU-285d): toolbar clustering into labeled groups (build · QA · nav).

Acceptance criteria under test:
  1. `_control_bar()` groups the buttons into three labeled clusters — 'build', 'QA', 'nav' —
     each rendered as a cluster label styled with the `--t-xs` token, instead of one flat
     button row inside `.tbar`.
  2. Every existing action is preserved verbatim: Patrol form (action=/api/patrol), Ship review
     (action=/api/ship-review), Jira link (/jira?app=), Roster link (/roster-doc), New-task Run
     form (/api/run), Autopilot forms (/api/autopilot), and the Reports menu links.
  3. Buttons inside the clusters render through the `cockpit_views._btn` partial (shared `.btn`
     base + `_btn`'s inline `--r-xl`/`--s-*`/`--t-md` tokens) — no new hand-rolled button CSS.
  4. Cluster spacing/separation uses `--s-*` spacing tokens, not new ad-hoc px literals.
  5. `python3 tests/run_all.py` stays green (covered by the harness itself, not here).
"""
import re
import sys
import tempfile
import types
from pathlib import Path

# --- stub the Agent SDK / requests so orchestrator modules import cleanly (no network) ---
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


_sdk.__getattr__ = lambda _n: _D
sys.modules["claude_agent_sdk"] = _sdk

_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = _req

sys.path.insert(0, ".")

from orchestrator import cockpit_views as V
from orchestrator.config import AppConfig, Config
from orchestrator import sync as _sync

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail) if not cond else ""))


_sync.can_promote = lambda: False  # keep the bar off git / network

_tmp = Path(tempfile.mkdtemp())
_cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_tmp / "app"), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="jira",
                    backlog={"base_url": "https://acme.atlassian.net"})],
    audit_path=str(_tmp / "audit.jsonl"), use_worktree=False,
)
_cfg.detected_auth = lambda: "test"

bar = V._control_bar(_cfg, "automatixy", healthy=True, is_mac=True)

# ---------------------------------------------------------------------------
# 1. Three labeled clusters — build / QA / nav — each styled with --t-xs
# ---------------------------------------------------------------------------
cluster_label_re = re.compile(
    r'class="?tclabel"?[^>]*font-size:var\(--t-xs\)[^>]*>(build|QA|nav)<'
    r'|font-size:var\(--t-xs\)[^>]*class="?tclabel"?[^>]*>(build|QA|nav)<'
)
# Simpler robust check: the .tclabel CSS rule uses --t-xs, and each label text sits inside a
# <span class=tclabel> (or similar) element wrapping exactly 'build' / 'QA' / 'nav'.
chk("a .tclabel rule (or inline style) ties cluster labels to the --t-xs token",
    ("tclabel" in bar) and ("--t-xs" in bar),
    "no tclabel styling referencing --t-xs found in the control bar output")

for _lbl in ("build", "QA", "nav"):
    chk(f"cluster label '{_lbl}' renders as its own labeled element (not just free text)",
        re.search(rf'class=[\'"]?[\w -]*tclabel[\w -]*[\'"]?[^>]*>{_lbl}<', bar) is not None,
        f"no <... class=...tclabel...>{_lbl}< element found")

chk("toolbar is no longer one flat run — cluster wrapper elements are present",
    bar.count("tclu") >= 3,
    "expected >=3 cluster wrapper occurrences (class open + any reference)")

chk("<div class=tbar> outer wrapper is preserved",
    "<div class=tbar>" in bar)

# ---------------------------------------------------------------------------
# 2. Every existing action preserved verbatim
# ---------------------------------------------------------------------------
chk("Patrol form action preserved (action=/api/patrol)", "action=/api/patrol" in bar)
chk("Patrol label glyph preserved", "&#128225; Patrol" in bar)
chk("Ship review form action preserved (action=/api/ship-review)", "action=/api/ship-review" in bar)
chk("Ship review label glyph preserved", "&#128640; Ship review" in bar)
chk("Jira link preserved (/jira?app=)", "/jira?app=automatixy" in bar)
chk("Jira label glyph preserved", "&#128268; Jira" in bar)
chk("Roster link preserved (/roster-doc)", 'href="/roster-doc"' in bar)
chk("Roster still a top-level class=\"btn\" element (EU-68/EU-94 contract)",
    'class="btn" href="/roster-doc"' in bar)
chk("New-task Run form preserved (/api/run)", "action=/api/run" in bar)
chk("New-task Run button label preserved", "&#9654; Run" in bar)
chk("Autopilot form preserved (/api/autopilot)", "action=/api/autopilot" in bar)
chk("Open logs link preserved (is_mac=True)", "&#128194; Open logs" in bar and "open-logs" in bar)

for _href in ("/tasks", "/council", "/memory", "/usage", "/budget", "/forensics",
             "/roster-doc"):
    chk(f"Reports menu link preserved: {_href}", f'href="{_href}"' in bar)

# ---------------------------------------------------------------------------
# 3. Buttons render through the _btn partial — no new hand-rolled button CSS
# ---------------------------------------------------------------------------
_btn_marker = "border-radius:var(--r-xl);padding:var(--s-2) var(--s-3);font-size:var(--t-md)"
chk("_btn partial's inline token signature appears in the control bar (buttons routed through it)",
    _btn_marker in bar,
    "no _btn-rendered element found — clusters may be hand-rolling button markup")
chk("_btn signature appears more than once (multiple buttons routed through the partial)",
    bar.count(_btn_marker) >= 3, f"only {bar.count(_btn_marker)} occurrence(s)")

# ---------------------------------------------------------------------------
# 4. Cluster spacing uses --s-* tokens, not new ad-hoc px
# ---------------------------------------------------------------------------
style_blocks = re.findall(r"<style>(.*?)</style>", bar, re.S)
assert style_blocks, "control bar must emit a <style> block"
style_block = "\n".join(style_blocks)  # tab_bar and the control bar each emit their own <style>
tclu_rules = "\n".join(l for l in style_block.split("}") if "tclu" in l or "tclabel" in l or "tcrow" in l)
chk("new cluster CSS rules exist", bool(tclu_rules.strip()), "no .tclu/.tclabel/.tcrow rules found")
_px_literals = re.findall(r"[:\s](\d+)px", tclu_rules)
chk("no new ad-hoc px literals in the cluster grouping CSS (uses var(--s-*) instead)",
    not _px_literals, f"found px literals in cluster CSS: {_px_literals}")

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
print("\n========== EU-299 TOOLBAR CLUSTERS QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
