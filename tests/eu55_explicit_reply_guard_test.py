"""EU-55 / F12(c) regression: the router guard `is_explicit_reply` decides whether a message
resolves a parked decision (pops the oldest pending one + rebuilds) or flows to the CTO chat.

The footgun the fix closes: while ANY question is parked, a normal chat sentence used to be
treated as the answer to the oldest pending decision. So the guard must return False for the
realistic free-text shapes a Commander actually types, and True only for the explicit forms
(`TICKET-ID: <decision>` or a leading reply marker). `_strip_reply_marker` must hand the inner
text to the resolver unchanged so an emoji/`re:` reply still parses.
"""
import sys, types
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

from orchestrator import decisions as D

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- free-text shapes that must NOT be routed to a parked decision (the whole point of F12) ---
not_replies = [
    "hey what's the eta on the dashboard?",
    "can you look at the budget panel tomorrow",
    "what is: the status of the migration",   # colon present, but multi-word head -> not a ticket id
    "ship it when green",
    "FYI the client wants dark mode",
]
for msg in not_replies:
    chk(f"free text NOT explicit: {msg!r}", D.is_explicit_reply(msg) is False, str(D.is_explicit_reply(msg)))

# --- explicit forms that MUST resolve a parked decision ---
explicit = ["AUTO-1: use DD/MM", "EU-9: ship it", "↩️ use DD/MM", "↩ use DD/MM",
            "re: use DD/MM", "RE: use DD/MM", "Reply: go with option B", "reply: option B"]
for msg in explicit:
    chk(f"explicit reply IS routed: {msg!r}", D.is_explicit_reply(msg) is True, str(D.is_explicit_reply(msg)))

# --- the marker must be stripped before the resolver parses the answer ---
chk("emoji marker stripped to bare answer", D._strip_reply_marker("↩️ use DD/MM") == "use DD/MM",
    D._strip_reply_marker("↩️ use DD/MM"))
chk("re: marker stripped to bare answer", D._strip_reply_marker("re: option B") == "option B",
    D._strip_reply_marker("re: option B"))
chk("RE: stripped case-insensitively", D._strip_reply_marker("RE: option B") == "option B",
    D._strip_reply_marker("RE: option B"))
# an id-form reply has no marker -> strip leaves it intact so parse_reply can split the id off
chk("id-form left intact by strip", D._strip_reply_marker("AUTO-1: use DD/MM") == "AUTO-1: use DD/MM",
    D._strip_reply_marker("AUTO-1: use DD/MM"))
# after stripping a marker, the remainder parses as a bare (id-less) answer -> resolve oldest pending
_id, _ans = D.parse_reply(D._strip_reply_marker("↩️ use DD/MM"))
chk("stripped emoji reply parses id-less", _id is None and _ans == "use DD/MM", f"{_id!r},{_ans!r}")

# empty / whitespace must be a no-op, never an explicit reply
chk("empty string not explicit", D.is_explicit_reply("") is False)
chk("whitespace not explicit", D.is_explicit_reply("   ") is False)

print("\n========== EU-55 EXPLICIT-REPLY GUARD QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
