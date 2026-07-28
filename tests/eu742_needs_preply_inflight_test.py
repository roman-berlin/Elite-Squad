"""EU-742 — in-flight + inline-confirm feedback for Needs-you buttons & pinned .preply replies.

The cockpit feedback this ticket asks for is supplied by the shared ``window.euPost`` helper
landed under **EU-719** (the `/needs` `/api/answer` fetch-submit + the `/chat` ``.preply``
fetch-submit with the inline '✓ sent' / `.perr` states). ``eu719_submit_feedback_test`` already
pins the *client*-side contracts. This harness exists so EU-742's acceptance criteria are
guarded **independently** of eu719_test — a later refactor of EU-719 could remove that file,
and EU-742's ACs would silently regress with nothing named for this ticket to go red.

Its headline assertion is the one thing eu719_test does NOT cover: **AC1's "including the Jira
file/close calls" clause**. The `/api/answer` `file_ticket` and `close` branches run their
backlog (Jira) calls **synchronously on the request thread** (only `clarification` self-
backgrounds), so the HTTP response is not returned until the Jira call has completed — which is
precisely what makes the client's '⏳ …' in-flight label span the Jira work instead of flashing
and vanishing. That synchronous-spanning contract is asserted here by recording the calling
thread inside a stubbed backlog adapter.

AC mapping:
  AC1 — Needs-you option/answer buttons disable + show an in-flight label until the response
        (including the synchronous Jira file/close calls) completes.
        Pinned two ways: (a) file/close backlog calls run on the request thread (this harness);
        (b) the /needs page binds every `form[action="/api/answer"]` through euPost with a
        '⏳ …' busy label + disable + reload-after-land + inline .nerr on failure.
  AC2 — A pinned .preply reply no longer does a native full-page POST/reload; it submits via
        fetch (delegated on document so the 5s poll's card swaps can't orphan it).
  AC3 — On submit the card body is replaced inline with '✓ sent' (preplySent) and the next poll
        drops the answered card rather than resurrecting a stale pending form.
  AC4 — On failure the card surfaces a visible .perr error; euPost re-enables the button, and
        the '✓ sent' success state is never reached on the failure path.

All HTTP is the in-process Flask test client — no network anywhere in this harness.
"""
from __future__ import annotations

import re
import sys
import tempfile
import threading
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

import orchestrator.cockpit_views as V  # noqa: E402
from orchestrator import backlog, decisions, needs, needs_sync, notify, server  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

# ── shared config / Flask test client ───────────────────────────────────────────
_TMP = Path(tempfile.mkdtemp())
# backlog_backend="jira" so /api/answer's supports_backlog gate is True; make_backlog is
# stubbed below so no real Jira call is ever made.
_CFG = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_TMP), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="jira")],
    audit_path=str(_TMP / "audit.jsonl"),
    use_worktree=False,
)
_CFG.detected_auth = lambda: "test"

# One structured pending decision so BOTH surfaces render their reply widgets: the chat page's
# pinned card (_chat_inner → decisions.load) and the Needs-you decision card (/needs →
# needs.summary). Mirrors eu719_submit_feedback_test.
_DECISION = {"id": "AUTO-77", "app": "automatixy",
             "question": ("EU-77: pick the cache layer\n\nProblem: the hot path re-reads "
                          "the config on every call.\n\nOptions:\n1. Redis\n2. Memcached\n\n"
                          "Recommendation: Option 1")}
decisions.load = lambda c: [dict(_DECISION)]
needs.summary = lambda c, a=None: {
    "total": 1,
    "rows": [{**_DECISION, "category": "decision", "why": "pick the cache layer"}],
    "decisions": [dict(_DECISION)], "proposals": [], "tasks": [],
}
needs_sync.reconcile = lambda *a, **k: {"checked": 0, "cleared": [], "unknown": []}
needs.clear_cache()

# Hermetic backlog: record the calling thread of each Jira-side method so we can prove the
# file/close branches run SYNCHRONOUSLY on the request thread (AC1's "including the Jira
# file/close calls" clause). create_task returns a key so the file path reports success.
_CALLS: dict[str, list[int]] = {"create_task": [], "set_status": [], "add_comment": []}


class _FakeBacklog:
    def create_task(self, summary, description, labels=None, **kw):
        _CALLS["create_task"].append(threading.get_ident())
        return "FAKE-1"

    def set_status(self, ticket, status):
        _CALLS["set_status"].append(threading.get_ident())

    def add_comment(self, ticket, body):
        _CALLS["add_comment"].append(threading.get_ident())


_orig_make_backlog = backlog.base.make_backlog
backlog.base.make_backlog = lambda app: _FakeBacklog()  # noqa: E731 - hermetic test stub
# notify.send is hit on the file-success path; stub it so no Telegram attempt leaves the harness.
_orig_notify_send = notify.send
notify.send = lambda *a, **k: None  # noqa: E731

_APP = server.create_app(_CFG)
_CLIENT = _APP.test_client()
_MAIN = threading.get_ident()  # the Flask test client serves the request on THIS thread

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


def _reset_calls() -> None:
    for k in _CALLS:
        _CALLS[k] = []


# =============================================================================
# AC1 (server) — the file/close Jira branches are SYNCHRONOUS on the request thread
# =============================================================================
_reset_calls()
r_file = _CLIENT.post("/api/answer", data={"ticket": "AUTO-77", "app": "automatixy",
                                           "text": "please open a ticket for this"})
chk("AC1 file_ticket: /api/answer answers with a redirect (the client helper treats as success)",
    r_file.status_code in (301, 302, 303, 307, 308), f"status={r_file.status_code}")
chk("AC1 file_ticket: the backlog create_task ran (the Jira call is not skipped)",
    len(_CALLS["create_task"]) == 1, f"calls={_CALLS['create_task']!r}")
chk("AC1 file_ticket: create_task ran on the REQUEST thread (synchronous — the in-flight "
    "label spans it, not a background thread that returns instantly)",
    _CALLS["create_task"] and _CALLS["create_task"][-1] == _MAIN,
    f"thread={_CALLS['create_task'][-1] if _CALLS['create_task'] else None} main={_MAIN}")

_reset_calls()
r_close = _CLIENT.post("/api/answer", data={"ticket": "AUTO-77", "app": "automatixy",
                                            "text": "close it"})
chk("AC1 close: /api/answer answers with a redirect",
    r_close.status_code in (301, 302, 303, 307, 308), f"status={r_close.status_code}")
chk("AC1 close: set_status (transition to Done) ran on the REQUEST thread (synchronous)",
    bool(_CALLS["set_status"]) and _CALLS["set_status"][-1] == _MAIN,
    f"set_status={_CALLS['set_status']!r}")
chk("AC1 close: add_comment ran on the REQUEST thread (synchronous)",
    bool(_CALLS["add_comment"]) and _CALLS["add_comment"][-1] == _MAIN,
    f"add_comment={_CALLS['add_comment']!r}")

# Contrast (race-free): a clarification answer never files a ticket — create_task is NOT called.
# (clarification self-backgrounds and returns instantly, which is WHY only file/close needed
#  the synchronous-wait feedback EU-719 added.)
_reset_calls()
_orig_handle_reply = decisions.handle_reply
decisions.handle_reply = lambda *a, **k: True
try:
    _CLIENT.post("/api/answer", data={"ticket": "AUTO-77", "app": "automatixy",
                                      "text": "use Redis for the cache"})
finally:
    decisions.handle_reply = _orig_handle_reply
chk("AC1 contrast: a clarification answer never calls create_task (only file/close file)",
    len(_CALLS["create_task"]) == 0, f"calls={_CALLS['create_task']!r}")

# =============================================================================
# AC1 (client) — /needs option/answer buttons disable + '⏳ …' label via euPost
# =============================================================================
needs_body = _CLIENT.get("/needs").get_data(as_text=True)
needs_script_m = re.search(r"form\[action=.{0,14}/api/answer.*?</script>", needs_body, re.S)
needs_script = needs_script_m.group(0) if needs_script_m else ""
chk("AC1 /needs: every /api/answer form is bound (option buttons + free-text answer box)",
    'querySelectorAll(\'form[action="/api/answer"]\')' in needs_body, needs_body[-2500:])
chk("AC1 /needs: submit is intercepted (no native POST reload mid-call)",
    "preventDefault" in needs_script, needs_script)
chk("AC1 /needs: submit routes through the shared euPost helper (disable + busy label)",
    "window.euPost(f)" in needs_script, needs_script)
chk("AC1 /needs: page reloads only AFTER the submit lands (banner + cleared row render)",
    "location.reload()" in needs_script, needs_script)
chk("AC1 /needs: failure surfaces an inline .nerr instead of silently reloading",
    "className='nerr'" in needs_body and "showErr(f)" in needs_script and ".nerr{" in needs_body,
    needs_script)
chk("AC1 /needs: forms stay native POSTs as the no-JS fallback",
    "method=post action=/api/answer" in needs_body)

# =============================================================================
# AC2 — pinned .preply reply submits via fetch (no native full-page POST/reload)
# =============================================================================
chat_body = _CLIENT.get("/chat").get_data(as_text=True)
chk("AC2 /chat: pinned card present with a .preply form keyed by data-tid",
    'class=pcard data-tid="AUTO-77"' in chat_body and "class=preply method=post action=/api/chat"
    in chat_body, chat_body[:3000])
chk("AC2 /chat: .preply submit is intercepted on document (survives the poll's card swaps)",
    'document.addEventListener("submit"' in chat_body
    and 'classList.contains("preply")' in chat_body)
chk("AC2 /chat: .preply submit goes through euPost (fetch, not a native POST/reload)",
    bool(re.search(r'contains\("preply"\).*?window\.euPost\(f\)', chat_body, re.S)))

# =============================================================================
# AC3 — card body replaced inline with '✓ sent'; the poll drops answered cards
# =============================================================================
chk("AC3 /chat: on success the card body is replaced inline with '✓ sent'",
    'innerHTML="<div class=psent>✓ sent</div>"' in chat_body, chat_body[-3000:])
chk("AC3 /chat: '✓ sent' state is visibly styled", ".psent{" in chat_body)
chk("AC3 /chat: answered tids are recorded (preplySent) so the poll can't resurrect pending",
    "preplySent[tid]=1" in chat_body and "var preplySent={}" in chat_body)
chk("AC3 /chat: the poll drops already-answered cards from the fetched fragment",
    'querySelectorAll(".pcard[data-tid]")' in chat_body
    and "preplySent[c.getAttribute(" in chat_body)

# =============================================================================
# AC4 — failure surfaces a visible error; never left on '✓ sent'
# =============================================================================
chk("AC4 /chat: failure appends a visible .perr error line",
    '"perr"' in chat_body and ".perr{" in chat_body and "retry" in chat_body)
# The '✓ sent' success state lives ONLY in euPost's success branch — extract the two .then
# branches and assert the failure branch surfaces .perr and does NOT set '✓ sent'.
_branches = re.search(r'window\.euPost\(f\)\.then\(\s*function\(\)\{(.*?)\},\s*'
                      r'function\(\)\{(.*?)\}\s*\)', chat_body, re.S)
chk("AC4 /chat: the euPost .then(success, failure) two-branch structure is present",
    bool(_branches), chat_body[-1200:])
if _branches:
    _ok_branch, _err_branch = _branches.group(1), _branches.group(2)
    chk("AC4 /chat: '✓ sent' is reached ONLY on the success branch", "✓ sent" in _ok_branch,
        _ok_branch)
    chk("AC4 /chat: the failure branch surfaces .perr (NOT a silent revert)", "perr" in _err_branch,
        _err_branch)
    chk("AC4 /chat: the failure branch never sets '✓ sent' (can't hang on it)",
        "✓ sent" not in _err_branch, _err_branch)
else:
    chk("AC4 /chat: '✓ sent' success branch present (fallback check)", "✓ sent" in chat_body)
    chk("AC4 /chat: failure .perr present (fallback check)", "perr" in chat_body)

# =============================================================================
# Shared helper sanity (the foundation AC1-AC4 all sit on)
# =============================================================================
wrap = V._wrap("Probe", "<p>x</p>")
chk("foundation: _wrap injects the shared window.euPost helper", "window.euPost" in wrap)
chk("foundation: euPost disables form buttons + relabels primary '⏳ …' while in flight",
    "b.disabled=true" in wrap and "⏳" in wrap and "Sending" in wrap, wrap[-1200:])
chk("foundation: euPost re-enables + restores buttons on error, then re-throws",
    "p[0].disabled=false" in wrap and "p[0].innerHTML=p[1]" in wrap and "throw e" in wrap,
    wrap[-1200:])

# ── restore patched module attrs so a later harness sees the real functions ──
backlog.base.make_backlog = _orig_make_backlog
notify.send = _orig_notify_send

# ============ Summary (k/n contract run_all.py verifies) ============ #
passed_n = sum(1 for _, ok, _ in results if ok)
print("\n========= EU-742 Needs-you / .preply in-flight feedback tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("---------------------------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
