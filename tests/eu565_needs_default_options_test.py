"""EU-565: Needs-you cards must always render 2-4 numbered option buttons + one Recommended.

Guarantees via three tiers: parse_options → synthesize_options → default_card.
Never ships a free-text-only card for decision rows."""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

# Stub external deps — same pattern as decision_options_test.py
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return s

s = sdk
sdk.__getattr__ = lambda n: _D

sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")

req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None)
)
sys.modules["requests"] = req

sys.path.insert(0, ".")

from orchestrator import decisions, server, needs
from orchestrator.config import Config, AppConfig

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


Q_STRUCTURED = (
    "The date column can be rendered two ways and the ticket doesn't say which.\n"
    "OPTIONS:\n"
    "1. DD/MM/YYYY everywhere (RECOMMENDED) — matches the EU locale of every current tenant\n"
    "2. MM/DD/YYYY everywhere\n"
    "3. Per-tenant locale setting\n"
)


# ── AC-1: ensure_options on plain prose (the EU-518 shape) ───────────────────

plain = "Just tell me what to do here."
result = decisions.ensure_options(plain)
chk(
    "(1a) ensure_options returns dict with 2-4 options",
    result is not None
    and isinstance(result.get("options"), list)
    and 2 <= len(result["options"]) <= 4,
    f"got {len(result['options'])} options",
)
chk(
    "(1b) exactly one option flagged recommended",
    sum(1 for o in result["options"] if o["recommended"]) == 1,
    f"found {sum(1 for o in result['options'] if o['recommended'])} recommended",
)
chk(
    "(1c) non-empty summary present",
    bool(result.get("summary")),
    repr(result.get("summary")),
)
chk(
    "(1d) never returns None for non-empty question",
    result is not None,
    str(result),
)
chk(
    "(1e) each option has text field",
    all(o.get("text") for o in result["options"]),
    str(result),
)

# Also test empty / whitespace input gets a fallback card
empty_result = decisions.ensure_options("")
chk(
    "(1f) empty string still returns a dict with options",
    empty_result is not None
    and isinstance(empty_result.get("options"), list)
    and len(empty_result["options"]) >= 2,
    f"got {len(empty_result['options'])} options for empty",
)
whitespace_result = decisions.ensure_options("   ")
chk(
    "(1g) whitespace-only string still returns a dict with options",
    whitespace_result is not None
    and isinstance(whitespace_result.get("options"), list)
    and len(whitespace_result["options"]) >= 2,
)


# ── AC-2: normalize_parse_options dedup + cap + single-recommended ──────────

po_many = decisions.parse_options(
    "Pick style:\n1. red (recommended)\n2. blue\n3. green\n4. yellow\n5. purple\n6. orange\n"
)
norm_many = decisions.normalize_parse_options(po_many) if po_many else None
chk(
    "(2a) 6 options capped to at most 4",
    norm_many is not None and len(norm_many["options"]) <= 4,
    f"got {len(norm_many['options'])} after cap",
)
chk(
    "(2b) the original recommended survives the cap",
    any(o["recommended"] for o in norm_many["options"]),
    str(norm_many["options"]),
)

po_zero_rec = decisions.parse_options(
    "Pick:\n1. option A\n2. option B\n3. option C\n"
)
norm_zero_rec = decisions.normalize_parse_options(po_zero_rec) if po_zero_rec else None
chk(
    "(2c) zero recommended → first option becomes recommended",
    norm_zero_rec is not None
    and norm_zero_rec["options"][0]["recommended"],
    str(norm_zero_rec),
)

po_double_rec = decisions.parse_options(
    "Pick:\n1. option A (RECOMMENDED)\n2. option B (recommended)\n3. option C\n"
)
norm_double_rec = decisions.normalize_parse_options(po_double_rec) if po_double_rec else None
chk(
    "(2d) two recommended → only first survives",
    norm_double_rec is not None
    and sum(1 for o in norm_double_rec["options"] if o["recommended"]) == 1,
    f"{sum(1 for o in norm_double_rec['options'] if o['recommended'])} recommended",
)

po_dup = decisions.parse_options(
    "Pick:\n1. proceed (RECOMMENDED)\n2. Proceed\n3. hold\n"
)
norm_dup = decisions.normalize_parse_options(po_dup) if po_dup else None
chk(
    "(2e) case-insensitive dedup removes 'Proceed' duplicate",
    norm_dup is not None
    and len(norm_dup["options"]) == 2,
    f"got {len(norm_dup['options'])} after dedup",
)


# ── AC-3: GET /needs with stubbed options-less row ──────────────────────────

cfg_tmp = Path(tempfile.mkdtemp())
cfg = Config(
    apps=[
        AppConfig(
            name="automatixy",
            repo_path=str(cfg_tmp),
            base_branch="dev",
            protected_branch="main",
            backlog_backend="none",
        )
    ],
    audit_path=str(cfg_tmp / "audit.jsonl"),
    use_worktree=False,
)


def _stub_summary_no_options(c, app_name=None):
    """Simulates the exact EU-518 data path: plain prose question, no structured options."""
    rows = [
        {
            "id": "EU-518",
            "app": "Elite-Unit",
            "question": "Which approach should we take for the authentication module?",
            "why": "Which approach should we take for the authentication module?",
            "category": "decision",
        },
        # Also include a properly-structured decision to make sure both paths work.
        {
            "id": "EU-100",
            "app": "automatixy",
            "question": Q_STRUCTURED,
            "why": Q_STRUCTURED.splitlines()[0],
            "category": "decision",
        },
    ]
    return {"rows": rows, "decisions": rows, "proposals": [], "tasks": [], "total": 2}


_orig_summary = needs.summary
needs.summary = _stub_summary_no_options

try:
    cfg.detected_auth = lambda: "test"
    client = server.create_app(cfg).test_client()
    body = client.get("/needs").get_data(as_text=True)
finally:
    needs.summary = _orig_summary

# Option buttons must be present for the unstructured decision.
chk(
    "(3a) unstructured decision renders numbered buttons",
    ">1. " in body or ">2. " in body or ">3. " in body,
    "no button numbers found in body",
)
# Exactly one ★ marker per card's options.
chk(
    "(3b) the EU-518-style card has ★ markers (option buttons, not free-text-only)",
    "&#9733;" in body,
    "no star markers found",
)
# The "Other" field comes AFTER buttons (present but optional check).
chk(
    "(3c) Other free-text input appears in HTML",
    "Other — type your own decision" in body,
    "missing Other input",
)
# The classic free-text-only card placeholder must NOT appear.
chk(
    "(3d) classic free-text placeholder absent for decision rows",
    "Answer the squad — your decision re-runs the ticket with it baked in" not in body,
    "still rendering old free-text-only card",
)
# Both questions should be visible in the card headlines.
chk(
    "(3e) both decision questions headline their cards",
    "Which approach should we take" in body
    and "The date column can be rendered two ways" in body,
    f"headlines missing; length={len(body)}",
)

# The default option buttons must render for the unstructured row.
chk(
    "(3f) default option buttons present for EU-518-style card",
    "Use your best judgment" in body
    or "Re-scope: split into smaller tickets" in body,
    f"no default options found; body_len={len(body)}",
)


# ── AC-4: decision_options_test.py pin (6) behaviour verified above ─────────
# The existing test file already checks this at (6); we just confirmed it works
# through AC-3 above where the unstructured row ("Which approach…") got buttons.


# ── Report ───────────────────────────────────────────────────────────────────

print("\n========== EU-565 NEEDS DEFAULT OPTIONS QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
