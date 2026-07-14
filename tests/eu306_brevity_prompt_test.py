"""EU-306: respond_to_commander's system prompt must carry an explicit brevity contract —
default to 1-3 short, Telegram-style sentences, no preamble filler ("Great question! Let me..."),
expand only on explicit request or genuine decision-structure need. Captures the system prompt
actually sent to the model (never the model's output, which isn't deterministic) and asserts the
instruction text is present — and that the old, looser '2-4 sentences' wording is gone."""
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
    return ns(final="got it", text="got it", is_error=False, cost_usd=0.0, num_turns=1, tools=[])

# isolate the prompt construction — same harness shape as tests/general_chat_test.py
council.run_agent = fake_run_agent
council.history = lambda cfg, limit=1: []
council.collect_signals = lambda cfg: {}
council.format_signals = lambda s: ""
council.recent_commander_notes = lambda cfg, lines=12: ""
async def fake_ticket_context(cfg, msg): return ""
council._ticket_context = fake_ticket_context
async def fake_needs_context(cfg): return ""
council._needs_context = fake_needs_context
async def _fake_report_brief(cfg, text, *a, **k): return text
council.notify = ns(send=lambda *a, **k: None, report_brief=_fake_report_brief)
council.add_commander_note = lambda *a, **k: None
council.memory = ns(preamble=lambda: "")

cfg = Config(apps=[AppConfig(name="automatixy",
                             repo_path="/Users/romanberlin/Projects/Automatixy/Automatixy_Business_Automation",
                             base_branch="DEV", protected_branch="MAIN", backlog_backend="jira",
                             backlog={"base_url": "https://toibis.atlassian.net", "project_key": "AUTO"})],
             audit_path=str(Path(tempfile.mkdtemp()) / "audit.jsonl"))

ans = asyncio.run(council.respond_to_commander(cfg, "hey, how's it going?"))
sysp = cap.get("system", "")

# — signature / return shape unaffected (acceptance criterion 3) —
chk("returns a plain string reply", isinstance(ans, str) and bool(ans))

# — acceptance criterion 1: explicit 1-3 sentence instruction, old 2-4 wording gone —
chk("system: explicit 1-3 sentence brevity default", "1–3" in sysp or "1-3" in sysp)
chk("system: old looser '2-4 sentences' wording removed", "2–4 sentences" not in sysp and "2-4 sentences" not in sysp)
chk("system: names Telegram-style", "Telegram" in sysp)

# — acceptance criterion 2: no-preamble-filler + expand-only-on-request/decision clause —
chk("system: bans preamble filler openers", "Great question" in sysp and ("Let me" in sysp or "Let me..." in sysp))
chk("system: explicit no-preamble-filler instruction", "preamble filler" in sysp.lower() or "throat-clearing" in sysp.lower())
chk("system: expand only on explicit request", "explicitly" in sysp.lower() and "ask" in sysp.lower())
chk("system: expand only for genuine decision-structure need", "decision" in sysp.lower() and "structure" in sysp.lower())

print("\n============ EU-306 BREVITY PROMPT QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
