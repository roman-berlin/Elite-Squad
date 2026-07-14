"""EU-304 — window the CTO chat transcript to the last ~20 messages, with a 'load earlier' batch.

Covers the ticket's testable acceptance criteria:
  1. ``_chat_inner(cfg)`` returns at most 20 regular ``.msg`` bubbles by default, while every pinned
     ``.pcard`` decision card still renders (pinned cards not counted toward the 20).
  2. A 'load earlier' control renders when there are >20 regular messages, and is absent when there
     are <=20.
  3. Calling with the pagination offset (``offset=20``) returns the next-older batch of bubbles as an
     HTML fragment suitable for prepending — never the composer, never a full page.
  4. ``GET /chat`` still returns 200, still carries the 5s ``setInterval`` -> ``/api/chat-thread`` poll
     and the ``/api/chat`` -> ``decisions.route_message`` composer path, untouched.
  5. This harness itself is picked up by ``tests/run_all.py`` (it is a plain ``tests/*_test.py``).
"""
import json
import re
import sys
import tempfile
import types
from pathlib import Path

# ── minimal SDK stub so the orchestrator imports without real deps ──
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import cockpit_views as V
from orchestrator import council, server
from orchestrator.config import AppConfig, Config

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


def _fresh_cfg() -> Config:
    tmp = Path(tempfile.mkdtemp())
    return Config(
        apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                         protected_branch="MAIN", backlog_backend="none")],
        audit_path=str(tmp / "audit.jsonl"),
        use_worktree=False,
    )


def _seed(cfg: Config, n: int) -> None:
    """Write n Q/A turns (2n bubbles) to the chat transcript, oldest first."""
    for i in range(n):
        council.append_chat(cfg, "Q", f"question number {i}")
        council.append_chat(cfg, "A", f"answer number {i}")


# ── AC1/AC2: oversized transcript -> windowed to <=20 regular bubbles ───────────────────────
cfg_big = _fresh_cfg()
_seed(cfg_big, 30)  # 60 bubbles total, well over the 20-message default window

inner = V._chat_inner(cfg_big)
msg_count = len(re.findall(r'class="msg ', inner))
chk("default render caps regular bubbles at <=20", msg_count <= 20, f"got {msg_count}")
chk("default render shows the MOST RECENT messages (last turn present)",
    "answer number 29" in inner and "question number 29" in inner)
chk("default render does NOT show the oldest turn (windowed away)",
    "answer number 0" not in inner and "question number 0" not in inner)
chk("'load earlier' control present when >20 regular messages exist",
    "load-earlier" in inner, inner[:200])

# ≤20 messages -> no 'load earlier' control, and every message shows.
cfg_small = _fresh_cfg()
_seed(cfg_small, 5)  # 10 bubbles, under the window
inner_small = V._chat_inner(cfg_small)
small_count = len(re.findall(r'class="msg ', inner_small))
chk("small transcript renders all its bubbles (<=20 already)", small_count == 10, f"got {small_count}")
chk("'load earlier' control ABSENT when <=20 regular messages", "load-earlier" not in inner_small)

# pinned cards: build a pending decision file and confirm the pcard renders + isn't counted in the 20.
try:
    Path(cfg_big.audit_path).with_name("pending_decisions.json").write_text(
        json.dumps([{"id": "AUTO-9", "question": "ship it?"}]), encoding="utf-8")
    inner_pinned = V._chat_inner(cfg_big)
    pcard_count = len(re.findall(r'class=pcard', inner_pinned))
    pinned_msg_count = len(re.findall(r'class="msg ', inner_pinned))
    chk("pinned decision card renders on top", pcard_count == 1 and "AUTO-9" in inner_pinned)
    chk("pinned card doesn't shrink the 20-message regular window",
        pinned_msg_count <= 20 and pinned_msg_count == msg_count, f"{pinned_msg_count} vs {msg_count}")
except Exception as e:  # noqa: BLE001
    chk("pinned decision card renders on top", False, str(e))
    chk("pinned card doesn't shrink the 20-message regular window", False, str(e))

# ── AC3: offset=20 returns the NEXT-OLDER batch as a bare fragment ──────────────────────────
batch = V._chat_inner(cfg_big, offset=20)
batch_msg_count = len(re.findall(r'class="msg ', batch))
chk("older-batch call returns bubbles (non-empty)", batch_msg_count > 0, f"got {batch_msg_count}")
chk("older batch contains messages OLDER than the default window",
    "answer number 19" in batch or "answer number 18" in batch)
chk("older batch does NOT repeat the default window's newest turn",
    "answer number 29" not in batch)
chk("older-batch fragment carries no composer/form markup", "action=/api/chat" not in batch)
chk("older-batch fragment is not a full page (no <html>)", "<html" not in batch.lower())

# offset far beyond the transcript -> empty fragment (no more older messages), used by the JS to
# know when to hide the 'load earlier' button.
far = V._chat_inner(cfg_big, offset=10_000)
chk("offset beyond history returns an empty fragment", far.strip() == "", repr(far[:80]))

# ── AC4: /chat still 200s, still polls /api/chat-thread every 5s, composer still posts /api/chat ──
app = server.create_app(cfg_big)
app.config.update(TESTING=True)
client = app.test_client()

r = client.get("/chat")
chk("GET /chat -> 200", r.status_code == 200, r.status_code)
body = r.get_data(as_text=True)
chk("5s auto-refresh setInterval -> /api/chat-thread present, unchanged",
    'setInterval' in body and '"/api/chat-thread"' in body and ',5000)' in body)
chk("composer still posts to /api/chat (decisions.route_message path untouched)",
    "action=/api/chat" in body)

r2 = client.get("/api/chat-thread")
chk("GET /api/chat-thread (no offset) -> 200, default windowed render", r2.status_code == 200)
default_api_count = len(re.findall(r'class="msg ', r2.get_data(as_text=True)))
chk("default /api/chat-thread poll also windowed to <=20", default_api_count <= 20, default_api_count)

r3 = client.get("/api/chat-thread?offset=20")
chk("GET /api/chat-thread?offset=20 -> 200, older batch", r3.status_code == 200)
older_body = r3.get_data(as_text=True)
chk("offset batch via HTTP excludes the newest default-window turn",
    "answer number 29" not in older_body and len(re.findall(r'class="msg ', older_body)) > 0)


print("\n============ EU-304 CHAT WINDOW ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
