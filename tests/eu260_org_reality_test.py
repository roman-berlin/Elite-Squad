"""EU-260: the roster must not ship officers whose code was deleted (the "phantom officer" guard).

Two retirements left phantoms behind, and nothing caught either one:

  • c276155 ("retire the Test Engineer", Phase-2 §2, 2026-07-06) deleted orchestrator/test_engineer.py
    + officers/test-engineer.md and dropped the Tests phase from phases.PHASES — yet ORG.md kept the
    row `| Test Engineer | Tests | … | **active** | officers/test-engineer.md, test_engineer.py |`
    (both paths dead) and roster.py kept its _OFFICER_ROWS entry. council.refresh() rebuilds ROSTER.md
    every morning, so the phantom was re-emitted daily — under roster.py's own docstring claim that the
    structure is "read straight from the code, so the doc can never drift from reality".
  • 4bf4fe2 (EU-325, 2026-07-15) deleted orchestrator/adjutant.py and said so out loud: "Left
    officers.py/roster.py/squad.py/config.py adjutant name-mapping/doc strings untouched — the ticket
    scopes the CLI/approvals/warroom removals only". ORG.md kept citing the deleted `adjutant.py`.

Officers load ORG.md/ROSTER.md as context, so a phantom advertises a gate no code runs and invites a
build to re-create a deliberately retired role. eu43_docs_reality_test.py greps CLAUDE.md only —
ORG.md's "Defined in" citations were entirely unguarded, which is exactly how three dead paths survived.

The two cases are NOT the same, and the guard is careful to encode the difference rather than pattern-
match on "the module is gone":
  • test_engineer — module AND charter deleted, Tests dropped from phases.PHASES. Genuinely retired,
    so it is purged from ORG.md and from the runtime roster outright.
  • adjutant — only the `general adjutant` CLI / EU-85 preview died. The Engineering Manager is STILL
    an active officer: it is council.COUNCIL[0], run every morning on cfg.discussion_model, grounding
    on officers/adjutant.md. So its roster row stays and ORG.md just re-points the citation at the code
    that really fills the post. "Owns a module" is not the test for "is in post" — being run is.

Pure filesystem + a stubbed-SDK roster import — no network, no models."""
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

from orchestrator import council, officers, roster
from orchestrator.config import Config

ROOT = Path(__file__).resolve().parent.parent
ORG = ROOT / "ORG.md"
OFF_README = ROOT / "officers" / "README.md"

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


org_text = ORG.read_text(encoding="utf-8") if ORG.is_file() else ""
readme_text = OFF_README.read_text(encoding="utf-8") if OFF_README.is_file() else ""
chk("ORG.md exists at the repo root", ORG.is_file())
chk("officers/README.md exists", OFF_README.is_file())


def _roster_rows(text: str) -> list[str]:
    """The body rows of ORG.md's '## Roster' table (the header/separator rows dropped)."""
    rows, seen_header = [], False
    for ln in text.splitlines():
        if ln.startswith("| Officer |") and "Defined in" in ln:
            seen_header = True
            continue
        if seen_header:
            if not ln.startswith("|"):
                break
            if not set(ln) <= set("|-: "):     # skip the |---|---| separator
                rows.append(ln)
    return rows


rows = _roster_rows(org_text)
chk("ORG.md's roster table parses into rows", len(rows) >= 5, f"{len(rows)} rows")

# --- 1) every file ORG.md's roster CITES must exist (the guard the two dead paths slipped past) ---
# Citations are bare filenames as often as paths (`builder.py`, `officers/engineer.md`), so a path
# resolves if it lands under the repo root, officers/, or orchestrator/.
_BASES = (ROOT, ROOT / "officers", ROOT / "orchestrator")
for row in rows:
    for cited in re.findall(r"`([A-Za-z0-9_\-./]+\.(?:md|py))`", row):
        chk(f"ORG.md roster cites a real file: {cited}",
            any((base / cited).is_file() for base in _BASES),
            f"cited in row: {row.strip()[:70]}")

# --- 2) a retired officer must be gone from the RUNTIME roster, not just from the doc ---
# key -> (display name, the commit that retired it). The tombstone self-checks: if a module ever comes
# back, the "module really is absent" line fails and tells you to drop it from this map rather than
# letting the rest of the guard quietly assert a lie.
RETIRED = {
    "test_engineer": ("Test Engineer", "c276155 / Phase-2 §2"),
}
cfg = Config(apps=[], audit_path=str(Path(tempfile.mkdtemp()) / "audit.jsonl"))
doc = roster.build_doc(cfg, "status line")
view = roster.html_view(cfg, "status line")
mer = roster.mermaid_chart()
row_keys = [key for key, *_ in roster._OFFICER_ROWS]

for key, (name, commit) in RETIRED.items():
    chk(f"{key}: orchestrator/{key}.py really is absent (tombstone still valid, {commit})",
        not (ROOT / "orchestrator" / f"{key}.py").is_file(),
        f"module is back — remove {key!r} from RETIRED instead of asserting it's dead")
    chk(f"{key}: dropped from roster._OFFICER_ROWS (the daily ROSTER.md source)",
        key not in row_keys)
    chk(f"{key}: '{name}' absent from the regenerated roster doc", name not in doc)
    chk(f"{key}: '{name}' absent from the cockpit roster view", name not in view)
    chk(f"{key}: '{name}' absent from the chain-of-command chart", name not in mer)

# roster.py keys the mermaid node ids off a hand-written `short` map; a stale entry there is dead
# weight, and a MISSING one is a KeyError at render time. Both directions, one assertion.
chk("mermaid_chart() renders every _OFFICER_ROWS officer and no others",
    mer.count("G --> ") == len(row_keys) - 1, f"{mer.count('G --> ')} vs {len(row_keys) - 1}")

# officers.OFFICER_NAMES is deliberately a SUPERSET (it must still render display('test_engineer')
# for historical audit records), so retired keys stay there — the roster is what must be truthful.
for key in RETIRED:
    chk(f"{key}: display name still resolvable for historical records", officers.display(key) != key)

# --- 3) the Test Engineer is retired outright (charter deleted too) ---
# Scoped to the surfaces that make a CLAIM about who is in post — the roster table and the diagrams.
# Naming a retired officer in a "Retired posts" note is the opposite of the bug (it's the record of the
# decision), so prose is deliberately left alone; the ticket's acceptance grep allows exactly that
# ("matches only explicitly-retired/historical mentions").
mermaid_blocks = "\n".join(re.findall(r"```mermaid\n(.*?)```", org_text, re.S))
chk("no Test Engineer row in ORG.md's roster table",
    not any(re.search(r"test.engineer", r, re.I) for r in rows),
    "ORG.md still rosters a role c276155 deleted")
chk("no Test Engineer node in any ORG.md diagram",
    not re.search(r"test.engineer", mermaid_blocks, re.I))
chk("officers/README.md no longer mentions the Test Engineer",
    not re.search(r"test.engineer", readme_text, re.I))
chk("ORG.md mission flow reconnects Gate straight to the Performance Engineer",
    "GATE --> PE2" in org_text, "removing the TE node must not dangle the flow")
chk("ORG.md mission flow still reaches the Reviewer", "PE2 --> REV" in org_text)

# --- 4) the Engineering Manager is active via the council, NOT via the deleted adjutant.py ---
# Pins the distinction that makes section 2's tombstone honest: this officer legitimately kept its post
# through EU-325, so the fix was the citation, not a retirement. If the council seat is ever dropped,
# this fails and the roster row must go with it.
chk("Engineering Manager still holds a council seat (what actually fills the post)",
    any(o[0] == officers.display("adjutant") for o in council.COUNCIL))
chk("adjutant row kept in roster._OFFICER_ROWS (council-backed, not a phantom)", "adjutant" in row_keys)
em_rows = [r for r in rows if "Engineering Manager" in r]
chk("ORG.md has exactly one Engineering Manager roster row", len(em_rows) == 1)
for r in em_rows:
    chk("Engineering Manager row no longer cites the deleted adjutant.py", "adjutant.py" not in r,
        r.strip()[:90])

# --- 5) officers/README.md must not tag a LIVE officer '(PLANNED)' ---
# scout.py / provost.py / quartermaster.py have been live and invoked for months (patrol.py:28-34,
# loop.py:2177, council.py:531-533) while README still called them planned. Written against the
# class: any word on a '(PLANNED)' line naming a real officer is a lie. An officer is a module that
# ALSO has a charter — otherwise plain nouns collide with plumbing modules ('gate', 'readiness').
def _is_live_officer(word: str) -> bool:
    k = word.lower()
    return (ROOT / "orchestrator" / f"{k}.py").is_file() and (ROOT / "officers" / f"{k}.md").is_file()

# Only the chain-of-command TREE rows make the claim ('└── Scout … (PLANNED)'); prose recording that
# the tag USED to be there is history, same carve-out as section 3.
for ln in [l for l in readme_text.splitlines() if "(PLANNED)" in l and "──" in l]:
    live = [w for w in re.findall(r"[A-Za-z_]+", ln) if _is_live_officer(w)]
    chk("officers/README.md '(PLANNED)' tree row names no live officer", not live,
        f"{live} in: {ln.strip()}")

print("\n=============== EU-260 ORG/ROSTER-REALITY QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
