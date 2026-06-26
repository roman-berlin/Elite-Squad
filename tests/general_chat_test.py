"""General-chat grounding QA: respond_to_commander tells the model the unit builds tickets ITSELF and
feeds the REAL repo paths, so it stops hallucinating ('paste into Claude Code' / invented paths)."""
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

ans = asyncio.run(council.respond_to_commander(cfg, "run AUTO-9 on mac"))
sysp, prm = cap.get("system", ""), cap.get("prompt", "")

chk("answer returned", isinstance(ans, str) and ans)
chk("system: the unit builds tickets itself", "YOU are the unit" in sysp and "Builder writes the code" in sysp)
chk("system: never offload to Claude Code / SSH", "Claude Code" in sysp and "SSH" in sysp and "do NOT tell him" in sysp.replace("do not", "do NOT"))
chk("system: never invent a file path", "never invent a file path" in sysp.lower() or "never invent one" in prm.lower())
chk("prompt: feeds the REAL repo path", REAL in prm)
chk("prompt: names the product + Jira key", "automatixy" in prm and "AUTO" in prm)
chk("prompt: shows the real branches", "DEV/MAIN" in prm)

print("\n============ GENERAL-CHAT GROUNDING QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
