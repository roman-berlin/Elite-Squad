"""EU-675 — Wire the dismiss-button click handler in the board's inline JS (+ its endpoint).

The strip itself is EU-670 (covered by eu670_result_strip_test.py). This ticket adds:
  1. POST /api/dismiss-result — pops the pending result from server state (it did NOT exist
     before this ticket, despite the ticket text assuming it did).
  2. A delegated document-level click listener in warroom's inline <script> that calls the
     endpoint and removes the strip from the DOM immediately on success.

Acceptance criteria covered:
  AC1 Clicking dismiss removes the strip within the same interaction — the JS removes the
      strip div in the fetch callback (asserted statically: handler targets
      [data-dismiss-result] via .closest and calls .remove(), no tick/setInterval wait).
  AC2 After POST /api/dismiss-result, a subsequent GET /api/board shows no strip/result —
      the server-side clear happened (live Flask test-client round-trip).
  AC3 Handler is delegated — registered on document with closest(), so strips rendered
      after initial page load (every SSE frame / applyBoard swap) are covered too.
  AC4 No regression to applyBoard/tick — both remain in the page script, untouched.
  AC5 Iteration-1 gate failure stays fixed: dismiss_result_api carries its -> Response
      annotation (EU-465 typing sweep requires one on every public def).
"""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import types
from pathlib import Path

# ── import stubs (same pattern as result_banner_test.py) ──────────────────────
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


print("\n================ EU-675 Dismiss-Handler QA ================")

# ── harness: live Flask test client ───────────────────────────────────────────
tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp / "app"), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()

MSG = "✓ EU-675 shipped DEV→MAIN"


def _clear() -> None:
    server._state.pop("last_result", None)
    server._state.pop("last_result_record", None)
    app_st = cockpit_state.get_state("automatixy")
    app_st.pop("last_result", None)
    app_st.pop("last_result_record", None)


# --- AC2: live round-trip — strip present → dismiss → strip gone --------------
# The board renders from the TAB's per-project state, and production writers store the
# result under the app name (set_last_result(app_name, ...) — answer/directive/run-start),
# so seed through the real writer and dismiss with the tab's app, exactly like the JS does.
_clear()
server.set_last_result("automatixy", "ok", MSG)
b1 = client.get("/api/board?app=automatixy").get_data(as_text=True)
chk("AC2 pre: /api/board shows the strip before dismiss",
    "data-dismiss-result" in b1 and MSG in b1)

r = client.post("/api/dismiss-result?app=automatixy")
body = r.get_data(as_text=True)
chk("AC2: POST /api/dismiss-result?app=… → 200 + {ok: true}",
    r.status_code == 200 and json.loads(body) == {"ok": True}, f"{r.status_code} {body[:80]}")
chk("AC2: the tab's per-project state is cleared (record AND legacy key)",
    "last_result_record" not in cockpit_state.get_state("automatixy")
    and not cockpit_state.get_state("automatixy").get("last_result"))

b2 = client.get("/api/board?app=automatixy").get_data(as_text=True)
chk("AC2: subsequent GET /api/board shows NO strip/result",
    "data-dismiss-result" not in b2 and MSG not in b2)

# --- writers that store globally (standup/council/QA) are cleared too ---------
_clear()
server.set_last_result(None, "ok", MSG)          # app=None → unit-wide _state
client.post("/api/dismiss-result?app=automatixy")
chk("dismiss also clears globally-stored results",
    "last_result_record" not in server._state and not server._state.get("last_result"))

# --- endpoint is idempotent (double-click before the DOM removal lands) -------
r2 = client.post("/api/dismiss-result?app=automatixy")
chk("endpoint idempotent on empty state",
    r2.status_code == 200 and json.loads(r2.get_data(as_text=True)).get("ok") is True)

# --- dismiss must not disturb unrelated sticky state --------------------------
_clear()
server._state["last_msg"] = "council note"
client.post("/api/dismiss-result?app=automatixy")
chk("dismiss leaves sticky last_msg untouched", server._state.get("last_msg") == "council note")
server._state.pop("last_msg", None)
_clear()

# --- AC1/AC3/AC4: the inline JS wiring (static assertions on the served page) -
page = client.get("/").get_data(as_text=True)
chk("AC3: click listener delegated on document",
    'document.addEventListener("click"' in page)
chk("AC3: matches strips via closest([data-dismiss-result])",
    'closest("[data-dismiss-result]")' in page)
chk("AC1: handler calls the dismiss endpoint with the tab's app",
    'fetch("/api/dismiss-result?app="+encodeURIComponent(APP),{method:"POST"})' in page)
chk("AC1: strip removed directly in the success callback (.remove(), no tick wait)",
    ".remove()" in page)
chk("AC3: no silent swallow — failure path logs to console",
    "console.warn" in page and "strip left in place" in page)
chk("AC4: applyBoard/tick rendering logic still present",
    "function applyBoard" in page and "async function tick" in page)

# --- AC5: the iteration-1 gate failure (EU-465 typing sweep) stays fixed ------
tree = ast.parse((Path(__file__).resolve().parent.parent / "orchestrator" / "server.py")
                 .read_text(encoding="utf-8"))
_fn = next((n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "dismiss_result_api"), None)
_ret = ast.unparse(_fn.returns) if _fn is not None and _fn.returns is not None else ""
chk("AC5: dismiss_result_api carries its -> Response annotation (iter-1 sweep failure)",
    _ret == "Response", _ret or "no annotation")

# --- Report --------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("-----------------------------------------------------------")
print(f"  {passed}/{total} passed")
if passed != total:
    print(f"  RESULT: {total - passed} FAIL")
    sys.exit(1)
print("  RESULT: ALL GREEN")
sys.exit(0)
