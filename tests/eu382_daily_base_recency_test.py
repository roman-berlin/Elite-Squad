"""EU-382 — the daily must not headline a STALE base-red.

Live incident (2026-07-17, verifying the daily brief): the brief's FOCUS read "Break the 'base dev
RED' logjam (24+ tickets stuck on it)" — but dev was GREEN (tip 74678bb, 382/382, 9/10 consecutive
green runs that day). The last `red_base_block` audit event was 2026-07-16 10:26 — over 30 hours
stale, and MERGES had landed since. Root cause: the standup's fact set carried the red-base signal
(the loop's red-base pending decisions) with no recency bound — the same unbounded-history class
EU-358 fixed for `_recent_no_changes_ticket_ids` (48h window) and EU-336 fixed for the shipped
window.

Pins the fix in dashboard.standup / dashboard._active_base_red:

  1. A red_base_block OLDER than the latest green signal (a `merged` land, or `dev_gate` with
     passed=true) is RESOLVED — no base-red headline, and the loop's stale "Base branch … is RED
     before any build" pending decisions are held out of the brief (render-side only).
  2. A red_base_block NEWER than every green signal (and within the 48h window) IS the live base
     state — the brief carries a deterministic ⛔ base-red line and keeps the red-base decisions.
  3. `dev_gate passed=false` is NOT a green signal.
  4. EU-358 precedent: a red older than 48h never headlines, even with no green signal since
     (the audit never rotates — one ancient red must not dominate forever).

Written fail-first: against pre-EU-382 code the ⛔-line checks go RED (standup had no deterministic
base-state fact at all) and the stale-decision checks go RED (the 30h-stale red-base decision was
listed verbatim — exactly the fuel for the false FOCUS headline).
"""
import sys, types, tempfile, json as _json
from datetime import datetime, timedelta
from pathlib import Path

REPO = "."
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, REPO)

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator import dashboard
from orchestrator.config import Config

# The EXACT question text loop.py's red-base short-circuit writes via decisions.add (loop.py:1430).
_RED_Q = ("Base branch 'dev' is RED before any build — the gate fails on the clean base tree. "
          "Fix the base (or land the fix ticket) before re-queuing this one.\n\ngate report…")


def _seed(events: list[dict], pending: list[dict] | None = None) -> Config:
    d = tempfile.mkdtemp()
    ap = Path(d) / "audit.jsonl"
    with ap.open("w") as f:
        for ev in events:
            f.write(_json.dumps(ev) + "\n")
    if pending is not None:
        (ap.with_name("pending_decisions.json")).write_text(_json.dumps(pending))
    cfg = Config(apps=[])
    cfg.audit_path = str(ap)
    return cfg


def _ts(**delta) -> str:
    """Audit ts in the REAL writer's shape (second precision + tz offset — see audit.py:33)."""
    return (datetime.now().astimezone() - timedelta(**delta)).replace(
        microsecond=0).strftime("%Y-%m-%dT%H:%M:%S%z")


def _base_red_line(su: str) -> str:
    return next((l for l in su.splitlines() if l.startswith("⛔")), "")


# ── 1. LIVE-INCIDENT REPLAY: red 30h ago, merges since → the red is RESOLVED ──────────────────────
cfg = _seed(
    [
        {"ts": _ts(hours=31), "event": "ticket_start", "ticket_id": "EU-374", "app": "general"},
        {"ts": _ts(hours=30), "event": "red_base_block", "ticket_id": "EU-374", "fingerprint": "x"},
        {"ts": _ts(hours=5), "event": "ticket_start", "ticket_id": "EU-211", "app": "general"},
        {"ts": _ts(hours=2), "event": "merged", "ticket_id": "EU-211", "app": "general"},
    ],
    pending=[{"id": "EU-374", "question": _RED_Q}],
)
su = dashboard.standup(cfg)
check("a red_base_block OLDER than a later merge does NOT headline (no ⛔ base-red line)",
      _base_red_line(su) == "", su)
check("the stale red-base pending decision is held OUT of the brief (no 'RED before any build')",
      "RED before any build" not in su, su)
check("the brief says the base went green again instead (so the held decisions aren't invisible)",
      "green again" in su and "1 stale base-red decision" in su, su)
check("held-out decisions still point at the cockpit inbox", "/needs" in su, su)

# Render-side ONLY — the stored decision survives untouched for the cockpit (the EU-336/337 split).
from orchestrator import decisions
check("standup does NOT mutate pending_decisions.json — the red-base decision survives for /needs",
      decisions.load(cfg) and decisions.load(cfg)[0]["question"] == _RED_Q)

# ── 2. ACTIVE red: red NEWER than the last green → deterministic ⛔ line + decisions kept ─────────
cfg = _seed(
    [
        {"ts": _ts(hours=5), "event": "merged", "ticket_id": "EU-211", "app": "general"},
        {"ts": _ts(hours=3), "event": "red_base_block", "ticket_id": "EU-374", "fingerprint": "x"},
        {"ts": _ts(hours=2), "event": "red_base_block", "ticket_id": "EU-375", "fingerprint": "x"},
    ],
    pending=[{"id": "EU-374", "question": _RED_Q}],
)
su = dashboard.standup(cfg)
line = _base_red_line(su)
check("a red_base_block NEWER than every green signal DOES headline (⛔ base-red line present)",
      line != "", su)
check("the ⛔ line counts the tickets blocked in the CURRENT red episode (2 here)",
      "2 ticket(s)" in line, repr(line))
check("the ⛔ line names the latest blocked ticket", "EU-375" in line, repr(line))
# 2026-07-21: daily bullets render the BRIEF pipeline's summary (synthesized for the red-base
# class), not the raw question wall — the pin follows the wording, the behavior is unchanged.
check("with an ACTIVE base-red the red-base decision stays IN the brief",
      "parked while base" in su or "RED before any build" in su, su)
check("no bogus 'green again' note while the base is red", "green again" not in su, su)

# ── 3. dev_gate passed=true is a green signal; passed=false is NOT ────────────────────────────────
cfg = _seed([
    {"ts": _ts(hours=3), "event": "red_base_block", "ticket_id": "EU-374", "fingerprint": "x"},
    {"ts": _ts(hours=1), "event": "dev_gate", "ticket_id": "EU-211", "passed": True},
])
check("a LATER dev_gate passed=true resolves the red (no ⛔ line)",
      _base_red_line(dashboard.standup(cfg)) == "", dashboard.standup(cfg))

cfg = _seed([
    {"ts": _ts(hours=3), "event": "red_base_block", "ticket_id": "EU-374", "fingerprint": "x"},
    {"ts": _ts(hours=1), "event": "dev_gate", "ticket_id": "EU-211", "passed": False},
])
check("a LATER dev_gate passed=false does NOT resolve the red (⛔ line stays)",
      _base_red_line(dashboard.standup(cfg)) != "", dashboard.standup(cfg))

# ── 4. EU-358 precedent: a red older than 48h never headlines, green signal or not ────────────────
cfg = _seed([
    {"ts": _ts(days=3), "event": "red_base_block", "ticket_id": "EU-374", "fingerprint": "x"},
])
check("a red_base_block older than 48h does NOT headline even with NO green signal since",
      _base_red_line(dashboard.standup(cfg)) == "", dashboard.standup(cfg))

cfg = _seed([
    {"ts": _ts(hours=2), "event": "red_base_block", "ticket_id": "EU-374", "fingerprint": "x"},
])
check("a FRESH red with no green signal headlines (the window bounds staleness, not truth)",
      _base_red_line(dashboard.standup(cfg)) != "", dashboard.standup(cfg))

# ── 5. Drift guard: the filter marker matches what loop.py actually writes ────────────────────────
_loop_src = Path("orchestrator/loop.py").read_text(encoding="utf-8")
_marker = getattr(dashboard, "_RED_BASE_MARKER", None)   # getattr: pre-fix code lacks it → clean RED
check("the red-base decision marker dashboard filters on is the text loop.py writes",
      isinstance(_marker, str) and _marker in _loop_src and _marker in _RED_Q, repr(_marker))

print("\n========= EU-382 DAILY BASE-RED RECENCY QA =========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
