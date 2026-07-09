"""EU-205: Remove unit dev→main promote button from cockpit UI.

Acceptance criteria:
- The "Update unit" button no longer renders on the cockpit sync status view
- No call to `_sync.can_promote()` remains in cockpit_views.py (for unit promotion)
- The `_ahead` badge computed from `sync.promote_status()` is gone from the control bar
- Cockpit loads without error; sync status view renders cleanly
- Tests pass (`python3 tests/run_all.py`)
"""
from __future__ import annotations

import sys
import types
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Minimal stubs so orchestrator modules load without the real SDK / Flask / etc.
# ---------------------------------------------------------------------------
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass  # noqa: N807
    def __call__(s, *a, **k): return s  # noqa: N807


_sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", _sdk)

_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", _req)

sys.path.insert(0, ".")

from orchestrator import cockpit_views  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    """Record a named assertion result."""
    results.append((name, bool(cond), str(detail)))


def _make_cfg(tmp: Path) -> Config:
    """Minimal Config pointing at *tmp*."""
    return Config(
        apps=[
            AppConfig(
                name="EU",
                repo_path=str(tmp),
                base_branch="dev",
                protected_branch="main",
                backlog_backend="none",
            )
        ],
        audit_path=str(tmp / "audit.jsonl"),
        log_folder="logs/",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
tmp = Path(tempfile.mkdtemp())
cfg = _make_cfg(tmp)

# Test 1: The "Update unit" button does not render on the cockpit sync status view
bar = cockpit_views._control_bar(cfg, "EU", healthy=True, is_mac=True)
chk(
    "The 'Update unit' button does not render",
    "Update unit" not in bar and "promote the CTO" not in bar,
    "Found 'Update unit' or 'promote the CTO' in control bar"
)

# Test 2: No call to `_sync.can_promote()` remains in cockpit_views.py for unit promotion
# The only remaining call should be in the ship_html block for product apps
with open(Path(__file__).parent.parent / "orchestrator" / "cockpit_views.py", encoding="utf-8") as f:
    content = f.read()

# Check that the unit promotion section (lines ~423-425) doesn't contain can_promote
lines = content.split("\n")
unit_promote_section = "\n".join(lines[422:426])  # Lines 423-425 (0-indexed)
chk(
    "No call to _sync.can_promote() in unit promotion section",
    "can_promote" not in unit_promote_section,
    f"Found can_promote in unit promotion section:\n{unit_promote_section}"
)

# Verify the only remaining can_promote call is in the ship_html block
can_promote_count = content.count("can_promote()")
chk(
    "Only one can_promote() call remains (for ship_html)",
    can_promote_count == 1,
    f"Expected 1 can_promote() call, found {can_promote_count}"
)

# Test 3: The `_ahead` badge computed from sync.promote_status() is gone from unit promotion
# The only remaining promote_status call should be _sync.app_promote_status for product apps
unit_promote_ahead_check = ("_sync.promote_status(cfg)" in content or
                           "promote_status(cfg)" in content)
chk(
    "No sync.promote_status(cfg) call in cockpit_views.py",
    not unit_promote_ahead_check,
    "Found sync.promote_status(cfg) call (unit promotion _ahead badge)"
)

# Verify app_promote_status is still there (for product apps)
chk(
    "app_promote_status still present (for product apps)",
    "app_promote_status" in content,
    "app_promote_status should remain for ship_html"
)

# Test 4: Cockpit loads without error; sync status view renders cleanly
try:
    bar = cockpit_views._control_bar(cfg, "EU", healthy=True, is_mac=True)
    has_basic_elements = (
        "<div class=tbar>" in bar and
        "New task" in bar and
        "Patrol" in bar and
        "Ship review" in bar
    )
    chk(
        "Cockpit loads without error and renders cleanly",
        has_basic_elements,
        "Control bar missing basic elements"
    )
except Exception as e:
    chk(
        "Cockpit loads without error",
        False,
        f"Exception raised: {e}"
    )

# Test 5: {promote_html} template interpolation removed
chk(
    "{promote_html} interpolation removed from template",
    "{promote_html}" not in content,
    "Found {promote_html} in template"
)

# Verify {ship_html} is still present
chk(
    "{ship_html} interpolation still present",
    "{ship_html}" in content,
    "{ship_html} should remain for product app shipping"
)

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
print("\n================ EU-205: Remove Unit Promote Button ================")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")

sys.exit(0 if passed == len(results) else 1)
