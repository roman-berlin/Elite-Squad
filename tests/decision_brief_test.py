"""Decision-brief QA: a verbose escalation is distilled to a phone-sized decision before it hits Telegram,
and if the cheap model errors it falls back to the rule-based brief so an escalation is never lost."""
import sys, types, asyncio

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import loop, agent
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

cfg = Config(apps=[], audit_path="/tmp/x.jsonl")
RAW = "Reviewer FAIL: " + " ".join(f"blocking point {i} with lots of detail" for i in range(60))

async def _ok(prompt, opts, tag=""):
    return types.SimpleNamespace(
        final="Decide: real jest-axe or keep structural tests?\n- Add jest-axe (already in lockfile)\n"
              "- Keep hand-rolled structural\nRec: add jest-axe", text="")
agent.run_agent = _ok
brief = asyncio.run(loop._decision_brief(cfg, "AUTO-31", RAW))
chk("distils to the model's short decision brief", brief.startswith("Decide:") and "Rec:" in brief, brief[:60])
chk("the brief is far shorter than the raw escalation", len(brief) < len(RAW) // 2, f"{len(brief)} vs {len(RAW)}")

async def _boom(prompt, opts, tag=""):
    raise RuntimeError("model down")
agent.run_agent = _boom
fb = asyncio.run(loop._decision_brief(cfg, "AUTO-31", RAW))
chk("model error → falls back to the rule-based brief (escalation never lost)", len(fb) > 0, fb[:40])

chk("empty escalation → empty brief (no model call)", asyncio.run(loop._decision_brief(cfg, "AUTO-1", "")) == "")

print("\n============ DECISION-BRIEF QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
