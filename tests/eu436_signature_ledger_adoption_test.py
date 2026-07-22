"""EU-436 — the 2026-07-21 state/ migration orphaned the filed-signature dedup ledger.

The audit_path migration moved audit.jsonl (and so every Path(audit_path).with_name(
'signature_filed.json')) one level deeper into state/, but left the real dedup ledger at the legacy
signature_filed.json sibling. _sig_state_file() now resolves to the empty state/signature_filed.json,
so _sig_last_filed() returns {} — the ledger that stops forensics from re-filing a postmortem for a
crash signature it ALREADY surfaced is invisible. A recurring crash class would then file a DUPLICATE
Jira ticket (active board harm).

Pins (one block per acceptance criterion), following the adopt_legacy_council template
(tests/eu431_council_archive_adoption_test.py):
  AC1 — legacy ledger present + new absent -> MOVED into state/ on boot; _sig_last_filed() then
        returns every entry (the dedup reader the adoption exists to serve actually sees them); the
        legacy file is gone (moved, not copied).
  AC3 — adoption emits a signature_ledger_adopted audit event carrying the adopted entry count.
  no-clobber — a new ledger that already exists is never touched (no move, no overwrite/merge, no
        audit event); the legacy file is left in place for the operator to reconcile.
  no-legacy — nothing to adopt -> no-op, no crash, no audit event.
  idempotent — a second boot does not re-adopt.

No network, no real models — stub the optional deps the rest of the forensics suite stubs. Hermetic:
the cfg's audit_path is one level deep under a tmp root (state/audit.jsonl), so the legacy sibling
resolves to <tmp>/signature_filed.json — the exact pre-migration layout — set up by the test itself.
"""
import json, sys, tempfile, types
from pathlib import Path

# --- stub the same optional deps the rest of the forensics test suite stubs ---
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
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
    signature_filed.json sibling resolves to <tmp>/signature_filed.json — the pre-migration layout."""
    app = AppConfig(name="Elite-Unit", repo_path=str(tmp / "repo"), base_branch="dev",
                    workdir=str(tmp / "repo"),
                    gate_commands=[f"{sys.executable} tests/run_all.py"], backlog_backend="none")
    return Config(apps=[app], audit_path=str(tmp / "state" / "audit.jsonl"), use_worktree=False)


def _legacy_file(cfg):
    return Path(cfg.audit_path).resolve().parent.parent / "signature_filed.json"


def _audit_events(cfg):
    out = []
    p = AuditLog(cfg.audit_path).path
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    return out


# A ledger that could realistically sit at the legacy path: a few crash signatures already filed.
ENTRIES = {"traceback_keyerror_x:42": 1721600000.0,
           "timeout_30s__gate": 1721600600.0,
           "merge_conflict_packages": 1721601200.0}


# ================================================================================================ #
# AC1: legacy ledger adopted on boot — MOVED (not copied); the dedup reader sees every entry
# ================================================================================================ #
print("\n=== AC1: legacy ledger adopted on boot (moved; _sig_last_filed sees all entries) ===")
tmp = Path(tempfile.mkdtemp())
cfg = _cfg_under(tmp)
legacy = _legacy_file(cfg)
legacy.parent.mkdir(parents=True, exist_ok=True)
legacy.write_text(json.dumps(ENTRIES), encoding="utf-8")
new = forensics._sig_state_file(cfg)
chk("new signature_filed.json is absent before adoption", not new.exists(), str(new))
audit = AuditLog(cfg.audit_path)
adopted = forensics.adopt_legacy_signature_ledger(cfg, audit)
chk("adopt_legacy_signature_ledger reports it adopted", adopted is True)
# The adoption exists ONLY so the dedup reader sees the entries — assert it directly (the harm this
# ticket prevents: a recurring crash re-filing a duplicate ticket).
seen = forensics._sig_last_filed(cfg)
chk("_sig_last_filed() returns all 3 entries after adoption (ledger is live)", seen == ENTRIES,
    f"{len(seen)} entries: {sorted(seen)[:2]}")
chk("legacy signature_filed.json is gone (moved, not copied)", not legacy.exists())
chk("adopted ledger lives at the new state/ path the reader uses", new.exists())

# ================================================================================================ #
# AC3: adoption emits a signature_ledger_adopted audit event carrying the entry count
# ================================================================================================ #
print("\n=== AC3: adoption emits a signature_ledger_adopted audit event ===")
events = _audit_events(cfg)
adopted_ev = [e for e in events if e.get("event") == "signature_ledger_adopted"]
chk("exactly one signature_ledger_adopted audit event", len(adopted_ev) == 1,
    str([e.get("event") for e in events]))
chk("audit event records the adopted entry count (3)", adopted_ev and adopted_ev[0].get("entries") == 3,
    str(adopted_ev))


# ================================================================================================ #
# Pin: no-clobber — a new ledger that already exists is never touched
# ================================================================================================ #
print("\n=== Pin: no-clobber — a new ledger that already exists is never touched ===")
tmp2 = Path(tempfile.mkdtemp())
cfg2 = _cfg_under(tmp2)
legacy2 = _legacy_file(cfg2)
legacy2.parent.mkdir(parents=True, exist_ok=True)
legacy2.write_text(json.dumps(ENTRIES), encoding="utf-8")           # legacy holds real entries...
new2 = forensics._sig_state_file(cfg2)
new2.parent.mkdir(parents=True, exist_ok=True)
own = {"a_signature_filed_in_the_new_location": 1721700000.0}
new2.write_text(json.dumps(own), encoding="utf-8")                   # ...but the new ledger already exists
audit2 = AuditLog(cfg2.audit_path)
adopted2 = forensics.adopt_legacy_signature_ledger(cfg2, audit2)
chk("new ledger present -> no adoption", adopted2 is False)
chk("new ledger's own entry is intact (not clobbered, not silently merged)",
    forensics._sig_last_filed(cfg2) == own, str(forensics._sig_last_filed(cfg2)))
chk("legacy ledger is untouched (left in place for the operator to reconcile)",
    legacy2.exists() and json.loads(legacy2.read_text(encoding="utf-8")) == ENTRIES)
chk("no adoption event recorded when the new ledger already existed",
    not any(e.get("event") == "signature_ledger_adopted" for e in _audit_events(cfg2)))


# ================================================================================================ #
# Pin: no legacy ledger -> no-op, no crash, no audit event
# ================================================================================================ #
print("\n=== Pin: no legacy -> no-op, no crash ===")
tmp3 = Path(tempfile.mkdtemp())
cfg3 = _cfg_under(tmp3)
audit3 = AuditLog(cfg3.audit_path)                                  # no legacy <tmp3>/signature_filed.json
adopted3 = forensics.adopt_legacy_signature_ledger(cfg3, audit3)
chk("no legacy ledger -> no adoption, no crash", adopted3 is False)
chk("no adoption event recorded when nothing was adopted",
    not any(e.get("event") == "signature_ledger_adopted" for e in _audit_events(cfg3)))


# ================================================================================================ #
# Pin: idempotent — a second boot does not re-adopt (the first cfg already adopted)
# ================================================================================================ #
print("\n=== Pin: idempotent — a second boot does not re-adopt ===")
adopted_again = forensics.adopt_legacy_signature_ledger(cfg, audit)
chk("second boot is a no-op (legacy gone, new present)", adopted_again is False)
chk("no second adoption event recorded",
    sum(1 for e in _audit_events(cfg) if e.get("event") == "signature_ledger_adopted") == 1)


print("\n================ EU-436 SIGNATURE LEDGER ADOPTION QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
