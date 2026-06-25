"""Dismiss QA: dismissing one needs-you run must hide ONLY that ticket's old run — never raise, never
wipe the panel. Regression: dismiss() writes a tz-AWARE timestamp (%z) but run 'started' times are naive,
so the old `st <= dt` compare raised TypeError; needs.summary() swallows exceptions, so a single dismissal
silently returned ZERO runs — every needs-you item (including the one you still had to answer) vanished."""
import sys, types, tempfile
from pathlib import Path

req = types.ModuleType("requests")
req.RequestException = Exception
req.post = req.get = lambda *a, **k: types.SimpleNamespace(status_code=200, json=lambda: {}, text="")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import dashboard as D, needs
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

def task(tid, started, outcome="escalated"):
    return {"ticket_id": tid, "started": started, "outcome": outcome}

# dismiss() writes a tz-AWARE time; the audit's run 'started' is naive — the exact mismatch that crashed.
DISMISSED = {"AUTO-32": "2026-06-24T11:20:00+0300"}

chk("dismissed ticket's older run is hidden (tz-aware vs naive no longer raises)",
    D._is_dismissed(task("AUTO-32", "2026-06-24T11:00:00"), DISMISSED) is True)
chk("a DIFFERENT ticket is NOT hidden by dismissing AUTO-32",
    D._is_dismissed(task("AUTO-31", "2026-06-24T11:00:00"), DISMISSED) is False)
chk("a NEWER run of the dismissed ticket shows again",
    D._is_dismissed(task("AUTO-32", "2026-06-24T12:00:00"), DISMISSED) is False)
chk("a run with no start time is SHOWN, not hidden (fail-safe)",
    D._is_dismissed(task("AUTO-32", None), DISMISSED) is False)
chk("junk timestamps never raise → SHOWN",
    D._is_dismissed(task("AUTO-32", "garbage"), {"AUTO-32": "garbage"}) is False)
chk("no dismissals → nothing hidden", D._is_dismissed(task("AUTO-1", "2026-06-24T11:00:00"), {}) is False)

# --- the panel-wipe regression, end to end through needs.summary ---
tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[], audit_path=str(tmp / "audit.jsonl"))
D.load_tasks = lambda p: [task("AUTO-32", "2026-06-24T11:00:00"), task("AUTO-31", "2026-06-24T11:00:00")]
D.load_dismissed = lambda p: dict(DISMISSED)
s = needs.summary(cfg)
chk("dismissing AUTO-32 hides ONLY it — AUTO-31 (the one to answer) survives, panel NOT wiped",
    [t["ticket_id"] for t in s["tasks"]] == ["AUTO-31"], str([t["ticket_id"] for t in s["tasks"]]))

print("\n============ DISMISS QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
