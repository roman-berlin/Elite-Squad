"""EU-307 — /chat composer: Enter-to-send, retain focus, stick to newest message.

Covers the ticket's testable acceptance criteria:
  1. GET /chat's composer <script> intercepts the form's submit (preventDefault) and POSTs to
     /api/chat via fetch instead of a native form submit -> Enter sends without a full-page reload.
  2. The composer script re-focuses the input after send (a `.focus()` call following the fetch).
  3. The composer script scrolls to the newest message after send (`window.scrollTo(0,
     document.body.scrollHeight)` executed after the send/refresh, not only on initial load).
  4. After send the handler refreshes #cinner via the existing /api/chat-thread endpoint, so it
     degrades gracefully against the current full-log render with no dependency on EU-286a.
  5. python3 tests/run_all.py stays green (this harness itself, plus no regressions elsewhere).
"""
from __future__ import annotations

import re
import sys
import tempfile
import types
from pathlib import Path

# ── minimal SDK / requests stubs so the orchestrator can import without real deps ──
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import server
from orchestrator.config import AppConfig, Config

# ── shared config / Flask test client ───────────────────────────────────────────
_TMP = Path(tempfile.mkdtemp())
_CFG = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_TMP), base_branch="DEV",
                     protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(_TMP / "audit.jsonl"),
    use_worktree=False,
)
_CFG.detected_auth = lambda: "test"

_APP = server.create_app(_CFG)
_CLIENT = _APP.test_client()

# ── result accumulator ──────────────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


resp = _CLIENT.get("/chat")
body = resp.get_data(as_text=True)

chk("GET /chat: HTTP 200", resp.status_code == 200, f"status={resp.status_code}")

# =============================================================================
# (1) submit is intercepted (preventDefault) and POSTs to /api/chat via fetch
# =============================================================================
composer_script_m = re.search(
    r'<div class=composer>.*?</script>', body, re.S)
chk("composer block present", composer_script_m is not None, body[:400])
composer_block = composer_script_m.group(0) if composer_script_m else ""

chk("composer JS calls preventDefault() on the form submit",
    "preventDefault" in composer_block, composer_block)
chk("composer JS fetches /api/chat (not a native form submit)",
    bool(re.search(r'fetch\(\s*["\']\/api\/chat["\']', composer_block)), composer_block)

# =============================================================================
# (2) focus() is called on the composer input after send
# =============================================================================
chk("composer JS re-focuses the input after send",
    ".focus()" in composer_block, composer_block)

# =============================================================================
# (3) scroll-to-bottom happens after send/refresh, not only on initial load
# =============================================================================
scroll_calls = re.findall(r'window\.scrollTo\(0,\s*document\.body\.scrollHeight\)', composer_block)
chk("composer JS scrolls to bottom after send (beyond the initial-load scroll)",
    len(scroll_calls) >= 2, f"found {len(scroll_calls)} scrollTo calls: {composer_block}")

# =============================================================================
# (4) send handler refreshes #cinner via the existing /api/chat-thread endpoint
# =============================================================================
chk("composer JS refreshes #cinner via /api/chat-thread after send",
    "/api/chat-thread" in composer_block and "cinner" in composer_block, composer_block)

# =============================================================================
# Summary
# =============================================================================
passed_n = sum(1 for _, ok, _ in results if ok)
print("\n========= EU-307 /chat composer tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("---------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
