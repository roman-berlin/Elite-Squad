"""EU-717 — shared in-flight submit helper for the cockpit model/secondary/mode selects.

Pins the ticket's acceptance criteria against the real Flask app (house SDK-stub + hermetic
tmp-store convention — same shape as eu719_submit_feedback_test / hybrid_mode_test):

  1. A SINGLE shared helper (``window.euInflightSelect``) is emitted once and used by all
     three selects — no copy-pasted per-control JS. Each select declares its intent via a
     ``data-eu-inflight=<key>`` attribute that the bootstrap reads, so the HTML is the source
     of truth for which control gets which busy-label resolver.
  2. Deferred (keyboard-safe) submit: the selects no longer carry
     ``onchange="this.form.submit()"`` (which POSTed on EVERY arrow-key browse and could
     switch the model before the user committed). The helper binds ``blur`` and ``Enter`` as
     the only commit triggers and does NOT submit on ``change``.
  3. Disable/relabel/restore cycle: while the POST is in flight the control is disabled
     (``disabled=true``) and its selected option is relabelled to a '⏳ …ing' state, then
     restored (``disabled=false`` + original label) — the page reloads on the 302, and the
     restore callback is a safety net so the control can never lock up.
  4. GLM-specific label: the model-select resolver returns 'testing connection…' when the
     picked value is ``glm`` (that pick runs a live connection test server-side,
     server.py ``model_api`` → ``backends.glm_test_connection``); the generic controls stay
     on the '⏳ switching…' busy label.
  5. The forms are still native ``method=post`` (no-JS fallback): POST /api/model still
     answers 302.

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

from orchestrator import backend_pref, cockpit_views, server  # noqa: E402
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

# GET / renders the control bar, whose autopilot section probes the machine-global PID file —
# point it at a per-harness path so a live daemon can't flip this harness (the 2026-07-06 flake).
try:
    from orchestrator import autopilot as _ap_mod
    _ap_mod._PID_FILE = Path(tempfile.mkdtemp()) / "general-autopilot.pid"
except Exception:  # noqa: BLE001
    pass
# GET / also calls health.summary (repo/tool probes) — stub it like eu63_tab_bar_test does.
server.health.summary = lambda c: {"healthy": True, "checks": []}

_CLIENT = server.create_app(_CFG).test_client()

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# A Secondary (+ hybrid mode) must be set so the Mode select renders alongside the Main +
# Secondary selects — i.e. ALL THREE wired selects are present in the bar.
backend_pref.set_secondary("glm", _CFG)
backend_pref.set_mode("hybrid", _CFG)
BAR = cockpit_views.backend_control(_CFG, "automatixy")

# =============================================================================
# (1) a SINGLE shared helper is emitted once and used by all three selects
# =============================================================================
chk("(1a) the shared window.euInflightSelect helper is defined",
    "window.euInflightSelect=function" in BAR)
chk("(1b) the helper is emitted EXACTLY ONCE (not copy-pasted per select)",
    BAR.count("window.euInflightSelect=function") == 1,
    f"count={BAR.count('window.euInflightSelect=function')}")
chk("(1c) the bootstrap wires every select through the ONE helper (data-attribute driven)",
    "querySelectorAll('select[data-eu-inflight]')" in BAR
    and "window.euInflightSelect(n,fn)" in BAR)

# =============================================================================
# (2) deferred (keyboard-safe) submit — no POST on every change
# =============================================================================
chk("(2a) no select keeps onchange=\"this.form.submit()\"",
    'onchange="this.form.submit()"' not in BAR, "the old arrow-key POST path must be gone")
chk("(2b) the helper binds blur as a commit trigger",
    "addEventListener('blur'" in BAR)
chk("(2c) the helper binds Enter as a commit trigger",
    "e.key==='Enter'" in BAR)
chk("(2d) the helper does NOT bind change (so arrow-key browsing never submits)",
    "addEventListener('change'" not in BAR,
    "a change listener would let keyboard-browsing fire a POST")
chk("(2e) blur commits only when the value actually changed since focus",
    "sel.value!==initial" in BAR)

# =============================================================================
# (3) the disable / relabel / restore cycle
# =============================================================================
chk("(3a) the control is disabled while the request is in flight", "sel.disabled=true" in BAR)
chk("(3b) the selected option is relabelled to the busy text while in flight",
    "opt.text=busyLabel" in BAR)
chk("(3c) the control is restored (re-enabled)", "sel.disabled=false" in BAR)
chk("(3d) the original option label is restored", "opt.text=prevText" in BAR)
chk("(3e) a guard prevents double-submit while already in flight", "if(sel.disabled)return" in BAR)

# =============================================================================
# (4) GLM-specific label vs the generic '⏳ …ing'
# =============================================================================
chk("(4a) the model resolver special-cases the glm pick", "v==='glm'" in BAR)
chk("(4b) the glm pick shows 'testing connection…' (it runs a live connection test)",
    "testing connection" in BAR)
chk("(4c) the generic controls show the '⏳ …ing' busy glyph", "⏳" in BAR and "switching" in BAR)

# =============================================================================
# (5) all three selects are wired via the shared helper (data-eu-inflight=<key>)
# =============================================================================
chk("(5a) the Main-model select is wired (data-eu-inflight=\"model\")",
    'data-eu-inflight="model"' in BAR)
chk("(5b) the Secondary select is wired (data-eu-inflight=\"secondary\")",
    'data-eu-inflight="secondary"' in BAR)
chk("(5c) the Mode select is wired (data-eu-inflight=\"mode\")",
    'data-eu-inflight="mode"' in BAR)
chk("(5d) the Mode select still appears only WITH a secondary set",
    "name=mode" in BAR)
backend_pref.set_secondary(None, _CFG)
chk("(5e) …and disappears when there is no secondary (behaviour preserved)",
    "name=mode" not in cockpit_views.backend_control(_CFG, "automatixy"))
backend_pref.set_secondary("glm", _CFG)   # restore for the GET / checks below

# =============================================================================
# (6) end-to-end on the rendered cockpit page + the native POST still works
# =============================================================================
BODY = _CLIENT.get("/").get_data(as_text=True)
chk("(6a) GET /: the shared helper is present on the rendered page",
    "window.euInflightSelect" in BODY)
chk("(6b) GET /: no onchange=submit remains anywhere on the page",
    'onchange="this.form.submit()"' not in BODY)
chk("(6c) GET /: all three selects are wired",
    'data-eu-inflight="model"' in BODY
    and 'data-eu-inflight="secondary"' in BODY
    and 'data-eu-inflight="mode"' in BODY)

resp = _CLIENT.post("/api/model", data={"mode": "backup"})
chk("(6d) POST /api/model still answers 302 (native form / no-JS fallback intact)",
    resp.status_code == 302, f"status={resp.status_code}")

# =============================================================================
# (7) iter2 review guard: the committed value must still reach /api/model.
#     iter1 disabled the <select> BEFORE form.submit() — and a disabled select is
#     EXCLUDED from the POST body, so backend=/secondary=/mode= shipped EMPTY and
#     the handler saw no value (a Secondary/Mode pick looked like a no-op; a GLM pick
#     skipped the connection-test branch). The helper must now mirror sel.value into
#     a hidden <input name=sel.name> BEFORE the disable. Guarded TWICE: statically on
#     the helper source (the regression is client-side JS, so a server POST can't see
#     it directly), then behaviourally that the value arrives and routes correctly.
# =============================================================================
HELPER = cockpit_views._INFLIGHT_SELECT_HELPER
chk("(7a) the committed value is mirrored into a hidden input of the select's name+value",
    "mirror.name=sel.name" in HELPER and "mirror.value=sel.value" in HELPER)
chk("(7b) the mirror is created BEFORE the select is disabled "
    "(disable-first drops the value-bearing control from the POST)",
    "mirror.name=sel.name" in HELPER and "sel.disabled=true" in HELPER
    and HELPER.index("mirror.name=sel.name") < HELPER.index("sel.disabled=true"))
chk("(7c) the mirror is removed on restore (no stale duplicate name after a blocked nav)",
    "removeChild(mirror)" in HELPER)

# ── behavioural: the committed value arrives at /api/model and routes correctly ──
# Clean baseline: Main=opus, no secondary, mode=hybrid, no alert.
backend_pref.set_active("opus", _CFG)
backend_pref.set_secondary(None, _CFG)
backend_pref.set_mode("hybrid", _CFG)
server._state.pop("model_alert", None)

# (7d/7e/7f) GLM pick reaches the bk==GLM branch and runs the connection test (success
# path): stub glm_test_connection -> (True,'ok') so active flips to glm + 'connection OK'.
# If the value had shipped empty, resolve_selection('') -> opus and the GLM branch would
# NEVER run — active would stay opus with no connection message. That is exactly the
# disable-before-submit regression this block exists to catch at the server boundary.
_orig_glm_test = server.backends.glm_test_connection
server.backends.glm_test_connection = lambda timeout=8.0: (True, "ok")
try:
    resp_glm = _CLIENT.post("/api/model", data={"backend": "glm"})
    _active_after_glm = backend_pref.active(_CFG)
    _msg_after_glm = (server._state.get("last_msg") or "")
finally:
    server.backends.glm_test_connection = _orig_glm_test
chk("(7d) POST backend=glm reaches the GLM branch: active flips to glm",
    _active_after_glm == server.backends.GLM, f"active={_active_after_glm!r}")
chk("(7e) …and the connection-test success path ran ('connection OK' confirmation)",
    "connection ok" in _msg_after_glm.lower(), f"last_msg={_msg_after_glm!r}")
chk("(7f) POST /api/model for a GLM pick still answers 302",
    resp_glm.status_code == 302, f"status={resp_glm.status_code}")

# (7g/7h) GLM pick with a FAILING connection test sets the alert and leaves Main unchanged
#         (the fail branch returns early, before set_active — so active stays glm from 7d).
server._state.pop("model_alert", None)
server.backends.glm_test_connection = lambda timeout=8.0: (False, "no token")
try:
    _CLIENT.post("/api/model", data={"backend": "glm"})
finally:
    server.backends.glm_test_connection = _orig_glm_test
_alert = server._state.get("model_alert") or ""
chk("(7g) GLM pick with a failing connection test sets model_alert "
    "(the bk==GLM connection-test branch ran)",
    "GLM" in _alert and "not enabled" in _alert.lower(), f"model_alert={_alert!r}")
chk("(7h) …and leaves the Main model unchanged (active still glm from 7d)",
    backend_pref.active(_CFG) == server.backends.GLM,
    f"active={backend_pref.active(_CFG)!r}")

# (7i/7j) a Secondary pick does NOT alter the Main model — the two selects are independent.
_main_before = backend_pref.active(_CFG)
resp_sec = _CLIENT.post("/api/model", data={"secondary": "opus"})
chk("(7i) POST secondary=opus sets the Secondary (value reached the handler)",
    backend_pref.get_secondary(_CFG) == "opus",
    f"secondary={backend_pref.get_secondary(_CFG)!r}")
chk("(7j) …and does NOT alter the Main model (Secondary ≠ Main)",
    backend_pref.active(_CFG) == _main_before,
    f"active={backend_pref.active(_CFG)!r} (was {_main_before!r})")
chk("(7k) POST /api/model for a Secondary pick still answers 302",
    resp_sec.status_code == 302, f"status={resp_sec.status_code}")

# (7l/7m) a Mode pick does NOT alter the Main model either.
_main_before = backend_pref.active(_CFG)
resp_mode = _CLIENT.post("/api/model", data={"mode": "backup"})
chk("(7l) POST mode=backup sets the Mode (value reached the handler)",
    backend_pref.get_mode(_CFG) == "backup", f"mode={backend_pref.get_mode(_CFG)!r}")
chk("(7m) …and does NOT alter the Main model",
    backend_pref.active(_CFG) == _main_before,
    f"active={backend_pref.active(_CFG)!r} (was {_main_before!r})")

# ============ Summary (k/n contract run_all.py verifies) ============ #
passed_n = sum(1 for _, ok, _ in results if ok)
print("\n========= EU-717 in-flight select helper tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("-------------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed_n == len(results) else f"{len(results) - passed_n} FAIL")
sys.exit(0 if passed_n == len(results) else 1)
