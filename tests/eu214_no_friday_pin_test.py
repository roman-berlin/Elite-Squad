#!/usr/bin/env python3
"""EU-214: remove or relax the stale 5-day "until Friday" Opus-pin prose in orchestrator/agent.py.

EU-108 already deleted the Friday-pin STATE MACHINE and EU-212 verified no persisted pin remains.
What survived were stale PROSE references in orchestrator/agent.py that still described the deleted
weekly pin and contradicted the real behavior: the module docstring ("stay on Opus until reset",
"arms the weekly fallback") and an inline comment ("~1.7x cost amplifier until Friday"). The real
behavior (run_agent_with_fallback) is a per-call, one-shot Opus retry that persists NOTHING — the
fallback self-clears because the next call starts cheap on Sonnet again.

This is prose-alignment only — no control-flow change. Written fail-first: against the un-edited
source (still carrying "Friday" / "until reset" / "arms the weekly fallback" phrasing) checks 1-3
go RED.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENT_PY = REPO / "orchestrator" / "agent.py"

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))


source = AGENT_PY.read_text()

# ── 1. no "friday" anywhere (case-insensitive) in the source ──────────────────────────────────────
check("no 'friday' substring anywhere in orchestrator/agent.py",
      "friday" not in source.lower())

# ── 2. the two stale phrasings the audit called out are gone ──────────────────────────────────────
check("no 'stay on Opus until reset' phrasing",
      "stay on opus until reset" not in source.lower())
check("no 'arms the weekly fallback' phrasing",
      "arms the weekly fallback" not in source.lower())

# ── 3. the module docstring instead documents per-call, self-clearing behavior ────────────────────
module_docstring = source.split('"""', 2)[1]
check("module docstring mentions the fallback persists nothing / self-clears",
      ("persist" in module_docstring.lower() or "self-clear" in module_docstring.lower())
      and "opus" in module_docstring.lower(),
      module_docstring)

# ── 4. run_agent_with_fallback's own docstring already documents the one-shot per-pass retry with
#      no persisted week / calendar reset — pin that contract too (regression guard, not new prose).
import re
m = re.search(r'async def run_agent_with_fallback.*?"""(.*?)"""', source, re.S)
check("run_agent_with_fallback docstring/comments found", m is not None)
if m:
    func_doc = m.group(1)
    check("run_agent_with_fallback docstring: persists nothing, no calendar reset",
          "persist" in func_doc.lower() and "friday" not in func_doc.lower(),
          func_doc)

# ── 5. the existing fallback contract (EU-108 / EU-212 / EU-210) is UNCHANGED by this prose-only
#      edit — run the three harnesses that already pin it and require them to stay green.
for t in ("eu108_pin_removed_test.py", "eu212_fallback_audit_test.py",
          "eu210_rate_limit_classify_test.py"):
    tpath = REPO / "tests" / t
    if not tpath.exists():
        check(f"{t} exists", False, "file not found")
        continue
    proc = subprocess.run([sys.executable, str(tpath)], cwd=str(REPO),
                          capture_output=True, text=True)
    check(f"{t} stays green", proc.returncode == 0,
          proc.stdout[-800:] + proc.stderr[-800:] if proc.returncode != 0 else "")

print("\n============ EU-214 NO-FRIDAY-PIN QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
