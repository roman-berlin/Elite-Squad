"""QA for chat: the group room never goes dead (all-PASS still replies); the General answers 1:1."""
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

d = Path(tempfile.mkdtemp())
(d / "audit.jsonl").write_text("")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)

# --- group room: EVERY officer PASSes (a greeting) -> the host must still reply ---
async def all_pass(prompt, opts, tag=""):
    if tag == "group-host":
        return R("Good to hear from you, Commander — all quiet here. Want us to pick up a ticket?")
    return R("PASS")
council.run_agent = all_pass
replies = asyncio.run(council.group_chat(cfg, "how are you? how is it going?"))
check("all-PASS greeting still gets a reply (room not dead)", len(replies) >= 1, str(replies))
check("the fallback reply isn't a PASS", replies and "PASS" not in replies[0][1])

# --- group room: an in-lane officer answers normally ---
# EU-287: group_chat now triages to the 1-2 relevant officers before polling anyone, so the stub
# must answer the "group-triage" call with the officer it wants selected.
async def scout_answers(prompt, opts, tag=""):
    if tag == "group-triage":
        return R("QA Engineer")
    return R("DEV smoke is green; nothing flaky today.") if tag.startswith("group-scout") else R("PASS")
council.run_agent = scout_answers
replies = asyncio.run(council.group_chat(cfg, "is DEV stable right now?"))
check("an in-lane officer answers", any("smoke is green" in t for _, t in replies), str(replies))

# --- the General 1:1: returns a short answer, pings Telegram, logs the exchange ---
sent = []
council.notify.send = lambda m: sent.append(m)
async def general_reply(prompt, opts, tag=""):
    return R("All quiet — the daily council's got status. Anything you want me to point the unit at?")
council.run_agent = general_reply
ans = asyncio.run(council.respond_to_commander(cfg, "hi, how's it going?"))
check("General returns an answer", "All quiet" in ans)
check("General pings Telegram", any("All quiet" in m for m in sent))

# --- commander notes stay lean: one bounded line each, only the last few feed back ---
council.add_commander_note(cfg, "huge\nmulti\nline answer " + "x" * 500)
lastline = council._notes_file(cfg).read_text(encoding="utf-8").splitlines()[-1]
check("a note collapses to one bounded line (no wall-of-text fed back)",
      "\n" not in lastline and len(lastline) < 300, str(len(lastline)))
for i in range(20):
    council.add_commander_note(cfg, f"decision {i}")
check("only the last ~12 notes feed back into prompts",
      len(council.recent_commander_notes(cfg).splitlines()) <= 12)

print("\n================ CHAT QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
