"""EU-287 — Group room: only the relevant officer(s) reply, briefly (stubbed SDK).

Covers the testable acceptance criteria:
  1. A backend-only question + a stubbed triage classifier returning the engineering-lane officer
     yields a reply from ONLY that officer — off-lane officers (Security, QA, ...) are never even
     called (no bubble, no run_agent call).
  2. Triage never polls more than 2 officers, even if the (stubbed) classifier lists more.
  3. A multi-sentence officer reply is trimmed to <=2 sentences by the length guard before it's
     persisted to the group thread.
  4. A greeting/small-talk message (triage returns 'NONE') still yields exactly one short (1-sentence)
     reply via the host fallback — the room is never empty.
  5. cockpit_views._group_inner renders only the most recent ~30 of 40 persisted messages, and the
     /group page renders a typing indicator while server._state['grouping'] is True (and none when
     False).
"""
import asyncio
import re
import sys
import tempfile
import types
from pathlib import Path

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

from orchestrator import server, council, cockpit_views
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


def _fresh_cfg():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "audit.jsonl").write_text("")
    cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                                 protected_branch="MAIN", backlog_backend="none")],
                 audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
    cfg.detected_auth = lambda: "test"
    return cfg


# Stub the cheap deterministic bits every case shares.
council.collect_signals = lambda c: {}
council.format_signals = lambda s: ""
council.recent_commander_notes = lambda c: ""

MULTI = ("Backend is fine. The 500s trace back to a stale connection pool entry. I already patched "
         "the retry logic. Ship it today and watch the error rate drop.")


def _scripted_run_agent(triage_reply: str, officer_replies: dict, calls: list):
    """Fake run_agent: routes on `tag`. 'group-triage' gets `triage_reply`; 'group-host' and any
    'group-<key>' officer tag look up `officer_replies` (default to a filler so an unexpected call
    is still visible in `calls` rather than crashing the harness)."""
    async def _run(prompt, options, tag=""):
        calls.append(tag)
        if tag == "group-triage":
            text = triage_reply
        else:
            text = officer_replies.get(tag, "PASS")
        return types.SimpleNamespace(final=text, text=text)
    return _run


# --- Case 1 + 3 (AC1, AC3): backend-only question -> ONLY the engineering-lane officer answers,
#     off-lane officers are never called, and the multi-sentence reply is trimmed to <=2 sentences. ---
cfg1 = _fresh_cfg()
calls1 = []
council.run_agent = _scripted_run_agent(
    "Dev Team Lead", {"group-field_engineer": MULTI}, calls1)
replies1 = asyncio.run(council.group_chat(cfg1, "Why is /api/leads returning 500s for tenant X?"))

chk("only the engineering-lane officer replies", [r for r, _ in replies1] == ["Dev Team Lead"], str(replies1))
chk("off-lane officers are never polled (no Security/QA/etc. run_agent call)",
    all(c in ("group-triage", "group-field_engineer") for c in calls1), str(calls1))
chk("exactly one officer tag was polled for a reply (plus triage)", calls1.count("group-field_engineer") == 1)
sent_count = len(re.split(r"(?<=[.!?])\s+", replies1[0][1].strip())) if replies1 else 99
chk("a multi-sentence reply is trimmed to <=2 sentences by the length guard", replies1 and sent_count <= 2,
    replies1[0][1] if replies1 else "")
chk("the trimmed reply keeps the FIRST sentences (not truncated mid-word)",
    replies1 and replies1[0][1].startswith("Backend is fine."), replies1[0][1] if replies1 else "")


# --- Case 2 (AC2): triage lists more than 2 officers -> at most 2 are ever polled/replying, never
#     the whole council (7 officers). ---
cfg2 = _fresh_cfg()
calls2 = []
council.run_agent = _scripted_run_agent(
    "Security Engineer, QA Engineer, Release Manager",   # classifier ignores the "at most 2" instruction
    {"group-provost": "Security looks clean here.", "group-scout": "QA has no signal yet.",
     "group-quartermaster": "Release readiness is fine."},
    calls2)
replies2 = asyncio.run(council.group_chat(cfg2, "General question for the room."))
officer_calls2 = [c for c in calls2 if c != "group-triage"]
chk("at most 2 officers are ever polled, never the full council", len(officer_calls2) <= 2, str(calls2))
chk("at most 2 officers reply", len(replies2) <= 2, str(replies2))
chk("the 3rd listed officer (Release Manager) was never called",
    "group-quartermaster" not in calls2, str(calls2))


# --- Case 4 (AC4): greeting / small talk -> triage returns NONE -> exactly one short (1-sentence)
#     reply via the host fallback; the room is never empty. ---
cfg3 = _fresh_cfg()
calls3 = []
council.run_agent = _scripted_run_agent(
    "NONE", {"group-host": "Morning! Hope the coffee's hot. Let's see what the day brings."}, calls3)
replies3 = asyncio.run(council.group_chat(cfg3, "hey team, good morning!"))
chk("a greeting still yields exactly one reply (room never dead)", len(replies3) == 1, str(replies3))
chk("the fallback reply came from the host tag", calls3 == ["group-triage", "group-host"], str(calls3))
sent3 = len(re.split(r"(?<=[.!?])\s+", replies3[0][1].strip())) if replies3 else 99
chk("the greeting fallback is exactly 1 sentence", replies3 and sent3 == 1, replies3[0][1] if replies3 else "")


# --- Case 5a (AC5): _group_inner windows to the most recent ~30 of 40 persisted messages. ---
cfg4 = _fresh_cfg()
for i in range(40):
    council._append_group(cfg4, "you" if i % 2 == 0 else "Dev Team Lead", f"message number {i}")
html_out = cockpit_views._group_inner(cfg4)
bubble_count = html_out.count('<div class="msg ')
chk("_group_inner renders at most ~30 of 40 persisted messages", bubble_count <= 30, f"count={bubble_count}")
chk("the newest message (39) is present", "message number 39" in html_out)
chk("an old message outside the window (0) is dropped", "message number 0" not in html_out)


# --- Case 5b (AC3 room UI): /group page shows a typing indicator while grouping, none when idle. ---
client = server.create_app(cfg4).test_client()
server._state["grouping"] = True
busy_html = client.get("/group").get_data(as_text=True)
chk("a typing indicator renders while _state['grouping'] is True", "weighing in" in busy_html)
server._state["grouping"] = False
idle_html = client.get("/group").get_data(as_text=True)
chk("no typing indicator renders once grouping finishes", "weighing in" not in idle_html)


print("\n============ EU-287 GROUP RELEVANCE + BREVITY QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
