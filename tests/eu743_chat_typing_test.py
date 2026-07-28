"""EU-743 — 'CTO is typing…' indicator + double-Enter dedupe (already covered by EU-726).

Covers this ticket's testable acceptance criteria:
  1. Rapid double-Enter sends exactly one message — already implemented/tested by EU-726
     (tests/eu726_chat_dedupe_test.py); sanity-checked here too so a regression on THIS ticket's
     diff would still be caught.
  2. While the CTO reply is generating, the chat thread shows a 'CTO is typing…' indicator using
     the existing ``.typing`` CSS class (cockpit_views._CHAT_STYLE already defines it).
  3. The indicator clears as soon as the reply is available AND on the error path (never sticks
     on) — council.set_typing/is_typing claim/clear flag, cleared in decisions.route_message's
     background ``_answer()`` ``finally`` block.
  4. Normal single-Enter send behaviour and existing message rendering are unchanged.
"""
import re
import sys
import tempfile
import types
from pathlib import Path

# ── minimal SDK / requests stubs so the orchestrator can import without real deps ──
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import cockpit_views as V
from orchestrator import council, decisions, server
from orchestrator.config import AppConfig, Config

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


def _fresh_cfg() -> Config:
    tmp = Path(tempfile.mkdtemp())
    return Config(
        apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                         protected_branch="MAIN", backlog_backend="none")],
        audit_path=str(tmp / "audit.jsonl"),
        use_worktree=False,
    )


# ── AC: council.set_typing/is_typing claim/clear flag ────────────────────────────────────────
council.set_typing(False)  # start from a known-clear state (module-level flag, shared)
chk("is_typing() starts False", council.is_typing() is False)
council.set_typing(True)
chk("set_typing(True) -> is_typing() True", council.is_typing() is True)
council.set_typing(False)
chk("set_typing(False) -> is_typing() False again (clears)", council.is_typing() is False)

# ── AC: _chat_inner renders the indicator using the existing .typing CSS class ONLY while typing
cfg = _fresh_cfg()
council.set_typing(False)
inner_clear = V._chat_inner(cfg)
chk("no 'CTO is typing…' indicator when the flag is clear",
    "typing" not in re.sub(r'data-seq="\d+"', "", inner_clear).replace("class=typing", "")
    or 'class=typing' not in inner_clear,
    inner_clear[:200])

council.set_typing(True)
try:
    inner_typing = V._chat_inner(cfg)
    chk("'CTO is typing…' indicator renders while the flag is set",
        'class=typing' in inner_typing and 'CTO is typing' in inner_typing, inner_typing[:200])
finally:
    council.set_typing(False)  # never leave the shared flag set for later tests/processes

inner_after = V._chat_inner(cfg)
chk("indicator is gone again once the flag is cleared",
    'class=typing' not in inner_after, inner_after[:200])

# the existing .typing CSS class (cockpit_views._CHAT_STYLE) is reused, not a new style block
chk("the reused .typing CSS rule already exists in _CHAT_STYLE",
    ".typing{" in V._CHAT_STYLE)

# ── AC: the background reply thread claims + clears the flag — success path ─────────────────
cfg2 = _fresh_cfg()


class _FakeAudit:
    def record(self, *a, **k):
        pass


seen_typing_during_call = {"v": None}


def _fake_respond_success(cfg_arg, text_arg):
    seen_typing_during_call["v"] = council.is_typing()

    async def _inner():
        return "ok"
    return _inner()


orig_respond = council.respond_to_commander
council.respond_to_commander = _fake_respond_success
try:
    decisions.route_message(cfg2, _FakeAudit(), "hello CTO")
    import threading
    for t in threading.enumerate():
        if t.name != "MainThread":
            t.join(timeout=2)
finally:
    council.respond_to_commander = orig_respond

chk("the flag was SET while respond_to_commander (the reply generation) was running",
    seen_typing_during_call["v"] is True, seen_typing_during_call)
chk("the flag is CLEARED again once the reply thread finished (success path)",
    council.is_typing() is False)

# ── AC: the error path also clears the flag (never sticks on) ───────────────────────────────
cfg3 = _fresh_cfg()


def _fake_respond_error(cfg_arg, text_arg):
    async def _inner():
        raise RuntimeError("boom")
    return _inner()


council.respond_to_commander = _fake_respond_error
try:
    decisions.route_message(cfg3, _FakeAudit(), "hello again")
    import threading
    for t in threading.enumerate():
        if t.name != "MainThread":
            t.join(timeout=2)
finally:
    council.respond_to_commander = orig_respond

chk("the flag is CLEARED even when reply generation raises (error path)",
    council.is_typing() is False)

# ── AC: no change to normal single-Enter send / existing message rendering ──────────────────
app = server.create_app(cfg)
app.config.update(TESTING=True)
client = app.test_client()

r = client.get("/chat")
chk("GET /chat still 200s", r.status_code == 200, r.status_code)
body = r.get_data(as_text=True)
chk("single-Enter composer submit handler is unchanged (setSending still guards re-entry)",
    "if(sending)return;" in body and "setSending(true);" in body)

r2 = client.get("/api/chat-thread")
chk("GET /api/chat-thread still 200s", r2.status_code == 200, r2.status_code)

# ── AC: the poller's client-side script reconciles the .typing node like .pending ───────────
chk("poller inserts the .typing indicator when it newly appears",
    'newTyping' in body and 'insertBefore(newTyping' in body)
chk("poller removes the .typing indicator once the fragment no longer has one",
    'oldTyping.remove()' in body)


print("\n============ EU-743 CHAT TYPING INDICATOR ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
