"""EU-537: render-level brand guards — cockpit & CLI prose must not show 'officer'.

These tests call the actual functions (html_view, build_doc) against a minimal Config stub
and assert on rendered output, not source text. They are the regression pin: adding a new
user-facing 'officer' string trips the guard.

Fail-first: run against UNCHANGED code → expect FAIL. Fix → expect GREEN.
"""
from __future__ import annotations

import re
import types
import sys
from pathlib import Path

# ── Mocks for imports that are irrelevant to roster logic ──────────────
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
    def __getattr__(self, name): return lambda *a, **k: None
s = _D()
sdk.ClaudeAgentOptions = _D
sys.modules["claude_agent_sdk"] = sdk

_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = _req

# Minimal display impl (mirrors officers.display)
_DISPLAY = {
    "general": "CTO", "adjutant": "Eng. Mgr.", "pm": "Product Mgr.",
    "scrum": "Scrum Master", "field_engineer": "Dev TL", "inspector": "Code Reviewer",
    "scout": "QA Engineer", "provost": "Security Eng.",
    "quartermaster": "Release Mgr.", "sentinel": "SRE",
}
sys.modules["orchestrator.officers"] = types.ModuleType("orchestrator.officers")
sys.modules["orchestrator.officers"].display = lambda k: _DISPLAY.get(k, k)
sys.modules["orchestrator.officers"].OFFICER_NAMES = _DISPLAY

_results = []

def chk(n, c, d=""):
    _results.append((n, bool(c), d))

# ── Stub Config used by roster.html_view / build_doc ───────────────────
class _Cfg:
    auto_model = True
    builder_model = "claude-opus-4-8"
    reviewer_model = "claude-opus-4-8"
    discussion_model = "claude-sonnet-5"
    smalltalk_model = "claude-haiku-4-5-20251001"

# We need enough of the package for roster to import without pulling live stuff.
# Insert repo root so relative imports work when we import via full package path.
sys.path.insert(0, ".")

# ── AC1: cockpit roster panel renders "engineer(s)", not "officer(s)" ──
from orchestrator import roster

_html = roster.html_view(_Cfg())
_chk_off = re.compile(r'[Oo]fficer')
chk("(6b) cockpit html_view has no 'officer'",
    not _chk_off.search(_html),
    f"'officer' found in html_view output at offsets: {[m.start() for m in _chk_off.finditer(_html)]}")

# Expected after fix: section header reads 'Engineers &amp; duties', table header '<th>Engineer</th>'

# ── AC2: generated ROSTER.md says "Engineers"/"Engineer", no "Officer" ──
_doc = roster.build_doc(_Cfg())
chk("(6c) roster.build_doc has no 'Officer' heading",
    not re.search(r'[Oo]fficer', _doc),
    "'officer' found in build_doc output")

# ── AC3: argparse help/description prose has no "officer" ──────────────
_main_src = Path("orchestrator/main.py").read_text(encoding="utf-8")
_found_prose = False
for _ln in _main_src.splitlines():
    stripped = _ln.strip()
    if ('help=' in stripped or 'description=' in stripped) and _chk_off.search(stripped):
        # Make sure this isn't only the --officers FLAG NAME (e.g. add_argument("--officers"...))
        # The flag NAME is acceptable; prose inside help="" / description="" is not.
        if '"--officers"' not in stripped:
            chk("(6d) no 'officer' in main.py help/description prose", False,
                f"'officer' in prose: {_ln.strip()}")
            _found_prose = True
            break
if not _found_prose:
    chk("(6d) no 'officer' in main.py help/description prose", True)

# ── Summary ────────────────────────────────────────────────────────────
print("\n========== EU-537 RENDER BRAND QA ==========")
passed = sum(1 for _, ok, _ in _results if ok)
for n, ok, det in _results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------")
print(f"  {passed}/{len(_results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(_results) else f"{len(_results)-passed} FAIL")
sys.exit(0 if passed == len(_results) else 1)
