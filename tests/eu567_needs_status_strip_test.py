"""EU-567: Needs-you section — strip status/info lines, keep only actionable decision cards.

Pins tested:
  (1) is_status_message() classifier: status messages return True; confirmations/errors return False.
  (2) Status message does NOT appear inside any .nsec category section, .ncard, or .nbanner.
  (3) Status message IS present in the dedicated .nstrip element.
  (4) Empty-inbox branch: "All clear" renders AND status strip with the QA text renders.
  (5) Needs-you count invariant: status-only last_msg doesn't affect summary total or card count.
"""
import re
import sys
import tempfile
import types
from pathlib import Path

# Stub dependencies.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules["requests"] = req
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import decisions, needs, server
from orchestrator.config import Config, AppConfig

results = []

def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ── Fixtures ───────────────────────────────────────────────────────────────

_STATUS_MSG = ("QA running for Elite-Unit — engineers inspect DEV and file findings, "
               "then deliver the DEV→MAIN readiness verdict (posts here and to Telegram).")
_ACTION_MSG = ("Answer sent to AUTO-23 — ticket re-queued.")
_ERROR_MSG = "could not start: boom"
_EMPTY_MSG = ""


# ── (1) is_status_message() classifier ────────────────────────────────────

chk("(1a) QA-running message classified as status",
    server.is_status_message(_STATUS_MSG), repr(_STATUS_MSG))
chk("(1b) action confirmation classified as NOT status",
    not server.is_status_message(_ACTION_MSG), repr(_ACTION_MSG))
chk("(1c) error line classified as NOT status",
    not server.is_status_message(_ERROR_MSG), repr(_ERROR_MSG))
chk("(1d) empty string classified as NOT status",
    not server.is_status_message(_EMPTY_MSG), repr(_EMPTY_MSG))
chk("(1e) ⏳ emoji prefix classified as status",
    server.is_status_message("⏳ Waiting for external API…"),
    repr("⏳ Waiting for external API…"))


# ── Helpers: build a minimal Config and create_app ────────────────────────

_orig_summary = needs.summary


def _decision_summary(c, app_name=None):
    """One pending-decision row — used when we need the populated inbox."""
    rows = [{"id": "AUTO-99", "app": "automatixy",
             "question": "Should we merge this PR?", "category": "decision"}]
    return {"rows": rows, "decisions": rows, "proposals": [], "tasks": [], "total": 1}


def _empty_summary(c, app_name=None):
    """Zero rows — empty inbox."""
    return {"rows": [], "decisions": [], "proposals": [], "tasks": [], "total": 0}


def make_client(cfg_override=None):
    cfg = cfg_override or Config(
        apps=[AppConfig(name="automatixy", repo_path=tempfile.mkdtemp(),
                        base_branch="dev", protected_branch="main",
                        backlog_backend="none")],
    )
    return server.create_app(cfg).test_client()


# ── (2)+(3) Status msg on populated inbox: NOT in .nsec/.ncard/.nbanner, IS in .nstrip ──

client = make_client()
with client:
    # Set status message, monkeypatch summary, then GET /needs.
    server._state["last_msg"] = _STATUS_MSG
    needs.summary = _decision_summary
    try:
        body = client.get("/needs").get_data(as_text=True)
    finally:
        needs.summary = _orig_summary

# Check: status text must NOT be inside any <div class=nbanner> element.
banner_match = re.search(r'<div[^>]*class="?nbanner"?\s*>(.*?)</div>', body, re.S)
chk("(2a) no <div class=nbanner> contains the status text",
    banner_match is None or _STATUS_MSG not in banner_match.group(1),
    f"banner content: {repr(banner_match.group(1))[:200] if banner_match else 'none'}")

# Check: status text IS inside .nstrip.
strip_match = re.search(r'<div[^>]*class="?nstrip"?\s*>(.*?)</div>', body, re.S)
chk("(3a) status text IS inside an .nstrip element",
    strip_match is not None and _STATUS_MSG in strip_match.group(1),
    f"strip content: {repr(strip_match.group(1))[:200] if strip_match else 'none'}")

# Check: status text is NOT inside any .nsec section (category header area).
for sec in ["Decisions", "Errored runs", "Parked tickets", "Open PRs"]:
    # Find the section content between the h3 header and next section or end.
    sec_pat = rf'>{re.escape(sec)}</h3>(.*?)(?:<h3|</div>$)'
    sec_match = re.search(sec_pat, body, re.S)
    sec_content = sec_match.group(1) if sec_match else ""
    chk(f"(2c) status text not in '{sec}' section",
        _STATUS_MSG not in sec_content,
        f"'{sec}' content excerpt: {repr(sec_content[:200])}")


# ── (4) Empty-inbox branch: "All clear" + status strip both render ────────

client2 = make_client()
with client2:
    server._state["last_msg"] = _STATUS_MSG
    needs.summary = _empty_summary
    try:
        body_empty = client2.get("/needs").get_data(as_text=True)
    finally:
        needs.summary = _orig_summary

chk("(4a) empty inbox still shows 'All clear'",
    "&#10003; All clear" in body_empty, "All clear present")
chk("(4b) empty inbox also shows status strip",
    "<label>Status</label>" in body_empty and _STATUS_MSG in body_empty,
    "status strip in empty inbox")
# Ensure no .nbanner for status-only message in empty inbox.
empty_banner = re.search(r'<div[^>]*class="?nbanner"?\s*>(.*?)</div>', body_empty, re.S)
chk("(4c) empty inbox: no .nbanner for status-only message",
    empty_banner is None or _STATUS_MSG not in empty_banner.group(1),
    f"empty banner: {empty_banner}")


# ── (5) Count invariant: status-only last_msg doesn't affect total/cards ──

client3 = make_client()
with client3:
    server._state["last_msg"] = _STATUS_MSG
    needs.summary = _decision_summary
    try:
        body_count = client3.get("/needs").get_data(as_text=True)
    finally:
        needs.summary = _orig_summary

# The summary returns total=1, so there should be exactly 1 decision card.
card_matches = re.findall(r'<div[^>]*class="?ncard"?\s*>', body_count)
summary_total = _decision_summary(None)["total"]
chk("(5a) status message doesn't inflate card count (expected {} cards)".format(summary_total),
    len(card_matches) == summary_total,
    f"cards={len(card_matches)}, expected={summary_total}")


# ── Extra: action confirmation keeps .nbanner (existing behaviour preserved) ──

client4 = make_client()
with client4:
    server._state["last_msg"] = _ACTION_MSG
    needs.summary = _decision_summary
    try:
        body_action = client4.get("/needs").get_data(as_text=True)
    finally:
        needs.summary = _orig_summary

action_banner = re.search(r'<div[^>]*class="?nbanner"?\s*>(.*?)</div>', body_action, re.S)
chk("(5b) action confirmation keeps .nbanner (existing behaviour)",
    action_banner is not None and _ACTION_MSG in action_banner.group(1),
    f"action banner: {action_banner}")
chk("(5c) action confirmation has NO .nstrip",
    "<label>Status</label>" not in body_action,
    "no nstrip for action confirmation")


print("\n========== EU-567 Needs-you Status Strip QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
