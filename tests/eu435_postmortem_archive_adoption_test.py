"""EU-435 — the 2026-07-21 state/ migration orphans the postmortem archive.

The audit_path migration moved audit.jsonl (and so every Path(audit_path).with_name('postmortems'))
one level deeper into state/, but left the real archive — one <ticket>.md per repeatedly-failing
ticket — in the legacy postmortems/ sibling. postmortems_dir() now resolves to the empty
state/postmortems/, so latest_postmortems() and write_postmortem() start fresh and the irreplaceable
failure history (identical in shape to the council/ archive) is unreachable. EU-431 already shipped
adopt_legacy_council; this is the same move for the postmortems dir-of-files archive.

Pins (one test per acceptance criterion):
  AC1/AC2 — on boot, the legacy postmortems/ archive (*.md) is MOVED into state/postmortems/ when
            the new dir is empty; latest_postmortems() then returns every file (byte-for-byte, so the
            failure history is preserved); a postmortem_archive_adopted audit event is recorded.
  no-clobber — a new dir that already holds a *.md postmortem is never touched (no adoption, no
            overwrite; latest_postmortems() reflects only the new dir's files).
  no-legacy — nothing to adopt -> no-op, no crash, no audit event.
  idempotent — a second boot does not re-adopt.

No network, no real models — stub the Agent SDK. Hermetic: the cfg's audit_path is one level deep
under a tmp root (state/audit.jsonl), so the legacy sibling resolves to <tmp>/postmortems — the exact
pre-migration layout — set up by the test itself. Mirrors tests/eu431_council_archive_adoption_test.py.
"""
import json, sys, tempfile, types
from pathlib import Path

# Stub the Agent SDK so importing orchestrator.forensics never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import forensics
from orchestrator.audit import AuditLog
from orchestrator.config import AppConfig, Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))
    print(("PASS: " if c else "FAIL: ") + n + (f" - {d}" if d and not c else ""))


def _cfg_under(tmp):
    """A Config whose audit_path is one level deep under `tmp` (state/audit.jsonl), so the legacy
    postmortems sibling resolves to <tmp>/postmortems — the pre-migration layout the migration
    abandoned."""
    app = AppConfig(name="Elite-Unit", repo_path=str(tmp / "repo"), base_branch="dev",
                    workdir=str(tmp / "repo"),
                    gate_commands=[f"{sys.executable} tests/run_all.py"], backlog_backend="none")
    return Config(apps=[app], audit_path=str(tmp / "state" / "audit.jsonl"), use_worktree=False)


def _legacy_dir(cfg):
    return Path(cfg.audit_path).resolve().parent.parent / "postmortems"


def _write_legacy_archive(legacy, files=3):
    """Write `files` postmortem *.md into the legacy dir — one per ticket, oldest content first so the
    byte content the move must preserve is deterministic."""
    legacy.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (legacy / f"EU-1{i:02d}.md").write_text(
            f"# Post-mortem — EU-1{i:02d} (Elite-Unit)\n\nlegacy body {i}\n", encoding="utf-8")


# ================================================================================================ #
# AC1/AC2: legacy archive adopted on boot — moved (not copied), latest_postmortems() returns all
# ================================================================================================ #
print("\n=== AC1/AC2: legacy archive adopted on boot (moved; latest_postmortems() returns all) ===")
tmp = Path(tempfile.mkdtemp())
cfg = _cfg_under(tmp)
legacy = _legacy_dir(cfg)
_write_legacy_archive(legacy, files=3)
expected = {(legacy / f"EU-1{i:02d}.md").read_text(encoding="utf-8") for i in range(3)}
new = forensics.postmortems_dir(cfg)                     # <tmp>/state/postmortems (does not exist yet)
chk("new postmortems dir is empty before adoption (no *.md yet)",
    not new.is_dir() or not any(new.glob("*.md")), str(new))
audit = AuditLog(cfg.audit_path)
adopted = forensics.adopt_legacy_postmortems(cfg, audit)
chk("adopt_legacy_postmortems reports it adopted", adopted is True)
listed = forensics.latest_postmortems(cfg, limit=100)
chk("latest_postmortems() returns all 3 files after adoption (no history lost)",
    len(listed) == 3, f"{len(listed)} files")
chk("every file moved byte-for-byte (content preserved by the move)",
    {Path(p["path"]).read_text(encoding="utf-8") for p in listed} == expected)
chk("legacy *.md files are gone (moved, not copied)",
    not any(legacy.glob("*.md")))
# AC2: an audit event was recorded with the adopted file count.
_alog = AuditLog(cfg.audit_path).path
events = []
if _alog.exists():
    for ln in _alog.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
adopted_ev = [e for e in events if e.get("event") == "postmortem_archive_adopted"]
chk("adoption emitted a postmortem_archive_adopted audit event", len(adopted_ev) == 1,
    str([e.get("event") for e in events]))
chk("audit event records the adopted file count (3)", adopted_ev and adopted_ev[0].get("files") == 3,
    str(adopted_ev))


# ================================================================================================ #
# Pin: no-clobber — a new dir that already holds a *.md postmortem is never touched
# ================================================================================================ #
print("\n=== Pin: no-clobber — a populated new dir is never touched ===")
tmp2 = Path(tempfile.mkdtemp())
cfg2 = _cfg_under(tmp2)
legacy2 = _legacy_dir(cfg2)
_write_legacy_archive(legacy2, files=3)
new2 = forensics.postmortems_dir(cfg2)
# Pre-populate the NEW dir with its own postmortem — as if write_postmortem already ran there.
new2.mkdir(parents=True, exist_ok=True)
(new2 / "EU-9999.md").write_text("# fresh postmortem\n", encoding="utf-8")
audit2 = AuditLog(cfg2.audit_path)
adopted2 = forensics.adopt_legacy_postmortems(cfg2, audit2)
chk("populated new dir -> no adoption", adopted2 is False)
chk("new dir's own *.md is intact (not clobbered)",
    (new2 / "EU-9999.md").read_text(encoding="utf-8") == "# fresh postmortem\n")
chk("legacy archive is untouched (not moved into the new dir)",
    len(list(legacy2.glob("*.md"))) == 3)
listed2 = forensics.latest_postmortems(cfg2, limit=100)
chk("latest_postmortems() reflects only the new dir's 1 file (no silent merge)", len(listed2) == 1,
    f"{len(listed2)} files")
chk("no adoption audit event when the new dir was already populated",
    not audit2.path.exists() or "postmortem_archive_adopted" not in audit2.path.read_text(encoding="utf-8"))


# ================================================================================================ #
# Pin: no legacy archive -> no-op, no crash, no audit event
# ================================================================================================ #
print("\n=== Pin: no legacy -> no-op, no crash ===")
tmp3 = Path(tempfile.mkdtemp())
cfg3 = _cfg_under(tmp3)
audit3 = AuditLog(cfg3.audit_path)                       # no legacy <tmp3>/postmortems exists
adopted3 = forensics.adopt_legacy_postmortems(cfg3, audit3)
chk("no legacy archive -> no adoption, no crash", adopted3 is False)
chk("no audit event recorded when nothing was adopted",
    not audit3.path.exists() or "postmortem_archive_adopted" not in audit3.path.read_text(encoding="utf-8"))


# ================================================================================================ #
# Pin: idempotent — a second boot does not re-adopt (the first cfg already adopted)
# ================================================================================================ #
print("\n=== Pin: idempotent — a second boot does not re-adopt ===")
adopted_again = forensics.adopt_legacy_postmortems(cfg, audit)
chk("second boot is a no-op (new dir now populated)", adopted_again is False)


print("\n================ EU-435 POSTMORTEM ARCHIVE ADOPTION QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
