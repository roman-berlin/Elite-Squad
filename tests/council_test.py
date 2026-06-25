"""Test the multi-round discussion engine (officer selection, rounds, PASS, convergence)."""
import asyncio
import sys
import types

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import council

ns = types.SimpleNamespace
CFG = ns(builder_model="m", reviewer_model="m", discussion_model="m", council_rounds=3)

results = []
def check(name, cond, d=""):
    results.append((name, bool(cond), d))

# --- _select_officers ---
all_off = council._select_officers(None)
check("select None -> all officers", len(all_off) == len(council.COUNCIL))
sel = council._select_officers(["provost", "scout"])
names = [o[0] for o in sel]
check("select by key -> Security + QA", "Security Engineer" in names and "QA Engineer" in names and len(sel) == 2, str(names))
sel2 = council._select_officers(["Dev Team Lead"])
check("select by name", sel2 and sel2[0][0] == "Dev Team Lead")

# --- discuss(): stub run_agent to script statements ---
SCRIPT = {}   # (rank, round) -> text
calls = []
async def fake_run_agent(prompt, options, tag=None):
    # infer round from the prompt ("Round N of")
    import re
    m = re.search(r"Round (\d+) of", prompt)
    rnd = int(m.group(1)) if m else 1
    calls.append((tag, rnd))
    text = SCRIPT.get((tag, rnd), f"{tag} statement r{rnd}")
    return ns(final=text, text=text, is_error=False, cost_usd=0.0, num_turns=1, tools=[])

council.run_agent = fake_run_agent

two = council.COUNCIL[:2]   # Engineering Manager, Dev Team Lead
a_key = council._officer_key(two[0][0])
b_key = council._officer_key(two[1][0])
# round 2: A passes, B adds; round 3: both pass -> convergence
SCRIPT[(a_key, 2)] = "PASS"
SCRIPT[(b_key, 2)] = "I disagree with the Adjutant — we need a DB soldier."
SCRIPT[(a_key, 3)] = "pass"
SCRIPT[(b_key, 3)] = "PASS"

transcript = asyncio.run(council.discuss(CFG, two, "digest", "", "staffing", rounds=3))
speakers = [s for s, _ in transcript]
texts = " ".join(t for _, t in transcript)

check("round 1: both officers open", sum(1 for s in speakers if "· r" not in s) == 2, str(speakers))
check("round 2: PASS skipped (only B speaks)", any("r2" in s for s in speakers) and sum(1 for s in speakers if "r2" in s) == 1)
check("round 2 speaker is Dev Team Lead", any(s.startswith("Dev Team Lead") and "r2" in s for s in speakers))
check("round 3 fully passed -> converged (no r3 entries)", not any("r3" in s for s in speakers), str(speakers))
check("officers reference each other (debate)", "disagree" in texts.lower())
check("converged early: stopped after round 3 had 0 speakers", max(r for _, r in calls) == 3)

print("\n=========== COUNCIL / MEETINGS QA ===========")
for n, ok, d in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({d})" if d and not ok else ""))
p = sum(1 for _, ok, _ in results if ok)
print(f"  {p}/{len(results)} passed", "✅" if p == len(results) else "❌")
print("  transcript:", [(s, t[:40]) for s, t in transcript])
assert p == len(results)
