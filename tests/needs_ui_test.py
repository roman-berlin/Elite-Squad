"""Needs-you UX QA: each 'runs that need you' card expands to the full problem detail, and
'Discuss with the General' pre-loads a brief of that problem into the chat composer."""
import sys, types, tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import dashboard as D
from orchestrator.config import Config, AppConfig
from orchestrator import server, needs as _needs

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

TASK = {
    "ticket_id": "AUTO-14", "outcome": "errored", "note": "builder halted — precondition/blocker",
    "passes_list": [{
        "n": 1,
        "build_summary": "Tried to add gate cost to pass-count; axes already failed inside the Inspector.",
        "review_summary": "Cannot verify — precondition broken.", "verdict": "FAIL",
        "required_changes": ["Restore the Inspector a11y axe baseline first"],
        "issues": [{"severity": "high", "area": "precondition", "detail": "axe-core failing before the change"}],
    }],
}

# --- 1. needs_detail_html: the full 'what went wrong' ---
detail = D.needs_detail_html(TASK)
chk("detail: leads with the brief ask", "The ask:" in detail and "precondition/blocker" in detail)
chk("detail: builder summary shown", "Builder · pass 1" in detail and "gate cost" in detail)
chk("detail: reviewer verdict shown", "verdict FAIL" in detail and "precondition broken" in detail)
chk("detail: reviewer findings listed", "Restore the Inspector" in detail and "axe-core failing" in detail)
chk("detail: empty run falls back gracefully",
    "No further detail" in D.needs_detail_html({"ticket_id": "X", "outcome": "errored", "passes_list": []}))

# --- 2. needs_chat_summary: the pre-loaded brief ---
summ = D.needs_chat_summary(TASK)
chk("summary: names ticket + outcome", summ.startswith("AUTO-14 ended 'errored'"))
chk("summary: includes builder + reviewer", "Builder:" in summ and "Reviewer (FAIL):" in summ)
chk("summary: includes open items", "Open items:" in summ and "Restore the Inspector" in summ)
chk("summary: ends with an ask", summ.endswith("What do you want me to do?"))
chk("summary: collapsed to one line (no newlines)", "\n" not in summ)
chk("summary: bounded length", len(summ) < 1100, str(len(summ)))

# --- 3. integration: the real /needs + /chat routes ---
tmp = Path(tempfile.mkdtemp())
repo = tmp / "app"; repo.mkdir()
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(repo), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
_needs.summary = lambda c, a=None: {"decisions": [], "approvals": [], "tasks": [TASK], "total": 1}

client = server.create_app(cfg).test_client()
r = client.get("/needs"); body = r.get_data(as_text=True)
chk("/needs returns 200", r.status_code == 200, str(r.status_code))
chk("/needs has an expand disclosure (arrow)", "<details>" in body and "<summary>" in body)
chk("/needs expand reveals the problem detail", "The ask:" in body and "gate cost" in body)
chk("/needs 'Discuss' carries a prefill link", "/chat?prefill=" in body)
chk("/needs prefill is URL-encoded (no raw spaces)", "prefill=AUTO-14%20" in body)
chk("/needs still has Dismiss", "Dismiss" in body and "/api/dismiss" in body)

r2 = client.get("/chat?prefill=AUTO-14%20ended%20here%20%E2%80%94%20what%20now")
body2 = r2.get_data(as_text=True)
chk("/chat returns 200", r2.status_code == 200, str(r2.status_code))
chk("/chat pre-fills the composer value", 'value="AUTO-14 ended here ' in body2)

r3 = client.get("/chat"); body3 = r3.get_data(as_text=True)
chk("/chat with no prefill -> empty value (unchanged behaviour)", 'value=""' in body3)

# --- 4. EU-307: composer — Enter-to-send, refocus, autoscroll (no native page reload) ---
chk("/chat composer intercepts submit (no native reload)",
    'addEventListener("submit"' in body3 and "preventDefault()" in body3)
chk("/chat composer posts to /api/chat via fetch", 'fetch("/api/chat"' in body3)
chk("/chat composer re-fetches /api/chat-thread into #cinner on success",
    "/api/chat-thread" in body3 and 'getElementById("cinner")' in body3)
chk("/chat composer refocuses the input after send", ".focus()" in body3)
chk("/chat composer scrolls to newest after send",
    body3.count("window.scrollTo(0,document.body.scrollHeight)") >= 2)
chk("/chat composer still has a real form (no-JS fallback / EU-286a independence)",
    "<form" in body3 and "method=post action=/api/chat>" in body3)

r4 = client.get("/group"); body4 = r4.get_data(as_text=True)
chk("/group returns 200", r4.status_code == 200, str(r4.status_code))
chk("/group composer intercepts submit (no native reload)",
    'addEventListener("submit"' in body4 and "preventDefault()" in body4)
chk("/group composer posts to /api/group via fetch", 'fetch("/api/group"' in body4)
chk("/group composer re-fetches /api/group-thread into #ginner on success",
    "/api/group-thread" in body4 and 'getElementById("ginner")' in body4)
chk("/group composer refocuses the input after send", ".focus()" in body4)
chk("/group composer scrolls to newest after send",
    body4.count("window.scrollTo(0,document.body.scrollHeight)") >= 2)
chk("/group composer still has a real form (no-JS fallback)",
    "<form" in body4 and "method=post action=/api/group>" in body4)

print("\n=============== NEEDS-YOU UX QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
