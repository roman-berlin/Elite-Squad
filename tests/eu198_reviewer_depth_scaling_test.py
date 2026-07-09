"""EU-198: Scale review depth down for trivial diffs.

Offline harness: tests that the Reviewer scales effort/turns based on diff size + nature:
  • Trivial diffs (tests-only, <30 lines, no production .tsx/.py changes) → low effort, max_turns ≤ 10
  • Production diffs → full cfg.reviewer_effort, max_turns = 30
  • Sizing decision is observable in the run log
"""
import sys, types, os, asyncio, io
from contextlib import redirect_stdout

# ── Rich SDK stub (installed BEFORE importing orchestrator) ────────────────────────────────────
sdk = types.ModuleType("claude_agent_sdk")

class ClaudeAgentOptions:
    def __init__(self, **kw):
        self.model = kw.get("model")
        self.env = dict(kw.get("env") or {})
        for k, v in kw.items():
            if k not in ("model", "env"):
                setattr(self, k, v)

class AssistantMessage:
    def __init__(self, content=None, error=None):
        self.content = content or []
        self.error = error

class ResultMessage:
    def __init__(self,result="", total_cost_usd=0.0, num_turns=1, is_error=False, usage=None):
        self.result = result
        self.total_cost_usd = total_cost_usd
        self.num_turns = num_turns
        self.is_error = is_error
        self.usage = usage or {"input_tokens": 1, "output_tokens": 1}

class TextBlock:
    def __init__(self, text=""):
        self.text = text

class ToolUseBlock:
    def __init__(self, name="", input=None):
        self.name = name
        self.input = input

# Spy query — records each call's (model, env) and yields a controllable result.
CALLS = []

async def _spy_query(prompt=None, options=None, **kw):
    CALLS.append({
        "model": getattr(options, "model", None),
        "max_turns": getattr(options, "max_turns", None),
        "effort": getattr(options, "effort", None),
    })
    yield AssistantMessage(content=[TextBlock("ok")])
    yield ResultMessage(result="ok", is_error=False)

sdk.ClaudeAgentOptions = ClaudeAgentOptions
sdk.AssistantMessage = AssistantMessage
sdk.ResultMessage = ResultMessage
sdk.TextBlock = TextBlock
sdk.ToolUseBlock = ToolUseBlock
sdk.query = _spy_query
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import reviewer, memory, config, contracts
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ── Test data ───────────────────────────────────────────────────────────────────────────────────

# Trivial diff: tests-only, <30 lines, no production files
TRIVIAL_DIFF = """\
diff --git a/tests/foo_test.py b/tests/foo_test.py
index 123..456 789
--- a/tests/foo_test.py
+++ b/tests/foo_test.py
@@ -1,5 +1,10 @@
 def test_foo():
     assert True
+def test_bar():
+    assert True
"""

# Production diff: changes a production .tsx file
PRODUCTION_DIFF_TSX = """\
diff --git a/src/components/Button.tsx b/src/components/Button.tsx
index 123..456 789
--- a/src/components/Button.tsx
+++ b/src/components/Button.tsx
@@ -1,5 +1,10 @@
 export const Button = () => {
-  return <button>Click</button>
+  return <button className="btn">Click</button>
 }
"""

# Production diff: changes a production .py file
PRODUCTION_DIFF_PY = """\
diff --git a/orchestrator/reviewer.py b/orchestrator/reviewer.py
index 123..456 789
--- a/orchestrator/reviewer.py
+++ b/orchestrator/reviewer.py
@@ -1,5 +1,10 @@
 def review(diff: str):
-    return True
+    return False
"""

# Large diff: tests-only but ≥30 lines
LARGE_DIFF = """\
""" + "\n".join(f"+ line {i}" for i in range(35)) + """

diff --git a/tests/test_large.py b/tests/test_large.py
index 123..456 789
"""

# Mixed diff: test file changes AND production file changes
MIXED_DIFF = PRODUCTION_DIFF_TSX + "\n" + TRIVIAL_DIFF

class _Cfg:
    """Minimal cfg stand-in."""
    def __init__(self, reviewer_effort="high"):
        self.reviewer_effort = reviewer_effort
        self.auto_model = False

class _App:
    """Minimal app stand-in."""
    def __init__(self):
        self.repo_path = "/tmp/fake-repo"
        self.workdir = None

def _fake_ticket():
    """Minimal ticket stand-in."""
    return contracts.Ticket(
        id="TEST-1",
        key="TEST-1",
        summary="Test ticket",
        description="Test description",
        acceptance_criteria=[],
    )

# ── 1. _classify_diff: trivial diff detection ─────────────────────────────────────────────────────

chk("trivial diff (tests-only, <30 lines)",
    reviewer._classify_diff(TRIVIAL_DIFF) == ("trivial", "tests-only, <30 lines"))

chk("production diff (.tsx file changed)",
    reviewer._classify_diff(PRODUCTION_DIFF_TSX) == ("production", ".tsx production file"))

chk("production diff (.py file changed)",
    reviewer._classify_diff(PRODUCTION_DIFF_PY) == ("production", ".py production file"))

chk("large diff (≥30 lines) is production",
    reviewer._classify_diff(LARGE_DIFF) == ("production", "≥30 lines"))

chk("mixed diff (test + production) is production",
    reviewer._classify_diff(MIXED_DIFF) == ("production", ".tsx production file"))

chk("empty diff is trivial",
    reviewer._classify_diff("") == ("trivial", "empty diff"))

# ── 2. _effort_for_diff: scales effort/turns by classification ──────────────────────────────────────
cfg = _Cfg(reviewer_effort="high")
trivial_cat, trivial_reason = reviewer._classify_diff(TRIVIAL_DIFF)
prod_cat, prod_reason = reviewer._classify_diff(PRODUCTION_DIFF_TSX)

trivial_effort, trivial_turns = reviewer._effort_for_diff(trivial_cat, cfg)
prod_effort, prod_turns = reviewer._effort_for_diff(prod_cat, cfg)

chk("trivial diff → low effort", trivial_effort == "low")
chk("trivial diff → max_turns ≤ 10", trivial_turns <= 10)
chk("production diff → high effort", prod_effort == "high")
chk("production diff → max_turns = 30", prod_turns == 30)

# Test with different reviewer_effort settings
cfg_medium = _Cfg(reviewer_effort="medium")
prod_effort_med, prod_turns_med = reviewer._effort_for_diff(prod_cat, cfg_medium)
chk("production diff (medium cfg) → medium effort", prod_effort_med == "medium")
chk("production diff (medium cfg) → max_turns = 30", prod_turns_med == 30)

# ── 3. Sizing decision is observable in the run log ────────────────────────────────────────────────
async def _run_review_with_capture(diff_text):
    CALLS.clear()
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        await reviewer.review(
            diff=diff_text,
            ticket=_fake_ticket(),
            app=_App(),
            cfg=cfg,
        )
    return buffer.getvalue()

# Run with trivial diff and capture output
trivial_output = asyncio.run(_run_review_with_capture(TRIVIAL_DIFF))
chk("trivial sizing decision in log",
    "trivial diff → low effort, 10 turns" in trivial_output.lower())

# Run with production diff and capture output
prod_output = asyncio.run(_run_review_with_capture(PRODUCTION_DIFF_TSX))
chk("production sizing decision in log",
    "production diff → high effort, 30 turns" in prod_output.lower())

# ── 4. End-to-end: actual ClaudeAgentOptions reflect the classification ───────────────────────────
CALLS.clear()
asyncio.run(_run_review_with_capture(TRIVIAL_DIFF))
chk("trivial review: max_turns ≤ 10",
    CALLS and CALLS[0]["max_turns"] <= 10)
chk("trivial review: effort = low",
    CALLS and CALLS[0]["effort"] == "low")

CALLS.clear()
asyncio.run(_run_review_with_capture(PRODUCTION_DIFF_TSX))
chk("production review: max_turns = 30",
    CALLS and CALLS[0]["max_turns"] == 30)
chk("production review: effort = high",
    CALLS and CALLS[0]["effort"] == "high")

# ── tally ──────────────────────────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok, _ in results if ok)
print("\n=========== EU-198 REVIEWER DEPTH SCALING QA ===========")
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
