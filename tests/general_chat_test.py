"""General-chat grounding QA: respond_to_commander tells the model the unit builds tickets ITSELF,
feeds the REAL repo paths, injects real Needs-you counts, and surfaces Builder comments on named
tickets — so the CTO can't invent counts or miss actionable next steps (EU-70 regression fix)."""
import sys, types, tempfile, asyncio
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class ClaudeAgentOptions:
    def __init__(s, **kw): s.__dict__.update(kw)
sdk.ClaudeAgentOptions = ClaudeAgentOptions
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import council
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace
cap = {}
async def fake_run_agent(prompt, options, tag=None):
    cap["prompt"] = prompt
    cap["system"] = options.system_prompt
    return ns(final="aye, queued", text="aye", is_error=False, cost_usd=0.0, num_turns=1, tools=[])

# isolate the prompt construction
council.run_agent = fake_run_agent
council.history = lambda cfg, limit=1: []
council.collect_signals = lambda cfg: {}
council.format_signals = lambda s: ""
council.recent_commander_notes = lambda cfg, lines=12: ""
async def fake_ticket_context(cfg, msg): return ""
council._ticket_context = fake_ticket_context
async def _fake_report_brief(cfg, text, *a, **k): return text
council.notify = ns(send=lambda *a, **k: None, report_brief=_fake_report_brief)
council.add_commander_note = lambda *a, **k: None
council.memory = ns(preamble=lambda: "")

REAL = "/Users/romanberlin/Projects/Automatixy/Automatixy_Business_Automation"
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=REAL, base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="jira",
                             backlog={"base_url": "https://toibis.atlassian.net", "project_key": "AUTO"})],
             audit_path=str(Path(tempfile.mkdtemp()) / "audit.jsonl"))

# ── Run 1: basic grounding (no ticket, no special context) ───────────────────
ans = asyncio.run(council.respond_to_commander(cfg, "run AUTO-9 on mac"))
sysp, prm = cap.get("system", ""), cap.get("prompt", "")

chk("answer returned", isinstance(ans, str) and ans)
chk("system: the unit builds tickets itself", "YOU are the unit" in sysp and "Builder writes the code" in sysp)
chk("system: never offload to Claude Code / SSH", "Claude Code" in sysp and "SSH" in sysp and "do NOT tell him" in sysp.replace("do not", "do NOT"))
chk("system: never invent a file path", "never invent a file path" in sysp.lower() or "never invent one" in prm.lower())
chk("prompt: feeds the REAL repo path", REAL in prm)
chk("prompt: names the product + Jira key", "automatixy" in prm and "AUTO" in prm)
chk("prompt: shows the real branches", "DEV/MAIN" in prm)

# ── Run 2: Needs-you count grounding (EU-70 fix #1) ─────────────────────────
# Simulate a realistic needs state: 2 decisions, 1 task.
async def fake_needs_context_nonzero(cfg):
    return ("Needs-you (live — cite ONLY these numbers, never guess): 3 total — "
            "2 open decision(s) awaiting your answer (ticket(s): AUTO-23, AUTO-32); "
            "1 run(s) needing attention (EU-17).")

council._needs_context = fake_needs_context_nonzero
cap.clear()
asyncio.run(council.respond_to_commander(cfg, "how many tickets need my attention?"))
prm2 = cap.get("prompt", "")

chk("system: never invent a count", "NEVER invent a count" in sysp or "never invent a count" in sysp.lower())
chk("prompt: needs-you state injected (non-zero)", "Needs-you" in prm2 and "AUTO-23" in prm2)
chk("prompt: needs-you is labelled live/cite-only", "live" in prm2 and ("cite ONLY" in prm2 or "never guess" in prm2.lower()))

# ── Run 3: zero Needs-you count (EU-70 fix #1 — zero case) ──────────────────
async def fake_needs_context_zero(cfg):
    return ("Needs-you (live — cite ONLY this, never guess): 0 items — "
            "nothing is waiting for the Commander right now.")

council._needs_context = fake_needs_context_zero
cap.clear()
asyncio.run(council.respond_to_commander(cfg, "anything waiting for me?"))
prm3 = cap.get("prompt", "")
chk("prompt: zero needs-you state also injected", "0 items" in prm3 or "nothing is waiting" in prm3)

# ── Run 4: ticket context with Builder comment (EU-70 fix #2) ────────────────
async def fake_ticket_context_with_builder(cfg, msg):
    return (
        "[EU-59] [Bug] CI write blocked by EU-92 guardrail\n"
        "Description: The CI file cannot be written by the Builder...\n\n"
        "Last unit post on EU-59 (the Builder/CTO's own comment — read this first when the Commander "
        "asks 'what to do'):\n"
        "[General] Concrete next steps for EU-59: open .github/workflows/ci.yml, paste the ready diff "
        "below, then set the two GitHub secrets GH_TOKEN and JIRA_API_TOKEN in repo settings."
    )

council._ticket_context = fake_ticket_context_with_builder
cap.clear()
asyncio.run(council.respond_to_commander(cfg, "what do I need to do exactly for EU-59?"))
prm4 = cap.get("prompt", "")
chk("prompt: ticket context injected when ticket is named", "EU-59" in prm4)
chk("prompt: builder comment surfaced in ticket context", "[General]" in prm4 and "next steps" in prm4.lower())
chk("prompt: builder comment includes concrete action", "paste the ready diff" in prm4 or "ci.yml" in prm4)

print("\n============ GENERAL-CHAT GROUNDING QA (EU-70) ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
