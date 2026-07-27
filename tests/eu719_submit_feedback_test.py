"""EU-719 — fetch-submit feedback for Needs-you buttons and pinned decision-card replies.

Covers the ticket's testable acceptance criteria:
  1. Shared helper: ``_wrap`` injects ONE ``window.euPost(form)`` helper used by both
     surfaces — it disables every button in the form and relabels the primary one
     '⏳ <busy>…' while the POST is in flight, POSTs via fetch with ``redirect:'manual'``
     (the endpoints answer 302), treats opaqueredirect/r.ok as success, and on failure
     re-enables the buttons with their original labels before re-throwing.
  2. Needs-you option/answer buttons: every ``form[action="/api/answer"]`` on /needs gets
     its submit intercepted (preventDefault) and routed through ``euPost``; success reloads
     (the one-shot banner + cleared row render on the fresh page), failure surfaces an
     inline ``.nerr`` instead of silently reloading.
  3. Pinned decision-card ``.preply`` forms: the /chat page intercepts ``.preply`` submits
     via fetch (delegated on document, so the 5s poll's card swaps can't orphan it); on
     success the card body is replaced inline with '✓ sent'; on failure the button is
     re-enabled and an inline ``.perr`` surfaces the error. The poll (refreshChat) drops
     answered cards (preplySent) so a slow background resolve can't put a stale pending
     form back over the '✓ sent' state.
  4. No-JS fallback unchanged: the forms are still native method=post forms in the markup.
  5. Card *content* is untouched (EU-538 scope): the card header/question copy still renders.
"""
from __future__ import annotations

import re
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

import orchestrator.cockpit_views as V
from orchestrator import decisions, needs, server
from orchestrator.config import AppConfig, Config

# ── shared config / Flask test client ───────────────────────────────────────────
_TMP = Path(tempfile.mkdtemp())
_CFG = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_TMP), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(_TMP / "audit.jsonl"),
    use_worktree=False,
)
_CFG.detected_auth = lambda: "test"

# One structured pending decision so BOTH surfaces render their reply widgets:
# the chat page's pinned card (_chat_inner → decisions.load) and the Needs-you
# decision card (/needs → needs.summary).
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
needs.clear_cache()

_APP = server.create_app(_CFG)
_CLIENT = _APP.test_client()

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# =============================================================================
# (1) the shared helper — injected by _wrap, used by both surfaces
# =============================================================================
wrap = V._wrap("Probe", "<p>x</p>")
chk("_wrap injects the shared window.euPost helper", "window.euPost" in wrap)
chk("helper disables the form buttons while in flight",
    "b.disabled=true" in wrap, wrap[-1200:])
chk("helper relabels the primary button '⏳ …ing' while in flight",
    "⏳" in wrap and "Sending" in wrap, wrap[-1200:])
chk("helper POSTs via fetch with redirect:'manual' (endpoint answers 302)",
    "new FormData(form)" in wrap and "redirect:'manual'" in wrap, wrap[-1200:])
chk("helper treats the server redirect as success (opaqueredirect)",
    "opaqueredirect" in wrap and "r.ok" in wrap, wrap[-1200:])
chk("helper re-enables + restores the buttons on error, then re-throws",
    "p[0].disabled=false" in wrap and "p[0].innerHTML=p[1]" in wrap
    and "throw e" in wrap, wrap[-1200:])

# =============================================================================
# (2) Needs-you option/answer buttons — fetch-submit via the shared helper
# =============================================================================
needs_resp = _CLIENT.get("/needs")
nbody = needs_resp.get_data(as_text=True)
chk("GET /needs: HTTP 200 with the decision card", needs_resp.status_code == 200
    and "AUTO-77" in nbody, f"status={needs_resp.status_code}")

script_m = re.search(r"form\[action=.{0,14}/api/answer.*?</script>", nbody, re.S)
needs_script = script_m.group(0) if script_m else ""
chk("/needs page script binds every /api/answer form (options + answer box)",
    'querySelectorAll(\'form[action="/api/answer"]\')' in nbody, nbody[-2500:])
chk("/needs submit is intercepted (no native POST reload mid-call)",
    "preventDefault" in needs_script, needs_script)
chk("/needs submit goes through the shared euPost helper",
    "window.euPost(f)" in needs_script, needs_script)
chk("/needs reloads only AFTER the submit lands (banner + cleared row render)",
    "location.reload()" in needs_script, needs_script)
chk("/needs failure surfaces an inline .nerr instead of silently reloading",
    "className='nerr'" in nbody and "showErr(f)" in needs_script and ".nerr{" in nbody,
    needs_script)
chk("/needs empty free-text answer is not shipped",
    "!ti.value.trim()" in needs_script, needs_script)
chk("/needs forms stay native POSTs as the no-JS fallback",
    "method=post action=/api/answer" in nbody)

# =============================================================================
# (3) Pinned decision-card .preply forms — fetch, '✓ sent', error-reenable
# =============================================================================
chat_resp = _CLIENT.get("/chat")
cbody = chat_resp.get_data(as_text=True)
chk("GET /chat: HTTP 200 with the pinned decision card",
    chat_resp.status_code == 200 and "AUTO-77" in cbody,
    f"status={chat_resp.status_code}")
chk("pinned card keys its ticket via data-tid for the submit/poll path",
    'class=pcard data-tid="AUTO-77"' in cbody, cbody[:3000])
chk(".preply submit is intercepted on document (survives the poll's card swaps)",
    'document.addEventListener("submit"' in cbody
    and 'classList.contains("preply")' in cbody)
chk(".preply submit goes through the shared euPost helper (fetch, not native POST)",
    bool(re.search(r'contains\("preply"\).*?window\.euPost\(f\)', cbody, re.S)))
chk("on success the card body is replaced inline with '✓ sent'",
    'innerHTML="<div class=psent>✓ sent</div>"' in cbody, cbody[-3000:])
chk("'✓ sent' cards get a visible style", ".psent{" in cbody)
chk("answered tids are recorded so the poll can't resurrect a stale pending form",
    "preplySent[tid]=1" in cbody and "var preplySent={}" in cbody)
chk("the poll drops already-answered cards from the fetched fragment",
    'querySelectorAll(".pcard[data-tid]")' in cbody
    and "preplySent[c.getAttribute(" in cbody)
chk("on failure an inline .perr surfaces the error (button re-enabled by euPost)",
    '"perr"' in cbody and ".perr{" in cbody and "retry" in cbody)
chk(".preply forms stay native POSTs as the no-JS fallback",
    "class=preply method=post action=/api/chat" in cbody)

# =============================================================================
# (4) endpoints still answer the way the helper + fallback expect
# =============================================================================
# Hermetic: swallow the background CTO-reply path (mirrors eu307_chat_send_test) so the
# POST exercises only the endpoint's redirect contract the client helper relies on.
_orig_route = decisions.route_message
decisions.route_message = lambda c, a, text: True
r = _CLIENT.post("/api/chat", data={"ticket": "AUTO-77", "text": "use Redis"})
decisions.route_message = _orig_route
chk("/api/chat still answers with a redirect (opaqueredirect == success client-side)",
    r.status_code in (301, 302, 303, 307, 308), f"status={r.status_code}")
r = _CLIENT.post("/api/answer", data={"ticket": "", "text": ""})
chk("/api/answer still answers with a redirect",
    r.status_code in (301, 302, 303, 307, 308), f"status={r.status_code}")

# =============================================================================
# (5) card CONTENT untouched — EU-538 owns layout/copy; this is submit-state only
# =============================================================================
chk("decision card copy unchanged (header + question still render)",
    "the unit needs your call" in cbody and "pick the cache layer" in cbody)
chk("EU-305 pending-swap guard still intact (unchanged-blocks aren't rewritten)",
    "oldPending.outerHTML!==newPending.outerHTML" in cbody)

# =============================================================================
# Summary
# =============================================================================
passed_n = sum(1 for _, ok, _ in results if ok)
print("\n========= EU-719 fetch-submit feedback tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("--------------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
