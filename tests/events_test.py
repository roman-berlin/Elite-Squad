"""Test the autonomy event reactor: routing, cooldown, probability gating."""
import asyncio
import sys
import tempfile
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import events
import orchestrator.council as council
from orchestrator.contracts import Outcome

d = Path(tempfile.mkdtemp())
ns = types.SimpleNamespace

def mkcfg(**over):
    base = dict(audit_path=str(d / "audit.jsonl"), autonomy_enabled=True, autonomy_cooldown_min=45,
                meeting_on_security_block=True, parks_meeting_threshold=3,
                smalltalk_prob=0.15, random_meeting_prob=0.06,
                builder_model="m", reviewer_model="m", council_rounds=1)
    base.update(over)
    return ns(**base)

calls = []
async def fake_meeting(cfg, topic, officers=None, rounds=None, audit=None):
    calls.append(("meeting", topic, officers)); return "decision"
async def fake_smalltalk(cfg, audit=None):
    calls.append(("smalltalk",)); return "chat"
council.hold_meeting = fake_meeting
council.small_talk = fake_smalltalk

NOW = 1_000_000.0
events.time.time = lambda: NOW

def sec_report():
    return ns(outcome=Outcome.PR_OPENED, notes="Provost blocked — CRITICAL security finding", ticket_id="AUTO-9")

def reset():
    p = d / "autonomy.json"
    if p.exists():
        p.unlink()
    calls.clear()

results = []
def check(n, c, det=""):
    results.append((n, bool(c), det))

# 1) disabled
reset()
r = asyncio.run(events.after_cycle(mkcfg(autonomy_enabled=False), [sec_report()], None, []))
check("autonomy disabled -> None", r is None and not calls)

# 2) security block -> huddle
reset()
r = asyncio.run(events.after_cycle(mkcfg(), [sec_report()], None, []))
check("security -> security-huddle", r == "security-huddle" and calls and calls[0][0] == "meeting", str(r))
check("huddle pulls provost+field+inspector", calls and calls[0][2] == ["provost", "field", "inspector"])

# 3) cooldown blocks an immediate repeat (state persists, same NOW)
r = asyncio.run(events.after_cycle(mkcfg(), [sec_report()], None, []))
check("cooldown blocks repeat", r is None, str(r))

# 4) parks threshold -> stuck-meeting
reset()
r = asyncio.run(events.after_cycle(mkcfg(), [], None, ["A", "B", "C"]))
check("3 parks -> stuck-meeting", r == "stuck-meeting", str(r))

# 5) quiet + low roll -> smalltalk
reset()
events.random.random = lambda: 0.05
r = asyncio.run(events.after_cycle(mkcfg(), [], None, []))
check("quiet + roll 0.05 -> smalltalk", r == "smalltalk" and ("smalltalk",) in calls, str(r))

# 6) quiet + mid roll -> random-meeting
reset()
events.random.random = lambda: 0.18      # in [0.15, 0.21)
r = asyncio.run(events.after_cycle(mkcfg(), [], None, []))
check("quiet + roll 0.18 -> random-meeting", r == "random-meeting", str(r))

# 7) quiet + high roll -> nothing
reset()
events.random.random = lambda: 0.9
r = asyncio.run(events.after_cycle(mkcfg(), [], None, []))
check("quiet + roll 0.9 -> None", r is None and not calls, str(r))

# 8) after cooldown elapses -> fires again
reset()
events.random.random = lambda: 0.05
asyncio.run(events.after_cycle(mkcfg(), [], None, []))      # fires, records last=NOW
events.time.time = lambda: NOW + 46 * 60                    # 46 min later
r = asyncio.run(events.after_cycle(mkcfg(), [], None, []))
check("after 46min cooldown -> fires again", r == "smalltalk", str(r))

print("\n=========== AUTONOMY REACTOR QA ===========")
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
p = sum(1 for _, ok, _ in results if ok)
print(f"  {p}/{len(results)} passed", "✅" if p == len(results) else "❌")
assert p == len(results)
