"""EU-564: Needs-you cards — plain-language headline + no mid-word truncation.

Pins tested:
  (1) word-boundary fold: long input is cut at a word boundary, never mid-word;
      output is either the full text or ends with '…' where head before '…' is a
      clean prefix ending exactly at a space.
  (2) leading-identifier strip: summarize_question strips a leading EU-XXX / TICKET-ID prefix so
      the headline starts with plain language, not an internal identifier.
  (3) expander gap closed (unstructured): between 140-limit and old 160-gate renders folded
      headline AND 'Full context' expander.
  (4) structured card expander: parse_options-structured question exceeding limit gets expander
      even when NOT synthesized.
  (5) parked card headline: >140-char parked why is word-boundary-folded + gets expander.
  (6) rendered /needs headline for the EU-508 fixture (long, ticket-id-prefixed, unstructured):
      (a) no raw [A-Z][A-Z0-9]+-\\d+ ticket-id pattern in the rendered headline;
      (b) the headline is never a raw mid-word/wall prefix of _qfull — it either equals the
          full (id-stripped) sentence or stops at a real word boundary behind a '…' with the
          uncut question inside the 'Full context' expander.
  (7) a mid-sentence ticket id keeps its preceding clause (strip is anchored at the start).
  (8) structured parse_options summary never begins with a raw ticket id."""
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
sys.path.insert(0, ".")

from orchestrator import decisions, needs, server
from orchestrator.config import Config, AppConfig

results = []

def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ── Fixtures ───────────────────────────────────────────────────────────────

# EU-508 reproducer — >140 chars after normalization, starts with ticket id.
_WALL_Q = ("EU-508: this ask fires once per drain cycle and needs a decision "
           "before the queue can advance past the parked ticket in the "
           "automated workflow pipeline.")
assert len(_WALL_Q) > 140, f"fixture must exceed 140 chars (got {len(_WALL_Q)})"

_LONG_UNSTRUCTURED = (
    "The date column rendering in the cockpit dashboard needs to be standardized "
    "across all tenant views and the current implementation varies by region."
)
assert len(_LONG_UNSTRUCTURED) > 140

_LONG_PARKED = (
    "This ticket requires coordination between the frontend team for UI changes, "
    "the backend team for API modifications, and the DevOps team to update "
    "deployment configs in the staging environment first."
)
assert len(_LONG_PARKED) > 140


# Normalize + strip leading id prefix — mirrors what summarize_question does internally.
_tkr = re.compile(r"[A-Z][A-Z0-9]+-\d+")
_norm_no_id = _tkr.sub("", _WALL_Q).lstrip(": \t")
_norm_no_id = " ".join(_norm_no_id.split())
result = decisions.summarize_question(_WALL_Q)
head = result.rstrip("…")
chk(
    "(1a) word-boundary fold: result is full text or ends with ellipsis",
    result == _WALL_Q or result.endswith("…"),
    repr(result)[:200],
)
chk(
    "(1b) head is a clean prefix of the no-id normalized source",
    _norm_no_id.startswith(head),
    f"head={repr(head)}, len={len(head)} vs norm={len(_norm_no_id)}",
)
chk(
    "(1b2) next char after head is NOT alphanumeric (word-boundary fold)",
    not head or not _norm_no_id[len(head)].isalnum(),
    f"next_src={repr(_norm_no_id[len(head)]) if len(head)<len(_norm_no_id) else 'end of source'}",
)
# The EU-508 bug: never end mid-word like 'once pe…'
chk("(1c) no mid-word cutoff like 'pe'", not head.endswith("pe") or head.endswith("pe "), repr(head))
chk("(1e) short text passes through verbatim",
    decisions.summarize_question("Hi there.") == "Hi there.",
    repr(decisions.summarize_question("Hi there.")))


# ── (2) Leading-identifier strip ──────────────────────────────────────────

_res_re = re.compile(r"[A-Z][A-Z0-9]+-\d+")
res2 = decisions.summarize_question("EU-508: the drain stalls when this ask fires.")
chk("(2a) leading ticket-id prefix stripped",
    not _res_re.search(res2), repr(res2))
chk("(2b) plain sentence remains intact",
    "the drain stalls" in res2, repr(res2))


# ── (3) Expander gap closed (unstructured) ────────────────────────────────

_orig_summary = needs.summary

def _unstruct_summary(c, app_name=None):
    rows = [{"id": "AUTO-99", "app": "automatixy",
             "question": _LONG_UNSTRUCTURED, "why": _LONG_UNSTRUCTURED,
             "category": "decision"}]
    return {"rows": rows, "decisions": rows, "proposals": [], "tasks": [], "total": 1}

needs.summary = _unstruct_summary
tmpdir = tempfile.mkdtemp()
cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=tmpdir,
                    base_branch="dev", protected_branch="main",
                    backlog_backend="none")],
)
try:
    body = server.create_app(cfg).test_client().get("/needs").get_data(as_text=True)
finally:
    needs.summary = _orig_summary

chk("(3a) unstructured long question renders 'Full context' expander",
    "Full context" in body,
    f"expander present: {'Full context' in body}",
)
details_section = body.split(">Full context</summary>")[-1].split("</details>")[0] if ">Full context</summary>" in body else ""
chk("(3b) full untruncated question appears inside the expander's <details>",
    _LONG_UNSTRUCTURED in details_section,
    "full text inside details fold",
)


# ── (4) Structured card expander (non-synthesized) ────────────────────────

_STRUC_Q = (
    "What should we do about the broken CI pipeline?\n"
    "OPTIONS:\n"
    "1. Re-enable the existing pipeline as-is\n"
    "2. Replace with GitHub Actions (RECOMMENDED)\n"
    "3. Disable CI temporarily\n"
)

long_summary = (
    "The CI pipeline requires urgent attention because builds are consistently "
    "failing on the main branch and blocking all feature work across the team, "
    "which has already caused delays in customer-facing releases. The team needs "
    "to decide whether to fix the current system or replace it entirely. This "
    "affects every developer on the project and creates a bottleneck for all "
    "ongoing work streams."
)
assert len(long_summary) > 140


def _synth_long_po(q):
    """Return a parse_options-shaped dict with a long summary (no 'synthesized')."""
    return {
        "summary": long_summary,
        "options": [
            {"n": 1, "text": "Re-enable the existing pipeline as-is", "recommended": False},
            {"n": 2, "text": "Replace with GitHub Actions (RECOMMENDED)", "recommended": True},
            {"n": 3, "text": "Disable CI temporarily", "recommended": False},
        ],
    }

_struc_rows = [
    {"id": "EU-500", "app": "automatixy",
     "question": _STRUC_Q, "why": "CI broken", "category": "decision"},
]


def _struc_summary(c, app_name=None):
    return {"rows": _struc_rows, "decisions": _struc_rows,
            "proposals": [], "tasks": [], "total": 1}

needs.summary = _struc_summary
tmpdir2 = tempfile.mkdtemp()
cfg2 = Config(
    apps=[AppConfig(name="automatixy", repo_path=tmpdir2,
                    base_branch="dev", protected_branch="main",
                    backlog_backend="none")],
)

import orchestrator.decisions as _dec_mod
_orig_parse = _dec_mod.parse_options
_dec_mod.parse_options = lambda q: _synth_long_po(q) or _dec_mod.synthesize_options(q)
try:
    body2 = server.create_app(cfg2).test_client().get("/needs").get_data(as_text=True)
finally:
    _dec_mod.parse_options = _orig_parse

chk("(4a) structured non-synthesized card renders 'Full context'",
    "Full context" in body2,
    f"expander present: {'Full context' in body2}",
)
chk("(4b) full original prose appears inside structured card's expander",
    any(w in body2 for w in ["urgent attention", "customer-facing", "bottleneck"]),
    "full prose inside fold",
)


# ── (5) Parked card headline ──────────────────────────────────────────────

_parked_rows = [
    {"ticket_id": "EU-700", "app": "automatixy",
     "why": _LONG_PARKED, "category": "parked"},
]


def _parked_summary(c, app_name=None):
    return {"rows": _parked_rows, "decisions": [],
            "proposals": [], "tasks": [], "total": 1}

needs.summary = _parked_summary
tmpdir3 = tempfile.mkdtemp()
cfg3 = Config(
    apps=[AppConfig(name="automatixy", repo_path=tmpdir3,
                    base_branch="dev", protected_branch="main",
                    backlog_backend="none")],
)
try:
    body3 = server.create_app(cfg3).test_client().get("/needs").get_data(as_text=True)
finally:
    needs.summary = _orig_summary

chk("(5a) parked row headline is word-boundary-folded (not raw wall)",
    "requires coordination" in body3,
    "parked headline checked",
)
chk("(5b) parked card shows 'Full context' for long why",
    "Full context" in body3,
    f"expander present: {'Full context' in body3}",
)
chk("(5c) full parked why appears inside expander",
    _LONG_PARKED in body3,
    "full why inside fold",
)


# ── (6) Rendered /needs headline: EU-508 fixture (long, ticket-id-prefixed, unstructured) ──
# The EU-508 bug: the card headline was a raw char-count slice of the stored question —
# 'EU-508: this ask fires once pe…'. The render must start with the plain-language summary
# (id stripped, word-boundary folded) and carry the uncut question behind 'Full context'.

_wall_rows = [{"id": "EU-508", "app": "automatixy",
               "question": _WALL_Q, "why": _WALL_Q, "category": "decision"}]


def _wall_needs_summary(c, app_name=None):
    return {"rows": _wall_rows, "decisions": _wall_rows,
            "proposals": [], "tasks": [], "total": 1}


needs.summary = _wall_needs_summary
tmpdir4 = tempfile.mkdtemp()
cfg4 = Config(
    apps=[AppConfig(name="automatixy", repo_path=tmpdir4,
                    base_branch="dev", protected_branch="main",
                    backlog_backend="none")],
)
try:
    body4 = server.create_app(cfg4).test_client().get("/needs").get_data(as_text=True)
finally:
    needs.summary = _orig_summary

import html as _html_mod
_hl_m = re.search(r"nbadge dec'>Decision</span>(.*?)</div>", body4, re.S)
_rendered_head = _html_mod.unescape(_hl_m.group(1)) if _hl_m else ""
_TK = re.compile(r"[A-Z][A-Z0-9]+-\d+")
chk("(6a) rendered /needs headline contains no raw ticket-id pattern",
    _hl_m is not None and not _TK.search(_rendered_head),
    f"headline={repr(_rendered_head)[:200]}")
# (b) never a raw mid-word/wall prefix of _qfull: the headline either equals the full
# (id-stripped) sentence, or it stops at a real word boundary — marked with '…' and backed
# by the 'Full context' expander.
_src_for_head = decisions.strip_leading_ticket_key(" ".join(_WALL_Q.split()))
_stem = _rendered_head.rstrip("…")
if _rendered_head == _src_for_head:
    _boundary_ok = True  # the full sentence — nothing cut
else:
    _boundary_ok = (
        _rendered_head.endswith("…")
        and _src_for_head.startswith(_stem)
        and (len(_stem) >= len(_src_for_head) or not _src_for_head[len(_stem)].isalnum())
        and "Full context" in body4
    )
chk("(6b) rendered headline is the full sentence or a word-boundary fold + expander",
    _boundary_ok,
    f"headline={repr(_rendered_head)[:160]} src={repr(_src_for_head)[:80]}")
chk("(6b2) the EU-508 cutoff 'once pe…' is gone",
    not _rendered_head.endswith("pe…") and "once pe…" not in _rendered_head,
    f"headline={repr(_rendered_head)[:160]}")
chk("(6c) the uncut EU-508 question sits inside the expander",
    _WALL_Q in body4,
    f"full text inside details fold: {_WALL_Q[:60]!r}…")


# ── (7) Mid-sentence ticket id keeps its leading clause ───────────────────
_MID_Q = "The drain for EU-508 fires once per cycle and needs a call before the queue advances."
_mid = decisions.summarize_question(_MID_Q)
chk("(7a) mid-sentence ticket id: leading clause kept",
    _mid.startswith("The drain for"), repr(_mid))
chk("(7b) mid-sentence ticket id: sentence intact, not re-split on the id",
    _mid == _MID_Q, repr(_mid))


# ── (8) Structured summary never begins with a raw ticket id ──────────────
_STRUC_ID_Q = ("EU-518: which rendering path should the card use?\n"
               "OPTIONS:\n"
               "1. Keep the current one (RECOMMENDED)\n"
               "2. Rewrite it\n")
_po_id = decisions.parse_options(_STRUC_ID_Q)
chk("(8a) parse_options summary strips a leading ticket id",
    _po_id is not None and not _TK.match(_po_id["summary"])
    and _po_id["summary"].startswith("which rendering"),
    repr(_po_id and _po_id["summary"]))
chk("(8b) server render strips defensively too",
    decisions.strip_leading_ticket_key("EU-518: which rendering path") == "which rendering path",
    repr(decisions.strip_leading_ticket_key("EU-518: which rendering path")))

print("\n========== EU-564 Needs-You Headlines QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
