"""Test _append_consult_note and consult-note wiring (EU-603)."""
import sys
import types

# Stub the Claude Agent SDK so importing council is network-free.
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _D

sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import council

results = []


def check(name, cond, d=""):
    results.append((name, bool(cond), d))


# ================================================================== #
# Test 1: Happy path — note appended with correct display name
# ================================================================== #
SEC_KEY = council._officer_key("Security Engineer")  # "provost" → displays as "Security Engineer"
check(
    "[1] happy path appends exact format with display name",
    council._append_consult_note(
        "Hi there.", SEC_KEY
    )
    == "Hi there.\n\n(asked the Security Engineer to check this.)",
)


# ================================================================== #
# Test 2: No consult (officer_key=None) → reply_text unchanged
# ================================================================== #
text_no_consult = "Just an answer."
result_nc = council._append_consult_note(text_no_consult, None)
check(
    "[2] no consult returns text byte-identical",
    result_nc is text_no_consult and result_nc == text_no_consult,
)


# ================================================================== #
# Test 3: display() raises → falls back to raw key without raising
# ================================================================== #
original_display = council.display
council.display = lambda k: (_ for _ in ()).throw(ValueError("boom"))
try:
    fallback_result = council._append_consult_note("fallback?", "scout")
    check(
        "[3] display raises → fallback uses raw key",
        fallback_result == "fallback?\n\n(asked the scout to check this.)",
    )
except Exception as e:
    check("[3] display raises → no exception raised", False, str(e))
finally:
    council.display = original_display


# ================================================================== #
# Test 4: Idempotent — applying twice yields exactly one note
# ================================================================== #
one = council._append_consult_note("base", "provost")
two = council._append_consult_note(one, "provost")
check(
    "[4] double apply idempotent — still one note",
    one == two and two.count("(asked the") == 1,
)


# ================================================================== #
# Test 5: Appended note never contains specialist-answer content
# ================================================================== #
SENTINEL_ANSWER = "SECRET_SPECIALIST_INJECTION_PAYLOAD_99x88zzz"
reply_with_answer = f"The CTO replied that {SENTINEL_ANSWER} is a good approach."
noted = council._append_consult_note(reply_with_answer, SEC_KEY)
# The helper only appends "(asked the <display_name> to check this.)" —
# verify the note suffix (last line) does NOT include the sentinel.
note_line = noted.strip().rsplit("\n", maxsplit=1)[-1]
check(
    "[5] appended note does not contain sentinel answer",
    SENTINEL_ANSWER not in note_line,
    f"note_line={repr(note_line)} full={repr(noted)}",
)


# ================================================================== #
# Test 6: Helper output against the actual response-text shape
# ================================================================== #
NOTED_REPLY = council._append_consult_note("All good.", SEC_KEY)
check(
    "[6] consult path produces note ending correctly",
    NOTED_REPLY.endswith("\n\n(asked the Security Engineer to check this.)"),
)


# ================================================================== #
# Summary
# ================================================================== #
print("\n=========== CONSULT NOTE QA (EU-603) ===========")
for n, ok, d in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({d})" if d and not ok else ""))
p = sum(1 for _, ok, _ in results if ok)
print(f"  {p}/{len(results)} passed", "✅" if p == len(results) else "❌")
assert p == len(results)
