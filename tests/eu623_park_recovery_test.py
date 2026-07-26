"""EU-623/624/625 — the three defects that lost EU-543 and burned $3.94 on a dead backend.

Found by the 2026-07-26 blocked-ticket triage (8 agents over the 5 Blocked tickets). Four of the
five were already-done work sitting on the board; the fifth (EU-543, the Commander's live-run-log
feature) was a real defect chain:

  1. EU-624 — the PM's RESOLVE hatch tested ONE prefix ("Verification failed"), but the
     verdict-mismatch site writes "Gate/Builder verdict mismatch: …". So when the PM said RESOLVE
     on a mismatch the loop fell through and escalated to the Commander anyway. It had fired 0
     times against 8 mismatches. EU-543 parked that way with its gate 501/502 green — the single
     red harness being the SUPERSEDED guard that ticket exists to replace.
  2. EU-623a — `_RED_BASE_MARKERS` carried a bare "red base" catch-all matched as a naked substring
     against the whole park question, so a PM note that merely MENTIONED a red base was treated as
     a red-base park and auto-resumed.
  3. EU-623b — that auto-resume cleared the decision card and the blocked set but never restored the
     tracker status, so the ticket stayed 'Blocked' while the drain draws only To Do / In Progress:
     in no list, on no screen, unreachable. (`unblock` had always called `_reopen_on_tracker`; this
     path just never did.)
  4. EU-625 — a builder process error reported the hardcoded note "builder process errored" and
     dropped the provider's real message, so a dead backend looked like a bad ticket. EU-476 was
     planned 3x (~$3.94) while the secondary answered HTTP 400 "free quota has been exhausted" in
     ~2s with 0 tokens on every attempt.

These are source-level pins: the behaviours live deep inside the drain loop, so each check asserts
the exact contract that regressed, with the reason attached.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, ".")

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


LOOP = pathlib.Path("orchestrator/loop.py").read_text(encoding="utf-8")
AUTO = pathlib.Path("orchestrator/autopilot.py").read_text(encoding="utf-8")

# ── EU-624: the requeue hatch accepts BOTH exhaustion shapes ──────────────────
chk("(1a) the RESOLVE hatch accepts all three exhaustion prefixes",
    "_REQUEUEABLE = (" in LOOP
    and all(p in LOOP for p in ('"Verification failed"', '"Gate/Builder verdict mismatch"',
                                '"Deterministic checks failed"')))
chk("(1b) the hatch tests the tuple, not the single old prefix",
    "last_changes[0].startswith(_REQUEUEABLE)" in LOOP
    and 'last_changes[0].startswith("Verification failed")' not in LOOP)
# the writer side must still produce a prefix the hatch accepts — the drift that caused the bug
_writes = [ln.strip() for ln in LOOP.splitlines() if "last_changes = [f\"" in ln]
chk("(1c) every last_changes writer starts with an accepted prefix (no new drift)",
    all(any(pfx in ln for pfx in ('Verification failed', 'Gate/Builder verdict mismatch',
                                  'Deterministic checks failed')) for ln in _writes),
    f"writers={_writes}")

# ── EU-623a: no loose red-base marker ─────────────────────────────────────────
chk("(2a) the bare 'red base' catch-all marker is gone",
    '_RED_BASE_MARKERS = ("is RED before any build", "gate fails on the clean base")' in AUTO)
chk("(2b) …and the literal park phrase is still matched (real parks still resume)",
    '"is RED before any build"' in AUTO)

import re  # noqa: E402

_markers = re.search(r"_RED_BASE_MARKERS\s*=\s*\(([^)]*)\)", AUTO)
chk("(2c) no marker is short enough to match incidental prose (every marker > 20 chars)",
    _markers is not None
    and all(len(m.strip().strip('"\'')) > 20
            for m in _markers.group(1).split(",") if m.strip()),
    _markers.group(1) if _markers else "no match")

# ── EU-623b: the auto-resume restores the tracker status ──────────────────────
_fn = AUTO[AUTO.find("def _auto_resume_red_base"):]
_fn = _fn[:_fn.find("\ndef _resumable_answered")]
chk("(3a) _auto_resume_red_base reopens each resumed ticket on the tracker",
    "_reopen_on_tracker(cfg, _tid)" in _fn)
chk("(3b) …guarded per ticket so one tracker miss can't strand the rest",
    "red_base_reopen_failed" in _fn)
chk("(3c) …and it still clears the local card + blocked set (resume stays complete)",
    "decisions._save(cfg, remaining)" in _fn and "remove_blocked(cfg, set(resumed))" in _fn)

# ── EU-625: the real builder error survives ───────────────────────────────────
chk("(4a) the errored report carries the provider's own message, not just a fixed string",
    'notes=("builder process errored — "' in LOOP)
chk("(4b) the bare hardcoded note is gone",
    'notes="builder process errored"' not in LOOP)
chk("(4c) a dead-backend shape (0 tokens, sub-5s) is audited so the cap shield can see it",
    'audit.record("builder_process_error"' in LOOP and "dead_backend=_dead_backend" in LOOP)

print("\n========== EU-623/624/625 PARK-RECOVERY PINS ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
