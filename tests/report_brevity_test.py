"""Report-brevity QA: the daily/meeting reports the unit pushes to the Commander's phone are SUMMARIES,
not walls of chat. The council + meeting chair prompts demand a short, skimmable brief; the stand-up
Telegram ping is a roll-up + hand-offs/blockers only — the full per-officer round-table stays saved for
the cockpit, never pushed to Telegram.

EU-62: a verbose report send is model-distilled into a COMPLETE bulleted brief (every decision / action
item / blocker), never a truncated prefix that punts with "…(full report in the cockpit)". The distiller
runs on the cheap small-talk model and falls back to a deterministic bulletizer so a report is never lost."""
import sys, types, asyncio
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)   # store kwargs so a test can read .model
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
chk("ping shows a roll-up count", "3 engineer(s) reported" in ping)
chk("ping carries the hand-offs/blockers", "Hand-offs & blockers" in ping and "Scout → Builder: needs the API contract first" in ping)
chk("ping points to the cockpit for the rest", "Full round-table in the cockpit" in ping)
chk("ping does NOT dump the per-officer round-table", "###" not in ping and "Today: a11y pass" not in ping)
chk("ping is genuinely short (< 600 chars)", len(ping) < 600, f"len={len(ping)}")

# --- the SAVED stand-up keeps the full per-officer detail (cockpit, not Telegram) ---
full = council._standup_text(rows, handoffs)
chk("saved stand-up keeps every officer's full report", "### Scout" in full and "### Quartermaster" in full and "Today: a11y pass" in full)

# --- empty hand-offs still renders cleanly (bulletize normalises the '-' marker to '•') ---
chk("no hand-offs -> '• none'", "• none" in council._standup_telegram(rows, []))

# --- EU-62: a verbose report send is a COMPLETE bulleted brief, not a truncate-and-punt ----------------
from orchestrator import notify
import orchestrator.agent as agent

cfg = types.SimpleNamespace(smalltalk_model="haiku", discussion_model="sonnet")
LONG = ("Decision: adopt blue-green deploys across all apps. "
        "Action item: Builder wires the health gate by Friday. "
        "Action item: Scout adds an a11y smoke test to the suite. "
        "Blocker: the staging secret is missing and must be provisioned. "
        "The council weighed several alternatives at length before agreeing on this path, "
        "and noted the rollback story still needs a follow-up review next week.")

# happy path: the cheap model returns a bullet brief and it's used verbatim (no cockpit punt)
seen = {}
async def _model(prompt, options, tag=""):
    seen["model"], seen["tag"] = getattr(options, "model", None), tag
    return types.SimpleNamespace(
        final="• Decision: adopt blue-green deploys\n• Builder wires the health gate by Friday\n"
              "• Scout adds an a11y smoke test\n• Blocker: staging secret missing", text="")
agent.run_agent = _model
brief = asyncio.run(notify.report_brief(cfg, LONG))
chk("report brief is a bullet list", brief.count("•") >= 3, brief[:40])
chk("report brief carries the key items (decision + action + blocker survive)",
    "blue-green" in brief and "health gate" in brief and "Blocker" in brief)
chk("report brief drops the 'full report in the cockpit' punt",
    "cockpit" not in brief.lower() and "continue in the" not in brief.lower())
chk("distiller runs on the cheap small-talk model", seen.get("model") == "haiku", str(seen))

# short report stays natural — no model call, no forced bullets
seen.clear()
short = "All green. Two tickets landed on DEV; no blockers."
chk("a short report passes through verbatim (no model call)",
    asyncio.run(notify.report_brief(cfg, short)) == short)
chk("short report made no model call", seen == {})

# fallback: the model errors -> deterministic bullets, report never lost, still no punt
async def _boom(prompt, options, tag=""):
    raise RuntimeError("model down")
agent.run_agent = _boom
fb = asyncio.run(notify.report_brief(cfg, LONG))
chk("model error -> deterministic bullet fallback (report never lost)", fb.count("•") >= 3, fb[:40])
chk("fallback keeps a key action item", "health gate" in fb)
chk("fallback adds no cockpit punt", "cockpit" not in fb.lower())

# the truncate-and-punt behaviour is retired: even a long report bulletizes with NO footer
huge = "We reviewed many tickets and decided several things today. " * 30
hb = notify.bulletize(huge)
chk("a long report bulletizes with no cockpit footer", "•" in hb and "cockpit" not in hb.lower())
chk("bulletize keeps the message phone-skimmable", len(hb) <= 1060, f"len={len(hb)}")
chk("bulletize output is a genuine bullet list", all(l.startswith("• ") for l in hb.splitlines() if l.strip()))

# --- EU-62 regression: clip()'s truncate-and-punt is RETIRED at every report send site ----------------
# The notify-level checks above pass even if a send site quietly reverted to the old clip() prefix-and-
# footer, because they test notify in isolation. These source-level guards pin the exact defect the ticket
# fixes — a truncated teaser ending in "(full report in the cockpit)" — so it can never silently return.
import pathlib
chk("notify.clip (truncate-and-punt) is removed from the notify module",
    not hasattr(notify, "clip"))
_orch = pathlib.Path(notify.__file__).resolve().parent
_send_sites = ["council.py", "loop.py"]
for _fn in _send_sites:
    _src = (_orch / _fn).read_text()
    chk(f"{_fn} no longer calls notify.clip on any report send", "notify.clip(" not in _src, _fn)
    chk(f"{_fn} routes report sends through the bulleted brief (report_brief/bulletize)",
        "report_brief(" in _src or "bulletize(" in _src, _fn)
# the old clip() default footer is gone as an emitted string: it now only ever survives as the model's
# *forbidden* phrase (named in the distiller's system prompt so the model never writes it), never as code
# that appends it. Guard that nothing reintroduces the literal `more="…(full report in the cockpit)"` arg.
chk("no report send reintroduces the clip-style '…(full report in the cockpit)' footer arg",
    not any("more=" in (_orch / _fn).read_text() and "cockpit" in (_orch / _fn).read_text()
            for _fn in _send_sites))

print("\n============ REPORT BREVITY QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
