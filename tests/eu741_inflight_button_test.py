"""EU-741 — shared in-flight submit helper for the three blocking action BUTTONS.

Pins the ticket's ACs against the real Flask app (house SDK-stub + hermetic tmp-store
convention — same shape as eu717_inflight_select_test / eu719_submit_feedback_test):

  1. A SINGLE shared helper (``window.euInflightBtn``) is emitted once via ``_wrap`` and used
     by all three buttons — no copy-pasted per-button JS. Each button declares its busy label
     via ``data-eu-inflight-btn="<text>"`` and the bootstrap reads that attribute, so the HTML
     is the source of truth for which button gets which label.
  2. Submit-event binding (NOT click): the lock engages on the form's ``submit`` event, so it
     only fires once native constraint-validation has PASSED — an invalid Add-backend form
     (missing required field) never locks. A ``click`` binding would prematurely lock on an
     invalid submit attempt and strand the button disabled.
  3. Disable / relabel / restore cycle: while the POST is in flight the button is disabled
     (``disabled=true``) + relabelled to its '⏳ …' busy text, then restored
     (``disabled=false`` + original label). The form is still a native POST + 302 so the page
     reloads on completion; the 10s restore + bfcache ``pageshow`` hook are safety nets so a
     button can never lock up if navigation is blocked or the user returns via back-forward
     cache.
  4. Double-submit guard: a second submit while one is already in flight is a no-op
     (``if(btn.disabled||locked)return``), so no duplicate POST can fire while one is running.
  5. The three buttons carry the attribute with their AC labels:
       'Develop selected' → '⏳ developing…'
       'Sync with Jira'   → '⏳ syncing…'
       'Add backend'      → '⏳ adding…'  (and '⏳ saving…' on the Edit form's 'Save changes')
  6. Native POST fallback + behaviour unchanged: POST /api/run-selected (no tickets),
     /api/needs-sync, and /models/add still answer 302 — the success/failure flow is untouched
     (the ticket only adds client-side feedback; no handler changed).

All HTTP is the in-process Flask test client — no network anywhere in this harness.
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
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import cockpit_views, models_views, needs as _needs_mod, needs_sync, server  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

# ── shared config / Flask test client ───────────────────────────────────────────
_TMP = Path(tempfile.mkdtemp())
_CFG = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_TMP), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(_TMP / "audit.jsonl"),
    use_worktree=False,
)
_CFG.detected_auth = lambda: "test"

# GET /tickets renders intake.from_drain(...); /needs renders needs.summary(...) + spawns a
# throttled background needs_sync.reconcile; POST /models/add runs backends.classify_model_tier
# (the three slow calls the ticket names). Stub them hermetically — no Jira / no LLM / no drain.
_T = lambda i, s: types.SimpleNamespace(id=i, summary=s)
_A = lambda n: types.SimpleNamespace(name=n)
server.intake.from_drain = lambda c, name, lim: [(_A("automatixy"), _T("AUTO-1", "do a thing"))]
_needs_mod.summary = lambda cfg, app_name=None: {"total": 0, "rows": []}
needs_sync.reconcile = lambda *a, **k: {"checked": 0, "cleared": [], "unknown": []}
server.health.summary = lambda c: {"healthy": True, "checks": []}
_orig_classify = server.backends.classify_model_tier
server.backends.classify_model_tier = lambda *a, **k: "mid"

_CLIENT = server.create_app(_CFG).test_client()

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


HELPER = cockpit_views._INFLIGHT_BUTTON_HELPER
WRAP = cockpit_views._wrap("t", "x")

# =============================================================================
# (1) a SINGLE shared helper is emitted once via _wrap and used by all three buttons
# =============================================================================
chk("(1a) the shared window.euInflightBtn helper is defined",
    "window.euInflightBtn=function" in HELPER)
chk("(1b) _wrap injects the helper on every page",
    "window.euInflightBtn=function" in WRAP)
chk("(1c) the helper is emitted EXACTLY ONCE per _wrap page (not per-button)",
    WRAP.count("window.euInflightBtn=function") == 1,
    f"count={WRAP.count('window.euInflightBtn=function')}")
chk("(1d) the bootstrap wires every button through the ONE helper (attribute-driven)",
    "querySelectorAll('button[data-eu-inflight-btn]')" in HELPER
    and "window.euInflightBtn(nodes[i]" in HELPER)

# =============================================================================
# (2) submit-event binding (NOT click) — invalid forms must never lock
# =============================================================================
chk("(2a) the lock engages on the form's SUBMIT event (fires only after validation passes)",
    "form.addEventListener('submit',lock)" in HELPER)
chk("(2b) the helper does NOT bind click (a click handler would lock an invalid submit attempt)",
    "addEventListener('click'" not in HELPER,
    "a click listener would disable the button before native validation could block the submit")

# =============================================================================
# (3) the disable / relabel / restore cycle
# =============================================================================
chk("(3a) the button is disabled while the request is in flight", "btn.disabled=true" in HELPER)
chk("(3b) the busy label is written to the button while in flight", "btn.innerHTML=label" in HELPER)
chk("(3c) the original label is saved before the swap (so it can come back)", "prevHTML=btn.innerHTML" in HELPER)
chk("(3d) the button is restored (re-enabled)", "btn.disabled=false" in HELPER)
chk("(3e) the original label is restored", "btn.innerHTML=prevHTML" in HELPER)
chk("(3f) a 10s safety-net restore prevents a locked button if navigation is blocked",
    "setTimeout(restore,10000)" in HELPER)
chk("(3g) a bfcache pageshow hook restores the button if the user navigates back",
    "pageshow" in HELPER and "e.persisted" in HELPER)
chk("(3h) aria-busy is set while in flight (a11y busy state)", "aria-busy" in HELPER)

# =============================================================================
# (4) double-submit guard — no duplicate POST while one is in flight
# =============================================================================
chk("(4a) a guard makes a second submit a no-op while already in flight",
    "if(btn.disabled||locked)return" in HELPER)

# =============================================================================
# (5) the three buttons carry the attribute with their AC labels
# =============================================================================
TICKETS = _CLIENT.get("/tickets?app=automatixy").get_data(as_text=True)
NEEDS = _CLIENT.get("/needs").get_data(as_text=True)
ADD_FORM = _CLIENT.get("/models/add").get_data(as_text=True)
EDIT_FORM = models_views.render_model_form(_CFG, record={"id": "x", "provider": "anthropic"})

chk("(5a) /tickets: the helper ships on the page", "window.euInflightBtn=function" in TICKETS)
chk("(5b) /tickets: 'Develop selected' button opts in with '⏳ developing…'",
    'data-eu-inflight-btn="⏳ developing…"' in TICKETS and "Develop selected" in TICKETS,
    "devbtn must carry the attribute alongside its existing id/disabled")
chk("(5c) /needs: the helper ships on the page", "window.euInflightBtn=function" in NEEDS)
chk("(5d) /needs: 'Sync with Jira' button opts in with '⏳ syncing…'",
    "data-eu-inflight-btn='⏳ syncing…'" in NEEDS and "Sync with Jira" in NEEDS)
chk("(5e) /models/add: the helper ships on the page", "window.euInflightBtn=function" in ADD_FORM)
chk("(5f) /models/add: 'Add backend' button opts in with '⏳ adding…'",
    'data-eu-inflight-btn="⏳ adding…"' in ADD_FORM and ">Add backend<" in ADD_FORM)
chk("(5g) /models/edit: 'Save changes' button opts in with '⏳ saving…'",
    'data-eu-inflight-btn="⏳ saving…"' in EDIT_FORM and ">Save changes<" in EDIT_FORM)

# =============================================================================
# (6) native POST fallback + behaviour unchanged — the three endpoints still answer 302
#     (the ticket only adds client-side feedback; no server handler changed)
# =============================================================================
r_run = _CLIENT.post("/api/run-selected", data={"app": "automatixy"})   # no tickets → 302, no build
chk("(6a) POST /api/run-selected still answers 302 (native form / no-JS fallback intact)",
    r_run.status_code == 302, f"status={r_run.status_code}")

r_sync = _CLIENT.post("/api/needs-sync")
chk("(6b) POST /api/needs-sync still answers 302 (full synchronous duration unchanged)",
    r_sync.status_code in (301, 302, 303), f"status={r_sync.status_code}")

r_add = _CLIENT.post("/models/add", data={
    "display_name": "T", "provider": "anthropic", "base_url": "https://a",
    "model_id": "m", "api_key": "sk-x"})   # tier blank → classify_model_tier (stubbed)
chk("(6c) POST /models/add still answers 302 (classify_model_tier path unchanged)",
    r_add.status_code in (301, 302, 303), f"status={r_add.status_code}")

# restore the patched module attr so a later harness importing backends sees the real fn
server.backends.classify_model_tier = _orig_classify

# ============ Summary (k/n contract run_all.py verifies) ============ #
passed_n = sum(1 for _, ok, _ in results if ok)
print("\n========= EU-741 in-flight button helper tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("-------------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed_n == len(results) else f"{len(results) - passed_n} FAIL")
sys.exit(0 if passed_n == len(results) else 1)
