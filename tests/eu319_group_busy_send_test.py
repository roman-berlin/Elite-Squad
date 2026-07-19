"""EU-319 — a group message typed while the officers are replying must never be silently dropped.

Found 2026-07-14 reviewing EU-307's landed code. Two changes met badly:

  * ``/api/group`` guarded the send with ``if text and not _state['grouping']`` but returned the
    same bare ``redirect('/group')`` (302) either way — a refused send was indistinguishable from
    an accepted one on the wire;
  * EU-307 turned the composer into Enter-to-send via ``fetch``, and fetch FOLLOWS the 302 to a
    200, so ``r.ok`` was true. The handler then cleared the input and showed no error.

Net effect: type into the group room while the unit is mid-reply and the Commander's message is
gone, with a UI that reported success. This harness pins the contract that closes it — a refused
send is a 409 that changed nothing, and the accepted path (EU-307) is untouched.

Mirrors tests/eu307_group_send_test.py's scaffolding (stubbed SDK/requests, inline threads, tmp
Config). Soft ``k/n passed`` tally so ``tests/run_all.py`` (the EU-44 gate) judges it honestly.
"""
import sys
import tempfile
import threading
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import cockpit_state, council, server  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

results: list[tuple[str, bool, str]] = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


class _SyncThread:
    """Run the officer-reply worker INLINE on .start() so the asserts are deterministic."""
    def __init__(s, target=None, daemon=None, **kw): s.t = target
    def start(s):
        if s.t:
            s.t()

server.threading = types.SimpleNamespace(Thread=_SyncThread, Event=threading.Event,
                                         Lock=threading.Lock)

tmp = Path(tempfile.mkdtemp())
(tmp / "audit.jsonl").write_text("", encoding="utf-8")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()


def thread_html() -> str:
    return client.get("/api/group-thread").get_data(as_text=True)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 1) BUSY: the officers are mid-reply — the send must be refused loudly, and change nothing
# ══════════════════════════════════════════════════════════════════════════════════════════════
cockpit_state._state["grouping"] = True    # exactly the window group_chat holds open

LOST = "message typed while the officers were replying zzz"
r_busy = client.post("/api/group", data={"text": LOST})

chk("a send during a reply returns a non-2xx busy signal, not a bare 302 (EU-319)",
    r_busy.status_code == 409,
    f"status={r_busy.status_code} — fetch follows a 302 to a 200 and the composer reports success")
chk("the busy response says nothing was queued (EU-319)",
    (r_busy.get_json() or {}).get("queued") is False and (r_busy.get_json() or {}).get("busy") is True,
    str(r_busy.get_json())[:120])
chk("the refused message is NOT echoed into the thread (no half-send)",
    LOST not in thread_html(), "the message was echoed but no officer will ever answer it")

cockpit_state._state.pop("grouping", None)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 2) The composer keeps the text and shows a BUSY notice on 409 (not the connection-error copy)
# ══════════════════════════════════════════════════════════════════════════════════════════════
body = client.get("/group").get_data(as_text=True)

chk("the composer branches on the 409 busy signal (EU-319)",
    "status===409" in body, "no 409 branch — every non-ok is reported as a connection failure")
chk("the busy notice tells the Commander the message was NOT sent (EU-319)",
    "NOT sent" in body or "not sent" in body, body[-400:] if "409" not in body else "")
chk("the input is only cleared on success — the typed text survives a 409 (EU-319)",
    body.count('groupinput.value=""') == 1 and 'groupinput.focus();return;' in body,
    "the clear is not guarded by the !ok early-return")
# Backpressure is not a failure: no red error ring on the busy path (the `cerr` class is only
# added on the real connection-error branch).
chk("a busy send does not paint the composer as broken (EU-319)",
    body.count('groupinput.classList.add("cerr")') == 1, "cerr is applied on the busy path too")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 3) NO EU-307 REGRESSION: the idle send path still 302s and echoes exactly once
# ══════════════════════════════════════════════════════════════════════════════════════════════
async def _fake_run(prompt, opts, tag=""):
    return types.SimpleNamespace(final="Officer acknowledges.", text="Officer acknowledges.")
council.run_agent = _fake_run
council.collect_signals = lambda c: {}
council.format_signals = lambda s: ""
council.recent_commander_notes = lambda c: ""
council._select_officers = lambda keys: [council.COUNCIL[0]]

SENT = "group unique message while idle yyy"
r_ok = client.post("/api/group", data={"text": SENT})
html = thread_html()

chk("an idle send still redirects (302) — the no-JS form fallback is untouched (EU-307)",
    r_ok.status_code == 302, f"status={r_ok.status_code}")
chk("an idle send is visible in the very next /api/group-thread (EU-307)",
    SENT in html, html[-200:])
chk("an idle send is echoed exactly once (EU-307)",
    html.count(SENT) == 1, f"count={html.count(SENT)}")
chk("the officer's reply still lands alongside it (EU-307)", "Officer acknowledges." in html)
chk("the room is released after the reply completes",
    not cockpit_state._state.get("grouping"), "grouping stayed latched — the room is now wedged")

# An empty send is still a plain redirect (it must not claim the room, nor 409).
r_empty = client.post("/api/group", data={"text": "   "})
chk("an empty send is a no-op redirect, not a 409", r_empty.status_code == 302,
    f"status={r_empty.status_code}")
chk("an empty send does not latch the room",
    not cockpit_state._state.get("grouping"))

print("\n============ EU-319 GROUP BUSY-SEND QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
