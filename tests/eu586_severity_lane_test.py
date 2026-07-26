"""EU-586 + EU-588 — the unit must not pollute its own queue.

Two defects measured on the live board 2026-07-26:

  EU-586 — `_SEVERITY_TO_PRIORITY` knew only the findings-block vocabulary
  (CRITICAL/HIGH/MEDIUM/LOW), but the Reviewer speaks a different scale: reviewer.py:49 declares
  `"severity": "blocker|major|minor"` and reviewer.py:1204 lowercases it, so filing's `.upper()`
  hands the map BLOCKER / MAJOR / MINOR. None matched -> priority None -> the Jira project default.
  Board evidence: BLOCKER -> Medium and MAJOR -> Medium, i.e. a blocker-level finding competed with
  the Commander's own Medium work. Both vocabularies are now mapped, self-filed work is capped at
  High (Highest is the Commander's lane), and BLOCKER joins CRITICAL as a paging severity.

  EU-588 — `is_tracker_ticket` matched only "infra-signature", but forensics also files
  "[postmortem] …" tickets whose body is a failure timeline plus operator guidance, not code work.
  The drain picked them up and burned a build trying to implement a timeline (EU-482 sat open as an
  empty "Uncategorized" postmortem until hand-closed).

Both are behavioural checks against the real functions, not just source greps.
"""
from __future__ import annotations

import sys
import types

# stub the SDK + requests so importing the modules never touches the network
_sdk = types.ModuleType("claude_agent_sdk")
_sdk.__getattr__ = lambda _n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", _sdk)
_req = types.ModuleType("requests")
_req.Session = lambda *a, **k: None
_req.RequestException = Exception
sys.modules.setdefault("requests", _req)

sys.path.insert(0, ".")

from orchestrator import filing, intake  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def T(summary: str, labels=None):
    return types.SimpleNamespace(summary=summary, labels=labels or [], id="EU-1", key="EU-1")


# ── EU-586: the Reviewer's own vocabulary maps to a real priority ─────────────
M = filing._SEVERITY_TO_PRIORITY
for sev in ("BLOCKER", "MAJOR", "MINOR"):
    chk(f"(1) reviewer severity {sev} maps to a priority (was: unmapped -> project default)",
        M.get(sev) is not None, f"{sev} -> {M.get(sev)!r}")
chk("(1a) BLOCKER outranks MAJOR outranks MINOR",
    ["High", "Medium", "Low"] == [M.get("BLOCKER"), M.get("MAJOR"), M.get("MINOR")],
    f"{M.get('BLOCKER')}/{M.get('MAJOR')}/{M.get('MINOR')}")
chk("(1b) the original findings-block vocabulary still maps",
    all(M.get(s) for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW")))
# The Reviewer scale is the HIGH-VOLUME one (every build can emit minors), so it is capped at High:
# routine self-findings must never outrank a ticket the Commander marked Highest. CRITICAL keeps
# "Highest" — EU-284's deliberate contract, and it is rare AND pages, so it is not queue noise.
chk("(2) the reviewer scale never claims 'Highest' (routine findings can't outrank the Commander)",
    "Highest" not in {M.get("BLOCKER"), M.get("MAJOR"), M.get("MINOR")},
    f"reviewer scale -> {M.get('BLOCKER')}/{M.get('MAJOR')}/{M.get('MINOR')}")
chk("(2a) …while EU-284's CRITICAL -> Highest contract is preserved",
    M.get("CRITICAL") == "Highest", f"CRITICAL -> {M.get('CRITICAL')!r}")
chk("(3) BLOCKER pages the Commander alongside CRITICAL",
    "BLOCKER" in filing._PAGING_SEVERITIES and "CRITICAL" in filing._PAGING_SEVERITIES,
    str(filing._PAGING_SEVERITIES))
chk("(3a) the paging check uses the tuple, not a bare CRITICAL comparison",
    "severity in _PAGING_SEVERITIES" in
    __import__("pathlib").Path("orchestrator/filing.py").read_text(encoding="utf-8"))

# ── EU-588: postmortems never enter the build queue ───────────────────────────
chk("(4) a [postmortem] ticket is a tracker (by title)",
    intake.is_tracker_ticket(T("[postmortem] EU-460 — Uncategorized")))
chk("(4a) …and by label, even when the title was rewritten",
    intake.is_tracker_ticket(T("EU-460 failure timeline", labels=["postmortem", "autofiled"])))
chk("(5) infra-signature still detected (no regression) — title",
    intake.is_tracker_ticket(T("[infra-signature] red base — gate fails")))
chk("(5a) …and by label",
    intake.is_tracker_ticket(T("red base pattern", labels=["infra-signature"])))

# real work must still pass through — the guard must not swallow buildable tickets
for good in ("Fix the tokens tile contrast",
             "Verify & close: postmortem handling",      # 'postmortem' NOT at the start
             "Add a per-day log aggregator endpoint"):
    chk(f"(6) real work is NOT treated as a tracker: {good[:38]!r}",
        not intake.is_tracker_ticket(T(good)), good)
chk("(6a) a ticket with no labels/summary attrs does not explode",
    intake.is_tracker_ticket(types.SimpleNamespace()) is False)

print("\n========== EU-586/588 SEVERITY + TRACKER LANE ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
