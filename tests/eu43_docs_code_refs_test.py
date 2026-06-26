"""EU-43 (regression): CLAUDE.md must not cite orchestrator code that doesn't exist either.

The ticket's goal is "make CLAUDE.md match the actual repo" AND "fold in the two behaviours added by
EU-41 (Development_Status changelog) and EU-42 (out-of-scope backlog filing) so the doc describes what
the unit actually does". To do that the reconciled CLAUDE.md now points readers at concrete code homes:

  | behaviour            | doc claims it lives in ...                                  |
  | EU-41 changelog      | `orchestrator/loop.py` -> `_record_changelog()`             |
  | EU-42 out-of-scope   | `orchestrator/filing.py` (`parse_tickets` / `file_findings`)|
  | in-build gate        | `orchestrator/gate.py` / `loop.run_gate` (`run_gate`)       |

The sibling guard (`eu43_docs_reality_test.py`) only verifies `*.md` and `.claude/skills/*` paths, so a
renamed/deleted module or symbol would make CLAUDE.md lie again with the suite still green — the exact
"docs lie" class EU-40/41/42/43 exist to kill, just one rung down (code instead of docs). This harness
closes that gap two ways:

  1. HAPPY PATH — every `orchestrator/*.py` path CLAUDE.md names resolves to a real file, and every code
     symbol it cites as a behaviour's home is actually defined in the orchestrator package.
  2. FOLD-IN — CLAUDE.md genuinely documents the two new EU-41/EU-42 behaviours (not just "no dangling
     refs", but the positive acceptance: the doc describes what the unit now does).
  3. TEETH — a pure predicate over a synthetic doc proves the check would FAIL on a fabricated module or
     symbol reference, so the guard can't be silently weakened.

Pure filesystem + string assertions: no orchestrator import, no network."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUDE = ROOT / "CLAUDE.md"

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


text = CLAUDE.read_text(encoding="utf-8") if CLAUDE.exists() else ""

# ---- 1) HAPPY PATH: every orchestrator/*.py path CLAUDE.md names is a real file ----
py_refs = sorted(set(re.findall(r"orchestrator/[A-Za-z0-9_/]+\.py", text)))
chk("CLAUDE.md cites at least one orchestrator/*.py module", len(py_refs) >= 1, str(py_refs))
for ref in py_refs:
    chk(f"cited module exists: {ref}", (ROOT / ref).is_file(), ref)

# ---- 2) every code SYMBOL CLAUDE.md cites as a behaviour's home is actually defined ----
# These are the function names the reconciled doc names; each must be `def`'d somewhere in orchestrator/.
CITED_SYMBOLS = ("_record_changelog", "parse_tickets", "file_findings", "run_gate")
pkg_src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "orchestrator").glob("*.py"))
for sym in CITED_SYMBOLS:
    if sym in text:  # only enforce symbols the doc actually mentions
        chk(f"cited symbol `{sym}` is defined in orchestrator/",
            re.search(rf"^\s*def {re.escape(sym)}\b", pkg_src, re.MULTILINE) is not None, sym)

# ---- 3) FOLD-IN: the two new behaviours from EU-41 & EU-42 are genuinely documented ----
chk("CLAUDE.md documents the EU-41 Development_Status changelog behaviour",
    "Development_Status.md" in text and "_record_changelog" in text and "EU-41" in text)
chk("CLAUDE.md documents the EU-42 out-of-scope backlog-filing behaviour",
    "orchestrator/filing.py" in text and "EU-42" in text
    and ("parse_tickets" in text or "file_findings" in text))

# ---- 4) TEETH: the predicate would catch a fabricated module / symbol reference ----
def unresolved_code_refs(doc, real_files, real_symbols):
    """Mirror of the live check as a pure function, for deterministic teeth tests."""
    bad = []
    for ref in sorted(set(re.findall(r"orchestrator/[A-Za-z0-9_/]+\.py", doc))):
        if ref not in real_files:
            bad.append(f"missing module: {ref}")
    for sym in CITED_SYMBOLS:
        if sym in doc and sym not in real_symbols:
            bad.append(f"missing symbol: {sym}")
    return bad

REAL_FILES = {"orchestrator/loop.py", "orchestrator/filing.py", "orchestrator/gate.py"}
REAL_SYMS = set(CITED_SYMBOLS)
chk("a doc naming only real modules+symbols is clean",
    unresolved_code_refs("`orchestrator/loop.py` -> `_record_changelog`", REAL_FILES, REAL_SYMS) == [])
chk("a fabricated orchestrator module path is flagged",
    unresolved_code_refs("see `orchestrator/ghost.py`", REAL_FILES, REAL_SYMS)
    == ["missing module: orchestrator/ghost.py"])
chk("a renamed/absent cited symbol is flagged",
    unresolved_code_refs("call `run_gate` now", REAL_FILES, REAL_SYMS - {"run_gate"})
    == ["missing symbol: run_gate"])
chk("empty doc => no code-ref problems (no false positives)",
    unresolved_code_refs("", REAL_FILES, REAL_SYMS) == [])

print("\n=========== EU-43 DOCS CODE-REF GUARD (regression) ===========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
