"""EU-82: intent classifier + action router for /api/answer.

Tests that:
  1. classify_intent correctly labels each intent family.
  2. /api/answer routes 'file_ticket' → create_task, no handle_reply.
  3. /api/answer routes 'close' → set_status + add_comment, no handle_reply.
  4. /api/answer routes 'defer' → no action beyond dismiss, no handle_reply.
  5. /api/answer routes 'clarification' → handle_reply as before (regression guard).
  6. _route_out_of_scope is never invoked on the three new directive paths.
"""
import sys
import types
import tempfile
from pathlib import Path

# --- stub the agent SDK and requests so no network calls escape ---
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules["requests"] = req

sys.path.insert(0, ".")

from orchestrator.intent import classify_intent
from orchestrator import server, decisions, autopilot
import orchestrator.backlog.base as backlog_base
from orchestrator.config import Config, AppConfig

results = []

def chk(name, cond, detail=""):
    results.append((name, bool(cond), detail))


# ── run /api/answer's background thread inline for deterministic assertions ──
class _SyncThread:
    def __init__(s, target=None, daemon=None): s.t = target
    def start(s):
        if s.t:
            s.t()

server.threading = types.SimpleNamespace(Thread=_SyncThread)

tmp = Path(tempfile.mkdtemp())
cfg = Config(
    apps=[AppConfig(
        name="automatixy", repo_path=str(tmp), base_branch="DEV",
        protected_branch="MAIN", backlog_backend="jira",
        backlog={"base_url": "https://toibis.atlassian.net", "project_key": "AUTO"},
    )],
    audit_path=str(tmp / "audit.jsonl"),
    use_worktree=False,
)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()


# ─────────────────────────────────────────────────────────────────────────────
# 1. classify_intent — unit tests
# ─────────────────────────────────────────────────────────────────────────────
print("\n── classify_intent unit tests ──")

file_ticket_cases = [
    "open a ticket",
    "open a new ticket",
    "create a ticket",
    "file a ticket for this",
    "raise a ticket",
    "please open a ticket",
    "can you make a ticket",
    "create an issue",
    "new ticket",
]
for phrase in file_ticket_cases:
    chk(f"classify_intent file_ticket: {phrase!r}",
        classify_intent(phrase) == "file_ticket",
        classify_intent(phrase))

close_cases = [
    "close it",
    "close this",
    "close out",
    "resolve it",
    "resolve this",
    "done",
    "mark it as done",
    "mark as closed",
    "won't fix",
    "wontfix",
    "invalid",
    "duplicate",
    "finished",
    "resolved",
    "complete",
]
for phrase in close_cases:
    chk(f"classify_intent close: {phrase!r}",
        classify_intent(phrase) == "close",
        classify_intent(phrase))

defer_cases = [
    "defer",
    "defer it",
    "skip it",
    "skip this",
    "later",
    "not now",
    "postpone",
    "hold off",
    "park it",
    "ignore it",
    "ignore for now",
    "backlog it",
]
for phrase in defer_cases:
    chk(f"classify_intent defer: {phrase!r}",
        classify_intent(phrase) == "defer",
        classify_intent(phrase))

clarification_cases = [
    "use the 8/5 IA",
    "go with option A",
    "use DD/MM format",
    "the second approach",
    "yes, proceed",
    "",   # empty → clarification (guard handles it)
]
for phrase in clarification_cases:
    chk(f"classify_intent clarification: {phrase!r}",
        classify_intent(phrase) == "clarification",
        classify_intent(phrase))


# ─────────────────────────────────────────────────────────────────────────────
# 1b. Incidental keyword guard (EU-82 iter-3): a multi-word clarification that merely
#     MENTIONS 'invalid'/'complete'/'done'/'ignore'/'later'/'duplicate'/'skip'/'hold'
#     while carrying a real instruction must stay 'clarification' — NOT close/defer.
# ─────────────────────────────────────────────────────────────────────────────
print("\n── incidental-keyword clarification tests ──")
incidental_cases = [
    "the current date parsing is invalid, switch to ISO 8601",       # 'invalid'
    "don't mark the export complete until the upload finishes",       # 'complete'
    "the migration is done so re-run against the new schema",         # 'done'
    "ignore the stale cache and re-fetch from the API",               # 'ignore'
    "we can polish the animation later, just ship the fix now",       # 'later'
    "this looks like a duplicate but verify the orders table first",  # 'duplicate'
    "skip the empty rows when you compute the totals",               # 'skip'
    "hold the toast for 3s, not 1s, before it fades",               # 'hold'
]
for phrase in incidental_cases:
    chk(f"classify_intent clarification (incidental keyword): {phrase!r}",
        classify_intent(phrase) == "clarification",
        classify_intent(phrase))

# Keyword-LED but still instructive: starts with a close/defer word yet continues with a real
# instruction. These are short enough to pass the word-count guard, so they prove the ANCHORED
# fullmatch (not just the length backstop) is what keeps them out of close/defer.
anchored_proof_cases = [
    "done with auth, now wire the dashboard",     # 6-7 words, leads with 'done'
    "later, after the modal lands, revisit this",  # leads with 'later'
    "invalid date, use ISO 8601 instead",          # leads with 'invalid'
]
for phrase in anchored_proof_cases:
    chk(f"classify_intent clarification (keyword-led, instructive): {phrase!r}",
        classify_intent(phrase) == "clarification",
        classify_intent(phrase))

# And the directives themselves must STILL classify (tightening must not over-correct):
chk("classify_intent close: 'won't fix — no longer needed' (stacked cores)",
    classify_intent("won't fix — no longer needed") == "close",
    classify_intent("won't fix — no longer needed"))
chk("classify_intent defer: 'ignore for now' (stacked cores)",
    classify_intent("ignore for now") == "defer",
    classify_intent("ignore for now"))


# ─────────────────────────────────────────────────────────────────────────────
# 2. /api/answer → 'file_ticket': create_task called, handle_reply NOT called
# ─────────────────────────────────────────────────────────────────────────────
print("\n── /api/answer intent routing tests ──")

handle_reply_called = {}
decisions.handle_reply = lambda c, a, t: (handle_reply_called.__setitem__("called", True) or True)

filed = {}

class _FileBL:
    def create_task(s, summary, description, labels=None, issue_type="Task"):
        filed["summary"] = summary
        filed["description"] = description
        filed["labels"] = labels
        return "AUTO-99"
    def add_comment(s, t, b): pass
    def set_status(s, t, st): pass
    def find_open_by_summary(s, summ): return None

backlog_base.make_backlog = lambda app: _FileBL()

handle_reply_called.clear()
filed.clear()
notify_msgs = []
import orchestrator.notify as _notify_mod
_notify_mod.send = lambda m: notify_msgs.append(m)

client.post("/api/answer", data={"ticket": "AUTO-10", "app": "automatixy", "text": "open a ticket"})
chk("file_ticket: create_task called", "summary" in filed)
chk("file_ticket: summary references source tid", "AUTO-10" in (filed.get("summary") or ""))
chk("file_ticket: description links source tid", "AUTO-10" in (filed.get("description") or ""))
chk("file_ticket: label is commander-directive",
    "commander-directive" in (filed.get("labels") or []))
chk("file_ticket: handle_reply NOT called", "called" not in handle_reply_called)
chk("file_ticket: Telegram notify sent", any("AUTO-99" in m for m in notify_msgs))


# ─────────────────────────────────────────────────────────────────────────────
# 3. /api/answer → 'close': set_status + add_comment, handle_reply NOT called
# ─────────────────────────────────────────────────────────────────────────────
closed = {}
commented = {}

class _CloseBL:
    def create_task(s, *a, **k): return None
    def set_status(s, ticket, status):
        closed["key"] = ticket.key
        closed["status"] = status
    def add_comment(s, ticket, body):
        commented["key"] = ticket.key
        commented["body"] = body
    def find_open_by_summary(s, summ): return None

backlog_base.make_backlog = lambda app: _CloseBL()
handle_reply_called.clear()

client.post("/api/answer", data={"ticket": "AUTO-11", "app": "automatixy", "text": "close it"})
chk("close: set_status called with Done", closed.get("status") == "Done" and closed.get("key") == "AUTO-11")
chk("close: add_comment mentions Commander",
    "Commander" in (commented.get("body") or "") and commented.get("key") == "AUTO-11")
chk("close: handle_reply NOT called", "called" not in handle_reply_called)


# ─────────────────────────────────────────────────────────────────────────────
# 4. /api/answer → 'defer': no create_task, no set_status, no handle_reply
# ─────────────────────────────────────────────────────────────────────────────
defer_calls = {}

class _DeferBL:
    def create_task(s, *a, **k):
        defer_calls["create_task"] = True
        return None
    def set_status(s, t, st):
        defer_calls["set_status"] = True
    def add_comment(s, t, b): pass
    def find_open_by_summary(s, summ): return None

backlog_base.make_backlog = lambda app: _DeferBL()
handle_reply_called.clear()
defer_calls.clear()

client.post("/api/answer", data={"ticket": "AUTO-12", "app": "automatixy", "text": "defer"})
chk("defer: create_task NOT called", "create_task" not in defer_calls)
chk("defer: set_status NOT called", "set_status" not in defer_calls)
chk("defer: handle_reply NOT called", "called" not in handle_reply_called)


# ─────────────────────────────────────────────────────────────────────────────
# 5. /api/answer → 'clarification': handle_reply called (regression guard)
# ─────────────────────────────name────────────────────────────────────────────
clarif_calls = {}
decisions.handle_reply = lambda c, a, t: (clarif_calls.__setitem__("text", t) or True)

clarif_bl_calls = {}
class _ClarifBL:
    def create_task(s, *a, **k):
        clarif_bl_calls["create_task"] = True
        return None
    def set_status(s, t, st):
        clarif_bl_calls["set_status"] = True
    def add_comment(s, t, b): pass
    def find_open_by_summary(s, summ): return None

backlog_base.make_backlog = lambda app: _ClarifBL()

client.post("/api/answer", data={"ticket": "AUTO-14", "app": "automatixy", "text": "use the 8/5 IA"})
chk("clarification: handle_reply called with TICKET: answer",
    clarif_calls.get("text") == "AUTO-14: use the 8/5 IA")
chk("clarification: create_task NOT called on clarification", "create_task" not in clarif_bl_calls)
chk("clarification: set_status NOT called on clarification", "set_status" not in clarif_bl_calls)


# ─────────────────────────────────────────────────────────────────────────────
# 6. 'clarification' fallback (no pending decision) → comment + unblock (regression)
# ─────────────────────────────────────────────────────────────────────────────
decisions.handle_reply = lambda c, a, t: False
fallback_posted = {}
fallback_unblocked = {}

class _FallbackBL:
    def add_comment(s, ticket, body):
        fallback_posted["key"] = ticket.key
        fallback_posted["body"] = body
    def create_task(s, *a, **k): return None
    def set_status(s, t, st): pass
    def find_open_by_summary(s, summ): return None

backlog_base.make_backlog = lambda app: _FallbackBL()
autopilot.unblock = lambda c, tid: (fallback_unblocked.__setitem__("tid", tid) or "ok")

client.post("/api/answer", data={"ticket": "AUTO-9", "app": "automatixy", "text": "go with option A"})
chk("clarif fallback: answer posted as comment",
    fallback_posted.get("key") == "AUTO-9" and fallback_posted.get("body") == "go with option A")
chk("clarif fallback: ticket unblocked", fallback_unblocked.get("tid") == "AUTO-9")


# ─────────────────────────────────────────────────────────────────────────────
# 7. classify_intent: exact ticket-reported phrase ("open a ticket: <text>")
# ─────────────────────────────────────────────────────────────────────────────
print("\n── exact-phrase regression (ticket EU-82 scenario) ──")

# The original bug: the Commander typed "open a ticket: add retry logic to the
# payment service" and it was routed as a clarification (baked into the parked
# ticket's re-run), which the Builder/Reviewer judged out-of-scope.  The fix must
# classify this as 'file_ticket' so a NEW ticket is filed instead.
ticket_colon_cases = [
    "open a ticket: fix the login bug",
    "open a ticket: add retry logic to the payment service",
    "Please open a ticket: track the race condition we just found",
    "open a ticket for this: DB migration needed",
]
for phrase in ticket_colon_cases:
    chk(f"classify_intent file_ticket (colon form): {phrase!r}",
        classify_intent(phrase) == "file_ticket",
        classify_intent(phrase))

# Clarifications that mention "ticket" in passing must NOT be misclassified.
non_ticket_cases = [
    "the ticket is missing acceptance criteria",  # mentions ticket but not a directive
    "yes, this ticket should use a new DB schema",
]
for phrase in non_ticket_cases:
    chk(f"classify_intent NOT file_ticket (incidental mention): {phrase!r}",
        classify_intent(phrase) != "file_ticket",
        classify_intent(phrase))


# ─────────────────────────────────────────────────────────────────────────────
# 8. _route_out_of_scope is NEVER reached for any directive path (EU-82 core fix)
# ─────────────────────────────────────────────────────────────────────────────
print("\n── _route_out_of_scope isolation ──")

# Patch loop._route_out_of_scope — if any directive path calls it the flag is set.
import orchestrator.loop as _loop_mod
_route_oos_called = {}
_original_route_oos = getattr(_loop_mod, "_route_out_of_scope", None)
_loop_mod._route_out_of_scope = lambda *a, **k: (
    _route_oos_called.__setitem__("called", True) or None)

# Reset handle_reply to a safe stub (not a clarification for these calls).
decisions.handle_reply = lambda c, a, t: True

oos_bl_calls = {}
class _OosBL:
    def create_task(s, *a, **k):
        oos_bl_calls["create_task"] = True
        return "AUTO-OOS"
    def set_status(s, t, st):
        oos_bl_calls["set_status"] = True
    def add_comment(s, t, b): pass
    def find_open_by_summary(s, summ): return None

backlog_base.make_backlog = lambda app: _OosBL()

for directive_text, label in [
    ("open a ticket", "file_ticket"),
    ("close it", "close"),
    ("defer", "defer"),
]:
    _route_oos_called.clear()
    client.post("/api/answer", data={"ticket": "AUTO-20", "app": "automatixy", "text": directive_text})
    chk(f"_route_out_of_scope NOT called for intent={label!r}",
        "called" not in _route_oos_called)

# Restore original if it existed (keep harness clean for any shared state).
if _original_route_oos is not None:
    _loop_mod._route_out_of_scope = _original_route_oos


# ─────────────────────────────────────────────────────────────────────────────
# 9. Input guards: empty text or empty ticket → no action, no crash
# ─────────────────────────────────────────────────────────────────────────────
print("\n── input guard tests ──")

guard_calls = {}
decisions.handle_reply = lambda c, a, t: (guard_calls.__setitem__("handle_reply", True) or True)

class _GuardBL:
    def create_task(s, *a, **k):
        guard_calls["create_task"] = True
        return None
    def set_status(s, t, st):
        guard_calls["set_status"] = True
    def add_comment(s, t, b): pass
    def find_open_by_summary(s, summ): return None

backlog_base.make_backlog = lambda app: _GuardBL()

guard_calls.clear()
r = client.post("/api/answer", data={"ticket": "AUTO-30", "app": "automatixy", "text": ""})
chk("empty text: no action taken", not guard_calls)
chk("empty text: redirect to /needs", r.status_code in (302, 303) and "/needs" in (r.headers.get("Location") or ""))

guard_calls.clear()
r = client.post("/api/answer", data={"ticket": "", "app": "automatixy", "text": "open a ticket"})
chk("empty ticket: no action taken", not guard_calls)
chk("empty ticket: redirect to /needs", r.status_code in (302, 303) and "/needs" in (r.headers.get("Location") or ""))


# ─────────────────────────────────────────────────────────────────────────────
# 10. file_ticket failure path (EU-82 iter-3): the Needs-you row must NOT be cleared
#     until the ticket is actually filed, and the message must report the real outcome
#     (never a fake "Filed"/"Logged").
# ─────────────────────────────────────────────────────────────────────────────
print("\n── file_ticket failure path (row kept on failure) ──")

# (a) backend present but create_task returns None → NOT filed → row kept, honest message
class _NoKeyBL:
    def create_task(s, *a, **k): return None
    def add_comment(s, t, b): pass
    def set_status(s, t, st): pass
    def find_open_by_summary(s, summ): return None

backlog_base.make_backlog = lambda app: _NoKeyBL()
server._state["last_msg"] = ""
client.post("/api/answer", data={"ticket": "AUTO-40", "app": "automatixy", "text": "open a ticket"})
chk("file_ticket none-key: row NOT dismissed",
    "AUTO-40" not in server.D.load_dismissed(cfg.audit_path))
chk("file_ticket none-key: message is not a false Filed/Logged",
    "Filed" not in (server._state.get("last_msg") or "")
    and "Logged" not in (server._state.get("last_msg") or ""),
    server._state.get("last_msg"))

# (b) no backlog backend resolvable (app omitted → appcfg None) → NOT filed → row kept
server._state["last_msg"] = ""
client.post("/api/answer", data={"ticket": "AUTO-41", "app": "", "text": "open a ticket"})
chk("file_ticket no-backend: row NOT dismissed",
    "AUTO-41" not in server.D.load_dismissed(cfg.audit_path))
chk("file_ticket no-backend: message names the missing backlog",
    "no backlog" in (server._state.get("last_msg") or "").lower(),
    server._state.get("last_msg"))

# (c) sanity: the happy path DOES dismiss (AUTO-10 was filed in section 2)
chk("file_ticket success: row dismissed",
    "AUTO-10" in server.D.load_dismissed(cfg.audit_path))


# ─────────────────────────────────────────────────────────────────────────────
# Results
# ─────────────────────────────────────────────────────────────────────────────
print("\n============ EU-82 INTENT ROUTER QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
