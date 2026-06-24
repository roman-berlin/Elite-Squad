"""Phone-brevity QA: officer reports pushed to Telegram are skimmable, not walls of text, and every
'needs you' question tells the Commander exactly how to answer it.

- notify.clip() trims a long report to a phone-sized window, cutting at a word/sentence boundary
  (never mid-word) and pointing at the cockpit for the full text — short reports pass through untouched.
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

norm = lambda s: " ".join(s.split())

# --- notify.clip: short text is untouched (no footer, no truncation) ---
short = "All green. Two tickets landed on DEV."
chk("short report passes through unchanged", notify.clip(short) == short)
chk("short report gets no 'cockpit' footer", "cockpit" not in notify.clip(short))

# --- long text is trimmed to the window, with a 'full in cockpit' footer ---
long = ("The unit reviewed seventeen tickets today and reached a decision on each one. " * 12).strip()
out = notify.clip(long, 700, "__END__")
body = out[: -len("\n__END__")]
chk("long report is trimmed below the window + slack", len(out) <= 700 + 40)
chk("long report ends with the cockpit footer", out.endswith("__END__"))
chk("trimmed body is a true prefix (no mangled/duplicated chars)", norm(long).startswith(norm(body)))
chk("cut lands on a word boundary, never mid-word",
    len(body) >= len(long) or long[len(body)] in " \n.,;!?")
chk("default footer points the Commander at the cockpit", "cockpit" in notify.clip(long))

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
