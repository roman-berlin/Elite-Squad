"""EU-566: Needs-you cards — truncation-safe option-button labels with detail in tooltip/expander.

Pins tested:
  (1) fold_option_label long input returns (label, True) where label ends '…',
      stem is exact prefix of source, next char is non-alphanumeric (word-boundary);
  (2) short input returns (itself, False) verbatim;
  (3) the folded label never reproduces the EU-508 signature ('once pe…');
  (4) rendered /needs: button label is word-boundary-safe prefix (no mid-word cut);
  (5) rendered /needs: button title contains full untruncated option text;
  (6) hidden name=text still carries full 'Option N: <full text>';
  (7) short options render verbatim with no '…' and keep existing generic tooltip."""
import re
import sys
import tempfile
import types
from pathlib import Path

# ── Stub dependencies (mirrors eu564) ──────────────────────────────────────

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

import html as _html_mod
from orchestrator import decisions, needs, server
from orchestrator.config import Config, AppConfig

results = []

def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ── Fixtures ───────────────────────────────────────────────────────────────

_LONG_OPTION = (
    "this ask fires once per drain cycle and needs a decision before the "
    "queue can advance past the parked ticket in the automated workflow pipeline"
)
assert len(_LONG_OPTION) > 110, f"fixture must exceed 110 chars (got {len(_LONG_OPTION)})"

_SHORT_TEXT = "Re-enable the existing pipeline"
assert len(_SHORT_TEXT) <= 110


# ── (1) + (2) + (3) fold_option_label unit tests ──────────────────────────

_chk_long = decisions.fold_option_label(_LONG_OPTION, 110)
_lbl, _was_folded = _chk_long

chk("(1a) long input returns (label, True)",
    isinstance(_chk_long, tuple) and _chk_long[1] is True,
    repr(_chk_long))
chk("(1b) long label ends with ellipsis",
    _lbl.endswith("…"),
    repr(_lbl))
chk("(1c) stem is clean prefix of source",
    _LONG_OPTION.startswith(_lbl.rstrip("…")),
    f"stem={repr(_lbl.rstrip('…'))} src={repr(_LONG_OPTION)[:120]}")
_stem_before_ellipsis = _lbl.rstrip("…")
chk("(1d) next char after stem is NOT alphanumeric (word-boundary)",
    not _stem_before_ellipsis
    or len(_stem_before_ellipsis) >= len(_LONG_OPTION)
    or not _LONG_OPTION[len(_stem_before_ellipsis)].isalnum(),
    f"next_src_char={repr(_LONG_OPTION[len(_stem_before_ellipsis)]) if len(_stem_before_ellipsis)<len(_LONG_OPTION) else 'end'}")
chk("(2a) short input returns (itself, False)",
    decisions.fold_option_label(_SHORT_TEXT, 110) == (_SHORT_TEXT, False),
    repr(decisions.fold_option_label(_SHORT_TEXT, 110)))
chk("(2b) even shorter passes through",
    decisions.fold_option_label("Hi!", 110) == ("Hi!", False),
    repr(decisions.fold_option_label("Hi!", 110)))

_chk_long3 = decisions.fold_option_label(_LONG_OPTION, 110)
_chk_lbl = _chk_long3[0]
chk("(3a) folded label does NOT end with 'pe…'",
    not _chk_lbl.endswith("pe…"),
    repr(_chk_lbl))
chk("(3b) folded label does NOT contain 'once pe…'",
    "once pe…" not in _chk_lbl,
    repr(_chk_lbl))


# ── Rendered /needs setup ─────────────────────────────────────────────────

_STRUC_Q = ("What should we do about the broken CI pipeline?\n"
            "OPTIONS:\n"
            "1. Re-enable the existing pipeline\n"
            "2. " + _LONG_OPTION + "\n"
            "3. Disable CI temporarily\n")

def _synth_with_long_po(q):
    return {
        "summary": "The CI pipeline requires urgent attention.",
        "options": [
            {"n": 1, "text": _SHORT_TEXT, "recommended": False},
            {"n": 2, "text": _LONG_OPTION, "recommended": True},
            {"n": 3, "text": "Disable CI temporarily", "recommended": False},
        ],
    }

_struc_rows = [
    {"id": "EU-566", "app": "automatixy",
     "question": _STRUC_Q, "why": "CI broken", "category": "decision"},
]

def _struc_summary(c, app_name=None):
    return {"rows": _struc_rows, "decisions": _struc_rows,
            "proposals": [], "tasks": [], "total": 1}

_needs_orig = needs.summary
import orchestrator.decisions as _dec_mod
_parse_orig = _dec_mod.parse_options

needs.summary = _struc_summary
_dec_mod.parse_options = lambda q: _synth_with_long_po(q) or _dec_mod.synthesize_options(q)

tmpdir = tempfile.mkdtemp()
cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=tmpdir,
                    base_branch="dev", protected_branch="main",
                    backlog_backend="none")],
)

body = server.create_app(cfg).test_client().get("/needs").get_data(as_text=True)

# Restore.
needs.summary = _needs_orig
_dec_mod.parse_options = _parse_orig


# ── Parse option-forms from the response body ─────────────────────────────

# Extract every <form action=/api/answer ...> that has an 'Option N:' hidden value.
# Each gives us: option number, hidden value, visible button text, title attribute.
_OPTION_FORMS = {}
for m in re.finditer(r'<form[^>]*action=/api/answer[^>]*>(.*?)</form>', body, re.DOTALL):
    frag = m.group(1)
    # Match Option-N hidden values: value="Option N: <full text>"
    opt_m = re.search(r'name=text\s+value="([^"]+)"', frag)
    if opt_m:
        hidden_val = _html_mod.unescape(opt_m.group(1))
        n_match = re.match(r'Option (\d+): (.+)', hidden_val)
        if n_match:
            opt_n = int(n_match.group(1))
            # Button inside this form.
            btn_m = re.search(r'<button[^>]*>(.*?)</button>', frag)
            raw_btn = _html_mod.unescape(btn_m.group(1)) if btn_m else ""
            # Title attr on this specific button.
            title_m = re.search(r"<button[^>]*title='([^']*)'", frag)
            btn_title = title_m.group(1) if title_m else ""
            _OPTION_FORMS[opt_n] = {
                'hidden_value': hidden_val,
                'button_raw': raw_btn,
                'button_title': btn_title,
            }

# Map of expected full texts per option.
_EXPECTED_FULL = {
    1: _SHORT_TEXT,
    2: _LONG_OPTION,
    3: "Disable CI temporarily",
}


# ── (4) + (5) + (6) Button label + title + hidden value checks ────────────

block2 = _OPTION_FORMS.get(2)
if block2:
    # (4a) Extract display label: remove star entity and 'N.' prefix, then strip '…'.
    _raw2 = block2['button_raw']
    _clean2 = _html_mod.unescape(_raw2)
    _display2 = re.sub(r'^[★*\*]\s*', '', _clean2)  # Remove ★ or star
    _display2 = re.sub(r'^\d+\.\s*', '', _display2)   # Remove '2. '
    _display2_noell = _display2.replace("…", "")

    chk("(4a) button label is a word-boundary-safe prefix of _LONG_OPTION",
        _LONG_OPTION.startswith(_display2_noell),
        f"label={repr(_display2_noell)} src={repr(_LONG_OPTION)[:120]}")

    if len(_display2_noell) < len(_LONG_OPTION):
        chk("(4b) char after label stem is NOT alphanumeric",
            not _LONG_OPTION[len(_display2_noell)].isalnum(),
            f"next={repr(_LONG_OPTION[len(_display2_noell)])}")

    # (5a) Button title contains the FULL untruncated option text.
    chk("(5a) button title contains the full option text",
        _LONG_OPTION in block2['button_title'],
        f"title excerpt={repr(block2['button_title'][:120])}")

else:
    chk("(4-5) COULD NOT FIND OPTION-2 BUTTON",
        False, f"option forms found: {sorted(_OPTION_FORMS.keys())}; btn keys={[k for k in _OPTION_FORMS][:3]}")


# ── (6) Hidden values carry full text ─────────────────────────────────────

for _of_n in sorted(_OPTION_FORMS.keys()):
    ef = _OPTION_FORMS[_of_n]
    expected = f"Option {_of_n}: {_EXPECTED_FULL.get(_of_n, '')}"
    chk(f"(6.{_of_n}) hidden value for Option {_of_n}",
        ef['hidden_value'] == expected,
        f"expected={repr(expected)[:80]} got={repr(ef['hidden_value'])[:80]}")


# ── (7) Short options: verbatim label, no '…' ─────────────────────────────

if 1 in _OPTION_FORMS:
    ef1 = _OPTION_FORMS[1]
    _raw1 = ef1['button_raw']
    _clean1 = _html_mod.unescape(_raw1)
    _short_display = re.sub(r'^[★*\*]\s*', '', _clean1)
    _short_display = re.sub(r'^\d+\.\s*', '', _short_display).strip()

    chk("(7a) short label renders verbatim (matches original)",
        _short_display == _SHORT_TEXT,
        f"display={repr(_short_display)} expected={repr(_SHORT_TEXT)}")
    chk("(7b) short label has NO ellipsis in raw HTML",
        "…" not in _raw1,
        repr(_raw1[:100]))
else:
    chk("(7a/b) COULD NOT FIND OPTION-1 BUTTON",
        False, f"option forms found: {sorted(_OPTION_FORMS.keys())}")


# ── Summary ────────────────────────────────────────────────────────────────

print("\n========== EU-566 Option Button Label QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
