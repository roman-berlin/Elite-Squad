"""EU-307 — Group composer: send-then-see-own-message (mirrors eu307_chat_send_test.py for /group).

Covers the *real* send-then-see behaviour of the Group room composer (not just JS source presence):
- A POST to /api/group must make the Commander's message visible in the very next GET of
  /api/group-thread purely from the SYNCHRONOUS echo (council._append_group), so the client's
  post-send refreshGroup() can stick the view to it WITHOUT waiting on the background
  council.group_chat officer replies to land. Proven here with the officer-reply path stubbed to a
  no-op — the message is still visible from the echo alone.
- When the real (non-stubbed) officer-reply path DOES run (echo=False, so group_chat must NOT
  re-append the Commander's line), the Commander's message appears EXACTLY once (no duplicate)
  alongside the officer's reply.
"""
import asyncio
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

from orchestrator import server, council
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Run every endpoint background thread INLINE so asserts are deterministic (mirrors chat send test).
class _SyncThread:
    def __init__(s, target=None, daemon=None): s.t = target
    def start(s):
        if s.t:
            s.t()
server.threading = types.SimpleNamespace(Thread=_SyncThread)

tmp = Path(tempfile.mkdtemp())
(tmp / "audit.jsonl").write_text("")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()


def thread_html():
    return client.get("/api/group-thread").get_data(as_text=True)


# --- Case 1 (required): send-then-see own message with the officer-reply path stubbed to a NO-OP.
#     Proves the message is visible from the SYNCHRONOUS echo alone — i.e. it does NOT depend on the
#     background council.group_chat reply landing first. Fail-first: without the echo, a no-op reply
#     path leaves the group thread empty of the just-sent line. ---
_orig_group_chat = council.group_chat
async def _noop_group_chat(cfg, message, officers=None, audit=None, echo=True):
    return []
council.group_chat = _noop_group_chat
client.post("/api/group", data={"text": "ping to the group room"})
html1 = thread_html()
chk("POST /api/group makes the message visible in the very next /api/group-thread",
    "ping to the group room" in html1, html1[-300:])
chk("the echoed group message renders as the Commander's own (you) bubble",
    'class="msg you"' in html1)
council.group_chat = _orig_group_chat

# --- Case 2: real officer-reply path runs (echo=False). The Commander's echoed line must appear
#     EXACTLY once (group_chat must not re-append it) alongside the officer's reply. Runs inline. ---
async def _fake_run(prompt, opts, tag=""):
    return types.SimpleNamespace(final="Officer acknowledges.", text="Officer acknowledges.")
council.run_agent = _fake_run
council.collect_signals = lambda c: {}
council.format_signals = lambda s: ""
council.recent_commander_notes = lambda c: ""
council._select_officers = lambda keys: [council.COUNCIL[0]]

client.post("/api/group", data={"text": "group unique message zzz"})
html2 = thread_html()
chk("the officer-reply path does not duplicate the echoed Commander message",
    html2.count("group unique message zzz") == 1, f"count={html2.count('group unique message zzz')}")
chk("the officer's reply is appended alongside the Commander's message",
    "Officer acknowledges." in html2)

print("\n============ EU-307 GROUP SEND QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
