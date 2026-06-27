"""EU-68 QA: Roster surfaced in the cockpit nav — acceptance criteria gate.

What this ticket added:
  1. A first-class "Roster" button in the cockpit's top nav bar (cockpit_views._control_bar),
     one click away, not buried in a sub-menu.
  2. The liaison / Mayor officer fully wired into officers.py (SOT) → roster.py
     → html_view() → /roster-doc, so every officer with role · duty · model shows up.

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
# 1. officers.py SOT: display("liaison") resolves to "Mayor"
#    (the canonical name that appears everywhere — board, roster, group-room labels)
# ---------------------------------------------------------------------------
chk('display("liaison") → "Mayor" (officers.py single source of truth)',
    officers.display("liaison") == "Mayor",
    f"got: {officers.display('liaison')!r}")

# ---------------------------------------------------------------------------
# 2. html_view() surfaces Mayor + Inter-unit Ambassador duty in the rendered table
#    (the acceptance criterion: "listing every officer (role · duty · model)")
# ---------------------------------------------------------------------------
cfg = Config(apps=[], audit_path=str(Path(tempfile.mkdtemp()) / "audit.jsonl"))
view = roster.html_view(cfg)

chk('html_view() includes "Mayor" in the officer table',
    "Mayor" in view,
    "liaison officer missing from the cockpit page")
chk('html_view() includes the liaison duty text',
    "Inter-unit Ambassador" in view,
    "liaison duty row missing from the cockpit page")
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
chk('/roster-doc page contains "Mayor" (new officer is live on the page)',
    "Mayor" in page,
    "Mayor absent from the rendered /roster-doc page")
chk('/roster-doc page contains "Inter-unit Ambassador"',
    "Inter-unit Ambassador" in page,
    "liaison duty absent from the rendered /roster-doc page")

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
first_occurrence_is_toplevel = bar.find(top_btn_markup) < bar.find('<a href="/roster-doc"')
chk("First occurrence of roster-doc is the top-level btn, not the dropdown anchor",
    first_occurrence_is_toplevel,
    "roster-doc first occurrence is inside the dropdown, not in the top bar")

chk('Roster button carries the descriptive title attribute ("Officers, soldiers")',
    "Officers, soldiers" in bar,
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
