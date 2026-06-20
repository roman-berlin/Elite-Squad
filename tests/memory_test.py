"""Unit Memory split: doctrine stays versioned; the officer-maintained live log is runtime + survives a reset."""
import sys, types, tempfile
from pathlib import Path
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import memory

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# redirect to a temp dir so we never touch the real memory files
d = Path(tempfile.mkdtemp())
memory.UNIT_PATH = d / "UNIT.md"
memory.LIVE_PATH = d / "UNIT.live.md"
memory._BACKUPS = d / "backups"

# seed a legacy UNIT.md: doctrine + an inline Scribe section
memory.UNIT_PATH.write_text(
    "# Doctrine\n\n## Mission\nDo X safely.\n\n"
    + memory._BEGIN + "\n" + memory._LOG_HEADING + "\n\n- 2026-01-01: an old lesson.\n" + memory._END + "\n",
    encoding="utf-8")

# 1) ensure() migrates the inline log out to the runtime live file
memory.ensure()
chk("migration: live file created from the legacy inline log", memory.LIVE_PATH.exists() and "an old lesson" in memory.LIVE_PATH.read_text())

# 2) doctrine view excludes the log; 3) preamble merges both with ONE heading
chk("doctrine excludes the scribe log", "Mission" in memory._doctrine() and "an old lesson" not in memory._doctrine())
pre = memory.preamble()
chk("preamble carries the doctrine", "Do X safely." in pre)
chk("preamble carries the live log", "an old lesson" in pre)
chk("preamble shows a single log heading (no duplication)", pre.count("Lessons & Decisions") == 1, str(pre.count("Lessons & Decisions")))

# 4) update_log writes the live file, never the versioned doctrine
before = memory.UNIT_PATH.read_text()
memory.update_log("- 2026-06-19: new lesson the officers learned.")
chk("update_log wrote to the live file", "new lesson the officers learned" in memory.LIVE_PATH.read_text())
chk("update_log left the versioned doctrine untouched", memory.UNIT_PATH.read_text() == before)
chk("preamble now reflects the new lesson", "new lesson the officers learned" in memory.preamble())

# 5) THE POINT: the server's self-update reverts UNIT.md (doctrine) — the live log must persist
memory.UNIT_PATH.write_text("# Doctrine\n\n## Mission\nDo X safely.\n", encoding="utf-8")   # main's version, no log
chk("live log SURVIVES a doctrine reset (self-update safe)", "new lesson the officers learned" in memory.preamble())

print("\n================ UNIT MEMORY SPLIT QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
