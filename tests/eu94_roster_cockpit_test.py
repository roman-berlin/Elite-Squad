"""EU-94 acceptance gate: Roster surfaced in the cockpit nav.

Acceptance criteria (from the ticket):
  • A cockpit nav entry opens a Roster view listing every officer and soldier with their duty.
  • A test asserts the page renders the roster.

This file is the formal gate for EU-94. The broader roster round-trip / model-column
tests live in roster_test.py; the nav-button specifics from EU-68 live in
eu68_roster_nav_test.py. This file focuses only on the EU-94 acceptance criteria so the
ticket is auditable and the gate stays green.
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

# ── 1. Cockpit nav entry ──────────────────────────────────────────────────────
# The Roster link must be a first-class button in the top control bar (one click,
# not hidden behind a sub-menu).
bar = cockpit_views._control_bar(cfg, "automatixy")
chk("cockpit nav bar contains the Roster link (/roster-doc)",
    'href="/roster-doc"' in bar)
chk("Roster is a top-level btn (not only inside a dropdown)",
    'class="btn" href="/roster-doc"' in bar)
chk("Roster nav button title mentions engineers",
    "Engineers" in bar)

# ── 2. /roster-doc page: every officer with their duty ───────────────────────
client = server.create_app(cfg).test_client()
resp = client.get("/roster-doc")
page = resp.get_data(as_text=True)

chk("/roster-doc returns HTTP 200", resp.status_code == 200, str(resp.status_code))

# Every officer that roster._OFFICER_ROWS defines must appear on the page.
_missing_officers = [officers.display(key) for key, *_ in roster._OFFICER_ROWS
                     if officers.display(key) not in page]
chk("Every officer is listed on the Roster page",
    not _missing_officers, f"missing: {_missing_officers}")

# Each officer's role / duty excerpt must appear too (spot-check a few canonical ones).
for snippet in ["Orchestrator", "Builder", "Security", "Reviewer"]:
    chk(f"Roster page shows the '{snippet}' role",
        snippet in page, f"'{snippet}' not found on /roster-doc")

# ── 4. Roster view structure ─────────────────────────────────────────────────
chk("Roster page includes the chain-of-command section",
    "Chain of command" in page)
chk("Roster page includes the Engineers &amp; duties section",
    "Engineers" in page and "duties" in page.lower())

# ── Report ────────────────────────────────────────────────────────────────────
print("\n============ EU-94 ROSTER COCKPIT GATE ============")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail else ""))
print("----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL(S)")
sys.exit(0 if passed == len(results) else 1)
