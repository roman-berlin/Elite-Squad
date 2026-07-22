"""EU-431 — the 2026-07-21 state/ migration orphans the VPS council archive.

The audit_path migration moved audit.jsonl (and so every Path(audit_path).with_name('council'))
one level deeper into state/, but left the real council archive — index.jsonl + N transcripts back
to 2026-06-20 — in the legacy council/ sibling. _council_dir() now resolves to the empty
state/council/, so the next ceremony would append a single fresh row to a brand-new
state/council/index.jsonl and history() would permanently lose every prior row (a lost-index
problem — the files stay on disk but nothing points at them).

Pins (one test per acceptance criterion):
  AC1/AC2 — on boot, the legacy council/ archive (index.jsonl + transcripts) is MOVED into
            state/council/ when the new dir is empty; history() then returns every row; cron.log
            STAYS in the legacy dir (the crontab's `>> council/cron.log` redirect still writes
            there); a council_archive_adopted audit event is recorded.
  no-clobber — a new dir that already holds an index.jsonl is never touched (no adoption, no
            overwrite, history() reflects only the new dir's rows).
  no-legacy — nothing to adopt -> no-op, no crash, no audit event.
  idempotent — a second boot does not re-adopt.

No network, no real models — stub the Agent SDK. Hermetic: the cfg's audit_path is one level deep
under a tmp root (state/audit.jsonl), so the legacy sibling resolves to <tmp>/council — the exact
pre-migration layout — set up by the test itself.
"""
import json, sys, tempfile, types
from pathlib import Path

# Stub the Agent SDK so importing orchestrator.council never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import council
from orchestrator.audit import AuditLog
from orchestrator.config import AppConfig, Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))
    print(("PASS: " if c else "FAIL: ") + n + (f" - {d}" if d and not c else ""))


def _cfg_under(tmp):
    """A Config whose audit_path is one level deep under `tmp` (state/audit.jsonl), so the legacy
    council sibling resolves to <tmp>/council — the pre-migration layout the migration abandoned."""
    app = AppConfig(name="Elite-Unit", repo_path=str(tmp / "repo"), base_branch="dev",
                    workdir=str(tmp / "repo"),
                    gate_commands=[f"{sys.executable} tests/run_all.py"], backlog_backend="none")
    return Config(apps=[app], audit_path=str(tmp / "state" / "audit.jsonl"), use_worktree=False)


def _legacy_dir(cfg):
    return Path(cfg.audit_path).resolve().parent.parent / "council"


def _write_legacy_archive(legacy, rows=3, transcripts=2, with_cronlog=True):
    legacy.mkdir(parents=True, exist_ok=True)
    # index.jsonl: `rows` entries OLDEST first (the on-disk append order); history() reverses to
    # newest-first. This is the byte order that must survive the move unchanged.
    with (legacy / "index.jsonl").open("w", encoding="utf-8") as idx:
        for i in range(rows):
            idx.write(json.dumps({"ts": f"2026-06-{20+i:02d}T08:30:00", "title": f"council #{i}",
                                  "file": f"council-2026062{i}-0830.md", "summary": f"row {i}"}) + "\n")
    for i in range(transcripts):
        (legacy / f"council-2026062{i}-0830.md").write_text(f"# transcript {i}\n", encoding="utf-8")
    if with_cronlog:
        (legacy / "cron.log").write_text("old cron output\n", encoding="utf-8")


# ================================================================================================ #
# AC1/AC2: legacy archive adopted on boot — moved (not copied), history() returns every row
# ================================================================================================ #
print("\n=== AC1/AC2: legacy archive adopted on boot (moved; history() returns all rows) ===")
tmp = Path(tempfile.mkdtemp())
cfg = _cfg_under(tmp)
legacy = _legacy_dir(cfg)
_write_legacy_archive(legacy, rows=3, transcripts=2, with_cronlog=True)
new = council._council_dir(cfg)                       # creates <tmp>/state/council (empty)
chk("new council dir is empty before adoption (no index.jsonl yet)",
    not (new / "index.jsonl").exists(), str(new))
audit = AuditLog(cfg.audit_path)
adopted = council.adopt_legacy_council(cfg, audit)
chk("adopt_legacy_council reports it adopted", adopted is True)
hist = council.history(cfg, limit=100)
chk("history() returns all 3 rows after adoption (no back-history lost)", len(hist) == 3,
    f"{len(hist)} rows")
chk("history() stays newest-first (row 2, 1, 0) — append ordering preserved by the move",
    [r["summary"] for r in hist] == ["row 2", "row 1", "row 0"],
    str([r["summary"] for r in hist]))
chk("transcripts moved into the new dir",
    (new / "council-20260620-0830.md").exists() and (new / "council-20260621-0830.md").exists())
chk("legacy index.jsonl is gone (moved, not copied)", not (legacy / "index.jsonl").exists())
# AC1: cron.log STAYS — the crontab's `>> council/cron.log` redirect still writes the legacy path,
# so the crontab needs no change. This is the explicit decision the ticket asked for.
chk("cron.log stays in the legacy dir (crontab redirect untouched)",
    (legacy / "cron.log").exists() and not (new / "cron.log").exists())
# AC2: an audit event was recorded with the adopted row count.
_alog = AuditLog(cfg.audit_path).path
events = []
if _alog.exists():
    for ln in _alog.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
adopted_ev = [e for e in events if e.get("event") == "council_archive_adopted"]
chk("adoption emitted a council_archive_adopted audit event", len(adopted_ev) == 1,
    str([e.get("event") for e in events]))
chk("audit event records the adopted row count (3)", adopted_ev and adopted_ev[0].get("rows") == 3,
    str(adopted_ev))


# ================================================================================================ #
# Pin: no-clobber — a new dir that already holds an index.jsonl is never touched
# ================================================================================================ #
print("\n=== Pin: no-clobber — a populated new dir is never touched ===")
tmp2 = Path(tempfile.mkdtemp())
cfg2 = _cfg_under(tmp2)
legacy2 = _legacy_dir(cfg2)
_write_legacy_archive(legacy2, rows=3, transcripts=2)
new2 = council._council_dir(cfg2)
# Pre-populate the NEW dir with its own index.jsonl (1 row) — as if a ceremony already ran there.
(new2 / "index.jsonl").write_text(
    json.dumps({"ts": "2026-07-22T08:30:00", "title": "fresh", "file": "council-20260722-0830.md",
                "summary": "fresh"}) + "\n", encoding="utf-8")
audit2 = AuditLog(cfg2.audit_path)
adopted2 = council.adopt_legacy_council(cfg2, audit2)
chk("populated new dir -> no adoption", adopted2 is False)
chk("new dir's own index.jsonl is intact (not clobbered)",
    json.loads((new2 / "index.jsonl").read_text(encoding="utf-8"))["summary"] == "fresh")
chk("legacy archive is untouched (not moved into the new dir)", (legacy2 / "index.jsonl").exists())
hist2 = council.history(cfg2, limit=100)
chk("history() reflects only the new dir's 1 row (no silent merge)", len(hist2) == 1,
    f"{len(hist2)} rows")


# ================================================================================================ #
# Pin: no legacy archive -> no-op, no crash, no audit event
# ================================================================================================ #
print("\n=== Pin: no legacy -> no-op, no crash ===")
tmp3 = Path(tempfile.mkdtemp())
cfg3 = _cfg_under(tmp3)
audit3 = AuditLog(cfg3.audit_path)                    # no legacy <tmp3>/council exists
adopted3 = council.adopt_legacy_council(cfg3, audit3)
chk("no legacy archive -> no adoption, no crash", adopted3 is False)
chk("no audit event recorded when nothing was adopted",
    not audit3.path.exists() or "council_archive_adopted" not in audit3.path.read_text(encoding="utf-8"))


# ================================================================================================ #
# Pin: idempotent — a second boot does not re-adopt (the first cfg already adopted)
# ================================================================================================ #
print("\n=== Pin: idempotent — a second boot does not re-adopt ===")
adopted_again = council.adopt_legacy_council(cfg, audit)
chk("second boot is a no-op (new dir now populated)", adopted_again is False)


print("\n================ EU-431 COUNCIL ARCHIVE ADOPTION QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
