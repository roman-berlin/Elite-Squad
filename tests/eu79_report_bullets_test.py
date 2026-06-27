"""EU-79: officer reports lead with a tight bulleted 'what's wrong / what to do', not walls of prose.

Roman's symptom: the Builder/Reviewer/PM reports in the cockpit (and the Jira QA hand-off comment) were
paragraph walls — hard to scan "what's actually wrong". This ticket makes every officer verdict/summary
LEAD with ≤5 (PM: ≤3) tight bullets, keeps the full prose available on expand, and reformats the QA
hand-off comment to: one-line status → ≤3 'what was done' bullets → the DEV test link on its own line.

These checks pin the three layers so the wall-of-prose defect can't silently return:
  1. the officer PROMPTS/SCHEMA instruct a bulleted lead (builder / reviewer / pm),
  2. dashboard.bullets() distils any summary into ≤N tight bullets (reusing the officer's own bullets),
     and the QA hand-off comment in loop._land is built from it,
  3. the cockpit transcript render (dashboard._detail_html) preserves the bullet line structure,
     and the activity-feed strip still word-boundary-trims its one-liner (EU-33 — must not regress).
"""
import datetime as _dt
import pathlib
import sys
import types

sys.path.insert(0, ".")

# Stub the Agent SDK + requests so the officer/render modules import without network or real models.
_sdk = types.ModuleType("claude_agent_sdk")
class _Dummy:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda n: _Dummy
sys.modules["claude_agent_sdk"] = _sdk
_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules.setdefault("requests", _req)

from orchestrator import builder, dashboard as D, pm, reviewer, warroom

_ORCH = pathlib.Path(D.__file__).resolve().parent
_DASH_SRC = (_ORCH / "dashboard.py").read_text()
_LOOP_SRC = (_ORCH / "loop.py").read_text()

results = []
def chk(name, cond, det=""):
    results.append((name, bool(cond), det))

# --- 1. the officer PROMPTS / SCHEMA instruct a bulleted lead -------------------------------------
chk("Builder summary must open with ≤5 tight bullets",
    "≤5 tight bullets" in builder.BUILDER_SYSTEM and "what was done" in builder.BUILDER_SYSTEM.lower())
chk("Builder bullets cover the gap + what-changed too",
    "gap" in builder.BUILDER_SYSTEM.lower() and "what changed" in builder.BUILDER_SYSTEM.lower())
chk("Reviewer verdict summary is ≤5 tight bullets leading with the blocking issue",
    "≤5 tight bullets" in reviewer.REVIEWER_SYSTEM and "blocking issue" in reviewer.REVIEWER_SYSTEM)
chk("PM DECIDE leads with ≤3 tight bullets (Decision / Rationale / Action)",
    "≤3 tight bullets" in pm.PM_SYSTEM and "Decision:" in pm.PM_SYSTEM and "Action:" in pm.PM_SYSTEM)

# --- 2. dashboard.bullets(): distil any summary into ≤N tight, scannable bullets -------------------
# (a) a summary that already leads with bullets -> reuse those lines verbatim (≤ limit of them)
rev = ("• Missing tenant filter on the /leads query\n"
       "• No test covers the error path\n"
       "• Otherwise a clean, in-scope diff")
chk("bullets() reuses the officer's own bullet lines",
    D.bullets(rev, limit=3, width=200).splitlines() == [
        "• Missing tenant filter on the /leads query",
        "• No test covers the error path",
        "• Otherwise a clean, in-scope diff"], D.bullets(rev))

# (b) a PROSE essay (the EU-73 wall) -> split into ≤limit sentence bullets, flattery tail dropped
essay = ("This is a well-executed visual polish pass over the date-grouping microsite, directly "
         "satisfying the ticket. Existing functionality is preserved. RTL and accessibility are "
         "handled correctly. No blocker or major issues — a solid, well-tested PASS that satisfies "
         "every acceptance criterion.")
pb = D.bullets(essay, limit=3, width=200)
chk("bullets() turns a prose essay into a bullet list", pb.count("•") == 3, pb)
chk("bullets() leads with what-was-done, drops the flattery tail",
    "visual polish pass" in pb and "solid, well-tested" not in pb and "satisfies every" not in pb)

# (c) more bullets than the limit -> capped; (d) empty -> a single safe placeholder, never blank
chk("bullets() caps at the limit", D.bullets("\n".join(f"• item {i}" for i in range(6)), limit=3).count("•") == 3)
chk("bullets() never returns empty", D.bullets("") == "• (no summary)" and D.bullets("   ") == "• (no summary)")
# (e) each bullet is word-boundary trimmed to width (no mid-word cut)
longbul = D.bullets("• " + "word " * 80, limit=1, width=40)
chk("bullets() word-boundary-trims an over-long bullet",
    longbul.endswith("…") and "wor…" not in longbul and len(longbul) <= 44, longbul)

# --- 2b. the QA hand-off comment in loop._land is built from bullets() (no prose essay) -----------
# Mirror the exact comment loop._land constructs, using the real helper.
base, turl = "dev", "https://dev.automatixy.app/leads"
whatdone = D.bullets(essay, limit=3, width=200)
comment = f"✅ Merged to {base} → moved to QA.\nWhat was done:\n{whatdone}\n🔗 Test on {base}: {turl}"
chk("QA hand-off leads with a one-line merge → QA status", comment.startswith("✅ Merged to dev → moved to QA."))
chk("QA hand-off 'What was done' is ≤3 tight bullets", "What was done:\n• " in comment and whatdone.count("•") <= 3)
chk("QA hand-off puts the DEV test link on its own line", f"\n🔗 Test on {base}: {turl}" in comment)
chk("QA hand-off carries no prose-essay tail", "solid, well-tested" not in comment and "satisfies every" not in comment)
# Source guard: loop._land routes the hand-off through bullets(), not a single brief() blob — so a
# revert to the prose/one-blob format is caught here (the behavioural mirror above can't see that).
chk("loop._land builds the hand-off from dashboard.bullets()",
    "_D.bullets(" in _LOOP_SRC and "What was done:" in _LOOP_SRC)

# --- 3. the cockpit transcript render preserves bullet line structure ------------------------------
task = {"ticket_id": "AUTO-12", "outcome": "merged→dev", "passes_list": [{
    "n": 1, "effort": "high", "tools": ["Read", "Edit"], "verdict": "FAIL",
    "build_summary": "• Added the tenant filter\n• Gap: error path untested\n• Changed: scoped the query",
    "review_summary": "• Missing tenant filter on /leads\n• No error-path test",
    "required_changes": ["Add the tenant filter", "Cover the error path"], "issues": [],
}]}
det = D._detail_html(task)
chk("transcript renders Builder + Reviewer summary blocks with the pre-wrap class",
    det.count('class="sub pre"') == 2, det[:120])
chk("transcript preserves the officer's bullet line structure (newlines survive escaping)",
    "• Added the tenant filter\n• Gap: error path untested" in det)
chk("transcript keeps the already-bulleted 'Reviewer asked for' list",
    "Reviewer asked for:" in det and "<li>Add the tenant filter</li>" in det)
chk("transcript CSS makes the summary blocks honour newlines",
    ".sub.pre{white-space:pre-wrap}" in _DASH_SRC)

# --- 3b. the activity-feed strip stays a trimmed one-liner (EU-33 must not regress) ----------------
now = _dt.datetime.now().astimezone()
cfg = types.SimpleNamespace(audit_path="/nonexistent/audit.jsonl",
                            apps=[types.SimpleNamespace(name="automatixy")])
long_note = ("The reviewer blocked this run because the tenant filter was missing on the leads query "
             "and several types were loosened to any during the build")
errored = [dict(ticket_id="AUTO-99", app="automatixy", outcome="errored",
                ended=now, started=now, note=long_note)]
fnote = next(f for f in warroom.feed(cfg, errored, None) if f["ticket"] == "AUTO-99")["text"]
chk("feed one-liner is word-boundary trimmed with an ellipsis (no bullet wall in the strip)",
    fnote.endswith("…") and "\n" not in fnote and " any" not in fnote, fnote)

print("\n============ EU-79 OFFICER-REPORT BULLETS QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
