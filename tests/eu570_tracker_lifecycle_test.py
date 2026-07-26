"""EU-570 — infra-signature trackers must not squat the ready queue, and must expire on their own.

Measured 2026-07-26: 11 [infra-signature] trackers had been filed to date and every single one was
closed BY HAND. Two defects made that inevitable:

  (a) FILING — the sweep filed each tracker at severity HIGH into the DEFAULT column. But
      intake.is_tracker_ticket deliberately excludes trackers from the build queue, so the unit
      would never build them. Once the drain began honouring priority (a9577a1) these sat at the
      very TOP of the ready column — permanently — and the board read as if priority ordering was
      broken. A ticket the unit refuses to build must not claim a build-order priority.
  (b) LIFECYCLE — nothing ever closed one. A tracker means "this failure keeps happening"; when the
      failure stops, the ticket is answering a question nobody is asking. There was no expiry.

Now: trackers file at LOW severity and are parked in the human-handoff column immediately, and a
sweep retires any tracked signature that has gone quiet inside the window, dropping it from the
ledger so a genuine relapse files fresh evidence.

The ledger entry widened from a bare float to {"ts", "key"}; the OLD shape must keep working
forever (a pre-EU-570 ledger simply carries no key, so those entries are never auto-closed).
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

_sdk = types.ModuleType("claude_agent_sdk")
_sdk.__getattr__ = lambda _n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", _sdk)
_req = types.ModuleType("requests")
_req.Session = lambda *a, **k: None
_req.RequestException = Exception
sys.modules.setdefault("requests", _req)

sys.path.insert(0, ".")

from orchestrator import forensics  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


SRC = Path("orchestrator/forensics.py").read_text(encoding="utf-8")

# ── (a) filing: LOW severity + parked out of the ready column ─────────────────
chk("(1) the tracker is filed at LOW severity, not HIGH",
    '"severity": "LOW"' in SRC and '"severity": "HIGH"' not in SRC)
chk("(2) …and parked into the human-handoff column right after filing",
    'set_status(_bl.get_task(key), "Needs Human")' in SRC)
chk("(2a) …best-effort, so a transition failure can't break the sweep",
    SRC.count("parking is a nicety, never a blocker") == 1)
chk("(3) the retirement pass is wired into the sweep",
    "_retire_quiet_signatures(cfg, app_cfg, audit, set(groups))" in SRC)

# ── ledger: new shape round-trips, OLD shape still readable ───────────────────
with tempfile.TemporaryDirectory() as td:
    cfg = types.SimpleNamespace(audit_path=str(Path(td) / "audit.jsonl"))
    state = Path(td) / "signature_filed.json"

    forensics._mark_sig_filed(cfg, "sig-alpha", 1000.0, key="EU-900")
    raw = json.loads(state.read_text(encoding="utf-8"))
    chk("(4) a new entry stores BOTH the timestamp and the ticket key",
        isinstance(raw.get("sig-alpha"), dict)
        and raw["sig-alpha"].get("key") == "EU-900"
        and float(raw["sig-alpha"]["ts"]) == 1000.0, str(raw))
    chk("(4a) _sig_last_filed still returns plain floats (callers unchanged)",
        forensics._sig_last_filed(cfg) == {"sig-alpha": 1000.0},
        str(forensics._sig_last_filed(cfg)))
    chk("(4b) _sig_filed_keys exposes the key for retirement",
        forensics._sig_filed_keys(cfg) == {"sig-alpha": "EU-900"})

    # a PRE-EU-570 ledger (bare floats) must still be read, and must not crash retirement
    state.write_text(json.dumps({"old-sig": 555.0}), encoding="utf-8")
    chk("(5) a legacy float ledger is still readable (backward compatible)",
        forensics._sig_last_filed(cfg) == {"old-sig": 555.0})
    chk("(5a) …and legacy entries expose no key, so they are never auto-closed",
        forensics._sig_filed_keys(cfg) == {})
    chk("(5b) a corrupt ledger fails OPEN (empty), never raises",
        (state.write_text("{not json", encoding="utf-8"),
         forensics._sig_last_filed(cfg) == {} and forensics._sig_filed_keys(cfg) == {})[1])

    # ── retirement behaviour, against a fake backlog ──────────────────────────
    state.write_text(json.dumps({
        "quiet-sig": {"ts": 10.0, "key": "EU-901"},
        "live-sig": {"ts": 20.0, "key": "EU-902"},
    }), encoding="utf-8")

    closed: list[str] = []
    commented: list[str] = []

    class _T:
        def __init__(s, key): s.key, s.id, s.status = key, key, "To Do"

    class _BL:
        def get_task(s, key): return _T(key)
        def set_status(s, t, st):
            if st == "Done":
                closed.append(t.key)
        def add_comment(s, t, body): commented.append(t.key)

    import orchestrator.backlog.base as _base
    _orig = _base.make_backlog
    _base.make_backlog = lambda app: _BL()
    try:
        # only "live-sig" recurred inside the window
        out = forensics._retire_quiet_signatures(cfg, types.SimpleNamespace(name="EU"),
                                                 None, {"live-sig"})
    finally:
        _base.make_backlog = _orig

    chk("(6) the QUIET signature's tracker is closed", closed == ["EU-901"], f"closed={closed}")
    chk("(6a) …the still-recurring one is left alone", "EU-902" not in closed)
    chk("(6b) …and the closure is explained on the ticket", commented == ["EU-901"])
    chk("(6c) …the retired signature is dropped so a relapse files fresh",
        "quiet-sig" not in forensics._sig_filed_keys(cfg)
        and forensics._sig_filed_keys(cfg).get("live-sig") == "EU-902",
        str(forensics._sig_filed_keys(cfg)))
    chk("(6d) …and it reports what it retired", out == ["EU-901"], str(out))

    # an ALREADY-closed tracker is not re-closed, just forgotten
    state.write_text(json.dumps({"gone-sig": {"ts": 1.0, "key": "EU-903"}}), encoding="utf-8")
    closed.clear()

    class _BLDone(_BL):
        def get_task(s, key):
            t = _T(key); t.status = "Done"; return t

    _base.make_backlog = lambda app: _BLDone()
    try:
        forensics._retire_quiet_signatures(cfg, types.SimpleNamespace(name="EU"), None, set())
    finally:
        _base.make_backlog = _orig
    chk("(7) an already-Done tracker is not transitioned again, only forgotten",
        closed == [] and forensics._sig_filed_keys(cfg) == {}, f"closed={closed}")

print("\n========== EU-570 TRACKER LIFECYCLE ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
