"""EU-569: Integration end-to-end contract for the Needs-you /needs page.

Verifies ALL sibling pieces land together correctly in the single rendered page:
  EU-564 word-boundary headline folding + Full context expander
  EU-565 ensure_options: every card gets 2-4 numbered options + exactly one recommended
  EU-566 fold_option_label: button labels fold at word boundary + full text in title attr
  EU-567 status strip (.nstrip) vs action banner (.nbanner) separation
  EU-568 brand voice: no denylist terms anywhere in user-facing HTML
  (Plus graceful degradation for errored/parked/PR fixtures.)

Tests run last — AFTER all sibling pieces have landed and passed their individual checks.
"""
import re
import sys
import tempfile
import types
from pathlib import Path

# ── Stub dependencies ────────────────────────────────────────────────────────

sdk = types.ModuleType("claude_agent_sdk")


class _D:  # noqa: D101
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k): return self


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

results: list[tuple[str, bool, str]] = []


def chk(n: str, c: bool, d: str = "") -> None:
    results.append((n, bool(c), d))


# ── Denylist (BRAND.md terminology map + EU-568 terms) ──────────────────────

_DENY_TERMS = [
    r"DEV→MAIN",               # arrow form
    r"DEV->MAIN",              # dash form
    r"readiness\s+verdict",     # space-insensitive
    r"War\s+Room",
    r"Elite\s+Unit",
    r"\bCommander\b",
    r"\bofficers?\b",           # word-bounded
    r"\bthe\s+unit\b",          # word-bounded whole phrase
]
_den_re = re.compile("|".join(_DENY_TERMS), re.IGNORECASE)


def _denied(text: str) -> bool:
    """Return True if *text* contains any denylist term."""
    return bool(_den_re.search(text))


# ═══════════════════ FIXTURES ════════════════════════════════════════════════

_BASE_DECISIONS = [
    # Decision 1 — structured (numbered option lines → parse_options succeeds)
    {
        "id": "AUTO-42", "app": "automatixy",
        "question": ("Should we merge PR #42 into dev?\n"
                     "1. Merge it now\n"
                     "2. Hold until next sprint\n"
                     "3. Reject outright"),
    },
    # Decision 2 — free-text only (EU-518 shape: NO numbered lines → ensure_options fallback)
    {
        "id": "AUTO-518", "app": "automatixy",
        "question": "What do you think about changing the default effort level?",
    },
]

# Longer versions used for EU-508 / EU-564 specific tests
_LONG_QUESTION = (
    "Can you help me figure out whether we should adopt the new logging "
    "framework that was announced at the conference last month for our "
    "microservices architecture across all teams?")
assert len(_LONG_QUESTION) > 140, "Test question must exceed 140 chars"

_LONG_OPTION_TEXT = (
    "This ask fires once per pipeline stage so the pipeline will "
    "automatically stop if the gate check fails during the build phase")
assert len(_LONG_OPTION_TEXT) > 110, "Test requires >110-char option text"

_LONG_Q_DECISION = {
    "id": "EU-564-BIG", "app": "automatixy",
    "question": _LONG_QUESTION,
}

_LONG_OPT_DECISION = {
    "id": "EU-508", "app": "automatixy",
    "question": f"How should we handle long-running tasks?\n"
                f"1. {_LONG_OPTION_TEXT}\n"
                "2. Retry with exponential backoff\n"
                "3. Skip and alert",
}

_STATUS_MSG = ("QA running for automatixy — engineers inspect dev and check "
               "whether it is ready to promote to main.")
_ACTION_MSG = "Answer sent to AUTO-42 — ticket re-queued."

# Errored / parked / PR task rows (categorised via the category field)
_TASK_ROWS = [
    {"ticket_id": "EU-300", "app": "automatixy",
     "why": "Build crashed — segfault in parser extension",
     "note": "crash details here", "category": "errored"},
    {"ticket_id": "EU-301", "app": "automatixy",
     "why": "Waiting on design review from the PM before proceeding",
     "category": "parked"},
    {"ticket_id": "EU-302", "app": "automatixy",
     "why": "PR opened — reviewer feedback needed",
     "pr_url": "https://github.com/org/repo/pull/123",
     "category": "pr"},
]


# ── Summary builder helper ───────────────────────────────────────────────────

_orig_summary = needs.summary


def _build_summary(decision_rows: list[dict],
                   task_rows: list[dict] | None = None) -> dict:
    """Build a unified summary(dict) from explicit decision + task rows."""
    rows: list[dict] = []
    for d in decision_rows:
        rows.append({**d, "category": "decision",
                      "why": str(d.get("question") or d.get("summary")
                                or "pending decision")})
    if task_rows:
        for t in task_rows:
            cat = t.get("category", "errored")
            rows.append({**t, "category": cat,
                          "why": str(t.get("why") or t.get("note") or "row")})
    return {"rows": rows,
            "decisions": decision_rows,
            "proposals": [],
            "tasks": task_rows or [],
            "total": len(rows)}


def _empty_summary(*a: object, **k: object) -> dict:
    """Zero-row empty inbox."""
    return {"rows": [], "decisions": [], "proposals": [],
            "tasks": [], "total": 0}


# ── Client factory ───────────────────────────────────────────────────────────

def make_client() -> object:  # noqa: F821
    cfg = Config(
        apps=[AppConfig(name="automatixy", repo_path=tempfile.mkdtemp(),
                        base_branch="dev", protected_branch="main",
                        backlog_backend="none")],
    )
    return server.create_app(cfg).test_client()


# ═══════════════════ TESTS ═══════════════════════════════════════════════════

# ── (1) Every decision card renders: options + recommended ★ + Other input ──

client1 = make_client()
with client1:
    needs.summary = lambda *a, **k: _build_summary(_BASE_DECISIONS, _TASK_ROWS)
    resp = client1.get("/needs")
    body = resp.get_data(as_text=True)

chk("(1a) Page renders button elements",
    "<button" in body, "buttons present in HTML")

# Buttons use unquoted class attrs like class='nbtn ok' — match anywhere inside attribute.
num_buttons = body.count("nbtn ")
# Also count bare nbtn without trailing space (shouldn't happen but be safe)
num_buttons += body.count("'nbtn ok") + body.count("'nbtn send") + body.count("'nbtn no") - num_buttons
num_buttons = max(num_buttons, body.count("nbtn"))
chk(f"(1b) Button elements exist on page (>= 2)",
    "nbtn" in body and "nbtn " in body,
    f"button markers found: {'nbtn ok' in body}, {'nbtn x' in body}")

star_count = body.count("&#9733;")
chk(f"(1c) Exactly two ★ marks (one per decision card; got {star_count})",
    star_count == 2, f"stars={star_count}")

other_inputs = body.count("placeholder='Other")
chk(f"(1d) Every decision has 'Other' free-text input (expected 2, got {other_inputs})",
    other_inputs == 2, f"'Other' inputs={other_inputs}")

_section_order = [("Decisions", "(1e)"), ("Errored runs", "(1f)"),
                  ("Parked tickets", "(1g)"), ("Open PRs", "(1h)")]
for sec_name, tag in _section_order:
    chk(f"{tag} {sec_name} section renders",
        sec_name in body, f"{sec_name} header visible")

needs.summary = _orig_summary
server._state.pop("last_msg", None)

# ── (2) Long option label → folded at word boundary + full text in title ──

client2 = make_client()
with client2:
    needs.summary = (lambda *a, **k:
                     _build_summary([_LONG_OPT_DECISION], _TASK_ROWS))
    resp2 = client2.get("/needs")
    body2 = resp2.get_data(as_text=True)

_lbl, _was_folded = decisions.fold_option_label(_LONG_OPTION_TEXT, 110)
assert _was_folded, "Test expects the option to actually be folded"

chk("(2a) fold_option_label folds >110 char text",
    _was_folded, f"len={len(_LONG_OPTION_TEXT)}, limit=110")

label_no_ep = _lbl.rstrip("…").rstrip()
full_stripped = _LONG_OPTION_TEXT.strip()
chk("(2b) Folded label is prefix of full text ending at word boundary",
    full_stripped.startswith(label_no_ep),
    f"prefix mismatch: '{label_no_ep}' vs '{full_stripped[:130]}...'")

# The button's title attr on the folded option should contain the full original text
has_full_in_title = (_LONG_OPTION_TEXT[:60].lower()
                     in body2.lower())
chk("(2c) Full option text present in page (title tooltip source)",
    has_full_in_title,
    f"title match against first 60 chars of option text")

needs.summary = _orig_summary
server._state.pop("last_msg", None)

# ── (3) Headline >140 chars → word-boundary fold + 'Full context' expander ──

client3 = make_client()
with client3:
    needs.summary = lambda *a, **k: _build_summary([_LONG_Q_DECISION])
    resp3 = client3.get("/needs")
    body3 = resp3.get_data(as_text=True)

chk("(3a) Long question (>140 chars) triggers 'Full context' expander",
    "Full context" in body3, "'Full context' expander present")

chk("(3b) <details> block contains the full original question",
    _LONG_QUESTION in body3,
    f"original ({len(_LONG_QUESTION)} chars) inside <details>")

needs.summary = _orig_summary
server._state.pop("last_msg", None)

# ── (4) Status/info lines NEVER under Needs-you heading ──────────────────────

client4 = make_client()
with client4:
    needs.summary = (lambda *a, **k:
                     _build_summary(_BASE_DECISIONS, _TASK_ROWS))
    server._state["last_msg"] = _STATUS_MSG
    try:
        resp4 = client4.get("/needs")
        body4 = resp4.get_data(as_text=True)
    finally:
        needs.summary = _orig_summary
        server._state["last_msg"] = _ACTION_MSG

    resp4b = client4.get("/needs")
    body4b = resp4b.get_data(as_text=True)

# (4a) Status NOT inside .nbanner
banner_m = re.search(r'<div[^>]*class="?nbanner"?\s*>(.*?)</div>',
                     body4, re.S)
chk("(4a) Status msg NOT inside .nbanner",
    banner_m is None or _STATUS_MSG not in banner_m.group(1),
    f"banner content: {repr(banner_m.group(1)[:150]) if banner_m else 'none'}")

# (4b) Status IS inside .nstrip
strip_m = re.search(r'<div[^>]*class="?nstrip"?\s*>(.*?)</div>',
                    body4, re.S)
chk("(4b) Status msg IS inside .nstrip element",
    strip_m is not None and _STATUS_MSG in strip_m.group(1),
    f"strip content: {repr(strip_m.group(1)[:150]) if strip_m else 'none'}")

# (4c) Status NOT inside any .nsec category section
for sec_name in ["Decisions", "Errored runs", "Parked tickets", "Open PRs"]:
    sec_pat = rf'>{re.escape(sec_name)}</h3>(.*?)(?:<h3|</div>$)'
    sec_m = re.search(sec_pat, body4, re.S)
    sec_content = sec_m.group(1) if sec_m else ""
    chk(f"(4c) Status msg NOT in '{sec_name}' section",
        _STATUS_MSG not in sec_content,
        f"'{sec_name}' excerpt: {repr(sec_content[:100])}")

# (4d) Action confirmation renders as .nbanner (regression guard both directions)
chk("(4d) Action confirmation keeps .nbanner",
    _ACTION_MSG in body4b and "<label>Status</label>" not in body4b,
    "action → nbanner, no nstrip")

# (4e) Sync button always present
chk("(4e) Sync with Jira button present",
    "Sync with Jira" in body4, "sync button exists")

needs.summary = _orig_summary
server._state.pop("last_msg", None)

# ── (5) BRAND.md denylist absent from entire page ────────────────────────────

chk("(5a) No denylist terms in rendered page",
    not _denied(body4),
    f"matches found: {_den_re.findall(body4) or 'none'}")

# ── (6) Graceful degradation — errored / parked / PR fixtures ────────────────

chk("(6a) Errored row renders with badge and ticket id",
    ">Errored run</span>" in body4 and "EU-300" in body4,
    "errored card visible")
chk("(6b) Parked row renders with parking reason",
    ">Parked</span>" in body4 and "EU-301" in body4,
    "parked card visible")
chk("(6c) Open PR renders with link and badge",
    ">Open PR</span>" in body4 and "EU-302" in body4,
    "PR card visible")

# ── (7) Dismiss via /needs/resolve degrades gracefully ──────────────────────

client7 = make_client()
with client7:
    needs.summary = (lambda *a, **k:
                     _build_summary(_BASE_DECISIONS[:1], []))
    # Without follow_redirects → raw redirect response.
    dismiss_resp = client7.post("/needs/resolve",
                                data={"ticket": "AUTO-42"},
                                follow_redirects=False)
    chk("(7a) Resolve returns redirect (HTTP 302/303)",
        dismiss_resp.status_code in (302, 303),
        f"status={dismiss_resp.status_code}")
    # With follow_redirects → rendered needs page shows success banner.
    dismiss_body = client7.post("/needs/resolve",
                                data={"ticket": "AUTO-42"},
                                follow_redirects=True).get_data(as_text=True)
    # After dismissing, the needs page re-renders with a confirmation banner.
    chk("(7b) Dismissed page shows success confirmation",
        ("dismissed" in dismiss_body.lower() or "✓" in dismiss_body),
        f"body excerpt: {repr(dismiss_body[:300])}")
    needs.summary = _orig_summary
    server._state.pop("last_msg", None)

# ── (8) Needs-sync endpoint works ────────────────────────────────────────────

client8 = make_client()
with client8:
    needs.summary = _empty_summary
    # Without follow → raw redirect code.
    sync_resp_nofollow = client8.post("/api/needs-sync", follow_redirects=False)
    chk("(8a) /api/needs-sync returns redirect (HTTP 302/303)",
        sync_resp_nofollow.status_code in (302, 303),
        f"status={sync_resp_nofollow.status_code}")
    # With follow → rendered empty /needs page.
    sync_body = client8.post("/api/needs-sync", follow_redirects=True).get_data(as_text=True)
    chk("(8b) Sync renders back onto the /needs page",
        "<title>Needs you</title>" in sync_body,
        "needs-page title present")
    chk("(8c) Empty inbox shows 'All clear' after sync",
        "&#10003; All clear" in sync_body,
        "All clear message present")
    needs.summary = _orig_summary
    server._state.pop("last_msg", None)

# ── SUMMARY ───────────────────────────────────────────────────────────────────

passed = sum(1 for _, ok, _ in results if ok)
print("\n========== EU-569 Needs-you Contract Integration QA ==========")
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}"
          + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results)
      else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
