"""EU-67: /needs/resolve dismisses a pending decision without re-running the ticket.

Regression: decision-type rows had no dismiss button — the Commander could not remove
a stale or irrelevant question from the Needs-you list without shipping an answer and
triggering a full re-run."""
import sys, types, json, tempfile
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

from orchestrator import server, decisions, needs
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- setup ---
tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()

# Seed one pending decision on disk so resolve() has something to pop.
pending = [{"id": "AUTO-32", "app": "automatixy", "question": "Use tabs or spaces?",
            "summary": "code style decision", "description": "", "acceptance": [], "ephemeral": True}]
(tmp / "pending_decisions.json").write_text(json.dumps(pending))

# --- 1: Dismiss button is rendered on decision cards ---
from orchestrator import dashboard as _dashboard
_dashboard.needs_detail_html = lambda t: "detail"
_dashboard.needs_chat_summary = lambda t: "summary"
_dashboard._short = lambda s, n=120: (s or "")[:n]
needs.summary = lambda c: {
    "total": 1,
    "decisions": [{"id": "AUTO-32", "app": "automatixy", "question": "Use tabs or spaces?"}],
    "approvals": [], "proposals": [], "tasks": [],
}
body = client.get("/needs").get_data(as_text=True)
chk("decision card has a Dismiss button", "Dismiss" in body)
chk("Dismiss form posts to /needs/resolve", "action=/needs/resolve" in body)
chk("Dismiss form carries the ticket id", "name=ticket" in body and "AUTO-32" in body)
chk("Ship answer form is still present alongside Dismiss", "Ship answer" in body)

# --- 2: POST /needs/resolve pops the decision from pending_decisions.json ---
resolve_called = {}
_orig_resolve = decisions.resolve
def _fake_resolve(c, answer, ticket_id=None, **kw):
    resolve_called["ticket_id"] = ticket_id
    resolve_called["answer"] = answer
    resolve_called["comment"] = kw.get("comment", True)
    return {"id": ticket_id}
decisions.resolve = _fake_resolve

r = client.post("/needs/resolve", data={"ticket": "AUTO-32"})
chk("POST /needs/resolve -> 302 redirect", r.status_code == 302)
chk("redirect lands on /needs", r.headers.get("Location", "").endswith("/needs"))
chk("resolve called with correct ticket_id", resolve_called.get("ticket_id") == "AUTO-32")
chk("resolve called with comment=False (no echo-back)", resolve_called.get("comment") is False)
chk("answer is the dismiss sentinel (not an empty string)", bool(resolve_called.get("answer")))
chk("last_msg confirms dismissal", "dismissed" in server._state.get("last_msg", "").lower()
    and "AUTO-32" in server._state.get("last_msg", ""))

# --- 3: Confirmation banner shows once on /needs then clears ---
server._state["last_msg"] = "✓ Decision for AUTO-32 dismissed — removed from Needs you."
body2 = client.get("/needs").get_data(as_text=True)
chk("banner shows after dismiss", "nbanner" in body2 and "AUTO-32" in body2 and "dismissed" in body2.lower())
chk("banner is one-shot (cleared after render)", not server._state.get("last_msg"))

# --- 4: POST without a ticket id is a graceful no-op ---
resolve_called.clear()
r2 = client.post("/needs/resolve", data={"ticket": ""})
chk("empty ticket -> 302 (no crash)", r2.status_code == 302)
chk("empty ticket -> resolve never called", "ticket_id" not in resolve_called)

# --- 5: /api/answer (Ship answer) still works — regression guard ---
answer_called = {}
decisions.handle_reply = lambda c, a, text: (answer_called.__setitem__("text", text) or True)
client.post("/api/answer", data={"ticket": "AUTO-32", "app": "automatixy", "text": "use spaces"})
chk("Ship answer still calls handle_reply (regression guard)", answer_called.get("text") == "AUTO-32: use spaces")

decisions.resolve = _orig_resolve   # restore

print("\n============ EU-67 DECISION DISMISS QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
