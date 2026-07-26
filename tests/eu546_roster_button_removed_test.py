"""EU-643 regression guard: /roster-doc fully unlinked from cockpit UI, route dormant.

Coordination note for rebrand tickets (EU-532 / EU-534):
  The roster nav-button strings were permanently removed as of EU-642/EU-643
  (2026-07-26). There is nothing to rename — these rebrand tickets should skip
  any roster-button copy entirely. If a rebrand lands first, EU-643 still
  removes the button regardless of its new label.

What EU-643 changed (beyond EU-642's nav-row button removal):
  - orchestrator/models_views.py: deleted the dead injection-by-anchor splice
    (_NAV_ANCHOR + add_models_nav_link) — its anchor left the nav row in EU-642
    and the '/models' link renders directly in backend_control now — which also
    removed the LAST stale '/roster-doc' literal outside the route definition.
  - orchestrator/server.py index(): dropped the now-no-op splice call.

Guard coverage:
  A. Rendered HTML: GET /, _control_bar(), GET /models and GET /models/add all
     contain zero '/roster-doc' — fails if any future commit re-links the page
     from any cockpit surface (nav row, dropdown, models pages).
  B. Dormant route: GET /roster-doc → HTTP 200 serving the real roster page
     (proves "dormant, not deleted").
  C. Source sweep: the ONLY 'roster-doc' occurrence anywhere under
     orchestrator/**/*.py is the server.py route decorator — codifies the
     ticket AC (`grep -rn "roster-doc" orchestrator/` shows only the route).

Scope note: orchestrator/roster.py and the ./general roster CLI are the
maintenance surface per parent AC (3) and stay untouched (no diff in that
file); roster_test.py + eu68_roster_nav_test.py pin their behaviour.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

# --- stub the Agent SDK so orchestrator modules import cleanly (no network) ---
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


_sdk.__getattr__ = lambda _n: _D
sys.modules["claude_agent_sdk"] = _sdk
sys.path.insert(0, ".")
# No real `claude -p` auth round-trip when this harness runs standalone (run_all sets this too).
os.environ["GENERAL_AUTH_PROBE"] = "0"

from orchestrator import autopilot as _ap_mod
from orchestrator import cockpit_views, server
from orchestrator import sync as _sync
from orchestrator.config import AppConfig, Config

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail) if not cond else ""))


# ============================================================================
# Fixtures (house convention — hermetic Config, SDK stub above, no network)
# ============================================================================
_repo = Path(tempfile.mkdtemp()) / "app"
_repo.mkdir()
cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_repo),
                    base_branch="DEV", protected_branch="MAIN",
                    backlog_backend="none")],
    audit_path=str(_repo / "audit.jsonl"),
    use_worktree=False,
)
cfg.detected_auth = lambda: "test"   # skip auth middleware
_sync.can_promote = lambda: False    # avoid git/network calls in the view layer
# GET / renders the control bar, whose autopilot section probes the machine-global PID file —
# point it at a per-harness path so a live daemon can't flip this harness (the 2026-07-06 flake).
_ap_mod._PID_FILE = Path(tempfile.mkdtemp()) / "general-autopilot.pid"
# GET / also calls health.summary (repo/tool probes) — stub it like cockpit_models_test does.
server.health.summary = lambda c: {"healthy": True, "checks": []}

client = server.create_app(cfg).test_client()

# ============================================================================
# A. Rendered HTML guard: no '/roster-doc' in any cockpit-rendered output
# ============================================================================
resp = client.get("/")
index_body = resp.get_data(as_text=True)

chk("A.1: GET / HTML contains no '/roster-doc'",
    resp.status_code == 200 and "/roster-doc" not in index_body,
    f"status={resp.status_code} hits={index_body.count('/roster-doc')}")

bar = cockpit_views._control_bar(cfg, "automatixy")
chk("A.2: _control_bar output contains no '/roster-doc'",
    "/roster-doc" not in bar,
    f"found {bar.count('/roster-doc')} time(s) in the control bar")

for _path in ("/models", "/models/add"):
    _body = client.get(_path).get_data(as_text=True)
    chk(f"A.3: GET {_path} HTML contains no '/roster-doc'",
        "/roster-doc" not in _body,
        f"found {_body.count('/roster-doc')} time(s) on {_path}")

# Sanity: the nav infrastructure is intact (guards against the guard passing
# because rendering broke). The '/models' link now comes ONLY from
# backend_control's "Add model" button — the splice that used to add a second
# one is gone (EU-643).
chk("A.4: control bar still renders the /models link (backend_control intact)",
    'href="/models"' in bar,
    f"missing /models; bar[:300] = {bar[:300]}")
chk("A.5: nav row buttons still render (Jira / Task log / Daily / Memory)",
    all(u in bar for u in ("/jira", "/tasks", "/council", "/memory")),
    "a nav-row link went missing")

# ============================================================================
# B. Dormant route proof: GET /roster-doc → 200 + the real roster page
# ============================================================================
resp = client.get("/roster-doc")
page = resp.get_data(as_text=True)

chk("B.1: /roster-doc returns HTTP 200 (dormant, not deleted)",
    resp.status_code == 200, str(resp.status_code))
chk("B.2: /roster-doc body serves the roster ('Chain of command')",
    "Chain of command" in page,
    f"'{page[:200].strip()}...'")
chk("B.3: /roster-doc lists officer roles ('Orchestrator', 'Builder')",
    "Orchestrator" in page and "Builder" in page,
    "expected role descriptions missing from roster page")

# ============================================================================
# C. Source sweep: codifies `grep -rn "roster-doc" orchestrator/` → the ONLY
#    match anywhere is the server.py route decorator. Fails on ANY reintroduced
#    literal — a nav href, a splice anchor, a stray comment — outside the route.
# ============================================================================
_ORCH = Path(__file__).resolve().parents[1] / "orchestrator"
_ROUTE_LINE = '@app.get("/roster-doc")'
hits = []
for _py in sorted(_ORCH.rglob("*.py")):
    for _ln, _line in enumerate(_py.read_text(encoding="utf-8").splitlines(), 1):
        if "roster-doc" in _line:
            hits.append((_py.relative_to(_ORCH.parent), _ln, _line.strip()))
_ok = bool(hits) and all(
    str(_f) == str(Path("orchestrator") / "server.py") and _s == _ROUTE_LINE
    for _f, _ln, _s in hits)
chk("C.1: only 'roster-doc' in orchestrator/**/*.py is the server.py route decorator",
    _ok, f"hits={hits}")

# ============================================================================
# Report (k/n contract run_all.py verifies)
# ============================================================================
print("\n============ EU-643 ROSTER-DOC GUARD ============")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail else ""))
print("----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL(S)")
sys.exit(0 if passed == len(results) else 1)
