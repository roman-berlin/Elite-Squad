"""EU-110 acceptance gate: Scrum Master surfaced on the roster.

Acceptance criteria (from the ticket):
  • The Scrum Master (exists in scrum.py) appears on the /roster-doc page.
  • A test asserts the officer is present in the rendered HTML.

This file is the formal gate for EU-110. It follows the pattern established in
eu94_roster_cockpit_test.py: GET /roster-doc, parse HTML, assert officer presence.
"""
from __future__ import annotations

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

from orchestrator import cockpit_views, officers, roster, server
from orchestrator import sync as _sync
from orchestrator.config import AppConfig, Config

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail) if not cond else ""))


# ── Shared fixtures ────────────────────────────────────────────────────────────
_repo = Path(tempfile.mkdtemp()) / "app"
_repo.mkdir()
cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_repo),
                    base_branch="DEV", protected_branch="MAIN",
                    backlog_backend="none")],
    audit_path=str(_repo / "audit.jsonl"),
    use_worktree=False,
)
cfg.detected_auth = lambda: "test"  # skip auth middleware
_sync.can_promote = lambda: False   # keep the bar off git / network

# ── Test: Scrum Master appears on /roster-doc ───────────────────────────────
client = server.create_app(cfg).test_client()
resp = client.get("/roster-doc")
page = resp.get_data(as_text=True)

chk("/roster-doc returns HTTP 200", resp.status_code == 200, str(resp.status_code))

# The Scrum Master's display name must appear on the page.
scrum_master_display = officers.display("scrum")
chk(f"Scrum Master ('{scrum_master_display}') is listed on the Roster page",
    scrum_master_display in page,
    f"'{scrum_master_display}' not found on /roster-doc")

# The role "S-6 · Scrum Master" should also be visible.
chk("Scrum Master role 'S-6 · Scrum Master' appears on the Roster page",
    "S-6" in page and "Scrum Master" in page,
    "'S-6 · Scrum Master' not found on /roster-doc")

# The duty excerpt from scrum.py should be present (it's included in the officer table).
from orchestrator import scrum as _scrum
scrum_duty_excerpt = _scrum.__doc__.split("\n\n")[0].strip()[:120]
chk(f"Scrum Master duty excerpt appears on the Roster page",
    scrum_duty_excerpt in page,
    f"Duty excerpt '{scrum_duty_excerpt[:50]}...' not found on /roster-doc")

# The Scrum Master should appear in the chain-of-command tree as well.
chk("Scrum Master appears in the chain-of-command tree on the Roster page",
    f"{scrum_master_display}" in page and ("S-6" in page or "scrum" in page.lower()),
    f"'{scrum_master_display}' not found in chain-of-command tree")

# ── Report ────────────────────────────────────────────────────────────────────
print("\n============ EU-110 SCRUM MASTER ROSTER GATE ============")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL(S)")
sys.exit(0 if passed == len(results) else 1)
