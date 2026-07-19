"""EU-298 (EU-285c): the "synced: mac · Nh Nm ago" footer must flag itself once stale.

Scope note — this ticket had two halves and only ONE is still live:
  - The security-block half ("1 SECURITY BLOCKS" showing a 12-day-old EU-116 item) was already
    fixed by EU-313 (commit e4cd0a0): STALE_BLOCK_CUTOFF_S + _active_security_blocks() drop stale
    blocks from the count, pinned by tests/dashboard_stale_block_test.py. Not re-opened here.
  - The sync footer is still live: `_sync_html_uncached` emitted a bare `<div class=synced>` with
    no staleness branch, and `.synced` hardcodes `color:#5b6b86`, so a 156h-old sync rendered
    visually identical to a 2-minute-old one — "stale-as-current", the exact defect EU-285c names.

AC under test: past the threshold the footer renders with the EU-285a `--warn`/`--critical`
semantic tokens; within it, fresh output is unchanged (no regression). The flag is NOT
colour-only — a textual marker carries it for colour-blind/monochrome readers.
"""
import os
import sys
import time
import types
import tempfile
from pathlib import Path

# --- stubs so warroom imports without the full dependency tree (no network / no SDK) ---
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


_sdk.__getattr__ = lambda _n: _D
sys.modules["claude_agent_sdk"] = _sdk
_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = _req
sys.path.insert(0, ".")

from orchestrator import warroom
from orchestrator import sync as _sync
from orchestrator.config import AppConfig, Config

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail) if not cond else ""))


_tmp = Path(tempfile.mkdtemp())
_audit = _tmp / "audit.jsonl"
_audit.touch()
_cfg = Config(
    apps=[AppConfig(name="test", repo_path=str(_tmp), base_branch="dev",
                    protected_branch="main", backlog_backend="none")],
    audit_path=str(_audit), use_worktree=False,
)


def _render_with_peer_age(hours: float) -> str:
    """Plant a single peer audit file aged `hours` and render the footer.

    The 45s _SYNC_CACHE (keyed by audit_path) would otherwise serve the previous case's string
    and produce a false green — clear it between cases.
    """
    peer = _tmp / "mac.jsonl"
    peer.write_text("{}\n")
    old = time.time() - hours * 3600
    os.utime(peer, (old, old))
    _sync.shared_files = lambda _c: [peer]
    warroom._SYNC_CACHE.clear()
    return warroom._sync_html(_cfg)


# ---------------------------------------------------------------------------
# 1. Fresh sync — unchanged, no stale flag (no regression for fresh data)
# ---------------------------------------------------------------------------
fresh = _render_with_peer_age(0.03)   # ~2 minutes old

chk("fresh footer still renders the synced badge", "synced: mac" in fresh, fresh)
chk("fresh footer carries no stale class", "stale" not in fresh, fresh)
chk("fresh footer stays neutral — no --warn/--critical token",
    "--warn" not in fresh and "--critical" not in fresh, fresh)
chk("fresh footer keeps its plain `class=synced` wrapper (byte-identical to pre-EU-298)",
    fresh.startswith("<div class=synced>"), fresh)

# ---------------------------------------------------------------------------
# 2. Stale sync — the evidenced 156h case must flag itself
# ---------------------------------------------------------------------------
stale = _render_with_peer_age(156)   # the "synced: mac · 156h 38m ago" case from the ticket

chk("stale footer still names the peer and its age",
    "synced: mac" in stale and "156h" in stale, stale)
chk("stale footer is marked with a stale class (not visually identical to fresh)",
    "stale" in stale, stale)
chk("stale footer is NOT colour-only — it carries a textual marker for "
    "colour-blind/monochrome readers",
    "stale" in stale.replace('class="synced stale"', "").replace("class=synced", ""),
    f"no textual 'stale' marker outside the class attribute: {stale}")

# The semantic token must be reachable from the rendered class — assert the CSS rule exists in
# _PAGE and binds .synced.stale to the EU-285a --warn token (rather than a new colour literal).
_page = warroom._PAGE
chk("a `.synced.stale` CSS rule exists in _PAGE", ".synced.stale" in _page,
    "no .synced.stale rule found — the stale class would have no visual effect")
chk("the stale rule reuses the EU-285a --warn/--critical token (no new colour literal)",
    any(f".synced.stale{{color:var({tok})}}" in _page.replace(" ", "")
        for tok in ("--warn", "--critical")),
    "the .synced.stale rule does not bind to var(--warn)/var(--critical)")

# ---------------------------------------------------------------------------
# 3. Threshold is a named constant, and the boundary behaves
# ---------------------------------------------------------------------------
chk("a STALE_SYNC_CUTOFF_S constant exists (configurable threshold, not a magic number)",
    isinstance(getattr(warroom, "STALE_SYNC_CUTOFF_S", None), (int, float)),
    "warroom.STALE_SYNC_CUTOFF_S is missing")

_cut = getattr(warroom, "STALE_SYNC_CUTOFF_S", 24 * 3600) / 3600.0
chk("just INSIDE the cutoff is still treated as fresh",
    "stale" not in _render_with_peer_age(_cut * 0.9))
chk("just OUTSIDE the cutoff is treated as stale",
    "stale" in _render_with_peer_age(_cut * 1.1))

# ---------------------------------------------------------------------------
# 4. The unconfigured / error paths stay byte-identical (empty, no clutter)
# ---------------------------------------------------------------------------
_sync.shared_files = lambda _c: []
warroom._SYNC_CACHE.clear()
chk("no state clone configured -> footer stays empty (stand-alone machine, no clutter)",
    warroom._sync_html(_cfg) == "")


def _boom(_c):
    raise RuntimeError("no sync module")


_sync.shared_files = _boom
warroom._SYNC_CACHE.clear()
chk("a failing sync lookup still degrades to an empty footer, not a traceback",
    warroom._sync_html(_cfg) == "")

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
print("\n========== EU-298 SYNC-FOOTER FRESHNESS QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("-----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
