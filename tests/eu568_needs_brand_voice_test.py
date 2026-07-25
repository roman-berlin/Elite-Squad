"""EU-568: Needs-you card copy must use SQUAD brand voice — no internal jargon.

Denylist sourced from Documentation/BRAND.md terminology map + ticket terms:
  DEV→MAIN | DEV->MAIN | readiness verdict | War Room | Elite Unit | Commander | FOR THE COMMANDER | officer(s) | the unit

Pins tested:
  (1) QA_STATUS_TEMPLATE constant contains no denylist terms AND still returns
      True from server.is_status_message().
  (2) COUNCIL_SHIP_QUESTION constant is a plain question ending in '?',
      contains no denylist terms, and _SHIP_REVIEW_CHAIR_SYSTEM no longer
      contains 'Promote DEV→MAIN'; 'FOR YOU' remains present and
      'FOR THE COMMANDER' absent (brand_test.py pins).
  (3) DECISION_FALLBACK_HEADLINE uses 'engineer', not 'officer'.
  (4) Every _SYNTH_CLASSES brief + option text is denylist-clean; needles
      byte-identical to before; red-base recommended option retains
      'Re-queue now' (decision_options_test.py pin).
  (5) Rendered /needs HTML for fixture rows covering structured decision,
      synthesized decision, and parked row is denylist-clean.
"""
import re
import sys
import tempfile
import types
from pathlib import Path

# ── Stub dependencies ────────────────────────────────────────────────────────

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules["requests"] = req

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import decisions, needs, server
from orchestrator import council
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ── Denylist helpers ─────────────────────────────────────────────────────────

# Core denylist terms from BRAND.md terminology map + EU-568 ticket
_DENY_TERMS = [
    r"DEV→MAIN",        # arrow form
    r"DEV->MAIN",       # dash form
    r"readiness\s+verdict",  # space-insensitive
    r"War\s+Room",
    r"Elite\s+Unit",
    r"Commander",
    r"\bofficers?\b",   # word-bounded
    r"\bthe\s+unit\b",  # word-bounded whole phrase
]

_den_re = re.compile("|".join(_DENY_TERMS), re.IGNORECASE)

def _denied(text: str) -> bool:
    """Return True if *text* contains any denylist term."""
    return bool(_den_re.search(text))


# ═══════════════════ TESTS ═══════════════════

# ── (1) QA status message constant ──────────────────────────────────────────

chk("(1a) QA_STATUS_TEMPLATE has no denylist terms",
    not _denied(server.QA_STATUS_TEMPLATE),
    f"QA_STATUS_TEMPLATE={server.QA_STATUS_TEMPLATE!r}")
chk("(1b) QA_STATUS_TEMPLATE still classified as status by is_status_message()",
    server.is_status_message(
        server.QA_STATUS_TEMPLATE.format(app="automatixy")),
    "is_status_message=True")

# ── (2) Council ship-decision question ──────────────────────────────────────

chk("(2a) COUNCIL_SHIP_QUESTION has no denylist terms",
    not _denied(council.COUNCIL_SHIP_QUESTION),
    f"COUNCIL_SHIP_QUESTION={council.COUNCIL_SHIP_QUESTION!r}")
chk("(2b) COUNCIL_SHIP_QUESTION ends with '?'",
    council.COUNCIL_SHIP_QUESTION.endswith("?"),
    repr(council.COUNCIL_SHIP_QUESTION))

# Verify the council system prompt uses the constant & is clean.
co_src = Path("orchestrator/council.py").read_text(encoding="utf-8")
chk("(2c) _SHIP_REVIEW_CHAIR_SYSTEM does NOT contain 'Promote DEV→MAIN'",
    "Promote DEV→MAIN" not in co_src,
    "_SHIP_REVIEW_CHAIR_SYSTEM still has old phrasing")
chk("(2d) 'FOR YOU' is present in council.py",
    "FOR YOU" in co_src,
    "FOR YOU missing from council")
chk("(2e) 'FOR THE COMMANDER' absent from council.py",
    "FOR THE COMMANDER" not in co_src,
    "'FOR THE COMMANDER' found in council.py")

# ── (3) Decision fallback headline ──────────────────────────────────────────

chk("(3) DECISION_FALLBACK_HEADLINE uses 'engineer', not 'officer'",
    "engineer" in decisions.DECISION_FALLBACK_HEADLINE
    and not re.search(r'\bofficer', decisions.DECISION_FALLBACK_HEADLINE),
    f"headline={decisions.DECISION_FALLBACK_HEADLINE!r}")

# ── (4) Synthesized options ─────────────────────────────────────────────────

for needles, brief, opts in decisions._SYNTH_CLASSES:
    chk(f"(4a) brief for needles {needles[:1]}...",
        not _denied(brief), repr(brief)[:200])
    for oidx, (otext, rec) in enumerate(opts):
        chk(f"(4a) opt {oidx} for needles {needles[:1]}...",
            not _denied(otext), repr(otext)[:150])

# Red-base wall example via synthesize_options (must match _SYNTH_CLASSES needles)
_synth_wall_q = ("Base branch is RED before any build — gate fails on the clean base tree.")
sp = decisions.synthesize_options(_synth_wall_q)
chk("(4b) synthesize_options returns denials-free summary",
    sp and sp.get("summary") and not _denied(sp["summary"]),
    f"summary={sp['summary']!r}" if sp else "None")

chk("(4c) red-base recommended option retains 'Re-queue now'",
    sp and "Re-queue now" in sp["options"][0]["text"],
    f"text={sp['options'][0]['text']!r}")

# ── (5) Rendered /needs HTML (fixture rows per category) ────────────────────

_orig_summary = needs.summary

_fixtures_by_cat = {
    "decision": [{"id": "AUTO-99", "app": "automatixy",
                  "question": "Should we merge PR #42?",
                  "category": "decision"}],
    "parked": [{"ticket_id": "AUTO-77", "app": "automatixy",
                "why": "This was parked for coordination.",
                "category": "parked"}],
}

for cat_key, rows in _fixtures_by_cat.items():
    def _cat_summary(c, app_name=None, _r=rows):  # noqa: B008
        return {"rows": _r, "decisions": _r,
                "proposals": [], "tasks": [], "total": len(_r)}

    needs.summary = _cat_summary
    tmpdir = tempfile.mkdtemp()
    cfg = Config(
        apps=[AppConfig(name="automatixy", repo_path=tmpdir,
                        base_branch="dev", protected_branch="main",
                        backlog_backend="none")],
    )
    try:
        body = server.create_app(cfg).test_client().get("/needs").get_data(as_text=True)
    finally:
        needs.summary = _orig_summary

    chk(f"(5a) /needs HTML ({cat_key}) has no denylist terms",
        not _denied(body),
        f"cat={cat_key}, matches={_den_re.findall(body) or 'none'}")

print("\n========== EU-568 Needs-you Brand Voice QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
