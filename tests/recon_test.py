"""Recon squads: officers recruit soldiers autonomously, solo is the default, fail-safe never breaks."""
import sys, types, asyncio
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import recon, memory
from orchestrator.config import Config, AppConfig
memory.preamble = lambda: ""   # avoid touching real memory files

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ---- parse_slices (pure) ----
chk("SOLO -> no slices (officer chose solo)", recon.parse_slices("SOLO") == [])
sl = recon.parse_slices('[{"area":"auth","detail":"check JWT"},{"area":"rls","detail":"check policies"}]')
chk("parses a 2-slice plan", len(sl) == 2 and sl[0].area == "auth")
chk("cap is respected", len(recon.parse_slices('[{"area":"a","detail":"x"},{"area":"b","detail":"y"},{"area":"c","detail":"z"}]', cap=2)) == 2)
chk("malformed json -> solo", recon.parse_slices("[not json") == [])
chk("empty-detail slice dropped", recon.parse_slices('[{"area":"a","detail":""}]') == [])

# ---- stub the agent runner; route canned replies by tag ----
class FakeRun:
    def __init__(s, final, is_error=False):
        s.final = final; s.text = final; s.num_turns = 1; s.cost_usd = 0.0; s.tools = []; s.is_error = is_error
        s.provider = "Anthropic"; s.model_version = "claude-sonnet-4-6"
calls = []
plan = {"reply": "SOLO"}
async def fake_run_agent(prompt, options, tag=""):
    calls.append(tag)
    if tag.endswith("-lead"):   return FakeRun(plan["reply"])
    if tag.startswith("soldier·"): return FakeRun(f"finding {tag}")
    if tag.endswith("-synth"):  return FakeRun("SYNTHESIZED REPORT")
    return FakeRun("SOLO REPORT")
recon.run_agent = fake_run_agent

cfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path="/tmp/a.jsonl", use_worktree=False)

class Audit:
    def __init__(s): s.events = []
    def record(s, event, **k): s.events.append({"event": event, **k})

def call(officer="provost", label="Provost Marshal", audit=None):
    return asyncio.run(recon.run_officer(
        officer=officer, label=label, system="SYS", task="TASK", cfg=cfg, cwd=".",
        model="m", soldier_tools=["Read", "Grep", "Glob", "Bash"], max_turns=10, effort="high",
        empty="(none)", audit=audit))

# ---- solo is the default (delegation off) ----
cfg.delegation_enabled = False
calls.clear()
out = call()
chk("delegation OFF -> solo report", out == "SOLO REPORT", out)
chk("delegation OFF -> exactly one agent call", calls == ["provost"], str(calls))

# ---- delegated: officer splits into a squad, soldiers run, lead synthesizes ----
cfg.delegation_enabled = True
plan["reply"] = '[{"area":"auth","detail":"JWT"},{"area":"rls","detail":"policies"}]'
calls.clear(); aud = Audit()
out = call(audit=aud)
chk("delegated -> synthesized report", out == "SYNTHESIZED REPORT", out)
chk("delegated -> lead + 2 soldiers + synth, in order",
    calls == ["provost-lead", "soldier·provost1", "soldier·provost2", "provost-synth"], str(calls))
chk("audit logged the squad (2 slices)", any(e.get("slices") == 2 for e in aud.events))
chk("audit logged each soldier", sum(1 for e in aud.events if e.get("officer") == "provost" and "area" in e) == 2)

# ---- autonomy: officer decides SOLO even when armed -> no soldiers ----
plan["reply"] = "SOLO"
calls.clear()
out = call(officer="scout", label="Scout")
chk("officer chooses SOLO when armed -> solo", out == "SOLO REPORT" and calls == ["scout-lead", "scout"], str(calls))

# ---- EU-182: fewer than 2 slices under delegation must still audit the solo fallback ----
plan["reply"] = "SOLO"
calls.clear(); aud = Audit()
out = call(officer="scout", label="Scout", audit=aud)
chk("delegation armed + SOLO fallback -> officer_recon audit entry present",
    any(e.get("event") == "officer_recon" and e.get("officer") == "scout" for e in aud.events), str(aud.events))

# ---- fail-safe: a planning hiccup degrades to solo, never errors ----
async def boom(prompt, options, tag=""):
    calls.append(tag)
    if tag.endswith("-lead"):
        raise RuntimeError("plan boom")
    return FakeRun("SOLO REPORT")
recon.run_agent = boom
calls.clear()
out = call()
chk("planning hiccup -> solo (fail-safe)", out == "SOLO REPORT" and calls == ["provost-lead", "provost"], str(calls))

# ---- EU-139: the solo path itself (_solo's run_agent call) must fail CLOSED, never crash ----
cfg.delegation_enabled = False

async def solo_crash(prompt, options, tag=""):
    calls.append(tag)
    raise RuntimeError("boom in solo")
recon.run_agent = solo_crash
calls.clear(); aud = Audit()
try:
    out = call(audit=aud)
    raised = False
except Exception:
    out = None
    raised = True
chk("solo run_agent raising -> run_officer does NOT propagate the exception", not raised)
chk("solo run_agent raising -> returns a fail-closed report string (not None/empty)",
    isinstance(out, str) and len(out.strip()) > 0, str(out))
chk("solo run_agent raising -> report reads as failed/closed, not a clean report",
    isinstance(out, str) and any(w in out.lower() for w in ("fail", "error")), out)
chk("solo run_agent raising -> officer_recon audit entry recorded with ok=False",
    any(e.get("event") == "officer_recon" and e.get("officer") == "provost" and e.get("ok") is False
        for e in aud.events), str(aud.events))

# ---- EU-139: the exact SDK quirk from the root-cause ("... returned an error result: success") ----
async def solo_success_quirk(prompt, options, tag=""):
    calls.append(tag)
    raise Exception("Claude Code returned an error result: success")
recon.run_agent = solo_success_quirk
calls.clear(); aud = Audit()
try:
    out = call(audit=aud)
    raised = False
except Exception:
    out = None
    raised = True
chk("'error result: success' quirk in solo -> no crash", not raised)
chk("'error result: success' quirk in solo -> fail-closed report returned",
    isinstance(out, str) and len(out.strip()) > 0, str(out))
chk("'error result: success' quirk in solo -> report reads as failed/closed",
    isinstance(out, str) and any(w in out.lower() for w in ("fail", "error")), out)
chk("'error result: success' quirk in solo -> officer_recon audit entry with ok=False",
    any(e.get("event") == "officer_recon" and e.get("officer") == "provost" and e.get("ok") is False
        for e in aud.events), str(aud.events))

print("\n================ RECON SQUADS QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
