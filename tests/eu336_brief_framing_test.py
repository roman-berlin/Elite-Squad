"""EU-336 — daily-brief framing: a rolling shipped window + one-line decision items.

Two defects this pins (both live on dev before this land, Roman 2026-07-15: "the standup is
incorrect … improve the daily report"):

1. dashboard.standup bucketed shipped work by CALENDAR DAY — "Shipped to DEV yesterday (N)" was the
   headline and same-day merges were relegated to a conditional "✅ …and today so far" afterthought.
   An unattended unit does most of its work overnight, so its freshest merges (the EU-294 collapse
   landed 01:00–04:00) landed on the wrong side of midnight and were demoted out of the headline.

2. The "Awaiting your decision" section rendered p["question"] verbatim — the officer's full,
   often multi-paragraph text. Live example from the 2026-07-15 brief: the EU-139 entry rendered as
   a wall of reviewer analysis ("Looking at the evidence: 1. EU-139's actual fix works: …"),
   unreadable on a phone.

Written fail-first: against the pre-EU-336 code check #1 goes RED on the "last 24h" headline and the
decision checks go RED on the multi-paragraph blob.
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
    """An audit ts in the REAL writer's shape — second precision + tz offset, e.g.
    '2026-06-15T00:01:40+0300' (verified against state/audit.jsonl). A bare .isoformat() carries
    microseconds, which dashboard._parse_ts does not accept, and would silently seed an undateable
    run — the fixture would then pass/fail for the wrong reason. Tz-aware on purpose: it also
    exercises the EU-181 .astimezone() cross-host normalization the window edge depends on."""
    return (datetime.now().astimezone() - timedelta(**delta)).replace(
        microsecond=0).strftime("%Y-%m-%dT%H:%M:%S%z")


def _shipped_line(su: str) -> str:
    return next((l for l in su.splitlines() if l.startswith("✅")), "")


def _decision_section(su: str) -> tuple[str, list[str]]:
    """(header, body lines) of the 'Awaiting your decision' section — EVERY line after the header,
    not just the ones that happen to start with a bullet. Filtering on the bullet would let a leaked
    paragraph 3 hide from the 'exactly one line' assertion, which is the whole defect."""
    ls = su.splitlines()
    i = next(i for i, l in enumerate(ls) if "Awaiting your decision" in l)
    return ls[i], [l for l in ls[i + 1:] if l.strip()]


# ── AC1: a merge that landed a few hours ago is in the PRIMARY shipped line ────────────────────────
# Seeded at 03:00-ish (3h ago) — under the old calendar-day split this fell into "today", i.e. the
# conditional afterthought line, while the headline reported yesterday. It must now BE the headline.
cfg = _seed([
    {"ts": _ts(hours=5), "event": "ticket_start", "ticket_id": "EU-329", "app": "general"},
    {"ts": _ts(hours=3), "event": "merged", "ticket_id": "EU-329", "app": "general"},
])
su = dashboard.standup(cfg)
ship = _shipped_line(su)

check("an overnight merge (3h ago) is in the PRIMARY shipped line, not a secondary 'today so far'",
      "EU-329" in ship, f"shipped line={ship!r}")
check("the primary shipped line is framed as a ROLLING window (last 24h), not 'yesterday'",
      "24h" in ship.lower() and "yesterday" not in ship.lower(), f"shipped line={ship!r}")
check("the shipped COUNT includes the overnight merge", "(1)" in ship, f"shipped line={ship!r}")
check("the demoted '…and today so far' afterthought line is gone",
      "today so far" not in su.lower(), su[:220])

# The window is a window: a merge from 3 days ago must NOT be counted.
cfg = _seed([
    {"ts": _ts(days=3, hours=1), "event": "ticket_start", "ticket_id": "EU-100", "app": "general"},
    {"ts": _ts(days=3), "event": "merged", "ticket_id": "EU-100", "app": "general"},
    {"ts": _ts(hours=4), "event": "ticket_start", "ticket_id": "EU-330", "app": "general"},
    {"ts": _ts(hours=2), "event": "merged", "ticket_id": "EU-330", "app": "general"},
])
ship = _shipped_line(dashboard.standup(cfg))
check("the rolling window EXCLUDES an older merge (3d ago) — it is a window, not all-time history",
      "EU-330" in ship and "EU-100" not in ship and "(1)" in ship, f"shipped line={ship!r}")

# Bucketing still keys on MERGE time, not run start (EU-181 lineage): started 2d ago, merged 2h ago.
cfg = _seed([
    {"ts": _ts(days=2), "event": "ticket_start", "ticket_id": "EU-221", "app": "general"},
    {"ts": _ts(hours=2), "event": "merged", "ticket_id": "EU-221", "app": "general"},
])
ship = _shipped_line(dashboard.standup(cfg))
check("the window keys on MERGE time, not run start (started 2d ago, merged 2h ago → shipped)",
      "EU-221" in ship, f"shipped line={ship!r}")

# ── AC2: a 5-paragraph decision renders as ONE capped line ────────────────────────────────────────
_BLOB = (
    "Looking at the evidence: 1. EU-139's actual fix works: Both targeted tests pass and the "
    "behaviour is pinned by a new harness that goes red on a revert.\n\n"
    "2. The two 'failing' tests pass in isolation — they only fail under the full-suite runner, "
    "which points at cross-harness state bleed rather than a real regression.\n\n"
    "3. The reviewer flagged this as a blocker, but the blocker is the runner, not the change.\n\n"
    "4. Recommendation: land EU-139 and file the runner isolation as its own ticket.\n\n"
    "5. Alternatively hold EU-139 until the runner is fixed, which blocks 3 downstream tickets."
)
cfg = _seed(
    [{"ts": _ts(hours=2), "event": "ticket_start", "ticket_id": "EU-139", "app": "general"}],
    pending=[{"id": "EU-139", "question": _BLOB}],
)
su = dashboard.standup(cfg)
header, dec = _decision_section(su)

check("the 5-paragraph decision occupies EXACTLY ONE line of the brief", len(dec) == 1, str(dec))
if dec:
    body = dec[0].split("EU-139:", 1)[-1].strip()
    check("the multi-paragraph blob is truncated (no reviewer wall-of-text in the brief)",
          len(body) <= 120, f"{len(body)} chars: {body!r}")
    check("the truncated decision carries a truncation marker (…)", body.endswith("…"), repr(body))
    check("the truncated decision keeps the START of the question (it is a lead, not a hash)",
          body.startswith("Looking at the evidence"), repr(body))
check("no later paragraph leaks anywhere into the brief",
      "Recommendation" not in su and "Alternatively" not in su, su[-260:])
check("the decision section points at the /needs inbox (2026-07-19: always — answering "
      "happens there, so the affordance is standing, not clip-conditional)",
      "/needs" in header, repr(header))

# A SHORT question must render verbatim — no gratuitous marker, no clipping.
cfg = _seed(
    [{"ts": _ts(hours=2), "event": "ticket_start", "ticket_id": "EU-140", "app": "general"}],
    pending=[{"id": "EU-140", "question": "Ship the paid tier now?"}],
)
su = dashboard.standup(cfg)
header, dec = _decision_section(su)
check("a SHORT question renders verbatim with no truncation marker",
      len(dec) == 1 and dec[0].strip() == "• EU-140: Ship the paid tier now?", str(dec))
check("the /needs answer-pointer is standing even for a short question (2026-07-19 order)",
      "/needs" in header, repr(header))

# The stored decision text is untouched — this is render-side only (the split EU-337/77c2216 drew).
from orchestrator import decisions
cfg = _seed(
    [{"ts": _ts(hours=2), "event": "ticket_start", "ticket_id": "EU-139", "app": "general"}],
    pending=[{"id": "EU-139", "question": _BLOB}],
)
dashboard.standup(cfg)
check("standup does NOT mutate the stored decision — the full question survives for the cockpit",
      decisions.load(cfg)[0]["question"] == _BLOB)

print("\n========= EU-336 DAILY-BRIEF FRAMING QA =========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
