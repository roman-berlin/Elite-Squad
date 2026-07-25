"""EU-449 — publish() ships a bounded tail of audit.jsonl, not the whole ever-growing file.

Pins against orchestrator/sync.publish / _tail_window:
  1. Bounded output: when GENERAL_PUBLISH_MAX_RECORDS=N and the source has >N lines,
     published shared/<host>.jsonl contains exactly the LAST N records.
  2. No unbounded growth: publishing repeatedly against a growing source keeps the
     published file at <= the configured bound every time.
  3. Freshness preserved: the newest ts is always present; peer_ages returns small age,
     peer_summary does NOT flag STALE.
  4. Verbatim under the bound: a source smaller than both bounds is published byte-identical.
  5. Single-writer + docstring: publish() writes only shared/<host>.jsonl; docstring documents
     the bounded recent-window tradeoff.
"""
import json
import os
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


# ── helpers ───────────────────────────────────────────────────────────────────────
def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(epoch))


def _make_audit(n: int, base_epoch: float | None = None) -> str:
    """Build an audit.jsonl string with n records, each ~60s apart."""
    if base_epoch is None:
        base_epoch = time.time() - n * 60
    lines = []
    for i in range(n):
        epoch = base_epoch + i * 60
        lines.append(json.dumps({
            "ts": _iso(epoch),
            "event": "agent_call",
            "seq": i,
        }))
    return "\n".join(lines) + "\n"


# ── setup env + temp dir ─────────────────────────────────────────────────────────
os.environ["GENERAL_HOST_ID"] = "mac"
tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"

# Set explicit bounds via env so tests are deterministic (no reliance on defaults).
os.environ["GENERAL_PUBLISH_MAX_RECORDS"] = "5"
os.environ["GENERAL_PUBLISH_MAX_BYTES"] = "500"

# Publish to tmp itself so peer_ages(cfg) → shared_files(cfg) discovers
# the same file: .unit-state/shared/<host>.jsonl inside tmp.
cfg = Config(
    apps=[AppConfig(name="EU", repo_path=str(tmp), base_branch="dev",
                    protected_branch="main", backlog_backend="none")],
    audit_path=str(audit), use_worktree=False)

# Ensure .unit-state/shared exists (where publish + peer_ages both live)
(sd := tmp / ".unit-state" / "shared").mkdir(parents=True, exist_ok=True)


# ── TEST 1: Bounded output — over-bound source → exactly N records ──────────────
OVERBOUND_N = 50  # we set MAX_RECORDS=5, so 50 should yield exactly 5
audit.write_text(_make_audit(OVERBOUND_N), encoding="utf-8")
dst = sync.publish(cfg)
published_lines = dst.read_text(encoding="utf-8").splitlines()

chk("TEST 1 — bounded output: published record count equals max_records",
    len(published_lines) == 5,
    f"expected 5 lines, got {len(published_lines)}")

last_seq_in_src = OVERBOUND_N - 1
published_text = dst.read_text(encoding="utf-8")
chk(f"TEST 1 — last record (seq={last_seq_in_src}) IS present in published file",
    f'"seq": {last_seq_in_src}' in published_text,
    "the newest record was dropped — freshness broken")

chk("TEST 1 — oldest record (seq=0) is absent from published file",
    '"seq": 0' not in published_text,
    "the tail did not prune old records")

# ── TEST 2: No unbounded growth — publish repeatedly while source grows ─────────
for cycle in range(4):  # 4 more publishes after the first
    extra_n = 20
    audit.write_text(_make_audit(OVERBOUND_N + extra_n * (cycle + 1)), encoding="utf-8")
    dst = sync.publish(cfg)
    published_text = dst.read_text(encoding="utf-8")
    n_lines = len(published_text.splitlines())
    chk(f"TEST 2 — cycle {cycle+1}: published still within record bound ({n_lines} ≤ 5)",
        n_lines <= 5,
        f"grew to {n_lines} lines after cycle {cycle+1}")
    chk(f"TEST 2 — cycle {cycle+1}: newest seq still present",
        f'"seq": {OVERBOUND_N + extra_n * (cycle + 1) - 1}' in published_text,
        "newest record dropped during growth cycle")

# ── TEST 3: Freshness preserved ─────────────────────────────────────────────────
# Use tight spacing so the oldest kept record (oldest of the tail window) is still
# very recent. With MAX_RECORDS=5 and 1-second intervals, the oldest surviving record
# is ≤ 4 s old — easily under any reasonable freshness threshold.
now = time.time()
RECORD_INTERVAL = 1  # seconds between records
audit.write_text(''.join(
    json.dumps({
        "ts": _iso(now - ((OVERBOUND_N - 1 - i) * RECORD_INTERVAL)),
        "event": "tick",
        "seq": i,
    }) + "\n"
    for i in range(OVERBOUND_N)  # 50 records
), encoding="utf-8")
dst = sync.publish(cfg)
published = dst.read_text(encoding="utf-8")

FRESH_TS = _iso(now)
chk("TEST 3 — newest ts in published file",
    FRESH_TS in published,
    "freshness signal lost")

ages = sync.peer_ages(cfg)
chk("TEST 3 — peer_ages returns an entry for mac",
    "mac" in ages and ages["mac"] is not None,
    f"peer_ages returned {ages}")
# With 1-s intervals and MAX_RECORDS=5 the oldest tail-record is only ~5s old,
# so even after interpreter overhead the age must stay tiny.
chk("TEST 3 — mac age is small (< 30s)",
    ages.get("mac") is not None and ages["mac"] < 30,
    f"mac age={ages.get('mac')} — too large?")

summary = sync.peer_summary(cfg)
chk("TEST 3 — peer_summary does NOT flag mac as STALE",
    "STALE" not in summary,
    f"peer_summary={summary} — incorrectly flagged STALE")

# ── TEST 4: Verbatim under the bound ────────────────────────────────────────────
TINY_SRC = ('{"ts": "2026-07-22T11:00:00+0000", "event": "agent_call"}\n'
            '{"ts": "2026-07-22T12:00:00+0000", "event": "build", "ticket_id": "EU-1"}\n')
audit.write_text(TINY_SRC, encoding="utf-8")
(sd / "mac.jsonl").unlink(missing_ok=True)
dst = sync.publish(cfg)
published_again = dst.read_text(encoding="utf-8")

chk("TEST 4 — verbatim under the bound (bytes identical)",
    published_again == TINY_SRC,
    f"expected byte-identical; diff: src={TINY_SRC!r} pub={published_again!r}")

# ── TEST 5: Single-writer invariant & docstring tradeoff ────────────────────────
shared_entries = list(sd.iterdir())
chk("TEST 5 — single-writer: only shared/mac.jsonl exists after publish",
    len(shared_entries) == 1 and shared_entries[0].name == "mac.jsonl",
    f"found {sorted(e.name for e in shared_entries)}")

docstr = sync.publish.__doc__ or ""
chk("TEST 5 — docstring mentions bounded/recent-window tradeoff",
    ("bound" in docstr.lower() or "recent" in docstr.lower() or "window" in docstr.lower())
    and "local" in docstr.lower(),
    f"docstring missing tradeoff note: {docstr!r}"[:200])

# ── Print summary ───────────────────────────────────────────────────────────────
print("\n========== EU-449 PUBLISH BOUNDS QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
