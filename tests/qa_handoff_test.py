"""QA hand-off comment QA: when a ticket merges -> QA, the Jira comment must be a BRIEF 'what was done'
plus the DEV test link — not the reviewer's full PASS essay (the wall of text Roman flagged)."""
import sys, types
sys.path.insert(0, ".")
from orchestrator import dashboard as D

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# A realistic reviewer essay (the kind that used to get dumped wholesale into the QA comment).
ESSAY = (
    "This is a well-executed visual polish pass over the existing date-grouping microsite feature, "
    "directly satisfying the ticket and the Commander's 'make it more beautiful' note: full-width pastel "
    "date banners with icon badges, a soft page gradient, a restructured PostCard that promotes departure "
    "date to its own prominent row alongside nights/city, destination emojis, and an emphasized price with "
    "a CTA pill. Existing functionality is preserved (single Link click target verified by a new test; "
    "contact/WhatsApp components untouched), RTL and accessibility are handled correctly (emojis "
    "aria-hidden, valid dl/dt/dd, sr-only labels), and new tests cover the added rows. The only items are "
    "minor: the secondary return-date range line was intentionally dropped from the card, and a contrast "
    "spot-check on the rotating pastel banner themes is advisable. No blocker or major issues — PASS."
)

wd = D.brief(ESSAY, n=220)
chk("brief shrinks the essay", len(wd) <= 240 and len(wd) < len(ESSAY), f"len={len(wd)}")
chk("brief keeps the opening (what was done)", "visual polish pass" in wd)
chk("brief drops the tail essay", "No blocker or major issues" not in wd and "contrast spot-check" not in wd)

# The comment format produced in loop._land (mirrored here).
base, turl = "DEV", "https://dev.automatixy.app/m/dana-tours"
test_line = f"\n🔗 Test on {base}: {turl}"
comment = f"✅ Merged to {base} — moved to QA.\nWhat was done: {wd}{test_line}"

chk("comment carries the DEV test link", turl in comment and "🔗 Test on DEV" in comment)
chk("comment is brief — no full essay", "restructured PostCard" not in comment and len(comment) < 420)
chk("comment leads with merge + QA", comment.startswith("✅ Merged to DEV — moved to QA."))
chk("comment names what was done", "What was done:" in comment)

# No test link configured (qa_url unset) -> the link line is simply absent, comment still brief.
no_link = f"✅ Merged to {base} — moved to QA.\nWhat was done: {wd}" + ("" if False else "")
chk("graceful when no test URL", "🔗" not in no_link and "What was done:" in no_link)

print("\n============ QA HAND-OFF QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
