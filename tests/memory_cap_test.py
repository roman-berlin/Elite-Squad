"""Two-tier memory cap QA: preamble() (prepended to EVERY officer prompt) inlines the doctrine in full
but only the newest PREAMBLE_LESSONS of the living log — the rest collapse to a one-line pointer — so
the per-call token cost stays bounded as the log grows. The cockpit /memory page still gets the FULL
log (limit=None). Pure functions, no agent/network."""
import sys, types, tempfile
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

from orchestrator import memory

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())

# A 20-bullet living log, newest-first (L01 newest ... L20 oldest), plus its heading.
bullets = "\n".join(f"- L{i:02d}: lesson number {i}" for i in range(1, 21))
log_file = tmp / "UNIT.live.md"
log_file.write_text(f"{memory._LOG_HEADING}\n\n{bullets}\n", encoding="utf-8")
memory.LIVE_PATH = log_file
memory.load = lambda *a, **k: "# Doctrine\nStanding Orders: obey the Commander."

chk("PREAMBLE_LESSONS default is a sane cap", isinstance(memory.PREAMBLE_LESSONS, int) and 1 <= memory.PREAMBLE_LESSONS <= 30)

# --- full log (limit=None) — the cockpit page view: every bullet, no pointer ---
full = memory._live_log()
chk("full log keeps the oldest bullet", "L20:" in full and "L01:" in full)
chk("full log has no truncation pointer", "older lessons" not in full)

# --- capped to 12 (what each officer actually sees) ---
cap = memory._live_log(limit=12)
chk("cap keeps the 12 newest", "L01:" in cap and "L12:" in cap)
chk("cap drops the 13th-oldest onward", "L13:" not in cap and "L20:" not in cap)
chk("cap leaves a pointer to the rest", "+8 older lessons" in cap)
chk("cap preserves the heading", "Lessons & Decisions" in cap)

# --- preamble() (the real per-officer block) is bounded the same way + carries doctrine + framing ---
pre = memory.preamble()
chk("preamble carries the doctrine in full", "Standing Orders: obey the Commander." in pre)
chk("preamble inlines only the newest lessons", "L01:" in pre and "L12:" in pre)
chk("preamble does NOT inline the older tail", "L13:" not in pre and "L20:" not in pre)
chk("preamble shows the pointer to the full log", "+8 older lessons" in pre)
chk("preamble keeps the UNIT MEMORY framing", "UNIT MEMORY" in pre and "END UNIT MEMORY" in pre)

# --- limit >= count: no pointer, everything kept ---
allkept = memory._live_log(limit=50)
chk("limit >= count keeps all, no pointer", "L20:" in allkept and "older lessons" not in allkept)

# --- empty log: preamble is doctrine-only, never crashes, no stray pointer ---
memory.LIVE_PATH = tmp / "does-not-exist.md"
memory.load = lambda *a, **k: "# Doctrine only"
pre_empty = memory.preamble()
chk("empty log -> doctrine-only preamble (no crash)", "Doctrine only" in pre_empty)
chk("empty log -> no lessons, no pointer", "L01:" not in pre_empty and "older lessons" not in pre_empty)

print("\n============ MEMORY CAP QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
