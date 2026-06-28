"""Tests for tests/two_tenant_smoke.py — two-tenant isolation smoke test (EU-98).

WHY: two_tenant_smoke.py reads env vars at module level and calls sys.exit() when they
are absent, so we seed the required vars before import.  All HTTP calls are stubbed with
unittest.mock so no real network is needed.

Coverage spans every acceptance criterion:
  AC-1: Tenant A authenticates → fetches own businesses → Tenant B cross-tenant check
  AC-2: A 200 (isolation breach) returns exit code 1; 403/404 returns 0
  AC-3: Missing or partial env vars cause an immediate exit(1) — no hardcoded creds

  POSITIVE CONTROL (EU-98 iter 2): Tenant A must read its OWN resource (HTTP 200) before any
  403/404 for Tenant B counts as "isolation confirmed". If A's own detail GET is non-200 (or a
  network error), main() returns 1 even when B got 403/404 — this eliminates the false-green where
  a route that denies everyone would masquerade as isolation. The existing 403/404 → 0 cases pass
  ONLY when A's detail GET is 200.

  Plus regression coverage for: partial env vars, network errors, unexpected status codes,
  empty business lists, malformed auth responses, and 'business_id' field alias.
"""
import os
import sys
import types
import subprocess
import pathlib
import unittest.mock as mock

# ---------------------------------------------------------------------------
# House rule: stub the Agent SDK (all EU harnesses do this)
# ---------------------------------------------------------------------------
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass

    def __call__(s, *a, **k): return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

# ---------------------------------------------------------------------------
# Seed required env vars BEFORE importing the smoke module — it reads them at
# module level and exits 1 if any are absent.
# ---------------------------------------------------------------------------
os.environ.update({
    "TENANT_A_EMAIL":    "a@test.com",
    "TENANT_A_PASSWORD": "passA",
    "TENANT_B_EMAIL":    "b@test.com",
    "TENANT_B_PASSWORD": "passB",
    "DEV_BASE_URL":      "https://dev.example.com",
})
# Remove optional overrides so we exercise the defaults (/api/auth/login, /api/businesses)
os.environ.pop("AUTH_PATH", None)
os.environ.pop("RESOURCE_PATH", None)

import importlib.util as _ilu

_SMOKE_PATH = pathlib.Path(__file__).resolve().parent / "two_tenant_smoke.py"
_spec = _ilu.spec_from_file_location("two_tenant_smoke", _SMOKE_PATH)
smoke = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

import requests as _requests  # available per requirements.txt

# ---------------------------------------------------------------------------
# Check/results helpers (matches the EU harness convention)
# ---------------------------------------------------------------------------
results = []


def check(name: str, condition, detail: str = "") -> None:
    """Record a named assertion."""
    results.append((name, bool(condition), detail))


def _resp(status: int, json_body=None, text: str = ""):
    """Build a minimal requests.Response-like mock."""
    r = mock.MagicMock()
    r.status_code = status
    r.ok = 200 <= status < 300
    r.text = text or str(json_body or "")
    r.json.return_value = json_body if json_body is not None else {}
    return r


# ---------------------------------------------------------------------------
# 1 — _auth_headers
# ---------------------------------------------------------------------------
h = smoke._auth_headers("tok_xyz")
check("_auth_headers: contains Authorization key", "Authorization" in h)
check("_auth_headers: value is Bearer <token>", h["Authorization"] == "Bearer tok_xyz")

# ---------------------------------------------------------------------------
# 2 — _authenticate: happy path returns token string
# ---------------------------------------------------------------------------
with mock.patch("requests.post", return_value=_resp(200, {"access_token": "tok_A"})):
    got_tok = smoke._authenticate("a@test.com", "passA", "Tenant A")
check("_authenticate: happy path returns the access_token", got_tok == "tok_A")

# ---------------------------------------------------------------------------
# 3 — _authenticate: non-2xx response → SystemExit(1)
# ---------------------------------------------------------------------------
with mock.patch("requests.post", return_value=_resp(401, text="Unauthorized")):
    try:
        smoke._authenticate("bad@e.com", "wrong", "X")
        _code = None
    except SystemExit as e:
        _code = e.code
check("_authenticate: 401 response -> SystemExit(1)", _code == 1, f"code={_code}")

# ---------------------------------------------------------------------------
# 4 — _authenticate: access_token field missing → SystemExit(1)
# ---------------------------------------------------------------------------
with mock.patch("requests.post", return_value=_resp(200, {"token": "no_access_key"})):
    try:
        smoke._authenticate("a@test.com", "passA", "Tenant A")
        _code = None
    except SystemExit as e:
        _code = e.code
check("_authenticate: missing access_token field -> SystemExit(1)", _code == 1)

# ---------------------------------------------------------------------------
# 5 — _authenticate: network error → SystemExit(1)
# ---------------------------------------------------------------------------
with mock.patch("requests.post", side_effect=_requests.ConnectionError("unreachable")):
    try:
        smoke._authenticate("a@test.com", "passA", "Tenant A")
        _code = None
    except SystemExit as e:
        _code = e.code
check("_authenticate: network error -> SystemExit(1)", _code == 1)

# ---------------------------------------------------------------------------
# 6 — _get_tenant_business_id: bare list response
# ---------------------------------------------------------------------------
with mock.patch("requests.get", return_value=_resp(200, [{"id": "biz-001"}])):
    biz = smoke._get_tenant_business_id("tok_A", "Tenant A")
check("_get_tenant_business_id: bare list -> first id", biz == "biz-001")

# ---------------------------------------------------------------------------
# 7 — _get_tenant_business_id: {'businesses': [...]} envelope
# ---------------------------------------------------------------------------
with mock.patch("requests.get", return_value=_resp(200, {"businesses": [{"id": "biz-002"}]})):
    biz = smoke._get_tenant_business_id("tok_A", "Tenant A")
check("_get_tenant_business_id: businesses-envelope -> first id", biz == "biz-002")

# ---------------------------------------------------------------------------
# 8 — _get_tenant_business_id: {'data': [...]} envelope with 'business_id' alias
# ---------------------------------------------------------------------------
with mock.patch("requests.get", return_value=_resp(200, {"data": [{"business_id": "biz-003"}]})):
    biz = smoke._get_tenant_business_id("tok_A", "Tenant A")
check("_get_tenant_business_id: data-envelope + business_id alias", biz == "biz-003")

# ---------------------------------------------------------------------------
# 9 — _get_tenant_business_id: empty list → SystemExit(1)
# ---------------------------------------------------------------------------
with mock.patch("requests.get", return_value=_resp(200, [])):
    try:
        smoke._get_tenant_business_id("tok_A", "Tenant A")
        _code = None
    except SystemExit as e:
        _code = e.code
check("_get_tenant_business_id: empty list -> SystemExit(1)", _code == 1)

# ---------------------------------------------------------------------------
# 10 — _get_tenant_business_id: HTTP error → SystemExit(1)
# ---------------------------------------------------------------------------
with mock.patch("requests.get", return_value=_resp(500, text="Internal Server Error")):
    try:
        smoke._get_tenant_business_id("tok_A", "Tenant A")
        _code = None
    except SystemExit as e:
        _code = e.code
check("_get_tenant_business_id: HTTP 500 -> SystemExit(1)", _code == 1)

# ---------------------------------------------------------------------------
# 11 — _get_business_detail: completed request returns the Response (any status)
# ---------------------------------------------------------------------------
with mock.patch("requests.get", return_value=_resp(200, {"id": "biz-001"})):
    _d = smoke._get_business_detail("tok_A", "biz-001", "Tenant A")
check("_get_business_detail: returns the Response on a completed request",
      getattr(_d, "status_code", None) == 200)

# ---------------------------------------------------------------------------
# 12 — _get_business_detail: network error → None (caller decides what that means)
# ---------------------------------------------------------------------------
with mock.patch("requests.get", side_effect=_requests.ConnectionError("down")):
    _d = smoke._get_business_detail("tok_A", "biz-001", "Tenant A")
check("_get_business_detail: network error -> None", _d is None)

# ---------------------------------------------------------------------------
# Helpers for end-to-end main() tests
# ---------------------------------------------------------------------------

def _run_main(cross_tenant_status: int, positive_control_status: int = 200) -> int:
    """Exercise main() end-to-end with configurable detail-route statuses.

    cross_tenant_status     — status Tenant B receives for Tenant A's resource.
    positive_control_status — status Tenant A receives for its OWN resource. Must be 200 for the
                              isolation verdict to be trusted; anything else means the gate is
                              non-functional and main() must return 1 regardless of B's status.

    All requests are stubbed (no network). Dispatch is by URL + bearer token, so the test does NOT
    depend on call ordering:
      * GET RESOURCE_URL (list)                   -> Tenant A's own businesses [{"id": "biz-001"}]
      * GET RESOURCE_URL/<id> with Tenant A token -> positive control -> positive_control_status
      * GET RESOURCE_URL/<id> with Tenant B token -> cross-tenant     -> cross_tenant_status
    """
    def _fake_post(url, json=None, timeout=None):
        # First auth = Tenant A, second = Tenant B. Dispatch by email so order is irrelevant.
        email = (json or {}).get("email")
        tok = "tok_a" if email == os.environ["TENANT_A_EMAIL"] else "tok_b"
        return _resp(200, {"access_token": tok})

    def _fake_get(url, headers=None, timeout=None):
        if url == smoke.RESOURCE_URL:                    # Tenant A lists its own businesses
            return _resp(200, [{"id": "biz-001"}])
        auth = (headers or {}).get("Authorization", "")  # detail route: A = positive control, B = isolation
        if auth == "Bearer tok_a":
            return _resp(positive_control_status, text=f"A-detail={positive_control_status}")
        return _resp(cross_tenant_status, text=f"B-detail={cross_tenant_status}")

    with mock.patch("requests.post", side_effect=_fake_post), \
         mock.patch("requests.get", side_effect=_fake_get):
        return smoke.main()


# ---------------------------------------------------------------------------
# 13 — AC-1 + AC-2: B 403 + A-detail 200 → isolation confirmed (exit 0)
# ---------------------------------------------------------------------------
check("main(): B 403 + A-detail 200 -> 0 (isolation confirmed — AC-1/AC-2)", _run_main(403) == 0)

# ---------------------------------------------------------------------------
# 14 — AC-1 + AC-2: B 404 + A-detail 200 → isolation confirmed (exit 0)
# ---------------------------------------------------------------------------
check("main(): B 404 + A-detail 200 -> 0 (isolation confirmed — AC-1/AC-2)", _run_main(404) == 0)

# ---------------------------------------------------------------------------
# 15 — AC-2: B 200 → isolation BREACH (exit 1, blocks Sentinel green)
# ---------------------------------------------------------------------------
check("main(): B 200 -> 1 (isolation breach, BLOCKS merge — AC-2)", _run_main(200) == 1)

# ---------------------------------------------------------------------------
# 16 — AC-2: unexpected status (500) for B treated conservatively → exit 1
# ---------------------------------------------------------------------------
check("main(): B 500 -> 1 (conservative failure)", _run_main(500) == 1)

# ---------------------------------------------------------------------------
# 17 — POSITIVE CONTROL: A's own detail GET 500 → exit 1 even though B got 403.
#      This is the false-green killer: a non-functional gate must NOT report "isolation confirmed".
# ---------------------------------------------------------------------------
check("main(): A-detail 500 + B 403 -> 1 (positive control failed, gate non-functional)",
      _run_main(403, positive_control_status=500) == 1)

# ---------------------------------------------------------------------------
# 18 — POSITIVE CONTROL: A's own detail GET 403 → exit 1 even though B got 404.
# ---------------------------------------------------------------------------
check("main(): A-detail 403 + B 404 -> 1 (positive control failed, gate non-functional)",
      _run_main(404, positive_control_status=403) == 1)

# ---------------------------------------------------------------------------
# 19 — POSITIVE CONTROL: A's own detail GET 404 → exit 1 (route serves nobody → false-green killed).
# ---------------------------------------------------------------------------
check("main(): A-detail 404 + B 404 -> 1 (false-green eliminated)",
      _run_main(404, positive_control_status=404) == 1)

# ---------------------------------------------------------------------------
# 20 — main(): network error on Tenant B's cross-tenant GET → exit 1 (positive control passed first)
# ---------------------------------------------------------------------------

def _run_main_network_error() -> int:
    """Run main() with a clean positive control (A-detail 200) but a ConnectionError on Tenant B's
    cross-tenant GET, so we exercise the cross-tenant network-error path specifically."""
    def _fake_post(url, json=None, timeout=None):
        email = (json or {}).get("email")
        tok = "tok_a" if email == os.environ["TENANT_A_EMAIL"] else "tok_b"
        return _resp(200, {"access_token": tok})

    def _fake_get(url, headers=None, timeout=None):
        if url == smoke.RESOURCE_URL:
            return _resp(200, [{"id": "biz-001"}])
        auth = (headers or {}).get("Authorization", "")
        if auth == "Bearer tok_a":
            return _resp(200, text="A-detail=200")        # positive control passes
        raise _requests.ConnectionError("network down")   # Tenant B GET fails

    with mock.patch("requests.post", side_effect=_fake_post), \
         mock.patch("requests.get", side_effect=_fake_get):
        return smoke.main()


check("main(): network error on cross-tenant GET -> 1", _run_main_network_error() == 1)

# ---------------------------------------------------------------------------
# 21 — POSITIVE CONTROL: network error on A's own detail GET → exit 1 (gate non-functional)
# ---------------------------------------------------------------------------

def _run_main_positive_control_network_error() -> int:
    """Run main() but raise a ConnectionError on Tenant A's own (positive-control) detail GET —
    we cannot prove the gate works, so main() must fail closed before ever checking Tenant B."""
    def _fake_post(url, json=None, timeout=None):
        email = (json or {}).get("email")
        tok = "tok_a" if email == os.environ["TENANT_A_EMAIL"] else "tok_b"
        return _resp(200, {"access_token": tok})

    def _fake_get(url, headers=None, timeout=None):
        if url == smoke.RESOURCE_URL:
            return _resp(200, [{"id": "biz-001"}])
        raise _requests.ConnectionError("network down")   # A's own detail GET fails

    with mock.patch("requests.post", side_effect=_fake_post), \
         mock.patch("requests.get", side_effect=_fake_get):
        return smoke.main()


check("main(): network error on positive-control GET -> 1 (gate non-functional)",
      _run_main_positive_control_network_error() == 1)

# ---------------------------------------------------------------------------
# 22 — AC-3: missing ALL env vars → script exits 1 with an informative message
# ---------------------------------------------------------------------------
proc = subprocess.run(
    [sys.executable, str(_SMOKE_PATH)],
    env={"PATH": os.environ.get("PATH", "")},   # strip all vars; keep PATH for Python itself
    capture_output=True, text=True,
)
check(
    "AC-3: no env vars -> exit 1 (credentials not hardcoded)",
    proc.returncode == 1,
    f"returncode={proc.returncode}",
)
check(
    "AC-3: error message names a missing variable (not a generic crash)",
    "TENANT_A_EMAIL" in proc.stderr or "environment variable" in proc.stderr.lower(),
    proc.stderr[:300],
)

# ---------------------------------------------------------------------------
# 23 — AC-3: partial env vars → still exits 1 fast (fail-fast on first missing)
# ---------------------------------------------------------------------------
partial = {
    "PATH":            os.environ.get("PATH", ""),
    "TENANT_A_EMAIL":  "a@test.com",
    "TENANT_A_PASSWORD": "passA",
    # TENANT_B_EMAIL, TENANT_B_PASSWORD, DEV_BASE_URL intentionally absent
}
proc2 = subprocess.run(
    [sys.executable, str(_SMOKE_PATH)],
    env=partial,
    capture_output=True, text=True,
)
check(
    "AC-3: partial env vars -> exit 1",
    proc2.returncode == 1,
    f"returncode={proc2.returncode}",
)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("\n======== TWO-TENANT SMOKE TEST QA ========")
_passed = sum(1 for _, ok, _ in results if ok)
for _name, _ok, _det in results:
    _tag = "PASS" if _ok else "FAIL"
    _suffix = f"  ({_det})" if _det and not _ok else ""
    print(f"  [{_tag}] {_name}{_suffix}")
print("------------------------------------------")
print(f"  {_passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if _passed == len(results) else f"{len(results) - _passed} FAIL ❌")
sys.exit(0 if _passed == len(results) else 1)
