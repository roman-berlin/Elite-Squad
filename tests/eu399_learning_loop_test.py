"""EU-399 — the learning loop learns from MORE than the 8 hardcoded reviewer-rejection themes.

The "memory compounds every cycle" mechanism (consolidate) used to only fold reviewer-rejection
keyword themes. The failure classes that actually dominate the live log — needs_human reasons,
normalized ticket_exception signatures, and stuck gate fingerprints — never became lessons. A class
of escalation repeating across many tickets produced zero standing guidance for the Planner/Builder.

This harness pins the new behaviour:
  * needs_human events are bucketed by `reason`; ticket_exception by `forensics.signature_key(error)`
    (the existing normalizer); gate_fingerprint_stuck by `fingerprint`.
  * a bucket recurring across >=3 DISTINCT tickets yields exactly ONE lesson (deduped); below the
    threshold yields none.
  * run() folds those lessons into the live log with the same cap/idempotence discipline as the
    rejection lessons, and memory.preamble() (what the Planner/Builder actually carry) shows them.
"""
import sys, types, tempfile, json
from pathlib import Path

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

from orchestrator import consolidate, memory, forensics
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
memory.UNIT_PATH = tmp / "UNIT.md"
memory.LIVE_PATH = tmp / "UNIT.live.md"
memory._BACKUPS = tmp / "backups"
memory.UNIT_PATH.write_text("## Doctrine\n\n- Be excellent.\n", encoding="utf-8")

# --- build a synthetic audit covering all three classes + below-threshold controls ---
audit = tmp / "audit.jsonl"
def ev(**kw):
    with audit.open("a") as f:
        f.write(json.dumps(kw) + "\n")

# needs_human by reason --------------------------------------------------------
# turn-limit across 3 DISTINCT tickets -> the only >=3 needs_human bucket
for tid in ("EU-1", "EU-2", "EU-3"):
    ev(event="needs_human", ticket_id=tid, reason="turn-limit", question="ran out of turns")
# product blocker across only 2 tickets -> below threshold
for tid in ("EU-4", "EU-5"):
    ev(event="needs_human", ticket_id=tid, reason="product blocker — escalated to Commander")
# one ticket hitting "solo-flake" 5 times -> below threshold (pins the DISTINCT-TICKETS semantic:
# raw occurrence count alone must NOT produce a lesson)
for _ in range(5):
    ev(event="needs_human", ticket_id="EU-6", reason="solo-flake")
# a needs_human with NO reason field is not bucketable by reason -> ignored, never a lesson
ev(event="needs_human", ticket_id="EU-7", question="no reason field at all")

# ticket_exception by normalized signature ------------------------------------
# the SAME infra error across 3 distinct tickets -> signature_key folds them to one bucket
SIG_ERR = "HTTPSConnectionPool(host='toibis.atlassian.net'): NameResolutionError(ValueError)"
for tid in ("EU-8", "EU-9", "EU-10"):
    ev(event="ticket_exception", ticket_id=tid, app="automatixy", error=SIG_ERR)
# two DIFFERENT error signatures, one ticket each -> no bucket reaches 3
ev(event="ticket_exception", ticket_id="EU-11", app="automatixy", error="KeyError: 'missing-key'")
ev(event="ticket_exception", ticket_id="EU-12", app="automatixy", error="ValueError: bad value")

# gate_fingerprint_stuck by fingerprint ---------------------------------------
for tid in ("EU-13", "EU-14", "EU-15"):
    ev(event="gate_fingerprint_stuck", ticket_id=tid, iteration=2, fingerprint="tsc:a11y")
for tid in ("EU-16", "EU-17"):
    ev(event="gate_fingerprint_stuck", ticket_id=tid, iteration=2, fingerprint="vitest:flake")

cfg = Config(apps=[], audit_path=str(audit))

# --- recurrence_lessons(): deterministic bucketing ---------------------------
lessons = consolidate.recurrence_lessons(cfg)
by_kind: dict[str, list[dict]] = {}
for l in lessons:
    by_kind.setdefault(l["kind"], []).append(l)

chk("needs_human: exactly one lesson (only turn-limit reaches >=3)",
    len(by_kind.get("needs_human", [])) == 1, str(by_kind.get("needs_human")))
nh = by_kind["needs_human"][0]
chk("needs_human lesson buckets on the reason", nh["key"] == "turn-limit", str(nh))
chk("needs_human counts DISTINCT tickets (3)", nh["count"] == 3, str(nh))
chk("needs_human lists the offending tickets", set(nh["tickets"]) == {"EU-1", "EU-2", "EU-3"}, str(nh))
chk("needs_human carries actionable guidance", nh["action"] and "turn" in nh["action"].lower())

chk("ticket_exception: exactly one lesson",
    len(by_kind.get("ticket_exception", [])) == 1, str(by_kind.get("ticket_exception")))
ex = by_kind["ticket_exception"][0]
chk("ticket_exception lesson key IS the normalized signature (reuses forensics.signature_key)",
    ex["key"] == forensics.signature_key(SIG_ERR), str(ex))
chk("ticket_exception counts 3 distinct tickets", ex["count"] == 3, str(ex))
chk("ticket_exception lists EU-8/9/10", set(ex["tickets"]) == {"EU-8", "EU-9", "EU-10"}, str(ex))

chk("gate_stuck: exactly one lesson",
    len(by_kind.get("gate_stuck", [])) == 1, str(by_kind.get("gate_stuck")))
gs = by_kind["gate_stuck"][0]
chk("gate_stuck buckets on the fingerprint", gs["key"] == "tsc:a11y", str(gs))
chk("gate_stuck counts 3 distinct tickets", gs["count"] == 3, str(gs))

chk("total = exactly 3 recurring lessons (one per recurring class)", len(lessons) == 3, str(lessons))
chk("sorted by count desc", [l["count"] for l in lessons] == sorted([l["count"] for l in lessons], reverse=True))

# --- below-threshold buckets produce NO lesson -------------------------------
chk("product blocker (2 tickets) yields no lesson",
    not any(l["kind"] == "needs_human" and "product blocker" in l["key"] for l in lessons))
chk("solo-flake (1 ticket x5 occurrences) yields no lesson — DISTINCT-TICKETS semantic",
    not any(l["key"] == "solo-flake" for l in lessons))
chk("reasonless needs_human yields no lesson",
    not any(l["kind"] == "needs_human" and l.get("key", "") == "" for l in lessons))
chk("two different exception signatures (1 ticket each) yield no lesson",
    not any(l["kind"] == "ticket_exception" and l["count"] < 3 for l in lessons))
chk("vitest:flake fingerprint (2 tickets) yields no lesson",
    not any(l["key"] == "vitest:flake" for l in lessons))

# --- threshold is exactly 3 distinct tickets ---------------------------------
chk("threshold pin: min_tickets=4 drops the turn-limit bucket (only 3 tickets)",
    not any(l["kind"] == "needs_human" and l["key"] == "turn-limit"
            for l in consolidate.recurrence_lessons(cfg, min_tickets=4)))
chk("threshold pin: min_tickets=3 keeps it",
    any(l["kind"] == "needs_human" and l["key"] == "turn-limit"
        for l in consolidate.recurrence_lessons(cfg, min_tickets=3)))

# --- harness pin: a SINGLE 3x recurrence produces EXACTLY one lesson ---------
audit_one = tmp / "audit_one.jsonl"
for tid in ("Y-1", "Y-2", "Y-3"):
    with audit_one.open("a") as f:
        f.write(json.dumps({"event": "needs_human", "ticket_id": tid, "reason": "only-reason"}) + "\n")
cfg_one = Config(apps=[], audit_path=str(audit_one))
one = consolidate.recurrence_lessons(cfg_one)
chk("harness pin: a synthetic 3x recurrence produces EXACTLY one lesson", len(one) == 1, str(one))
chk("harness pin: the lesson is the recurring bucket",
    one and one[0]["kind"] == "needs_human" and one[0]["key"] == "only-reason")

# --- harness pin: below threshold produces NONE ------------------------------
audit_below = tmp / "audit_below.jsonl"
for tid in ("X-1", "X-2"):
    with audit_below.open("a") as f:
        f.write(json.dumps({"event": "needs_human", "ticket_id": tid, "reason": "rare-reason"}) + "\n")
cfg_below = Config(apps=[], audit_path=str(audit_below))
chk("harness pin: below threshold (2 tickets) produces NO lesson",
    consolidate.recurrence_lessons(cfg_below) == [])

# --- run(): folds recurrence lessons into the live log, idempotent, cap-safe -
memory.LIVE_PATH.write_text("## Lessons & Decisions\n\n- 2026-06-10: Keep PRs small\n", encoding="utf-8")
r1 = consolidate.run(cfg)
recurred_added = [a for a in r1["added"] if "recurred" in a.lower()]
chk("run: folded the 3 recurrence lessons into the log", len(recurred_added) == 3, str(r1["added"]))
chk("run: return carries the recurrences it found", len(r1.get("recurrences", [])) == 3, str(r1.get("recurrences")))
log = memory.LIVE_PATH.read_text()
chk("run: needs_human lesson written", "Needs-human reason 'turn-limit'" in log)
chk("run: ticket_exception lesson written", "Error signature" in log)
chk("run: gate_stuck lesson written", "Stuck gate fingerprint" in log)
chk("run: cites an offending ticket", "EU-1" in log)
chk("run: pre-existing lesson preserved", "Keep PRs small" in log)
chk("run: wrote the file", r1["written"])
# idempotent — re-running must not duplicate any recurrence lesson
r2 = consolidate.run(cfg)
chk("run: idempotent — no duplicate recurrence lessons on re-run",
    not any("recurred" in a.lower() for a in r2["added"]), str(r2["added"]))

# --- AC #2: the Planner/Builder preamble carries these lessons (bounded) -----
# planner.py / builder.py consume memory.preamble(); the lessons live in the live log it inlines.
pre = memory.preamble()
chk("preamble carries the needs_human recurrence lesson", "Needs-human reason 'turn-limit'" in pre)
chk("preamble carries the gate_stuck recurrence lesson", "Stuck gate fingerprint" in pre)
chk("preamble still carries the doctrine", "Be excellent" in pre)

# below-threshold recurrence must NOT reach the preamble
memory.LIVE_PATH.write_text("## Lessons & Decisions\n\n- 2026-06-10: Keep PRs small\n", encoding="utf-8")
rb = consolidate.run(cfg_below)
chk("below threshold: run adds nothing", rb["added"] == [], str(rb["added"]))
chk("below threshold: preamble does not carry the rare-reason lesson",
    "rare-reason" not in memory.preamble())

print("\n=============== EU-399 LEARNING LOOP QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
