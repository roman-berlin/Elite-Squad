"""Phone-brevity QA: officer reports pushed to Telegram are skimmable, not walls of text, and every
'needs you' question tells the Commander exactly how to answer it.

- notify.bulletize() folds a report into a COMPLETE, phone-skimmable bullet list — every decision /
  action item / blocker as its own '•' line — with NO "…(full report in the cockpit)" truncate-and-punt
  footer. It is the deterministic fallback behind notify.report_brief() (the cheap-model distiller), so a
  report is summarised, never lost. (EU-62 retired the old clip() truncate-and-punt on report sends.)
- decisions.reply_hint() is the SINGLE source of truth for the Telegram reply syntax, and stays in
  lock-step with parse_reply(): 'TICKET-ID: <decision>' targets one stacked question; a bare reply
  answers the oldest. (This is the gap that confused the Commander when 3 questions were stacked.)"""
import sys, types
from pathlib import Path

req = types.ModuleType("requests")
req.RequestException = Exception
req.post = lambda *a, **k: types.SimpleNamespace(status_code=200)
req.get = lambda *a, **k: types.SimpleNamespace(status_code=200, json=lambda: {})
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import notify, decisions

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- notify.bulletize: a report becomes a COMPLETE bullet brief, never truncated-and-punted ---
short = "All green. Two tickets landed on DEV."
sb = notify.bulletize(short)
chk("short report becomes clean bullets", sb.startswith("• ") and "All green" in sb)
chk("bulletize never adds a 'cockpit' punt footer", "cockpit" not in sb.lower())

report = ("Decision: adopt jest-axe for a11y. Action item: Builder adds the dependency. "
          "Action item: Scout wires the smoke test. Blocker: staging secret missing.")
rb = notify.bulletize(report)
chk("bulletize splits into one bullet per point", rb.count("•") >= 4, rb)
chk("bulletize keeps every key item (decision/action/blocker survive)",
    "jest-axe" in rb and "Builder" in rb and "Scout" in rb and "Blocker" in rb)
chk("bulletize adds no truncate-and-punt footer",
    "cockpit" not in rb.lower() and "continue in the" not in rb.lower())

# a long report is summarised to a phone size, NOT cut off with a 'more in the cockpit' footer
long = ("The unit reviewed seventeen tickets today and reached a decision on each one. " * 12).strip()
lb = notify.bulletize(long)
chk("long report stays phone-skimmable", len(lb) <= 1060, f"len={len(lb)}")
chk("long report carries no cockpit footer", "cockpit" not in lb.lower())
chk("bulletize output is a genuine bullet list",
    all(l.startswith("• ") for l in lb.splitlines() if l.strip()))

# --- decisions.reply_hint: the answer-this-question one-liner ---
hint_id = decisions.reply_hint("AUTO-32")
chk("per-ticket hint names the ticket to target", "AUTO-32" in hint_id)
chk("per-ticket hint explains a bare reply hits the oldest", "oldest" in hint_id.lower())
generic = decisions.reply_hint()
chk("generic hint shows the TICKET-ID: prefix form", "TICKET-ID" in generic and "oldest" in generic.lower())

# --- the hint stays in lock-step with the parser it documents ---
chk("parse_reply round-trips the documented 'TICKET: decision' form",
    decisions.parse_reply("AUTO-32: yes recruit him") == ("AUTO-32", "yes recruit him"))
chk("a bare reply parses as 'answer the oldest' (no ticket id)",
    decisions.parse_reply("yes recruit him") == (None, "yes recruit him"))

print("\n============ PHONE-BREVITY QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
