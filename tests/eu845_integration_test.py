"""EU-845 — end-to-end integration: cockpit backend-change POSTs are audited source='cockpit',
GET /api/backend/effective returns resolved summary, HTML #effective-backend matches.

Also verifies the CLI path is NOT affected (source == 'cli').

Pattern: mirrors eu717 / eu843 harnesses — tmp-store convention + SDK stubs + Flask
test client. All HTTP is in-process; no network.
"""
import json
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
from orchestrator.backends import NATIVE, GLM  # noqa: E402

# ── shared config / Flask test client ───────────────────────────────────────────
_TMP = Path(tempfile.mkdtemp())
_CFG = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_TMP), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(_TMP / "audit.jsonl"),
    use_worktree=False,
)
_CFG.detected_auth = lambda: "test"

# Stub autopilot PID file like eu717/eu843 do.
try:
    from orchestrator import autopilot as _ap_mod
    _ap_mod._PID_FILE = Path(tempfile.mkdtemp()) / "general-autopilot.pid"
except Exception:  # noqa: BLE001
    pass
server.health.summary = lambda c: {"healthy": True, "checks": []}

# ── Fake sink ────────────────────────────────────────────────────────────────────
class _FakeSink:
    def __init__(self):
        self.records: list[dict] = []

    def record(self, event: str, **fields: object) -> None:
        row = {"ts": "stub", "event": event}
        row.update(fields)
        self.records.append(dict(row))


def _clear_store():
    """Erase the entire persisted store for this harness."""
    backend_pref._file(_CFG).write_text("{}")


def _attach_fake_sink():
    """Attach a fresh fake sink (replaces the module-level _AUDIT_LOG)."""
    backend_pref._AUDIT_LOG = _FakeSink()


def _detach_fake_sink():
    """Detach the fake sink — mutations go through the real path again."""
    backend_pref._AUDIT_LOG = None


def _last_record():
    """Return the last audit record (None if none emitted or empty)."""
    log = backend_pref._AUDIT_LOG
    if not log or not log.records:
        return None
    return log.records[-1]


def _count_emitted():
    log = backend_pref._AUDIT_LOG
    return len(log.records) if log else 0


results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


print("\n=== EU-845 integration tests ===\n")

# =============================================================================
# Setup: Create the Flask app first — this calls configure_source('cockpit').
# Then seed the store (using the real default sink; 'cockpit' tag doesn't matter
# for the seed since we just need a FROM value). Attach fake sink for test mutations.
# =============================================================================
_CLIENT = server.create_app(_CFG).test_client()
assert backend_pref._SOURCE == "cockpit", \
    f"_SOURCE expected 'cockpit' after create_app, got '{backend_pref._SOURCE}'"

_clear_store()
# Seed store with NATIVE — uses default sink (real or None). We don't care where
# the audit row lands; we only need {'active': NATIVE} on disk.
backend_pref.set_active(NATIVE, _CFG)

_attach_fake_sink()                     # from now on, mutations go to FAKE sink
_sink = backend_pref._AUDIT_LOG         # keep reference for inspection
assert _sink is not None and isinstance(_sink, _FakeSink), \
    f"_AUDIT_LOG not a _FakeSink: {type(backend_pref._AUDIT_LOG)}"

# Verify: sink is empty (seed went to real/default path, not the fake)
assert len(_sink.records) == 0, f"sink has records from seed: {_sink.records}"

# =============================================================================
# AC1: POST /api/model (backend=opus) → audit event source='cockpit'
# Use 'opus' (not 'glm') to avoid hitting the GLM connection test.
# =============================================================================
r = _CLIENT.post("/api/model", data={"backend": "opus"}, content_type="multipart/form-data")
chk("(AC1a) POST /api/model returns redirect (302)",
    r.status_code == 302, f"status={r.status_code}")

rec = _last_record()
_chk_detail = f"count={_count_emitted()} rec={rec}"
chk("(AC1b) exactly one model_backend_changed row emitted",
    rec is not None and rec.get("event") == "model_backend_changed" and _count_emitted() == 1,
    _chk_detail)
chk("(AC1c) source == 'cockpit'",
    rec is not None and rec.get("source") == "cockpit",
    f"source={rec.get('source') if rec else 'none'}")
chk("(AC1d) field == 'active', scope == 'global'",
    rec is not None and rec.get("field") == "active" and rec.get("scope") == "global",
    f"field={rec.get('field')} scope={rec.get('scope')}")
chk("(AC1e) from/to reflect the switch (NATIVE→opus)",
    rec is not None and rec.get("from") == NATIVE and rec.get("to") == "opus",
    f"from={rec.get('from')!r} to={rec.get('to')!r}")

# =============================================================================
# AC2: POST /api/model (secondary=..., then mode=...) → all source='cockpit'
# =============================================================================
_attach_fake_sink()
_sink = backend_pref._AUDIT_LOG

r2 = _CLIENT.post("/api/model", data={"secondary": "custom_id"}, content_type="multipart/form-data")
chk("(AC2a) POST /api/model?secondary redirects",
    r2.status_code == 302, f"status={r2.status_code}")
rec2 = _last_record()
chk("(AC2b) secondary: source=='cockpit', field=='secondary'",
    rec2 is not None and rec2.get("source") == "cockpit" and rec2.get("field") == "secondary",
    f"src={rec2.get('source')} field={rec2.get('field')}")

_attach_fake_sink()
_sink = backend_pref._AUDIT_LOG

r3 = _CLIENT.post("/api/model", data={"mode": "hybrid"}, content_type="multipart/form-data")
chk("(AC2c) POST /api/model?mode=hybrid redirects",
    r3.status_code == 302, f"status={r3.status_code}")
rec3 = _last_record()
chk("(AC2d) mode: source=='cockpit', field=='mode'",
    rec3 is not None and rec3.get("source") == "cockpit" and rec3.get("field") == "mode",
    f"src={rec3.get('source')} field={rec3.get('field')}")

# =============================================================================
# AC3: GET /api/backend/effective → 200 + non-empty resolved summary
# =============================================================================
_r = _CLIENT.get("/api/backend/effective")
summary_api = _r.get_json().get("effective", "")
ref_summary = cockpit_views.resolved_backend_summary(_CFG)
chk("(AC3a) GET /api/backend/effective → 200",
    _r.status_code == 200, f"status={_r.status_code}")
chk("(AC3b) response has 'effective' key with non-empty string",
    isinstance(summary_api, str) and len(summary_api) > 0,
    f"value={summary_api!r}")
chk("(AC3c) API summary matches Python helper resolved_backend_summary",
    summary_api == ref_summary,
    f"api={summary_api!r}  helper={ref_summary!r}")

# =============================================================================
# AC4: Rendered HTML contains #effective-backend matching endpoint text
# =============================================================================
body = _CLIENT.get("/").get_data(as_text=True)
chk("(AC4a) rendered page has id='effective-backend' element",
    'id="effective-backend"' in body, "#effective-backend missing from page HTML")

import re
m = re.search(r'id="effective-backend"[^>]*>(.*?)</p>', body, re.DOTALL)
if m:
    inline_text = m.group(1).strip()
    endpoint_data = _CLIENT.get("/api/backend/effective").get_json().get("effective", "")
    chk("(AC4b) inline text matches endpoint's resolved summary",
        inline_text == endpoint_data,
        f"inline={inline_text!r}  endpoint={endpoint_data!r}")
else:
    chk("(AC4b) could extract #effective-backend inner text", False, "regex did not match")

# =============================================================================
# AC5: CLI path remains source='cli' (only create_app sets 'cockpit'; main.py
#      doesn't call configure_source, so its default stays 'cli')
# =============================================================================
_attach_fake_sink()
backend_pref.configure_source("cli")
backend_pref.set_active(GLM, _CFG)
cli_rec = _last_record()
chk("(AC5a) default-path mutation uses source 'cli'",
    cli_rec is not None and cli_rec.get("source") == "cli",
    f"source={cli_rec.get('source') if cli_rec else 'none'}")

# =============================================================================
# Summary
# =============================================================================
passed_n = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("-------------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("RESULT:", "ALL GREEN" if passed_n == len(results) else f"{len(results) - passed_n} FAIL")
sys.exit(0 if passed_n == len(results) else 1)
