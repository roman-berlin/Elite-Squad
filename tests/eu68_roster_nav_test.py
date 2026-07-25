"""EU-68 QA: Roster surfaced in the cockpit nav — acceptance criteria gate.

What this ticket added:
  1. A first-class "Roster" button in the cockpit's top nav bar (cockpit_views._control_bar),
     one click away, not buried in a sub-menu.
  2. (Historical) The liaison / Mayor officer wired into the roster. The Mayor's roster row was
     removed in the 2026-07-19 stabilization — liaison.py died in 40da120 and the row was the last
     phantom reference — so this harness now pins the label superset (officers.py) and the row's
     ABSENCE, while the nav-button criterion stays as landed.

Tests here cover the EU-68 acceptance criterion specifically:
  "a Roster button in the cockpit opens a page listing every officer (role · duty · model)
   and the on-demand engineers (lane), sourced from officers.py + roster.py;
   reachable from the cockpit in one click."

The broader roster structure (mermaid chart, model column, round-trip) lives in roster_test.py.
"""
import sys
import types
import tempfile
from pathlib import Path

# --- stub the Agent SDK so orchestrator modules import cleanly (no network) ---
_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda _n: _D
sys.modules["claude_agent_sdk"] = _sdk
sys.path.insert(0, ".")

from orchestrator import roster, officers, cockpit_views, server
from orchestrator import sync as _sync
from orchestrator.config import Config, AppConfig

results: list[tuple[str, bool, str]] = []

def chk(name: str, cond, detail: str = "") -> None:
    """Soft-check — accumulates results so all checks run before exit."""
    results.append((name, bool(cond), str(detail) if not cond else ""))


# ---------------------------------------------------------------------------
# 1. officers.py SOT: display("liaison") still resolves to "Mayor" — the EU-260 label-superset
#    doctrine keeps retired keys renderable for historical audit records.
# ---------------------------------------------------------------------------
chk('display("liaison") → "Mayor" (label superset for historical records)',
    officers.display("liaison") == "Mayor",
    f"got: {officers.display('liaison')!r}")

# ---------------------------------------------------------------------------
# 2. html_view() lists every CURRENT officer and no longer renders the retired Mayor row
# ---------------------------------------------------------------------------
cfg = Config(apps=[], audit_path=str(Path(tempfile.mkdtemp()) / "audit.jsonl"))
view = roster.html_view(cfg)

chk('html_view() does NOT render the retired Mayor row (2026-07-19 stabilization)',
    "Mayor" not in view and "Inter-unit Ambassador" not in view,
    "the liaison phantom row is back on the cockpit page")
chk('html_view() includes every officer listed in _OFFICER_ROWS',
    all(officers.display(key) in view for key, *_ in roster._OFFICER_ROWS),
    str([officers.display(k) for k, *_ in roster._OFFICER_ROWS if officers.display(k) not in view]))

# ---------------------------------------------------------------------------
# 3. /roster-doc route: the page served to the Commander contains Mayor
# ---------------------------------------------------------------------------
repo = Path(tempfile.mkdtemp()) / "app"; repo.mkdir()
scfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(repo),
                              base_branch="DEV", protected_branch="MAIN",
                              backlog_backend="none")],
              audit_path=str(repo / "audit.jsonl"), use_worktree=False)
scfg.detected_auth = lambda: "test"   # skip auth middleware

client = server.create_app(scfg).test_client()
resp = client.get("/roster-doc")
page = resp.get_data(as_text=True)

chk("/roster-doc returns HTTP 200", resp.status_code == 200, str(resp.status_code))
chk('/roster-doc page does NOT render the retired Mayor row',
    "Mayor" not in page and "Inter-unit Ambassador" not in page,
    "the liaison phantom row is back on the served /roster-doc page")

# ---------------------------------------------------------------------------
# 4. Nav button: the "Roster" link is in the TOP-LEVEL control bar, NOT gated
#    behind the Reports <details> sub-menu (requirement: reachable in ≤1 click)
# ---------------------------------------------------------------------------
_sync.can_promote = lambda: False   # avoid git/network calls
bar = cockpit_views._control_bar(scfg, "automatixy")

# The link must be present in the bar at all
chk('_control_bar has href="/roster-doc"',
    'href="/roster-doc"' in bar, "Roster link absent from the control bar")

# The top-level button is rendered as class="btn" (same class as Choose-a-ticket, Jira, etc.).
# The secondary link inside the Reports dropdown omits the class. So checking for the class="btn"
# variant proves the link is a first-class nav element, not buried in a sub-menu.
top_btn_markup = 'class="btn" href="/roster-doc"'
chk('Roster is a top-level class="btn" element (not hidden inside the Reports dropdown)',
    top_btn_markup in bar,
    "top-level btn markup missing — link may be inside the Reports sub-menu only")

# The Reports <details> sub-menu must NOT be the ONLY place the link appears:
# verify the first occurrence has class="btn" (top-level), not a plain <a> (dropdown-only).
#
# EU-289 (2026-07-17) removed the duplicate "Unit roster" anchor from the Reports dropdown, which
# exposed a bug in this check: it compared raw .find() results, and a MISSING dropdown anchor
# returns -1, so `900 < -1` went False — i.e. it only passed while the duplicate it was meant to
# tolerate still existed. No-dropdown is the strongest form of EU-68's "one click, not buried in a
# sub-menu" AC, so treat absent as a pass and only fail when a dropdown anchor genuinely precedes
# the top-level button.
_top_at = bar.find(top_btn_markup)
_drop_at = bar.find('<a href="/roster-doc"')
first_occurrence_is_toplevel = _top_at != -1 and (_drop_at == -1 or _top_at < _drop_at)
chk("First occurrence of roster-doc is the top-level btn, not the dropdown anchor",
    first_occurrence_is_toplevel,
    f"roster-doc first occurrence is inside the dropdown, not in the top bar "
    f"(top={_top_at}, dropdown={_drop_at})")

chk('Roster button carries the descriptive title attribute ("Engineers")',
    "Engineers" in bar,
    "title attr missing — tooltip won't appear")

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
print("\n========== EU-68 ROSTER NAV QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
