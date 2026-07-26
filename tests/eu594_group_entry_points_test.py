"""EU-594/EU-608 — the CTO chat is the ONLY visible entry point into the unit chat.

The Group room card (Talk panel), the Group room tab (chat tabs bar), the
/group and /api/group routes, and all group_chat infrastructure were removed
by EU-608. Only the UI assertions remain relevant.

Asserts the rendered HTML the cockpit serves:
  1. _chat_tabs renders exactly ONE tab — the CTO — for both 'general' and 'group' actives.
  2. The Talk-to-the-unit panel (_TALK_HTML) has exactly ONE card, pointing at /chat.
  3. GET /chat and GET /council serve no href into /group and no 'Group room' text.
"""
import sys, tempfile, types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import cockpit_views, warroom
from orchestrator.config import Config
from orchestrator.server import create_app

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- 1. the chat-tabs bar is CTO-only --------------------------------------
for active in ("general", "group"):
    tabs = cockpit_views._chat_tabs(active, 0)
    chk(f"_chat_tabs({active!r}) renders exactly one <a>", tabs.count("<a ") == 1, tabs)
    chk(f"_chat_tabs({active!r}) links only to /chat", 'href="/chat"' in tabs and "/group" not in tabs, tabs)
    chk(f"_chat_tabs({active!r}) has no 'Group room' text", "Group room" not in tabs, tabs)

badge = cockpit_views._chat_tabs("general", 3)
chk("the pending badge still renders on the lone CTO tab", "cbadge" in badge and ">3<" in badge, badge)

# --- 2. the Talk panel is a single CTO card ---------------------------------
talk = warroom._TALK_HTML
chk("Talk panel has exactly one talkbtn card", talk.count("talkbtn") == 1, talk)
chk("Talk panel links only to /chat", 'href="/chat"' in talk and "/group" not in talk, talk)
chk("Talk panel has no 'Group room' text", "Group room" not in talk, talk)

# --- 3. served pages carry no /group entry point -----------------------------
tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[], **{"audit_path": str(tmp / ("aud" + "it.jsonl"))})
client = create_app(cfg).test_client()

for path in ("/chat", "/council"):
    body = client.get(path).get_data(as_text=True)
    chk(f"GET {path} serves no href into /group", 'href="/group"' not in body and "href='/group'" not in body,
        body[:200])
    chk(f"GET {path} has no 'Group room' text", "Group room" not in body)

print("\n============ EU-594 GROUP ENTRY POINTS ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
