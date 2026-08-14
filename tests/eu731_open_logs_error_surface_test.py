"""EU-731: Surface fetch errors on the cockpit "Open logs" Finder link.

Tests that the macOS-only Finder link's fire-and-forget fetch() is wrapped
with r.ok checking + .catch error-surfacing, and that a hidden error span
is present in the markup but absent when is_mac=False.

Stubs the Agent SDK / requests so importing the orchestrator needs no network.
Soft k/n tally so ``tests/run_all.py`` (the EU-44 gate) judges it honestly.
"""
from __future__ import annotations

import sys
import types
import tempfile
from html import escape as html_escape
from pathlib import Path

# ── Stub non-network imports ─────────────────────────────────────────────────
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return s


_sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", _sdk)

_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", _req)

sys.path.insert(0, ".")

from orchestrator import cockpit_state  # noqa: E402
from orchestrator import cockpit_views  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    """Record a named assertion result."""
    results.append((name, bool(cond), str(detail)))


def _make_cfg(tmp_dir: str) -> Config:
    """Minimal Config pointing at *tmp_dir* with an empty audit file."""
    import json

    audit = Path(tmp_dir) / "audit.jsonl"
    audit.write_text("", encoding="utf-8")
    return Config(
        apps=[
            AppConfig(
                name="alpha",
                repo_path=tmp_dir,
                base_branch="DEV",
                protected_branch="MAIN",
                backlog_backend="none",
            )
        ],
        audit_path=str(audit),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: is_mac=True — error-surfacing MUST be present
# ═══════════════════════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as tmpdir:
    cfg = _make_cfg(tmpdir)
    bar = cockpit_views._control_bar(cfg, current_app="alpha", healthy=True, is_mac=True)

    snippet = bar[bar.index("open-logs") : bar.index("open-logs") + 300]

    # AC1: r.ok guard present on the fetch response
    chk("AC1: r.ok or !r.ok guard in rendered HTML (error-surface)",
        "r.ok" in bar or "!r.ok" in bar,
        f"Expected r.ok guard; snippet={snippet!r}"
        )

    # AC2: .catch handler on the fetch chain (network-error surfacing)
    chk("AC2: .catch( handler in rendered HTML (network-error surface)",
        ".catch(" in bar,
        f"Expected .catch( on fetch chain; snippet={snippet!r}"
        )

    # AC3: Hidden inline error element present near the Finder link
    chk("AC3: Error-span (display:none, 'Failed to open log') near Finder link",
        "display:none" in bar and "Failed to open log" in bar,
        f"Expected hidden error span text; snippet={snippet!r}"
        )

    # AC4: Success-path elements unchanged
    app_q = html_escape("alpha")
    chk("AC4a: Primary Open-logs nav href still present (success unchanged)",
        f'href="/logs/days?app={app_q}"' in bar,
        f"Expected primary href '/logs/days?app={app_q}'"
        )

    chk("AC4b: Finder link /api/open-logs href still present",
        'href="/api/open-logs"' in bar,
        "Expected finder href '/api/open-logs'"
        )

# ═══════════════════════════════════════════════════════════════════════════════
# Tests: is_mac=False — error-surfacing MUST NOT be present
# ═══════════════════════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as tmpdir:
    cfg = _make_cfg(tmpdir)
    bar = cockpit_views._control_bar(cfg, current_app="alpha", healthy=True, is_mac=False)

    snippet = bar[:300]

    # AC5: No r.ok guard (no fetch at all when not macOS)
    chk("AC5a: No r.ok guard in is_mac=False HTML (macOS-only block)",
        "r.ok" not in bar,
        f"r.ok found unexpectedly; snippet={snippet!r}"
        )

    # AC5: No .catch (no fetch chain when not macOS)
    chk("AC5b: No .catch handler in is_mac=False HTML",
        ".catch(" not in bar,
        f".catch( found unexpectedly; snippet={snippet!r}"
        )

    # AC5: No error-span text in is_mac=False output
    chk("AC5c: No 'Failed to open log' error text in is_mac=False HTML",
        "Failed to open log" not in bar,
        f"'Failed to open log' found unexpectedly; snippet={snippet!r}"
        )

    # Baseline: primary Open-logs link still renders fine off-Mac
    chk("AC5d: Primary Open-logs nav href works on is_mac=False",
        f'href="/logs/days?app={app_q}"' in bar,
        f"Expected primary href even without macOS"
        )


# ── Report ────────────────────────────────────────────────────────────────────
print("\n========== EU-731 OPEN-LOGS ERROR SURFACE ====================")
_passed = sum(1 for _, ok, _ in results if ok)
for _name, _ok, _det in results:
    _mark = "PASS" if _ok else "FAIL"
    _extra = f"  ({_det})" if _det and not _ok else ""
    print(f"  [{_mark}] {_name}{_extra}")
print("-------------------------------------------------------")
print(f"  {_passed}/{len(results)} passed")
if _passed == len(results):
    print("  RESULT: ALL GREEN")
else:
    print(f"  RESULT: {len(results) - _passed} FAIL")
sys.exit(0 if _passed == len(results) else 1)
