"""Report-brevity QA: the daily/meeting reports the unit pushes to the Commander's phone are SUMMARIES,
not walls of chat. The council + meeting chair prompts demand a short, skimmable brief; the stand-up
Telegram ping is a roll-up + hand-offs/blockers only — the full per-officer round-table stays saved for
the cockpit, never pushed to Telegram."""
import sys, types
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import council

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- chair prompts instruct a SHORT, summarized brief (with an explicit ceiling) ---
chk("council chair told to be SHORT + skimmable + word-capped",
    "SHORT" in council._CHAIR_SYSTEM and "under ~90 words" in council._CHAIR_SYSTEM and "skim" in council._CHAIR_SYSTEM)
chk("council chair told NOT to recap officer-by-officer",
    "do NOT restate the debate" in council._CHAIR_SYSTEM or "recap officer-by-officer" in council._CHAIR_SYSTEM)
chk("meeting chair told to be SHORT + word-capped",
    "SHORT" in council._MEETING_CHAIR_SYSTEM and "under ~80 words" in council._MEETING_CHAIR_SYSTEM)

# --- stand-up Telegram ping is a concise roll-up + hand-offs/blockers, NOT every officer's report ---
rows = [("Scout", "Yesterday: swept DEV\nToday: a11y pass\nBlockers: none"),
        ("Provost Marshal", "Yesterday: CSP review\nToday: secrets audit\nBlockers: none"),
        ("Quartermaster", "Yesterday: deploy check\nToday: Dockerfile\nBlockers: none")]
handoffs = ["Scout → Builder: needs the API contract first"]
ping = council._standup_telegram(rows, handoffs)
chk("ping shows a roll-up count", "3 officer(s) reported" in ping)
chk("ping carries the hand-offs/blockers", "Hand-offs & blockers" in ping and "Scout → Builder: needs the API contract first" in ping)
chk("ping points to the cockpit for the rest", "Full round-table in the cockpit" in ping)
chk("ping does NOT dump the per-officer round-table", "###" not in ping and "Today: a11y pass" not in ping)
chk("ping is genuinely short (< 600 chars)", len(ping) < 600, f"len={len(ping)}")

# --- the SAVED stand-up keeps the full per-officer detail (cockpit, not Telegram) ---
full = council._standup_text(rows, handoffs)
chk("saved stand-up keeps every officer's full report", "### Scout" in full and "### Quartermaster" in full and "Today: a11y pass" in full)

# --- empty hand-offs still renders cleanly ---
chk("no hand-offs -> '- none'", "- none" in council._standup_telegram(rows, []))

print("\n============ REPORT BREVITY QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
