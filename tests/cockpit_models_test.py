"""EU-235 — cockpit /models CRUD UI over the model registry (EU-233) + secrets store (EU-234).

Pins the ticket's acceptance criteria against the real Flask app (house SDK-stub + hermetic
tmp-store convention — the Config's tmp audit_path anchors ModelRegistry(cfg)/Secrets(cfg) to a
throwaway state dir, see model_registry.py's module docstring):

  1. GET /models lists every backend — display name, provider label, presence MASK for the key —
     and the raw key NEVER appears in any rendered page (list, edit form, or error re-render).
  2. GET /models/add renders the empty form with all six fields and the provider enum.
  3. POST /models/add: a missing required field or a missing API key re-renders the form with a
     clear error and persists NOTHING; a valid submit stores the raw key in Secrets, persists the
     record with the derived credential_ref=secret://<id>, and redirects to /models.
  4. GET /models/edit/<id> pre-fills the form; the key field is a masked placeholder only.
  5. POST /models/edit/<id>: blank key preserves the stored credential; a new key replaces it;
     an invalid provider re-renders with the registry's own error. Unknown id -> 404.
  6. POST /models/delete/<id> removes the record AND its secret, then redirects to the list.
  7. The POST routes sit behind the EU-254 CSRF/Origin guard (cross-origin POST -> 403) — the
     EU-292 finding on the abandoned WIP branch, landed here instead.
  8. GET / carries a nav link to /models (the EU-293 finding, landed here instead).
"""
import os
import sys
import tempfile
import types
from pathlib import Path

# ── Stub the Agent SDK + requests before any orchestrator import (house convention) ──
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")
# No real `claude -p` auth round-trip when this harness runs standalone (run_all sets this too).
os.environ["GENERAL_AUTH_PROBE"] = "0"

from orchestrator import autopilot as _ap_mod
from orchestrator import server
from orchestrator.config import AppConfig, Config
from orchestrator.model_registry import ModelRegistry
from orchestrator.secrets import Secrets

# GET / renders the control bar, whose autopilot section probes the machine-global PID file —
# point it at a per-harness path so a live daemon can't flip this harness (the 2026-07-06 flake).
_ap_mod._PID_FILE = Path(tempfile.mkdtemp()) / "general-autopilot.pid"
# GET / also calls health.summary (repo/tool probes) — stub it like eu63_tab_bar_test does.
server.health.summary = lambda c: {"healthy": True, "checks": []}

_TMP = Path(tempfile.mkdtemp())
_AUDIT = _TMP / "audit.jsonl"
_AUDIT.write_text("", encoding="utf-8")
_CFG = Config(apps=[AppConfig(name="automatixy", repo_path=str(_TMP / "app"), base_branch="DEV",
                              protected_branch="MAIN", backlog_backend="none")],
              audit_path=str(_AUDIT), use_worktree=False)
_CFG.detected_auth = lambda: "test"

_CLIENT = server.create_app(_CFG).test_client()
_REG = ModelRegistry(_CFG)          # anchors to _TMP/model_registry.json (audit-path parent)
_SEC = Secrets(_CFG)                # anchors to _TMP/secrets.json — same dir set_credential uses

RAW_KEY = "sk-ant-RAW-SECRET-should-never-render-12345"
NEW_KEY = "sk-ant-NEW-SECRET-after-rotation-67890"
FORM = {
    "display_name": "My Claude",
    "provider": "anthropic",
    "base_url": "https://api.anthropic.com",
    "model_id": "claude-opus-4-6",
    "small_fast_model_id": "claude-haiku-4-6",
    "api_key": RAW_KEY,
}
MASK = Secrets.presence_display()   # the fixed 8-bullet mask — the ONLY key representation allowed

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# ============ 1) empty list + empty add form ============ #
resp = _CLIENT.get("/models")
body = resp.get_data(as_text=True)
chk("GET /models: HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
chk("GET /models: page title", "Model backends" in body, body[:300])
chk("GET /models: links to the add form", 'href="/models/add"' in body)
chk("GET /models: empty state before any add", "No model backends yet" in body, body[:600])

resp = _CLIENT.get("/models/add")
body = resp.get_data(as_text=True)
chk("GET /models/add: HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
for field in ("display_name", "provider", "base_url", "model_id", "small_fast_model_id", "api_key"):
    chk(f"GET /models/add: has {field} field", f"name={field}" in body or f'name="{field}"' in body)
chk("GET /models/add: provider enum offers Anthropic-compatible", "Anthropic-compatible" in body)
chk("GET /models/add: provider enum offers OpenAI-compatible", "OpenAI-compatible" in body)
chk("GET /models/add: api_key is a password input", "type=password" in body or 'type="password"' in body)

# ============ 2) add validation: nothing persisted, clear errors ============ #
resp = _CLIENT.post("/models/add", data={**FORM, "display_name": ""})
body = resp.get_data(as_text=True)
chk("POST add w/o display_name: re-renders (200, no redirect)", resp.status_code == 200,
    f"status={resp.status_code}")
chk("POST add w/o display_name: error names the field", "display_name" in body, body[:800])
chk("POST add w/o display_name: nothing persisted", _REG.list() == [], str(_REG.list()))
chk("POST add w/o display_name: submitted values re-filled", 'value="claude-opus-4-6"' in body)

resp = _CLIENT.post("/models/add", data={**FORM, "api_key": ""})
body = resp.get_data(as_text=True)
chk("POST add w/o api_key: re-renders (200, no redirect)", resp.status_code == 200,
    f"status={resp.status_code}")
chk("POST add w/o api_key: error names api_key", "api_key" in body, body[:800])
chk("POST add w/o api_key: nothing persisted", _REG.list() == [] and _SEC.list_refs() == [],
    f"records={_REG.list()} refs={_SEC.list_refs()}")

# ============ 3) valid add: secret via Secrets, record via registry, redirect ============ #
resp = _CLIENT.post("/models/add", data=FORM)
chk("POST /models/add: redirects to /models", resp.status_code == 302
    and resp.headers.get("Location", "").endswith("/models"),
    f"status={resp.status_code} loc={resp.headers.get('Location')}")
recs = _REG.list()
chk("POST /models/add: one record persisted", len(recs) == 1, str(recs))
REC = recs[0] if recs else {}
RID = str(REC.get("id") or "")
chk("POST /models/add: fields round-trip", REC.get("display_name") == "My Claude"
    and REC.get("provider") == "anthropic" and REC.get("model_id") == "claude-opus-4-6")
chk("POST /models/add: credential_ref is the derived secret://<id>",
    REC.get("credential_ref") == f"secret://{RID}", str(REC.get("credential_ref")))
chk("POST /models/add: raw key stored in the Secrets store",
    _SEC.get(f"secret://{RID}") == RAW_KEY)

# ============ 4) list shows the backend, masked ============ #
body = _CLIENT.get("/models").get_data(as_text=True)
chk("list: shows display name", "My Claude" in body, body[:600])
chk("list: shows provider label", "Anthropic-compatible" in body)
chk("list: shows the presence mask for the key", MASK in body)
chk("list: raw key never renders", RAW_KEY not in body)
chk("list: row offers Edit", f'href="/models/edit/{RID}"' in body)
chk("list: row offers Delete", f'action="/models/delete/{RID}"' in body)

# ============ 5) edit form: pre-filled, key masked ============ #
resp = _CLIENT.get(f"/models/edit/{RID}")
body = resp.get_data(as_text=True)
chk("GET edit: HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
chk("GET edit: pre-fills display name", 'value="My Claude"' in body, body[:800])
chk("GET edit: pre-fills base URL", 'value="https://api.anthropic.com"' in body)
chk("GET edit: existing key renders as the mask only", MASK in body and RAW_KEY not in body)
chk("GET edit: unknown id -> 404", _CLIENT.get("/models/edit/no-such-id").status_code == 404)

# ============ 6) edit: blank key preserves the credential; fields update ============ #
edit_form = {k: v for k, v in FORM.items() if k != "api_key"}
resp = _CLIENT.post(f"/models/edit/{RID}", data={**edit_form, "display_name": "Renamed",
                                                 "api_key": ""})
chk("POST edit (blank key): redirects to /models", resp.status_code == 302
    and resp.headers.get("Location", "").endswith("/models"), f"status={resp.status_code}")
after = _REG.get(RID) or {}
chk("POST edit (blank key): field updated", after.get("display_name") == "Renamed", str(after))
chk("POST edit (blank key): stored credential preserved",
    _SEC.get(f"secret://{RID}") == RAW_KEY)

# a new key replaces the stored credential
_CLIENT.post(f"/models/edit/{RID}", data={**edit_form, "api_key": NEW_KEY})
chk("POST edit (new key): credential replaced", _SEC.get(f"secret://{RID}") == NEW_KEY)

# validation errors re-render with the registry's own message; record untouched
resp = _CLIENT.post(f"/models/edit/{RID}", data={**edit_form, "provider": "bogus",
                                                 "api_key": ""})
body = resp.get_data(as_text=True)
chk("POST edit (bad provider): re-renders (200)", resp.status_code == 200,
    f"status={resp.status_code}")
chk("POST edit (bad provider): shows the enum error", "provider" in body, body[:800])
chk("POST edit (bad provider): record untouched", (_REG.get(RID) or {}).get("provider") == "anthropic")
chk("POST edit: unknown id -> 404",
    _CLIENT.post("/models/edit/no-such-id", data={**edit_form, "api_key": ""}).status_code == 404)

# ============ 7) CSRF: the EU-254 guard covers these POSTs ============ #
resp = _CLIENT.post("/models/add", data=FORM, headers={"Origin": "http://evil.example"})
chk("cross-origin POST /models/add -> 403 (EU-254 guard)", resp.status_code == 403,
    f"status={resp.status_code}")
chk("cross-origin POST: nothing persisted", len(_REG.list()) == 1)
resp = _CLIENT.post(f"/models/delete/{RID}", headers={"Origin": "http://evil.example"})
chk("cross-origin POST /models/delete -> 403 (EU-254 guard)", resp.status_code == 403,
    f"status={resp.status_code}")
chk("cross-origin delete: record survives", _REG.get(RID) is not None)

# ============ 8) nav link on the cockpit page ============ #
body = _CLIENT.get("/").get_data(as_text=True)
chk("GET /: nav carries a /models link", 'href="/models"' in body, body[:400])

# ============ 9) delete: record AND secret both removed ============ #
resp = _CLIENT.post(f"/models/delete/{RID}")
chk("POST delete: redirects to /models", resp.status_code == 302
    and resp.headers.get("Location", "").endswith("/models"), f"status={resp.status_code}")
chk("POST delete: record removed", _REG.get(RID) is None and _REG.list() == [])
chk("POST delete: secret removed with it", _SEC.get(f"secret://{RID}") is None)
chk("POST delete: unknown id is a clean no-op redirect",
    _CLIENT.post("/models/delete/no-such-id").status_code == 302)

# ============ Summary (k/n contract run_all.py verifies) ============ #
passed_n = sum(1 for _, ok, _ in results if ok)
print("\n========= EU-235 cockpit /models CRUD tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("-----------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed_n == len(results) else f"{len(results) - passed_n} FAIL")
sys.exit(0 if passed_n == len(results) else 1)
