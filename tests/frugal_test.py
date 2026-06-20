"""QA for the frugal server profile: cheap models for discussion, merged daily, usage governor."""
import asyncio, sys, tempfile, types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)   # store kwargs so we can read .model
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator import council, governor, memory
from orchestrator.config import Config, AppConfig

class RR:
    def __init__(s, t): s.final, s.text = t, t

captured = []
async def fake(prompt, options, tag=""):
    captured.append((getattr(options, "model", None), tag))
    return RR("Briefing: focus on AUTO-21 today.") if tag == "the-general" \
        else RR("Yesterday: shipped AUTO-16. Today: AUTO-21. Blockers: none.")
council.run_agent = fake
sent = []
council.notify.send = lambda m: sent.append(m)
async def _noscribe(cfg): return "(scribe stubbed)"
memory.scribe = _noscribe

d = Path(tempfile.mkdtemp())
(d / "audit.jsonl").write_text("")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)
OPUS = cfg.builder_model

# --- models: small-talk on Haiku, never Opus ---
captured.clear()
asyncio.run(council.small_talk(cfg))
st = [m for m, t in captured if t == "smalltalk"]
check("small-talk runs on Haiku", bool(st) and all(m == cfg.smalltalk_model for m in st), str(st))
check("small-talk never touches Opus", all(m != OPUS for m, _ in captured))

# --- daily muster = council + stand-up merged, one briefing, all on Sonnet ---
captured.clear(); sent.clear()
asyncio.run(council.hold_council(cfg))     # no topic = the daily muster
tags = [t for _, t in captured]
check("daily gathers the stand-up (officers report)", any(t.startswith("standup-") for t in tags), str(tags))
check("daily has the General brief", "the-general" in tags)
check("daily discussions never run on Opus",
      all(m != cfg.builder_model and m != cfg.reviewer_model for m, _ in captured))
check("daily discussions run on Sonnet", all(m == cfg.discussion_model for m, _ in captured))
check("daily sends exactly ONE muster briefing", sum(1 for m in sent if "Briefing" in m) == 1, str(sent))
check("no separate stand-up Telegram (truly merged)", not any(m.startswith("🫡") for m in sent))
check("daily still records the stand-up status", (d / "last-standup.md").exists())

# --- usage governor ---
cfg.usage_cap_per_hour = 2
governor.note_call(cfg, 3)
check("over the hourly cap -> throttled", not governor.under_budget(cfg))
check("calls_last_hour counts recent calls", governor.calls_last_hour(cfg) >= 3)
cfg.usage_cap_per_hour = 0
check("cap 0 disables the governor", governor.under_budget(cfg))

print("\n================ FRUGAL-SERVER QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
