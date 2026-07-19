"""Test ship_review: certifies via QM, convenes QM+Provost+Inspector, recommends, never promotes."""
import asyncio
import sys
import tempfile
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): self.__dict__.update(k)   # keep kwargs so the read-only check can inspect ClaudeAgentOptions
    def __call__(self, *a, **k): return self
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import council
import orchestrator.quartermaster as qm
import orchestrator.memory as memory
import orchestrator.notify as notify

ns = types.SimpleNamespace
tmp = Path(tempfile.mkdtemp())

qm_called = []
async def fake_inspect(cfg, name):
    qm_called.append(name)
    return "READY. build ok, types ok, migrations clean."
qm.inspect = fake_inspect

calls = []
seen_options = []
async def fake_run_agent(prompt, options, tag=None):
    calls.append(tag)
    seen_options.append(options)
    if tag == "the-general":
        text = ("**VERDICT** — GO WITH CAVEATS\n\n**BLOCKERS** — None\n\n"
                "**PRE-FLIGHT** — smoke-test /leads\n\n**FOR THE COMMANDER** — Promote DEV→MAIN? Your call.")
    else:
        text = f"{tag}: readiness looks acceptable from my lens"
    return ns(final=text, text=text, is_error=False, cost_usd=0.0, num_turns=1, tools=[])
council.run_agent = fake_run_agent

council._save_transcript = lambda cfg, topic, digest, said, decision: tmp / "ship.md"
async def fake_scribe(cfg):
    return ""
memory.scribe = fake_scribe
notify.configured = lambda: False
notify.send = lambda *a, **k: None

cfg = ns(audit_path=str(tmp / "audit.jsonl"), apps=[ns(name="automatixy")],
         app=lambda n: ns(name=n), builder_model="m", reviewer_model="m", discussion_model="m", council_rounds=1)

decision = asyncio.run(council.ship_review(cfg, "automatixy", audit=None))

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

officer_tags = [t for t in calls if t != "the-general"]
check("Quartermaster certified readiness", qm_called == ["automatixy"], str(qm_called))
check("convened Quartermaster + Provost + Inspector",
      set(officer_tags) == {"quartermaster", "provost", "inspector"}, str(officer_tags))
check("the General chaired the recommendation", "the-general" in calls)
check("verdict + blockers present", "VERDICT" in decision and "BLOCKERS" in decision)
check("promotion framed as the Commander's call", "Your call" in decision and "Promote DEV" in decision)
# 2026-07-19 stabilization: this check was a hardcoded True ("by construction") — it asserted
# nothing. Inspect the captured ClaudeAgentOptions for the real read-only contract instead.
check("no MAIN write — every officer call disallows Write/Edit",
      bool(seen_options) and all(
          "Write" in (getattr(o, "disallowed_tools", None) or [])
          and "Edit" in (getattr(o, "disallowed_tools", None) or [])
          for o in seen_options),
      str([getattr(o, "disallowed_tools", None) for o in seen_options]))

print("\n=========== SHIP-REVIEW QA ===========")
for n, ok, d in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({d})" if d and not ok else ""))
p = sum(1 for _, ok, _ in results if ok)
print(f"  {p}/{len(results)} passed", "✅" if p == len(results) else "❌")
print("  decision head:", decision.splitlines()[0])
assert p == len(results)
