"""QA for corridor small-talk: an INSIGHT surfaces to Telegram; plain banter doesn't."""
import asyncio, sys, tempfile, types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator import council
from orchestrator.config import Config, AppConfig

class R:
    def __init__(s, t): s.final, s.text = t, t

sent = []
council.notify.send = lambda m: sent.append(m)

d = Path(tempfile.mkdtemp())
(d / "audit.jsonl").write_text("")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)

# --- a corridor chat that lands a real point ---
seq = iter(["Flaky tests on /leads again, huh?",
            "Yeah. INSIGHT: wrap the /leads e2e in a retry before it blocks merges."])
async def fa(p, o, tag=""): return R(next(seq))
council.run_agent = fa
asyncio.run(council.small_talk(cfg))
check("insight -> Telegram ping", any("Corridor insight" in m for m in sent), str(sent))
check("insight text included in ping", any("retry" in m for m in sent))

# --- a chat that's just banter ---
sent.clear()
seq2 = iter(["Quiet day on the board.", "Indeed — nothing on fire."])
async def fb(p, o, tag=""): return R(next(seq2))
council.run_agent = fb
asyncio.run(council.small_talk(cfg))
check("plain banter -> no ping", sent == [], str(sent))

# --- the extractor ---
check("_corridor_insight parses an INSIGHT line",
      council._corridor_insight([("Scout", "hmm\nINSIGHT: do the thing")]) == "do the thing")
check("_corridor_insight empty when none",
      council._corridor_insight([("Scout", "just chatting")]) == "")

print("\n================ SMALL-TALK QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
