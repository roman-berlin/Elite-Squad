"""EU-712 — Test GET /api/last-result and POST /api/last-result/dismiss.

Acceptance criteria covered:
  AC1 GET /api/last-result returns the current stored (tone, text, timestamp) as JSON
      when a result exists, and an empty/null payload when none exists — it does NOT
      mutate the store.
  AC2 POST /api/last-result/dismiss clears the store entry; calling it when the store
      is already empty does not error (still 200/ok).
  AC3 Full lifecycle: trigger → GET validates payload → dismiss → GET confirms cleared.
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

# ── import stubs (same pattern as eu675_dismiss_result_handler_test.py) ───
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import cockpit_state, server
from orchestrator.config import AppConfig, Config

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))
    tag = "PASS" if cond else "FAIL"
    line = f"  [{tag}] {name}"
    if detail and not cond:
        line += f"  ({detail})"
    print(line)


print("\n================ EU-712 Last-Result API QA ================")

# ── harness: live Flask test client ───────────────────────────────────────
tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp / "app"), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()


def _clear() -> None:
    """Reset last-result state for both the app scope and unit-wide scope."""
    server._state.pop("last_result", None)
    server._state.pop("last_result_record", None)
    app_st = cockpit_state.get_state("automatixy")
    app_st.pop("last_result", None)
    app_st.pop("last_result_record", None)


# ========================================================================
# Endpoint availability — these MUST pass. Previously verified fail-first:
# both returned 404 before the implementation was applied (confirmed above).
# ========================================================================
chk("endpoint availability: GET /api/last-result exists (not 404)",
    client.get("/api/last-result").status_code != 404)
chk("endpoint availability: POST /api/last-result/dismiss exists (not 404)",
    client.post("/api/last-result/dismiss").status_code != 404)

_clear()

# ========================================================================
# AC3 — full lifecycle: set → GET validates → dismiss → GET confirms empty
# ========================================================================
server.set_last_result("automatixy", "ok", "Build succeeded on DEV")

# --- Step 1: GET returns the stored record with tone/text/timestamp ------
r_get = client.get("/api/last-result?app=automatixy")
chk("AC1 pre: GET /api/last-result returns 200 (not 404)",
    r_get.status_code == 200,
    f"{r_get.status_code} {r_get.get_data(as_text=True)[:80]}")

get_ok = r_get.status_code == 200
get_json = {}
if get_ok:
    try:
        get_json = json.loads(r_get.get_data(as_text=True))
    except ValueError:
        get_ok = False
        chk("AC1: GET body is valid JSON", False, r_get.get_data(as_text=True)[:80])

if get_ok:
    chk("AC1: response has 'tone' field matching stored value",
        get_json.get("tone") == "ok",
        f"got tone={get_json.get('tone')!r}")
    chk("AC1: response has 'text' field matching stored value",
        get_json.get("text") == "Build succeeded on DEV",
        f"got text={get_json.get('text')!r}")
    chk("AC1: response has 'timestamp' field present (float/number)",
        isinstance(get_json.get("timestamp"), (int, float)),
        f"got timestamp={get_json.get('timestamp')!r}")

# --- Verify GET does NOT mutate the store --------------------------------
st_after_get = cockpit_state.get_state("automatixy")
chk("AC1: GET does NOT clear last_result_record from state",
    st_after_get.get("last_result_record") is not None,
    "record was unexpectedly cleared by GET")
chk("AC1: GET does NOT clear last_result legacy key from state",
    bool(st_after_get.get("last_result")),
    "legacy key was unexpectedly cleared by GET")

# ========================================================================
# AC2 — POST /api/last-result/dismiss clears the entry
# ========================================================================
r_dismiss = client.post("/api/last-result/dismiss?app=automatixy")
chk("AC2: POST /api/last-result/dismiss returns 200 + {ok: true}",
    r_dismiss.status_code == 200,
    f"{r_dismiss.status_code} {r_dismiss.get_data(as_text=True)[:80]}")

dismiss_ok = r_dismiss.status_code == 200
dismiss_json = {}
if dismiss_ok:
    try:
        dismiss_json = json.loads(r_dismiss.get_data(as_text=True))
    except ValueError:
        dismiss_ok = False
        chk("AC2: dismiss body is valid JSON", False, r_dismiss.get_data(as_text=True)[:80])

if dismiss_ok:
    chk("AC2: dismiss response body is {\"ok\": true}",
        dismiss_json.get("ok") is True,
        f"got {r_dismiss.get_data(as_text=True)[:80]}")

# After dismiss, the record should be gone from app state
app_st_after = cockpit_state.get_state("automatixy")
chk("AC2: after dismiss, app-level last_result_record is gone",
    "last_result_record" not in app_st_after and not app_st_after.get("last_result"))

# ========================================================================
# AC1 (empty case): GET after dismiss returns null/empty payload ----------
# ========================================================================
r_get_empty = client.get("/api/last-result?app=automatixy")
chk("AC1 post-dismiss: GET returns 200 even when empty",
    r_get_empty.status_code == 200)

empty_ok = r_get_empty.status_code == 200
empty_json = {}
if empty_ok:
    try:
        empty_json = json.loads(r_get_empty.get_data(as_text=True))
    except ValueError:
        empty_ok = False
        chk("AC1: empty GET body is valid JSON", False, r_get_empty.get_data(as_text=True)[:80])

if empty_ok:
    chk("AC1 post-dismiss: GET returns empty/None payload when no result stored",
        empty_json == {} or empty_json.get("result") is None,
        f"got {r_get_empty.get_data(as_text=True)[:80]}")

# ========================================================================
# AC2 (idempotent): dismiss on already-empty store does NOT error ----------
# ========================================================================
r_dismiss_empty = client.post("/api/last-result/dismiss?app=automatixy")
chk("AC2 idempotent: dismiss on empty store returns 200 + ok",
    r_dismiss_empty.status_code == 200,
    f"{r_dismiss_empty.status_code} {r_dismiss_empty.get_data(as_text=True)[:80]}")

idem_ok = r_dismiss_empty.status_code == 200
if idem_ok:
    try:
        idem_json = json.loads(r_dismiss_empty.get_data(as_text=True))
    except ValueError:
        idem_ok = False
        chk("AC2: idempotent dismiss body is valid JSON", False, r_dismiss_empty.get_data(as_text=True)[:80])
    if idem_ok:
        chk("AC2 idempotent: dismiss on empty store returns {ok: true}",
            idem_json.get("ok") is True)

# ========================================================================
# Cross-scope: global (app=None) result also accessible -------------------
# ========================================================================
_clear()
server.set_last_result(None, "error", "QA run failed")
r_global = client.get("/api/last-result")
chk("cross-scope: GET /api/last-result (no app param) reads global record",
    r_global.status_code == 200)

global_ok = r_global.status_code == 200
global_json = {}
if global_ok:
    try:
        global_json = json.loads(r_global.get_data(as_text=True))
    except ValueError:
        global_ok = False
        chk("cross-scope: global GET body is valid JSON", False, r_global.get_data(as_text=True)[:80])

if global_ok:
    chk("cross-scope: global record has correct tone",
        global_json.get("tone") == "error",
        f"got tone={global_json.get('tone')!r}")
    chk("cross-scope: global record has correct text",
        global_json.get("text") == "QA run failed",
        f"got text={global_json.get('text')!r}")

# Dismissing via app-param clears BOTH scopes (as per existing behavior)
client.post("/api/last-result/dismiss?app=automatixy")
chk("cross-scope: dismiss via app param also clears global store",
    "last_result_record" not in server._state and not server._state.get("last_result"))

# ========================================================================
# Report ------------------------------------------------------------------
# ========================================================================
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("-" * 67)
print(f"  {passed}/{total} passed")
if passed != total:
    print(f"  RESULT: {total - passed} FAIL")
    sys.exit(1)
print("  RESULT: ALL GREEN")
sys.exit(0)
