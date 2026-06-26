"""EU-65 [Ordnance BE] — multi-chat send() + poll-filter origin tagging (notify.py).

Asserts two invariants of the inter-unit liaison wiring:
  1. send() defaults to the ops chat (TELEGRAM_CHAT_ID) so ops reports / escalations / council
     summaries are NEVER auto-broadcast to an external chat; an explicit chat_id targets a
     specific chat (the only way an outward liaison reply reaches an allied unit).
  2. incoming_texts() tags each accepted message 'ops' or 'external' and accepts an external
     chat ONLY when a cfg with an ACTIVE liaison channel is supplied — otherwise it is ops-only,
     byte-identical to today. The origin tag lets downstream routing branch.
"""
import os
import sys

sys.path.insert(0, ".")
from orchestrator import notify
from orchestrator.config import Config, AppConfig

_app = AppConfig(name="x", repo_path=".")


def cfg(**kw) -> Config:
    return Config(apps=[_app], **kw)


# --- a tiny fake `requests` capture so we never hit the network ---
class _Resp:
    status_code = 200


class _FakeRequests:
    RequestException = notify.requests.RequestException

    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        return _Resp()


_real_requests = notify.requests
fake = _FakeRequests()
notify.requests = fake
os.environ["TELEGRAM_BOT_TOKEN"] = "T"
os.environ["TELEGRAM_CHAT_ID"] = "555"

print("=== send() defaults to the ops chat ===")
assert notify.send("hi") is True, "ops send succeeds"
assert fake.calls[-1]["chat_id"] == "555", "default target is TELEGRAM_CHAT_ID"

print("=== send(chat_id=...) targets an explicit (external) chat ===")
assert notify.send("liaison hello", chat_id="-100999") is True, "explicit send succeeds"
assert fake.calls[-1]["chat_id"] == "-100999", "explicit chat id targeted"
assert notify.send("int id", chat_id=-100888) is True, "int chat id coerced"
assert fake.calls[-1]["chat_id"] == "-100888", "int chat id stringified"

print("=== an explicit chat id needs only the token, not TELEGRAM_CHAT_ID ===")
del os.environ["TELEGRAM_CHAT_ID"]
assert notify.send("default w/o ops chat") is False, "no ops chat -> default send is a no-op"
assert notify.send("still external", chat_id="-100999") is True, "explicit send still works"
os.environ["TELEGRAM_CHAT_ID"] = "555"

notify.requests = _real_requests  # restore; the rest is pure filtering


def _upd(uid, cid, text="hello"):
    return {"update_id": uid, "message": {"text": text, "chat": {"id": cid}}}


updates = [
    _upd(1, 555, "from ops"),
    _upd(2, -100123, "from allied unit"),
    _upd(3, -100999, "from a stranger"),
    {"update_id": 4, "message": {"chat": {"id": 555}}},  # no text -> dropped
]

print("=== no cfg => ops only, tagged 'ops' (byte-identical to today) ===")
got = notify.incoming_texts(updates)
assert got == [(1, "from ops", "ops")], got

print("=== cfg with INACTIVE liaison => still ops only ===")
got = notify.incoming_texts(updates, cfg())
assert got == [(1, "from ops", "ops")], got
got = notify.incoming_texts(updates, cfg(liaison_enabled=True))  # flag on, no ids -> inert
assert got == [(1, "from ops", "ops")], got

print("=== ACTIVE liaison => external chat accepted and tagged 'external' ===")
on = cfg(liaison_enabled=True, liaison_external_chat_ids=["-100123"])
got = notify.incoming_texts(updates, on)
assert got == [(1, "from ops", "ops"), (2, "from allied unit", "external")], got
# the unknown -100999 stranger is never accepted, even with the channel active
assert all(cid != "from a stranger" for _, cid, _ in got), "foreign chat ignored"

print("=== ops chat is never reclassified as external ===")
both = cfg(liaison_enabled=True, liaison_external_chat_ids=["555", "-100123"])
got = notify.incoming_texts(updates, both)
ops_rows = [r for r in got if r[0] == 1]
assert ops_rows == [(1, "from ops", "ops")], "ops id stays 'ops' even if mis-listed externally"

print("ALL GREEN — eu65_liaison_notify")
