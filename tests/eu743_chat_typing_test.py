"""EU-743 — 'CTO is typing…' indicator: claim/clear state flag, rendered via the existing .typing style.

Three layers, mirroring the ticket's acceptance criteria:

  (a) STATE CONTRACT — ``chat_typing`` is a first-class ``_STATE_KEYS`` entry and ``_new_state()``
      defaults it to False, so a cockpit restart mid-generation can never boot with a stuck
      indicator.

  (b) RENDER — ``_chat_inner()`` emits ``<div class=typing>`` (styled by the EXISTING ``.typing``
      CSS in ``_CHAT_STYLE``) exactly while the flag is set and nothing when it isn't, and the
      served ``/api/chat-thread`` poll fragment carries the same.

  (c) THREAD LIFECYCLE — the POST /api/chat reply thread (``_bg``) claims the flag before
      ``decisions.route_message`` runs and clears it on EVERY settle path. Simulated exactly as
      the review requires: ``decisions.route_message`` is patched — once to a recorder (success
      path: flag True *during* generation, False after), once to raise (error path: the finally
      must clear the flag so the indicator can't get stuck).

  Plus the live-page half: the composer stays on page after Enter (EU-307) and the append-only
  poll (EU-305) only carries selectors it knows about, so the served /chat script must reconcile
  the ``.typing`` banner itself — insert when it newly appears, remove when it clears.

The endpoint background thread runs INLINE (_SyncThread, mirroring eu307_chat_send_test) so every
assertion is deterministic — no sleeps, no races.
"""
import sys
import tempfile
import types
from pathlib import Path

# ── minimal SDK / requests stubs so the orchestrator imports without real deps ──
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import cockpit_state, cockpit_views, decisions, server
from orchestrator.config import AppConfig, Config

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: object = "") -> None:
    results.append((name, bool(cond), str(detail)))


# Run every endpoint background thread INLINE so asserts are deterministic (mirrors
# eu307_chat_send_test): Thread(...).start() executes the target synchronously.
class _SyncThread:
    def __init__(s, target=None, daemon=None):
        s.t = target

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

_state = cockpit_state._state
client = server.create_app(cfg).test_client()

# =============================================================================
# (a) STATE CONTRACT — registered key, False default
# =============================================================================
fresh = cockpit_state._new_state()
chk("(a) _new_state() returns chat_typing=False", fresh.get("chat_typing") is False,
    repr(fresh.get("chat_typing")))
chk("(a) 'chat_typing' is a registered _STATE_KEYS entry",
    "chat_typing" in cockpit_state._STATE_KEYS, cockpit_state._STATE_KEYS)

# =============================================================================
# (b) RENDER — _chat_inner shows the .typing banner iff the flag is set
# =============================================================================
_state["chat_typing"] = True
html_typing = cockpit_views._chat_inner(cfg)
chk("(b) _chat_inner renders '<div class=typing>' while the flag is set",
    "<div class=typing>" in html_typing, html_typing[:300])
chk("(b) the banner is the 'CTO is typing…' indicator", "CTO is typing" in html_typing,
    html_typing[:300])
chk("(b) the banner sits ABOVE the .thread (server render order: pending, typing, thread)",
    html_typing.find("<div class=typing>") != -1
    and html_typing.find("<div class=typing>") < html_typing.find("<div class=thread>"),
    html_typing[:300])

_state["chat_typing"] = False
html_idle = cockpit_views._chat_inner(cfg)
chk("(b) _chat_inner renders NO typing banner when the flag is clear",
    "<div class=typing>" not in html_idle, html_idle[:300])

# The flag must reach the served poll fragment too (that is what refreshChat fetches).
_state["chat_typing"] = True
api_body = client.get("/api/chat-thread").get_data(as_text=True)
chk("(b) GET /api/chat-thread carries the typing banner while generating",
    "<div class=typing>" in api_body, api_body[:300])
_state["chat_typing"] = False
api_body_idle = client.get("/api/chat-thread").get_data(as_text=True)
chk("(b) GET /api/chat-thread omits the banner once the flag clears",
    "<div class=typing>" not in api_body_idle, api_body_idle[:300])

# =============================================================================
# (c) THREAD LIFECYCLE — _bg claims the flag, finally clears it on EVERY path
# =============================================================================
# Success path: the flag is True WHILE route_message runs, False once it returns.
seen: dict = {}


def _route_ok(c, a, text):
    seen["during"] = _state.get("chat_typing")


decisions.route_message = _route_ok
client.post("/api/chat", data={"text": "hello cto"})
chk("(c) the reply thread claims the flag before generation starts",
    seen.get("during") is True, seen)
chk("(c) the flag is cleared once generation succeeds",
    _state.get("chat_typing") is False, repr(_state.get("chat_typing")))

# Error path: patch route_message to raise; after _bg() returns the flag MUST be False
# (the finally clears it), so the indicator can never get stuck on a failed generation.
def _route_raise(c, a, text):
    raise RuntimeError("boom")


decisions.route_message = _route_raise
client.post("/api/chat", data={"text": "this one errors"})
chk("(c) the flag is cleared when generation errors out (finally)",
    _state.get("chat_typing") is False, repr(_state.get("chat_typing")))
chk("(c) the failure surfaces as a red last_msg instead of a stuck indicator",
    "chat failed" in (_state.get("last_msg") or ""), repr(_state.get("last_msg")))

# =============================================================================
# LIVE PAGE — the stay-on-page poll reconciles the banner (insert / remove)
# =============================================================================
page = client.get("/chat").get_data(as_text=True)
chk("the served /chat poll reconciles .typing (queries both fetched and live nodes)",
    'frag.querySelector(".typing")' in page and 'cinner.querySelector(".typing")' in page)
chk("the poll inserts the banner when it newly appears", "insertBefore(newTyping" in page)
chk("the poll removes the banner when the fetched fragment no longer has it",
    "oldTyping.remove()" in page)

# =============================================================================
# Summary
# =============================================================================
passed_n = sum(1 for _, ok, _ in results if ok)
print("\n========= EU-743 chat typing-indicator tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("------------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
