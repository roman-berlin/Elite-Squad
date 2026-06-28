"""Two-tenant DEV smoke test — tenant isolation verification.

WHY THIS EXISTS
---------------
RLS and per-query tenant filters are defense-in-depth, but they are only as strong as the
boundary they protect.  The signed Security Engineer artifact proves the *diff* is clean; it
cannot prove that a guard present in code is actually evaluated in the correct middleware
position in the live stack — only a live request catches a middleware-ordering error.  This
script proves that boundary holds in a *live* environment: two real Supabase user accounts
(Tenant A and Tenant B) are authenticated against the running DEV backend, and the test asserts
that Tenant B **cannot** read Tenant A's protected resource even when armed with a valid JWT and
the exact resource ID.

A 200 response for the cross-tenant request is an immediate hard failure — it means the
backend is leaking data across tenants, which is a showstopper that must block any merge.

POSITIVE CONTROL (eliminates the false-green)
---------------------------------------------
A 403/404 for Tenant B is only meaningful if the detail route genuinely *serves* Tenant A's
data.  A route that 404s for everyone (wrong path, not deployed on DEV, id-format mismatch)
would otherwise look exactly like "isolation confirmed" — a false green.  So before trusting any
cross-tenant denial, Tenant A must first GET its OWN resource and receive HTTP 200.  If Tenant A
cannot read its own resource, the gate is NON-FUNCTIONAL and the script exits 1 (fail-closed) —
it does NOT report isolation as confirmed.

HOW SENTINEL INVOKES THIS
-------------------------
Sentinel — the SRE post-merge guard (``orchestrator/sentinel.py`` → ``guard()``) — runs each
app's ``postmerge_commands`` on the freshly-landed DEV via ``gate.run_commands()``, which fails
on the FIRST non-zero exit.  A non-zero exit here therefore makes ``sentinel.guard`` treat the
run as RED → it reverts the merge and hands the ticket back, so a failed isolation check blocks
the ticket from going green (AC-2).

Those commands run with ``cwd = the APP's repo`` (e.g. automatixy), NOT this (EU) repo, so the
command MUST reference this script by ABSOLUTE path.  Wire it into ``config.yaml`` under the
automatixy app (see ``config.example.yaml`` for the copy-paste block)::

    postmerge_commands:
      - "/Users/romanberlin/Projects/General/.venv/bin/python /Users/romanberlin/Projects/General/tests/two_tenant_smoke.py"

The EU venv interpreter is named explicitly because this script imports ``requests`` and the app
repo (a Node/Bun project) has no Python venv of its own.

REQUIRED ENV VARS  (injected from the autopilot env — never hardcoded, AC-3)
-----------------
  TENANT_A_EMAIL, TENANT_A_PASSWORD  — credentials for the first tenant account
  TENANT_B_EMAIL, TENANT_B_PASSWORD  — credentials for the second (different) tenant account
  DEV_BASE_URL                        — base URL of the running DEV environment
                                        e.g. https://dev.automatixy.com

OPTIONAL ENV VARS
-----------------
  AUTH_PATH        — path used to obtain a JWT  (default: /api/auth/login)
  RESOURCE_PATH    — path listing the tenant's own businesses  (default: /api/businesses)

EXIT CODES
----------
  0 — isolation confirmed: Tenant A read its OWN resource (200) AND Tenant B received 403/404 for
      that same resource.
  1 — isolation BROKEN (Tenant B received 200), OR the positive control failed (Tenant A could not
      read its own resource — gate non-functional), OR a script error (missing env / auth failed /
      unexpected status). Any exit 1 is RED to sentinel.guard and blocks the ticket going green.
"""

import os
import sys

# ---------------------------------------------------------------------------
# Dependency check — requests is the only non-stdlib import
# ---------------------------------------------------------------------------
try:
    import requests
except ImportError:
    print("ERROR: 'requests' is not installed.  Run: pip install requests", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration — fail fast with a clear message if any var is absent
# ---------------------------------------------------------------------------
REQUIRED_ENV = [
    "TENANT_A_EMAIL",
    "TENANT_A_PASSWORD",
    "TENANT_B_EMAIL",
    "TENANT_B_PASSWORD",
    "DEV_BASE_URL",
]

missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
if missing:
    print(
        "ERROR: The following required environment variables are not set:\n"
        + "\n".join(f"  {k}" for k in missing),
        file=sys.stderr,
    )
    sys.exit(1)

TENANT_A_EMAIL    = os.environ["TENANT_A_EMAIL"]
TENANT_A_PASSWORD = os.environ["TENANT_A_PASSWORD"]
TENANT_B_EMAIL    = os.environ["TENANT_B_EMAIL"]
TENANT_B_PASSWORD = os.environ["TENANT_B_PASSWORD"]
BASE_URL          = os.environ["DEV_BASE_URL"].rstrip("/")

AUTH_PATH     = os.environ.get("AUTH_PATH", "/api/auth/login")
RESOURCE_PATH = os.environ.get("RESOURCE_PATH", "/api/businesses")

AUTH_URL     = BASE_URL + AUTH_PATH
RESOURCE_URL = BASE_URL + RESOURCE_PATH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _authenticate(email: str, password: str, label: str) -> str:
    """POST credentials to the auth endpoint; return the JWT access token.

    Exits with code 1 if the server returns a non-2xx status or if the
    response body does not contain an 'access_token' field — both conditions
    mean we cannot conduct a meaningful isolation check.
    """
    try:
        resp = requests.post(
            AUTH_URL,
            json={"email": email, "password": password},
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"ERROR: Could not reach auth endpoint {AUTH_URL}: {exc}", file=sys.stderr)
        sys.exit(1)

    if not resp.ok:
        print(
            f"ERROR: Authentication failed for {label} "
            f"(status {resp.status_code}): {resp.text[:300]}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        token = resp.json()["access_token"]
    except (ValueError, KeyError):
        print(
            f"ERROR: Auth response for {label} did not contain 'access_token'. "
            f"Body: {resp.text[:300]}",
            file=sys.stderr,
        )
        sys.exit(1)

    return token


def _auth_headers(token: str) -> dict:
    """Return HTTP headers that carry the bearer token."""
    return {"Authorization": f"Bearer {token}"}


def _get_tenant_business_id(token: str, label: str) -> str:
    """Fetch the tenant's own businesses list and return the first business_id.

    Exits with code 1 on HTTP error or an empty / malformed response.
    """
    try:
        resp = requests.get(RESOURCE_URL, headers=_auth_headers(token), timeout=15)
    except requests.RequestException as exc:
        print(
            f"ERROR: Could not reach resource endpoint {RESOURCE_URL}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not resp.ok:
        print(
            f"ERROR: {label} could not fetch own businesses "
            f"(status {resp.status_code}): {resp.text[:300]}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        body = resp.json()
        # Accept both {"businesses": [...]} envelope and bare list
        items = body if isinstance(body, list) else body.get("businesses") or body.get("data") or []
        if not items:
            print(
                f"ERROR: {label} has no businesses — cannot conduct isolation check. "
                "Create at least one business for each test tenant before running this script.",
                file=sys.stderr,
            )
            sys.exit(1)
        business_id = items[0].get("id") or items[0].get("business_id")
    except (ValueError, KeyError, IndexError, AttributeError) as exc:
        print(
            f"ERROR: Could not extract business_id from {label}'s response: {exc}. "
            f"Body: {resp.text[:300]}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not business_id:
        print(
            f"ERROR: First business in {label}'s list has no 'id' or 'business_id' field. "
            f"Body: {resp.text[:300]}",
            file=sys.stderr,
        )
        sys.exit(1)

    return str(business_id)


def _get_business_detail(token: str, business_id: str, label: str):
    """GET the single-business *detail* route (``RESOURCE_PATH/<business_id>``) as ``label``.

    Returns the ``requests.Response`` for any completed request (so the caller can inspect the
    status), or ``None`` when the request could not be made at all (network error) — the caller
    decides what a network error means for it.
    """
    url = f"{RESOURCE_URL}/{business_id}"
    try:
        return requests.get(url, headers=_auth_headers(token), timeout=15)
    except requests.RequestException as exc:
        print(f"ERROR: {label} → GET {url} failed with a network error: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Main isolation check
# ---------------------------------------------------------------------------

def main() -> int:
    """Run the two-tenant isolation check; return exit code (0 = pass, 1 = fail)."""
    print("=== Two-Tenant DEV Smoke Test ===")
    print(f"  Base URL     : {BASE_URL}")
    print(f"  Auth path    : {AUTH_PATH}")
    print(f"  Resource path: {RESOURCE_PATH}")
    print()

    # Step 1 — authenticate as Tenant A
    print("[1/5] Authenticating as Tenant A …")
    token_a = _authenticate(TENANT_A_EMAIL, TENANT_A_PASSWORD, "Tenant A")
    print("      OK — Tenant A authenticated.")

    # Step 2 — discover Tenant A's business_id
    print("[2/5] Fetching Tenant A's businesses …")
    business_id_a = _get_tenant_business_id(token_a, "Tenant A")
    print(f"      OK — Tenant A's business_id: {business_id_a}")

    target_url = f"{RESOURCE_URL}/{business_id_a}"

    # Step 3 — POSITIVE CONTROL: Tenant A must be able to read its OWN resource (HTTP 200).
    # Without this, a 403/404 for Tenant B proves nothing — a detail route that denies/404s for
    # everyone (wrong path, not deployed, id-format mismatch) is a FALSE GREEN. Only once we've
    # proven the route genuinely serves A's data do we trust a cross-tenant denial below.
    print(f"[3/5] Positive control — Tenant A → GET {target_url}")
    resp_a = _get_business_detail(token_a, business_id_a, "Tenant A")
    if resp_a is None:
        # Could not even complete A's own request — we cannot prove the gate works → fail closed.
        print(
            "ERROR ❌  Positive control could not complete (network error) — the gate is "
            "NON-FUNCTIONAL. Failing closed.",
            file=sys.stderr,
        )
        return 1
    print(f"      Response status: {resp_a.status_code}")
    if resp_a.status_code != 200:
        print()
        print(
            f"ERROR ❌  Positive control FAILED: Tenant A received HTTP {resp_a.status_code} (not 200) "
            f"for its OWN resource (business_id={business_id_a}).\n"
            "  The detail route is not serving A's data, so a 403/404 for Tenant B would prove "
            "nothing — this gate is NON-FUNCTIONAL, not passing. Failing closed.\n"
            f"  Response body: {resp_a.text[:300]}",
            file=sys.stderr,
        )
        return 1
    print("      OK — Tenant A can read its own resource (the gate is live).")

    # Step 4 — authenticate as Tenant B (same process, different credentials)
    print("[4/5] Authenticating as Tenant B …")
    token_b = _authenticate(TENANT_B_EMAIL, TENANT_B_PASSWORD, "Tenant B")
    print("      OK — Tenant B authenticated.")

    # Step 5 — Tenant B attempts to access Tenant A's resource
    print(f"[5/5] Tenant B → GET {target_url}")
    resp_b = _get_business_detail(token_b, business_id_a, "Tenant B")
    if resp_b is None:
        # A network error is not an isolation failure, but we cannot confirm isolation either.
        return 1

    status = resp_b.status_code
    print(f"      Response status: {status}")

    if status == 200:
        print()
        print(
            "FAILURE ❌  Tenant isolation BROKEN: Tenant B received HTTP 200 when fetching\n"
            f"  Tenant A's resource (business_id={business_id_a}).\n"
            "  This is a critical data-leak — the DEV merge must be investigated immediately.",
            file=sys.stderr,
        )
        return 1

    if status in (403, 404):
        print()
        print(
            f"PASS ✅  Tenant isolation confirmed: Tenant A reads its own resource (200) while "
            f"Tenant B receives HTTP {status} (access denied) for that same resource."
        )
        return 0

    # Unexpected status (e.g. 500) — treat conservatively as a failure so CI is not silently green.
    print()
    print(
        f"WARNING ⚠️  Unexpected HTTP status {status} from cross-tenant request.\n"
        "  Expected 403 or 404 to confirm isolation, or 200 to signal a breach.\n"
        f"  Response body: {resp_b.text[:400]}\n"
        "  Treating as FAILURE — investigate before declaring isolation safe.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
