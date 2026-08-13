"""EU-589: filing gate — the reviewer decides KEEP or CANCEL per finding, the loop caps the
volume per build, and every decision is announced in Telegram.

Pins the advisory-ship filing logic in loop.py (no network, no real models):
  1. CANCEL / non-ticket-worthy findings are NOT filed — they come back as (title, reason)
     pairs for finding_cancelled audit records, so nothing silently vanishes.
  2. GIVEN N KEEP findings and cap K < N: exactly K proposals (highest severity first) and the
     overflow folds into ONE digest Task; the digest carries the origin ticket id + anchors.
  3. Minors file as Task (not Bug); every synthesized body carries the origin ticket id, the
     reviewer's file:line anchor, a one-line AC, and the files the origin build changed.
  4. Old-style findings (no new schema fields) default to KEEP + ticket-worthy (back-compat).
  5. _format_filing_decisions renders human Telegram lines: '📋 kept: X — reason' /
     '🗑 cancelled: X — reason', with no dangling dash when the reason is empty.
  6. reviewer._parse picks the new per-issue fields out of the verdict JSON and defaults them
     safely when absent.

Import / stub pattern matches eu215_advisory_ship_test.py.
"""
import sys
import types

# ---------------------------------------------------------------------------
# Stub the Agent SDK so importing the orchestrator never reaches the network.
# ---------------------------------------------------------------------------
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        s.__dict__.update(k)

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop
import orchestrator.reviewer as reviewer_mod
from orchestrator.contracts import QualityIssue

results = []


def chk(name, cond, detail=""):
    results.append((name, bool(cond), detail))


# ===========================================================================
# 1. CANCEL / non-ticket-worthy findings are not filed; they come back for the
#    audit trail with their reason preserved.
# ===========================================================================
issues_1 = [
    QualityIssue(severity="minor", area="style", detail="rename var x",
                 verdict="CANCEL", reason="pure style preference", ticket_worthy=False,
                 location="foo.py:42"),
    QualityIssue(severity="minor", area="tests", detail="accepted 2s timeout, bounded",
                 verdict="KEEP", reason="", ticket_worthy=False, location="foo.py:50"),
    QualityIssue(severity="major", area="tests", detail="missing error path test",
                 verdict="KEEP", reason="covers failure mode", ticket_worthy=True,
                 location="bar.py:100"),
]
proposals_1, cancelled_1, digest_1, deferred_1 = loop._advisory_ship_decide(
    issues_1, "EU-589", "test ticket", "add error handling", ["bar.py"], cap=5,
)
chk("1a: only the KEEP+ticket-worthy issue becomes a proposal",
    len(proposals_1) == 1, f"got {len(proposals_1)} proposals")
chk("1b: both non-worthy issues land in the cancelled list",
    len(cancelled_1) == 2, f"got {len(cancelled_1)} cancelled")
chk("1c: cancelled titles carry severity/area",
    all("[MINOR/" in t for t, _ in cancelled_1), str(cancelled_1))
chk("1d: the reviewer's CANCEL reason is preserved",
    any("pure style" in r for _, r in cancelled_1), str(cancelled_1))
chk("1e: a ticket_worthy=False KEEP gets a default rationale",
    any("not ticket-worthy" in r for _, r in cancelled_1), str(cancelled_1))
chk("1f: no digest / no deferrals under the cap",
    digest_1 is None and deferred_1 == 0, f"digest={digest_1} deferred={deferred_1}")

# ===========================================================================
# 2. Cap overflow: K proposals highest-severity-first + ONE digest Task for the rest.
# ===========================================================================
cap = 3
issues_2 = [QualityIssue(
    severity=("blocker", "major", "minor")[i % 3],
    area=f"area{i}",
    detail=f"finding {i}",
    verdict="KEEP", reason=f"reason {i}", ticket_worthy=True,
    location=f"file{i}.py:{i * 10 + 1}",
) for i in range(7)]

proposals_2, cancelled_2, digest_2, deferred_2 = loop._advisory_ship_decide(
    issues_2, "EU-589-cap", "cap test", "one line ac", ["f.py"], cap=cap,
)
chk("2a: nothing cancelled when everything is KEEP+worthy",
    cancelled_2 == [], str(cancelled_2))
chk("2b: exactly cap direct proposals", len(proposals_2) == cap,
    f"got {len(proposals_2)}, expected {cap}")
_order = {"BLOCKER": 0, "MAJOR": 1, "MINOR": 2}
sevs = [p.get("severity", "") for p in proposals_2]
chk("2c: proposals are highest-severity-first",
    sevs == sorted(sevs, key=lambda s: _order.get(s, 99)), f"order: {sevs}")
chk("2d: overflow produces ONE digest Task", digest_2 is not None and deferred_2 == 4,
    f"digest={str(digest_2)[:80]} deferred={deferred_2}")
if digest_2:
    chk("2e: digest is a Task titled with the deferred count + origin ticket id",
        digest_2.get("type") == "Task" and "[digest]" in digest_2.get("title", "")
        and "4" in digest_2.get("title", "") and "EU-589-cap" in digest_2.get("title", ""),
        digest_2.get("title", ""))
    # severity cycle is blocker/major/minor over i=0..6, so after the severity sort the three
    # blockers (i=0,3,6) are filed and the rest (i=1,2,4,5) are deferred into the digest
    chk("2f: digest body lists every DEFERRED finding with its location anchor",
        all(f"finding {i}" in digest_2.get("body", "") for i in (1, 2, 4, 5))
        and "file1.py:11" in digest_2.get("body", "")
        and "file5.py:51" in digest_2.get("body", ""),
        digest_2.get("body", ""))
    chk("2g: the FILED findings are not duplicated into the digest",
        all(f"finding {i}" not in digest_2.get("body", "") for i in (0, 3, 6)),
        digest_2.get("body", ""))

# ===========================================================================
# 3. Minors file as Task; bodies carry the origin ticket id, anchor, AC, files.
# ===========================================================================
issues_3 = [
    QualityIssue(severity="minor", area="style", detail="format fix",
                 verdict="KEEP", reason="lint compliance", ticket_worthy=True,
                 location="app/auth.py:12"),
    QualityIssue(severity="major", area="security", detail="missing auth check",
                 verdict="KEEP", reason="auth bypass risk", ticket_worthy=True,
                 location="app/auth.py:99"),
]
proposals_3, _, _, _ = loop._advisory_ship_decide(
    issues_3, "EU-589-type", "origin summary", "ensure auth is required", ["app/auth.py"], cap=10,
)
minor_p = [p for p in proposals_3 if p.get("severity") == "MINOR"][0]
major_p = [p for p in proposals_3 if p.get("severity") == "MAJOR"][0]
chk("3a: minor files as Task (not Bug)", minor_p.get("type") == "Task", minor_p.get("type"))
chk("3b: major files as Bug", major_p.get("type") == "Bug", major_p.get("type"))
_body = minor_p.get("body", "")
chk("3c: body carries the origin ticket id AND summary",
    "Origin: EU-589-type — origin summary" in _body, _body)
chk("3d: body carries the file:line anchor", "@app/auth.py:12" in _body, _body)
chk("3e: body carries the one-line AC", "ensure auth is required" in _body, _body)
chk("3f: body carries files_changed", "Files changed: app/auth.py" in _body, _body)
chk("3g: body carries the reviewer's KEEP rationale", "Reviewer rationale: lint compliance" in _body,
    _body)
chk("3h: the proposal keeps the reason for the Telegram line",
    minor_p.get("reason") == "lint compliance" and major_p.get("reason") == "auth bypass risk",
    f"{minor_p.get('reason')} / {major_p.get('reason')}")

# ===========================================================================
# 4. Back-compat: old-style findings (no new fields) default to KEEP + worthy.
# ===========================================================================
old_issues = [
    QualityIssue(severity="minor", area="style", detail="old-style find"),
    QualityIssue(severity="major", area="tests", detail="another old find"),
]
proposals_4, cancelled_4, digest_4, deferred_4 = loop._advisory_ship_decide(
    old_issues, "OLD-1", "legacy ticket", "legacy AC", ["legacy.py"], cap=5,
)
chk("4a: old-style issues all file (KEEP default)", len(proposals_4) == 2,
    f"got {len(proposals_4)} proposals")
chk("4b: old-style issues are never cancelled", cancelled_4 == [], str(cancelled_4))
chk("4c: old-style bodies still carry the origin id",
    all("Origin: OLD-1" in p.get("body", "") for p in proposals_4), proposals_4[0].get("body", ""))

# ===========================================================================
# 5. cap=0 folds EVERYTHING into the digest (legal knob setting).
# ===========================================================================
proposals_5, cancelled_5, digest_5, deferred_5 = loop._advisory_ship_decide(
    issues_2, "EU-589-zero", "zero cap", "ac", ["f.py"], cap=0,
)
chk("5a: cap=0 files no direct proposals", proposals_5 == [], str(proposals_5))
chk("5b: cap=0 folds all 7 into the digest",
    digest_5 is not None and deferred_5 == 7 and "7" in digest_5.get("title", ""),
    f"deferred={deferred_5} title={digest_5.get('title', '') if digest_5 else None}")

# ===========================================================================
# 6. Telegram line formatting — one human line per decision.
# ===========================================================================
tg = loop._format_filing_decisions([
    ("kept", "EU-9001", "covers failure mode"),
    ("kept", "[MINOR/style] no reason finding", ""),
    ("cancelled", "[MINOR/style] rename var x", "pure style preference"),
])
lines = tg.splitlines()
chk("6a: kept line names the key and the reason",
    lines[0] == "📋 kept: EU-9001 — covers failure mode", lines[0])
chk("6b: a kept line without reason has NO dangling dash",
    lines[1] == "📋 kept: [MINOR/style] no reason finding", lines[1])
chk("6c: cancelled line carries title + reason",
    lines[2] == "🗑 cancelled: [MINOR/style] rename var x — pure style preference", lines[2])

# ===========================================================================
# 7. reviewer._parse picks up the new per-issue schema (with safe defaults).
# ===========================================================================
_rich = reviewer_mod._parse('```json\n{"verdict":"PASS","spec_conformance":{"met":true,"gaps":[]},'
                            '"quality":{"issues":['
                            '{"severity":"minor","area":"style","detail":"pure style",'
                            ' "verdict":"cancel","reason":"sanctioned by AC",'
                            ' "ticket_worthy":false,"location":"a/b.py:10"},'
                            '{"severity":"major","area":"tests","detail":"no anchor given"}'
                            ']},"required_changes":[],"needs_human":false,"question":"",'
                            '"summary":"ok"}\n```')
chk("7a: parse yields both issues", len(_rich.quality_issues) == 2,
    f"got {len(_rich.quality_issues)}")
if len(_rich.quality_issues) == 2:
    q0, q1 = _rich.quality_issues
    chk("7b: explicit CANCEL parsed uppercased with reason + anchor",
        q0.verdict == "CANCEL" and q0.reason == "sanctioned by AC"
        and q0.ticket_worthy is False and q0.location == "a/b.py:10", str(vars(q0)))
    chk("7c: absent fields default to KEEP / ticket-worthy / empty anchor",
        q1.verdict == "KEEP" and q1.ticket_worthy is True and q1.location == ""
        and q1.reason == "", str(vars(q1)))

# ===========================================================================
# Results
# ===========================================================================
passed = sum(1 for _, ok, _ in results if ok)
print("\n========== EU-589 ADVISORY FILING QA ==========")
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
