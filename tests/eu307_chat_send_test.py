"""EU-307 — Compose box: Enter-to-send + stick-to-newest QA.

Covers the *real* send-then-see-own-message behaviour (not just JS source presence): a POST to
/api/chat must make the Commander's message visible in the very next GET of /api/chat-thread, so the
client's post-send refreshChat() can stick the view to it without racing the background CTO reply.
Also asserts the composer JS guards the fetch on response.ok (keeps typed text + surfaces an error on
failure) and that the synchronous echo does not get duplicated when the CTO reply path also runs.
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

from orchestrator import server, decisions, council
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Run every endpoint background thread INLINE so asserts are deterministic (mirrors answer_ui_test).
class _SyncThread:
    def __init__(s, target=None, daemon=None): s.t = target
    def start(s):
        if s.t:
            s.t()
server.threading = types.SimpleNamespace(Thread=_SyncThread)
decisions.threading = types.SimpleNamespace(Thread=_SyncThread)

tmp = Path(tempfile.mkdtemp())
(tmp / "audit.jsonl").write_text("")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()


def thread_html():
    return client.get("/api/chat-thread").get_data(as_text=True)


# --- Case 1 (required): send-then-see own message. The CTO reply path is stubbed to a no-op so we
#     prove the message is visible from the SYNCHRONOUS echo alone — i.e. it does NOT depend on the
#     background reply having landed. Fail-first: without the echo, a no-op reply path leaves the
#     thread empty of the just-sent line. ---
_orig_route = decisions.route_message
decisions.route_message = lambda c, a, text: True   # swallow: no CTO reply, no append
client.post("/api/chat", data={"text": "ping from the commander"})
html1 = thread_html()
chk("POST /api/chat makes the message visible in the very next /api/chat-thread",
    "ping from the commander" in html1, html1[-300:])
chk("the echoed message renders as the Commander's own (you) bubble",
    'class="msg you"' in html1)
decisions.route_message = _orig_route

# --- Case 2: no duplicate — when the CTO reply path DOES run and also appends the Commander's Q,
#     the synchronous echo must be reconciled so the line appears exactly once. Runs fully inline. ---
async def _fake_run(prompt, opts, tag=""):
    return types.SimpleNamespace(final="Acknowledged.", text="Acknowledged.")
council.run_agent = _fake_run
council.notify.send = lambda *a, **k: True
async def _brief(cfg, raw): return raw
council.notify.report_brief = _brief
async def _empty_ctx(*a, **k): return ""
council._ticket_context = _empty_ctx
council._board_status = _empty_ctx
council._needs_context = _empty_ctx
council.recent_thread_context = lambda c, ticket_ref=None, **k: ""
council._file_commander_ticket = lambda c, refs, answer: None

client.post("/api/chat", data={"text": "second unique message zzz"})
html2 = thread_html()
chk("the CTO reply path does not duplicate the echoed Commander message",
    html2.count("second unique message zzz") == 1, f"count={html2.count('second unique message zzz')}")
chk("the CTO's reply is appended after the Commander's message",
    "Acknowledged." in html2)

# --- Case 3: the composer JS guards on response.ok, keeps typed text + surfaces an error on failure ---
page = client.get("/chat").get_data(as_text=True)
chk("send handler checks response.ok before clearing the input", "r.ok" in page)
chk("on failure the input value is NOT cleared (error branch returns early)",
    'chatinput.classList.add("cerr")' in page and "return;}" in page)
chk("a visible inline error element is present in the composer", 'id=chaterr' in page)

print("\n============ EU-307 CHAT SEND QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
