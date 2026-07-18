"""EU-362 — the per-session Workspace store and the needs summary cache must be bounded.

The remaining two of the three resource leaks in the 2026-07-16 audit's EU-362 batch (the
/api/terminal half landed separately as 5a5a167, pinned by tests/eu362_terminal_bounds_test.py —
its docstring explicitly leaves these two open):

  1. ``cockpit_state.workspace_for()`` stored a fresh ``Workspace`` under EVERY session id it was
     ever asked about, forever — and ``server._session_id()`` mints a fresh ``token_hex`` for any
     cookieless request, so every curl / health probe / monitor hit of ``/`` grew ``_workspaces``
     permanently. Bounded now: idle sessions expire after a TTL, and a hard LRU cap holds the
     store's size regardless of how fast probes mint ids.

  2. ``needs._SUMMARY_CACHE`` keys embed the audit signature + per-source-file mtimes, so every
     audit append orphans ALL previously cached entries (their keys can never be looked up again)
     while nothing ever evicted them — one long-lived serve process retained a dict entry per
     audit append per open tab. Bounded now: stale entries are purged on write and the cache is
     hard-capped.

Regression guards: the EU-63 same-object contract (``workspace_for(sid)`` returns the identical
live object on repeat calls) and the EU-129 memo behaviour (two calls in one render share one
computation) must survive the bounding.

Soft ``k/n passed`` tally so ``tests/run_all.py`` (the EU-44 gate) judges it honestly.
"""
import json
import sys
import tempfile
import time
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import cockpit_state, needs  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

results: list[tuple[str, bool, str]] = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 1) Workspace store is bounded (EU-362/1)
# ══════════════════════════════════════════════════════════════════════════════════════════════
CAP = getattr(cockpit_state, "_WORKSPACE_MAX", None)
chk("a workspace LRU cap exists (_WORKSPACE_MAX)", isinstance(CAP, int) and CAP > 0, repr(CAP))
TTL = getattr(cockpit_state, "_WORKSPACE_TTL_S", None)
chk("a workspace idle TTL exists (_WORKSPACE_TTL_S)",
    isinstance(TTL, (int, float)) and TTL > 0, repr(TTL))

# 1a. Flood: simulate the cookieless-probe pattern — every request mints a fresh session id.
cockpit_state.reset_workspaces()
N = (CAP or 256) * 2 + 88
for i in range(N):
    cockpit_state.workspace_for(f"probe-{i:05d}")
size = len(cockpit_state._workspaces)
chk(f"{N} one-shot cookieless sessions leave the store at/under the cap",
    CAP is not None and size <= CAP, f"len(_workspaces)={size}, cap={CAP}")
chk("the newest session survives the flood",
    f"probe-{N - 1:05d}" in cockpit_state._workspaces, f"len={size}")
chk("the oldest probe was evicted (LRU, not FIFO-of-everything)",
    "probe-00000" not in cockpit_state._workspaces, f"len={size}")

# 1b. Same-object contract (EU-63 pin) survives for live sessions under the cap.
cockpit_state.reset_workspaces()
w1 = cockpit_state.workspace_for("sess-live")
w1.add_tab("automatixy")
chk("EU-63 contract: repeat workspace_for returns the identical live object",
    cockpit_state.workspace_for("sess-live") is w1)
chk("EU-63 contract: tab state survives the repeat call",
    cockpit_state.workspace_for("sess-live").projects() == ["automatixy"])

# 1c. A recently-touched session survives other sessions being created (no over-eviction) …
cockpit_state.workspace_for("sess-other")
chk("creating another session does not evict a fresh one",
    cockpit_state.workspace_for("sess-live") is w1)

# 1d. … but an idle-past-TTL session is expired on the next store access.
if isinstance(TTL, (int, float)):
    touch = getattr(cockpit_state, "_workspace_touch", {})
    touch["sess-other"] = time.time() - (TTL + 60)
    cockpit_state.workspace_for("sess-trigger")     # any access runs the sweep
    chk("a session idle past the TTL is evicted from the store",
        "sess-other" not in cockpit_state._workspaces,
        f"keys={sorted(cockpit_state._workspaces)[:6]}")
    chk("the TTL sweep leaves fresh sessions alone",
        cockpit_state.workspace_for("sess-live") is w1)
else:
    chk("a session idle past the TTL is evicted from the store", False, "no TTL constant")
    chk("the TTL sweep leaves fresh sessions alone", False, "no TTL constant")

# 1e. rehydrate_workspace stamps the session as live too (it is the other store writer).
rehydrated = cockpit_state.rehydrate_workspace(
    "sess-rehydrated", {"tabs": [{"project": "automatixy"}], "active": "automatixy"})
chk("rehydrated workspace is stored and returned as the same object",
    cockpit_state.workspace_for("sess-rehydrated") is rehydrated)
cockpit_state.reset_workspaces()
chk("reset_workspaces clears the store", len(cockpit_state._workspaces) == 0)
_touch = getattr(cockpit_state, "_workspace_touch", None)
chk("reset_workspaces clears the touch ledger too (no orphaned timestamps)",
    not _touch, repr(_touch)[:80])


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 2) needs._SUMMARY_CACHE is bounded (EU-362/3)
# ══════════════════════════════════════════════════════════════════════════════════════════════
NCAP = getattr(needs, "_SUMMARY_CACHE_MAX", None)
chk("a summary-cache cap exists (_SUMMARY_CACHE_MAX)",
    isinstance(NCAP, int) and NCAP > 0, repr(NCAP))

tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"
audit.write_text("", encoding="utf-8")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(audit), use_worktree=False)

needs.clear_cache()
APPENDS = (NCAP or 16) * 2 + 9
with audit.open("a", encoding="utf-8") as fh:
    for i in range(APPENDS):
        # Each append changes the audit signature, so each summary() call mints a NEW cache key —
        # exactly the single-use-key growth pattern the audit called out.
        fh.write(json.dumps({"ts": f"2026-07-18T10:{i % 60:02d}:00",
                             "event": "ticket_start", "ticket_id": f"EU-{i}"}) + "\n")
        fh.flush()
        needs.summary(cfg)
csize = len(needs._SUMMARY_CACHE)
chk(f"{APPENDS} audit appends leave the summary cache at/under the cap",
    NCAP is not None and csize <= NCAP, f"len(_SUMMARY_CACHE)={csize}, cap={NCAP}")

# EU-129 memo pin: with the audit unchanged, back-to-back calls share one cached computation.
s1 = needs.summary(cfg)
s2 = needs.summary(cfg)
chk("EU-129 memo still works: unchanged audit -> the second call is a cache hit", s1 is s2)
chk("the memoized summary is coherent (total == len(rows))",
    isinstance(s1, dict) and s1.get("total") == len(s1.get("rows", [])),
    str(s1.get("total")))
needs.clear_cache()
chk("clear_cache still empties the cache", len(needs._SUMMARY_CACHE) == 0)


print("\n============ EU-362 WORKSPACE / NEEDS-CACHE BOUNDS QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
