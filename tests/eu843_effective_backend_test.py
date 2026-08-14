"""EU-843 — effective-config plain-English display for the cockpit.

Tests the resolved-backend-summary string via both the Python helper
(resolved_backend_summary) and the GET endpoint (/api/backend/effective),
plus that the cockpit HTML contains #effective-backend.

Same hermetic pattern as eu717_inflight_select_test: tmp-store convention
+ SDK stubs + Flask test client. All HTTP is in-process; no network.
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

# Stub autopilot PID file like eu717 does.
try:
    from orchestrator import autopilot as _ap_mod
    _ap_mod._PID_FILE = Path(tempfile.mkdtemp()) / "general-autopilot.pid"
except Exception:  # noqa: BLE001
    pass
server.health.summary = lambda c: {"healthy": True, "checks": []}

_CLIENT = server.create_app(_CFG).test_client()

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


def _clear_store():
    """Erase the entire persisted store for this harness so states don't accumulate."""
    backend_pref._file(_CFG).write_text("{}")


def _get_effective():
    """Convenience: fetch /api/backend/effective and return dict."""
    r = _CLIENT.get("/api/backend/effective")
    code = r.status_code
    data = r.get_json() or {}
    return code, data


# =============================================================================
# AC1: GET /api/backend/effective returns HTTP 200 + non-empty string
# =============================================================================
_clear_store()
code, data = _get_effective()
chk("(AC1a) GET /api/backend/effective returns HTTP 200",
    code == 200, f"status={code}")
chk("(AC1b) response is JSON with an 'effective' key",
    isinstance(data, dict) and "effective" in data,
    f"keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
chk("(AC1c) 'effective' value is a non-empty string",
    isinstance(data.get("effective"), str) and len(data.get("effective") or "") > 0,
    f"value={data.get('effective')!r}")

# =============================================================================
# AC2: Single-backend state (no secondary)
# =============================================================================
_clear_store()
backend_pref.set_active(NATIVE, _CFG)
backend_pref.set_mode("hybrid", _CFG)     # explicit so no stale mode leaks
code, data = _get_effective()
summary = data.get("effective", "")
chk("(AC2) single-backend: summary contains Main model label",
    "Opus (Claude)" in summary, f"='{summary}'")
chk("(AC2) single-backend: summary mentions role coverage",
    any(w in summary.lower() for w in ("plans, reviews and builds", "all roles")),
    f"='{summary}'")

# =============================================================================
# AC3: Hybrid state (Main != Secondary, mode=hybrid)
# =============================================================================
_clear_store()
backend_pref.set_active(NATIVE, _CFG)
backend_pref.set_secondary(GLM, _CFG)
backend_pref.set_mode("hybrid", _CFG)

code, data = _get_effective()
summary_hb = data.get("effective", "")
chk("(AC3a) hybrid: summary contains both model labels",
    "Opus (Claude)" in summary_hb and "GLM (Z.ai)" in summary_hb,
    f"='{summary_hb}'")
chk("(AC3b) hybrid: summary distinguishes roles (main plans/reviews · secondary builds)",
    "plan" in summary_hb.lower() and "build" in summary_hb.lower(),
    f"='{summary_hb}'")

# =============================================================================
# AC4a: Backup state (secondary set, mode=backup)
# =============================================================================
_clear_store()
backend_pref.set_active(NATIVE, _CFG)
backend_pref.set_secondary(GLM, _CFG)
backend_pref.set_mode("backup", _CFG)

code, data = _get_effective()
summary_bp = data.get("effective", "")
chk("(AC4a) backup: summary is non-empty",
    len(summary_bp) > 0, f"='{summary_bp}'")
chk("(AC4a) backup: summary shows Main and Secondary (standby/fallback)",
    "Opus (Claude)" in summary_bp and "GLM (Z.ai)" in summary_bp,
    f"='{summary_bp}'")

# =============================================================================
# AC4b: Per-app-override state (apps non-empty)
# =============================================================================
_clear_store()
backend_pref.set_active(NATIVE, _CFG)
backend_pref.set_active(GLM, _CFG, app_name="automatixy")

code, data = _get_effective()
summary_pa = data.get("effective", "")
chk("(AC4b) per-app-override: summary is non-empty",
    len(summary_pa) > 0, f"='{summary_pa}'")
chk("(AC4b) per-app-override: summary mentions the overridden app",
    "automatixy" in summary_pa, f"='{summary_pa}'")

# Restore global pref (don't pollute other harnesses reading this module).
_clear_store()
backend_pref.set_active(NATIVE, _CFG)
backend_pref.set_mode("hybrid", _CFG)

# =============================================================================
# AC5: Cockpit HTML contains <p id='effective-backend'> with matching text
# =============================================================================
_clear_store()
backend_pref.set_active(NATIVE, _CFG)
BAR = cockpit_views.backend_control(_CFG, "automatixy")

chk("(AC5a) cockpit page has #effective-backend element",
    'id="effective-backend"' in BAR, "#effective-backend not found in backend_control output")

BODY = _CLIENT.get("/").get_data(as_text=True)
chk("(AC5b) rendered page has #effective-backend element",
    'id="effective-backend"' in BODY, "#effective-backend not found in GET / body")

# The text inside the element must match what the endpoint returns.
import re
m = re.search(r'id="effective-backend"[^>]*>(.*?)</p>', BODY, re.DOTALL)
assert m, "Could not extract #effective-backend inner text"
inline_text = m.group(1).strip()

endpoint_data = _CLIENT.get("/api/backend/effective").get_json().get("effective", "")
chk("(AC5c) inline text matches endpoint's resolved summary",
    inline_text == endpoint_data, f"inline={inline_text!r}  endpoint={endpoint_data!r}")

# =============================================================================
# Summary
# =============================================================================
passed_n = sum(1 for _, ok, _ in results if ok)
print("\n========= EU-843 effective backend config tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("-------------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed_n == len(results) else f"{len(results) - passed_n} FAIL")
sys.exit(0 if passed_n == len(results) else 1)
