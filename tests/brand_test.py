"""SQUAD brand guard (2026-07-21, Commander decision — Documentation/BRAND.md is canonical).

The brand lives on the USER-FACING surface; internals deliberately keep their names. These pins
hold the surface on-brand so new code can't drift back to war vocabulary:

  (1) the cockpit shell: title 'SQUAD — HQ', the ⬢ wordmark, the --brand accent token in BOTH
      themes — and no 'War Room' / 'Elite Unit' in the rendered shell;
  (2) Jira comments: writes are '[Squad] '-prefixed; reads accept BOTH [Squad] and the legacy
      [General] (history must stay recognizable);
  (3) the daily brief asks for 'FOR YOU', never 'FOR THE COMMANDER';
  (4) docs: BRAND.md exists and CLAUDE.md lists it; SQUAD_HQ.md exists and WAR_ROOM.md is gone;
  (5) voice: the fragment-split notify says 'the squad', not 'the unit'.
"""
import sys
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# (1) cockpit shell
wr = Path("orchestrator/warroom.py").read_text(encoding="utf-8")
chk("(1a) title is 'SQUAD — HQ'", "<title>SQUAD — HQ</title>" in wr)
chk("(1b) the ⬢ wordmark renders in the brand div", "&#x2B22;</span> SQUAD" in wr)
chk("(1c) --brand accent exists in dark AND light themes",
    wr.count("--brand:#ff7a59") == 1 and wr.count("--brand:#e8590c") == 1)
_page_start = wr.index("<title>")
chk("(1d) no 'War Room' / 'Elite Unit' in the rendered shell",
    "War Room" not in wr[_page_start:_page_start + 4000]
    and "Elite Unit" not in wr[_page_start:_page_start + 4000])

# (2) Jira comment prefix
ja = Path("orchestrator/backlog/jira.py").read_text(encoding="utf-8")
chk("(2a) comment writes carry the [Squad] prefix",
    '_adf("[Squad] " + body)' in ja and '_adf("[Squad] " + comment)' in ja
    and '_adf("[General]' not in ja)
chk("(2b) reads accept BOTH [Squad] and legacy [General]",
    ja.count('startswith(("[Squad]", "[General]"))') >= 2)

# (3) daily brief
co = Path("orchestrator/council.py").read_text(encoding="utf-8")
chk("(3) the daily asks for FOR YOU, never FOR THE COMMANDER",
    "FOR YOU" in co and "FOR THE COMMANDER" not in co)

# (4) docs
chk("(4a) BRAND.md exists with the terminology map",
    Path("Documentation/BRAND.md").exists()
    and "Terminology map" in Path("Documentation/BRAND.md").read_text(encoding="utf-8"))
chk("(4b) CLAUDE.md lists BRAND.md", "Documentation/BRAND.md" in Path("CLAUDE.md").read_text(encoding="utf-8"))
chk("(4c) SQUAD_HQ.md replaced WAR_ROOM.md",
    Path("SQUAD_HQ.md").exists() and not Path("WAR_ROOM.md").exists())

# (5) voice
lp = Path("orchestrator/loop.py").read_text(encoding="utf-8")
chk("(5) split notify says 'the squad takes the fragments next'",
    "The squad takes the fragments next" in lp and "The unit takes the fragments next" not in lp)

# (6) user-facing docs: no 'officer' prose — rebranded to 'engineer' (EU-536)
import re
for _fname in ("README.md", "SQUAD_HQ.md"):
    _text = Path(_fname).read_text(encoding="utf-8")
    chk(f"(6a) no 'officer' in {_fname}",
        not re.search(r'[Oo]fficer', _text),
        f"'officer' still present in {_fname}")

# (7) BRAND.md terminology map explicitly lists officer(internal)/engineer(user-facing) split (EU-536)
brand = Path("Documentation/BRAND.md").read_text(encoding="utf-8")
chk("(7a) BRAND.md terminology map mentions officer→engineer split",
    bool(re.search(r'(?i)officer.*engineer', brand)),
    "Terminology map lacks officer→engineer row")

# EU-537: render-level guards — cockpit roster surfaces & CLI help must not show "officer"
# Run the separate render test and graft its results into this suite.
import subprocess as _sub
_r = _sub.run(
    [sys.executable, "tests/eu537_render_brand_test.py"],
    capture_output=True, text=True, timeout=15)
if _r.returncode != 0:
    for _ln in _r.stdout.splitlines():
        if "[FAIL]" in _ln:
            _m = re.search(r"\((.+?)\)", _ln)
            det = _m.group(1).strip() if _m else ""
            chk(f"(6b-e) EU-537 render tests passed ({det})", False, _ln.strip())
else:
    chk("(6b-e) EU-537 render tests passed", True)

print("\n========== SQUAD BRAND QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
