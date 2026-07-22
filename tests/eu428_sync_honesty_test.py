"""EU-428 AC1 — transport honesty: a successful pull of UNCHANGED data must read as stale.

The VPS cron logs ``sync[server] pulled=True pushed=read-only peers=mac`` every 15 min and did so
throughout the 3h Mac outage — because ``pulled=True`` only means "the git pull succeeded", NOT
"new rows arrived". A successful pull of a 25-day-old file is indistinguishable from a live feed,
so the log looked healthy while transporting nothing. This is the defect that made the dead
heartbeat invisible in the very channel meant to catch it.

AC1: the peers= field must carry each peer's NEWEST-EVENT age (parsed from the ts INSIDE
shared/<peer>.jsonl — NOT the file mtime, which a no-op pull refreshes). A stale peer must be
visibly stale in the cron log.

Pins (against orchestrator/sync.peer_ages / peer_summary / _fmt_age):
  • a fresh peer (event ts ~now) reports a small age.
  • a stale peer (event ts 25 days ago) reports ~25d and is flagged STALE.
  • a peer file with no parseable ts reports age=? (never a misleading "fresh").
  • the mtime of the file is IRRELEVANT — a freshly-touched file carrying an old event reads stale.
"""
import json
import sys
import tempfile
import time
import types
from pathlib import Path

_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


_sdk.__getattr__ = lambda _n: _D
sys.modules["claude_agent_sdk"] = _sdk
_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = _req
sys.path.insert(0, ".")

from orchestrator import sync
from orchestrator.config import Config, AppConfig

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail) if not cond else ""))


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(epoch))


tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"
audit.touch()
cfg = Config(
    apps=[AppConfig(name="EU", repo_path=str(tmp), base_branch="dev",
                    protected_branch="main", backlog_backend="none")],
    audit_path=str(audit), use_worktree=False)

# shared_files(cfg) reads <repo-root>/.unit-state/shared/*.jsonl ; _repo_root = tmp here.
shared = tmp / ".unit-state" / "shared"
shared.mkdir(parents=True, exist_ok=True)
now = time.time()


def _plant(name: str, event_ts_epoch: float | None, *, touch_now: bool = False) -> None:
    p = shared / f"{name}.jsonl"
    if event_ts_epoch is None:
        p.write_text("", encoding="utf-8")
    else:
        p.write_text(json.dumps({"ts": _iso(event_ts_epoch), "event": "agent_call"}) + "\n",
                     encoding="utf-8")
    if touch_now:
        os_utime = (now, now)
        import os as _os
        _os.utime(p, os_utime)


# mac: fresh event (6 min ago). server-on-this-box wouldn't exist, but plant a stale one too.
_plant("mac", now - 6 * 60)
# a SECOND peer that is stale (25 days) but whose FILE was just rewritten (mtime = now) — the trap:
# a no-op pull refreshes mtime while the event inside is ancient. mtime must NOT be the signal.
_plant("stalepeer", now - 25 * 86400, touch_now=True)
# a peer with no parseable ts at all
_plant("empty", None)

# ── 1. peer_ages parses the NEWEST EVENT ts inside each file ────────────────────────────
ages = sync.peer_ages(cfg)
chk("peer_ages returns one entry per peer file", set(ages) == {"mac", "stalepeer", "empty"},
    f"ages={ages}")
chk("fresh peer (6m-old event) reports a small age (~6 min)",
    ages.get("mac") is not None and 5 * 60 <= ages["mac"] <= 7 * 60, f"mac age={ages.get('mac')}")
chk("stale peer (25d-old event) reports ~25 days regardless of a fresh file mtime",
    ages.get("stalepeer") is not None and 24 * 86400 <= ages["stalepeer"] <= 26 * 86400,
    f"stalepeer age={ages.get('stalepeer')}")
chk("a peer with no parseable event ts reports None (never a misleading 'fresh')",
    ages.get("empty") is None, f"empty age={ages.get('empty')}")

# ── 2. _fmt_age renders human-short ages ───────────────────────────────────────────────
chk("_fmt_age renders minutes", sync._fmt_age(6 * 60) == "6m", sync._fmt_age(6 * 60))
chk("_fmt_age renders hours", sync._fmt_age(3 * 3600) == "3h", sync._fmt_age(3 * 3600))
chk("_fmt_age renders days", sync._fmt_age(25 * 86400) == "25d", sync._fmt_age(25 * 86400))
chk("_fmt_age renders unknown for None", sync._fmt_age(None) == "?", sync._fmt_age(None))

# ── 3. peer_summary builds the log fragment and flags stale peers visibly ──────────────
summary = sync.peer_summary(cfg)
chk("peer_summary names each peer with its age", "mac(age=6m)" in summary, summary)
chk("peer_summary flags a stale peer visibly (STALE marker)",
    "stalepeer(age=25d)" in summary and "STALE" in summary, summary)
chk("peer_summary shows age=? for a peer with no parseable ts", "empty(age=?)" in summary, summary)
chk("peer_summary does NOT flag a fresh peer as stale",
    "STALE" not in summary.split("stalepeer")[0], summary)

# ── 4. the fresh-vs-stale boundary is a named, configurable threshold ──────────────────
chk("a STALE_PEER_S threshold constant exists (not a magic number)",
    isinstance(getattr(sync, "STALE_PEER_S", None), (int, float)), "sync.STALE_PEER_S missing")

print("\n========== EU-428 AC1 SYNC-TRANSPORT-HONESTY QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
