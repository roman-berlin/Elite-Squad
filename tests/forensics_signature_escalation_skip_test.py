"""EU-400 — the 'max passes — PM escalated' failure is a DESIGNED human-handoff (the run exhausted
its pass budget and parked itself for the Commander via the needs_human event + Telegram notify +
Blocked park at loop.py:2674-2686). It must NOT be auto-filed as an infra-signature meta-ticket by
forensics.signature_sweep. Mirrors the existing worktree_busy skip: an EXPECTED terminal outcome,
not a crash.

Testable acceptance:
  1. >=3 escalation rows (reason 'max passes — PM escalated') across >=2 tickets in-window ->
     signature_sweep returns [] and records NO crash_signature_filed audit event.
  2. A genuine crash signature (errored + repeated traceback/timeout reason) still files exactly one.
  3. Mixed window -> only the crash-signature ticket files; the escalation group is skipped.
  4. The skip is a documented module constant in forensics.py and classify()/taxonomy() are unchanged.
"""
import sys, types, tempfile, json
from pathlib import Path
from datetime import datetime

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
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace

# ---- a tiny audit.jsonl writer; each terminal event becomes one failed run row ----
tmp = Path(tempfile.mkdtemp())
audit_path = tmp / "audit.jsonl"

def ev(**kw):
    with audit_path.open("a") as f:
        f.write(json.dumps(kw) + "\n")
def run_at(tid, ts, term, app="eu", **extra):
    ev(event="ticket_start", ticket_id=tid, app=app, ts=ts)
    ev(event=term, ticket_id=tid, app=app, ts=ts, **extra)

# `now` is the latest event + 1h so every row below falls inside the 7-day window.
NOW = datetime.strptime("2026-07-17T12:00:00", "%Y-%m-%dT%H:%M:%S").timestamp() + 3600

# Capture every _file_one call so we can assert what WOULD file without a real backlog backend.
filed_calls = []
def _fake_file_one(app_cfg, label, proposal):
    filed_calls.append((label, proposal))
    return f"FAKE-{label}-{len(filed_calls)}"
forensics._file_one = _fake_file_one
# Resolve to a sentinel app so signature_sweep reaches the filing step — WITHOUT this, apps=[]
# makes _self_app/_resolve_app return None and the sweep `continue`s past filing for EVERY group,
# which would make the criterion-1 'files nothing' assertion vacuously green for the wrong reason.
_SENTINEL_APP = ns(name="eu-sentinel")
forensics._self_app = lambda cfg: _SENTINEL_APP
forensics._resolve_app = lambda cfg, rows: _SENTINEL_APP


def fresh_cfg():
    return Config(apps=[], audit_path=str(audit_path), postmortem_after=3)

def fresh_audit_log():
    global filed_calls
    filed_calls = []
    try:
        audit_path.unlink()
    except FileNotFoundError:
        pass


# ============================================================ #
# CRITERION 1 — escalation group is NOT filed (designed handoff)
# ============================================================ #
fresh_audit_log()
run_at("AUTO-144", "2026-07-14T10:42:00", "escalated", reason="max passes — PM escalated FAIL")
run_at("EU-298",   "2026-07-14T11:30:00", "escalated", reason="max passes — PM escalated FAIL")
run_at("AUTO-151", "2026-07-14T11:44:00", "escalated", reason="max passes — PM escalated FAIL")

cfg = fresh_cfg()
audit_calls = []
fake_audit = ns(record=lambda k, **kw: audit_calls.append((k, kw)))
filed = forensics.signature_sweep(cfg, fake_audit, now=NOW)

chk("1: escalation group files nothing", filed == [], str(filed))
chk("1: no crash_signature_filed audit event",
    not any(k == "crash_signature_filed" for k, _ in audit_calls), str(audit_calls))
chk("1: _file_one never called for the escalation group", filed_calls == [], str(filed_calls))
# Sanity: the rows ARE seen by scan() — so the skip, not absence, is why nothing filed.
esc_rows = [r for r in forensics.scan(cfg) if "pm escalated" in (r.get("note") or "").lower()]
chk("1: scan still sees the escalation rows (skip is real, not vacuous)", len(esc_rows) == 3, str(len(esc_rows)))


# ============================================================ #
# CRITERION 2 — a genuine crash signature STILL files exactly one
# ============================================================ #
fresh_audit_log()
run_at("AUTO-200", "2026-07-15T09:00:00", "ticket_exception", error="Traceback: SDK connection timeout")
run_at("AUTO-201", "2026-07-15T10:00:00", "ticket_exception", error="Traceback: SDK connection timeout")
run_at("AUTO-202", "2026-07-15T11:00:00", "ticket_exception", error="Traceback: SDK connection timeout")

cfg = fresh_cfg()
audit_calls = []
fake_audit = ns(record=lambda k, **kw: audit_calls.append((k, kw)))
filed = forensics.signature_sweep(cfg, fake_audit, now=NOW)

chk("2: genuine crash signature files exactly one", len(filed) == 1, str(filed))
chk("2: filed under the infra-signature label",
    any(lbl == "infra-signature" for lbl, _ in filed_calls), str(filed_calls))
chk("2: crash_signature_filed audit event recorded",
    any(k == "crash_signature_filed" for k, _ in audit_calls), str(audit_calls))


# ============================================================ #
# CRITERION 3 — mixed window: only the crash-signature ticket files
# ============================================================ #
fresh_audit_log()
run_at("AUTO-144", "2026-07-14T10:42:00", "escalated", reason="max passes — PM escalated FAIL")
run_at("EU-298",   "2026-07-14T11:30:00", "escalated", reason="max passes — PM escalated FAIL")
run_at("AUTO-151", "2026-07-14T11:44:00", "escalated", reason="max passes — PM escalated FAIL")
run_at("AUTO-200", "2026-07-15T09:00:00", "ticket_exception", error="Traceback: SDK connection timeout")
run_at("AUTO-201", "2026-07-15T10:00:00", "ticket_exception", error="Traceback: SDK connection timeout")
run_at("AUTO-202", "2026-07-15T11:00:00", "ticket_exception", error="Traceback: SDK connection timeout")

cfg = fresh_cfg()
filed = forensics.signature_sweep(cfg, None, now=NOW)

chk("3: mixed window files exactly one (the crash sig only)", len(filed) == 1, str(filed))
chk("3: filed ticket is the crash signature, not the escalation",
    len(filed_calls) == 1 and filed_calls[0][0] == "infra-signature", str(filed_calls))
filed_sigs = [p.get("title", "") for _, p in filed_calls]
chk("3: no escalation signature in filed titles",
    not any("pm escalated" in t.lower() for t in filed_sigs), str(filed_sigs))


# ============================================================ #
# CRITERION 4 — skip is a documented module constant; classify()/taxonomy unchanged
# ============================================================ #
chk("4: _SIG_SKIP_SIGNATURES constant exists and documents the handoff",
    hasattr(forensics, "_SIG_SKIP_SIGNATURES")
    and "max passes — pm escalated" in getattr(forensics, "_SIG_SKIP_SIGNATURES", ()),
    str(getattr(forensics, "_SIG_SKIP_SIGNATURES", None)))
esc_class = forensics.classify("escalated", "max passes — PM escalated FAIL")
chk("4: classify() returns a real taxonomy category (no synthetic skip category)",
    esc_class["category"] in {"unknown", "too_big", "gate_fail", "infra", "not_ready",
                              "product_blocker", "merge_conflict", "security_block", "worktree_busy"},
    str(esc_class))
fresh_audit_log()
run_at("AUTO-144", "2026-07-14T10:42:00", "escalated", reason="max passes — PM escalated FAIL")
run_at("EU-298",   "2026-07-14T11:30:00", "escalated", reason="max passes — PM escalated FAIL")
run_at("AUTO-151", "2026-07-14T11:44:00", "escalated", reason="max passes — PM escalated FAIL")
cfg = fresh_cfg()
total = sum(t["count"] for t in forensics.taxonomy(cfg))
chk("4: taxonomy() still counts the escalation rows (skip is sweep-only)", total == 3, str(total))


print("\n=============== EU-400 ESCALATION-SKIP QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
