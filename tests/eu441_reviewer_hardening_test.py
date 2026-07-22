"""EU-441 regression: harden the Reviewer gate so it mechanically blocks a handoff that ships
production code WITHOUT the unit's required safety nets — missing tests and missing/eroded typing.

The Builder convention is "tests with the code" + full typing, but until EU-441 the Reviewer
ACCEPTED prose claims of coverage instead of enforcing them. tests (x276) and correctness (x114)
are the top two recurring rework areas (49-ticket pattern logged 2026-06-30). This adds two
deterministic diff-string backstops in the exact shape of the existing
_enforce_admitted_red_tests / _enforce_execution_gate gates (pure functions that take a
ReviewResult + diff and force FAIL when a condition holds), and sharpens REVIEWER_SYSTEM with a
REQUIRED-CHECKS block so the LLM reviewer cannot silently wave tests/typing/tenant-isolation/
error-handling through.

Stages (each leaves the tree green): run a single stage with `python3 tests/eu441_..._test.py
stage-N`, or everything with no arg / `all`.

  stage-1 — pure helpers _diff_changed_production_files / _diff_has_test_file /
            _untyped_public_defs (+ the shared _added_python_production_lines cursor).
  stage-2 — _enforce_missing_tests (AC 1, 3): production diff, no test, stubbed-LLM PASS -> forced
            FAIL (blocker, area 'tests'); with test / docs / config / tests-only -> no block.
  stage-3 — _enforce_missing_typing (AC 2, 4): untyped public def or explicit Any -> FAIL (major,
            area 'typing'); private/dunder/removed-lines/fully-typed/non-.py -> no block.
  stage-4 — REVIEWER_SYSTEM REQUIRED-CHECKS block (AC 5) names all four areas.
  stage-5 — end-to-end review() (SDK + run_agent_with_fallback stubbed) forced FAIL + unchanged
            tool permissions (AC 1, 2, 6).

All offline — SDK and agents stubbed; no network, no real models.
"""
import asyncio
import sys
import types

# ── SDK stub (no real model calls) — mirrors tests/reviewer_execution_ac_test.py ────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): self.__dict__.update(k)
    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import reviewer as R       # noqa: E402
from orchestrator.config import Config, AppConfig  # noqa: E402
from orchestrator.contracts import ReviewResult, Ticket, Verdict  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def _pass_result() -> ReviewResult:
    """A fresh PASS/spec_met result — the deterministic gates mutate the object they receive, so
    every call site needs its own instance (mirrors reviewer_execution_ac_test.py)."""
    return ReviewResult(verdict=Verdict.PASS, spec_met=True, spec_gaps=[], summary="looks fine")


# ── fixture diffs ────────────────────────────────────────────────────────────────────────────
# Production .py, >=5 added code lines, FULLY typed, NO test file -> only the missing-tests gate fires.
PROD_TYPED_NO_TEST = (
    "diff --git a/src/lib.py b/src/lib.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/lib.py\n"
    "@@ -0,0 +1,6 @@\n"
    "+def add(a: int, b: int) -> int:\n"
    "+    return a + b\n"
    "+\n"
    "+def greet(name: str) -> str:\n"
    "+    return f\"hi {name}\"\n"
    "+\n"
    "+CONST = 42\n"
)

# Same production code, but a test file accompanies it -> missing-tests gate stays silent.
PROD_TYPED_WITH_TEST = PROD_TYPED_NO_TEST + (
    "diff --git a/tests/test_lib.py b/tests/test_lib.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/tests/test_lib.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def test_add():\n"
    "+    assert add(1, 2) == 3\n"
)

# Production .py with an UNTYPED public def, plus a test file (so only the typing gate fires).
PROD_UNTYPED_WITH_TEST = (
    "diff --git a/src/svc.py b/src/svc.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/svc.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def process(data):\n"
    "+    return data\n"
    "diff --git a/tests/test_svc.py b/tests/test_svc.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/tests/test_svc.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def test_process():\n"
    "+    assert process(1) == 1\n"
)

# Production .py with an explicit Any annotation (return-typed def, but erodes typing) + test file.
PROD_ANY_WITH_TEST = (
    "diff --git a/src/svc.py b/src/svc.py\n"
    "+++ b/src/svc.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def process(data: int) -> int:\n"
    "+    cache: Any = {}\n"
    "diff --git a/tests/test_svc.py b/tests/test_svc.py\n"
    "+++ b/tests/test_svc.py\n"
    "@@ -0,0 +1,1 @@\n"
    "+def test_any(): pass\n"
)

DOCS_ONLY = (
    "diff --git a/README.md b/README.md\n"
    "+++ b/README.md\n"
    "@@ -1,3 +1,8 @@\n"
    "+# Title\n"
    "+Some documentation paragraph one.\n"
    "+Some documentation paragraph two.\n"
    "+Some documentation paragraph three.\n"
    "+Some documentation paragraph four.\n"
    "+Some documentation paragraph five.\n"
)

CONFIG_ONLY = (
    "diff --git a/config.example.yaml b/config.example.yaml\n"
    "+++ b/config.example.yaml\n"
    "@@ -1,1 +1,7 @@\n"
    "+app:\n"
    "+  name: demo\n"
    "+  repo_path: .\n"
    "+  base_branch: DEV\n"
    "+  protected_branch: MAIN\n"
    "+  backlog_backend: none\n"
)

TESTS_ONLY = (
    "diff --git a/tests/test_widget.py b/tests/test_widget.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/tests/test_widget.py\n"
    "@@ -0,0 +1,6 @@\n"
    "+def test_one():\n"
    "+    assert True\n"
    "+\n"
    "+def test_two():\n"
    "+    assert True\n"
    "+\n"
)

# Fully-typed production .py + test file -> neither gate fires (clean PASS).
PROD_TYPED_CLEAN_WITH_TEST = (
    "diff --git a/src/util.py b/src/util.py\n"
    "+++ b/src/util.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def double(x: int) -> int:\n"
    "+    return x * 2\n"
    "diff --git a/tests/test_util.py b/tests/test_util.py\n"
    "+++ b/tests/test_util.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def test_double():\n"
    "+    assert double(3) == 6\n"
)

# A private helper and a dunder, both untyped — must NOT trip the typing gate; fully-typed public
# def alongside. Plus a REMOVED untyped def (must never be counted) and a non-.py (.tsx) "function".
TYPING_NEGATIVES = (
    "diff --git a/src/neg.py b/src/neg.py\n"
    "+++ b/src/neg.py\n"
    "@@ -1,4 +1,7 @@\n"
    "+def public_ok(x: int) -> int:\n"        # fully annotated -> not flagged
    "+    return x\n"
    "+def _helper(data):                    # private -> not flagged\n"
    "+    return data\n"
    "+def __init__(self):                   # dunder -> not flagged\n"
    "+    pass\n"
    "-def old_removed(value):               # removed line -> never inspected\n"
    "-    return value\n"
    "diff --git a/src/cmp.tsx b/src/cmp.tsx\n"
    "+++ b/src/cmp.tsx\n"
    "@@ -0,0 +1,1 @@\n"
    "+export function untyped_ts() { return 1 }   # non-.py -> typing scan skips it\n"
)

# EU-441 iter-2 regression — MULTI-LINE public def signatures. The single-line 'fully-typed never
# flagged' checks above miss these: a 'def name(' line with NO '->' must NOT be flagged when the
# '->' appears on the closing ')' line of the SAME signature. Mirrors orchestrator/routing.py
# classify_task / should_route_to_local, which are fully typed across multiple lines today.
# (a) fully-typed multi-line signature — '-> RoutingTier:' is on the line that closes the parens.
PROD_MULTILINE_TYPED_WITH_TEST = (
    "diff --git a/src/router.py b/src/router.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/router.py\n"
    "@@ -0,0 +1,8 @@\n"
    "+def classify_task(\n"
    "+    ticket_description: str = \"\",\n"
    "+    task_type: str = \"\",\n"
    "+    effort: str = \"\",\n"
    "+    size: str = \"\",\n"
    "+) -> RoutingTier:\n"
    "+    return RoutingTier.LOCAL\n"
    "diff --git a/tests/test_router.py b/tests/test_router.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/tests/test_router.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def test_classify():\n"
    "+    assert True\n"
)

# (b) same multi-line shape but GENUINELY untyped — no '->' anywhere in the signature span.
PROD_MULTILINE_UNTYPED_WITH_TEST = (
    "diff --git a/src/router.py b/src/router.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/router.py\n"
    "@@ -0,0 +1,8 @@\n"
    "+def classify_task(\n"
    "+    ticket_description=\"\",\n"
    "+    task_type=\"\",\n"
    "+    effort=\"\",\n"
    "+    size=\"\",\n"
    "+):\n"
    "+    return None\n"
    "diff --git a/tests/test_router.py b/tests/test_router.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/tests/test_router.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def test_classify():\n"
    "+    assert True\n"
)


# ════════════════════════════════════════════════════════════════════════════════════════════
# stage-1 — pure helpers
# ════════════════════════════════════════════════════════════════════════════════════════════
def stage1() -> None:
    chk("S1 _diff_changed_production_files: PROD_TYPED_NO_TEST lists src/lib.py",
        R._diff_changed_production_files(PROD_TYPED_NO_TEST) != [],
        str(R._diff_changed_production_files(PROD_TYPED_NO_TEST)))
    chk("S1 _diff_changed_production_files: lib.py is detected",
        any("lib.py" in p for p in R._diff_changed_production_files(PROD_TYPED_NO_TEST)))
    chk("S1 _diff_changed_production_files: docs-only diff -> [] (no .py/.tsx production file)",
        R._diff_changed_production_files(DOCS_ONLY) == [], str(R._diff_changed_production_files(DOCS_ONLY)))
    chk("S1 _diff_changed_production_files: config-only diff -> []",
        R._diff_changed_production_files(CONFIG_ONLY) == [])
    chk("S1 _diff_changed_production_files: tests-only diff -> [] (test files are not production)",
        R._diff_changed_production_files(TESTS_ONLY) == [])
    chk("S1 _diff_changed_production_files: .tsx production file is detected",
        any(p.endswith(".tsx") for p in R._diff_changed_production_files(TYPING_NEGATIVES)))
    chk("S1 _diff_changed_production_files: the .tsx file's sibling test in TYPING_NEGATIVES is "
        "NOT counted as production",
        not any("test" in p.lower() for p in R._diff_changed_production_files(TYPING_NEGATIVES)))

    chk("S1 _diff_has_test_file: PROD_TYPED_WITH_TEST -> True",
        R._diff_has_test_file(PROD_TYPED_WITH_TEST) is True)
    chk("S1 _diff_has_test_file: a .test.ts path -> True",
        R._diff_has_test_file("+++ b/src/app.test.ts\n+// x\n") is True)
    chk("S1 _diff_has_test_file: a .spec.tsx path -> True",
        R._diff_has_test_file("+++ b/src/widget.spec.tsx\n+// x\n") is True)
    chk("S1 _diff_has_test_file: a _test.py path -> True",
        R._diff_has_test_file("+++ b/foo_test.py\n+1\n") is True)
    chk("S1 _diff_has_test_file: PROD_TYPED_NO_TEST (no test) -> False",
        R._diff_has_test_file(PROD_TYPED_NO_TEST) is False)
    chk("S1 _diff_has_test_file: docs-only -> False",
        R._diff_has_test_file(DOCS_ONLY) is False)

    untyped_clean = R._untyped_public_defs(PROD_UNTYPED_WITH_TEST)
    chk("S1 _untyped_public_defs: PROD_UNTYPED_WITH_TEST flags 'process'",
        any("process" in u for u in untyped_clean), str(untyped_clean))
    chk("S1 _untyped_public_defs: TYPING_NEGATIVES finds NO untyped public def "
        "(private/dunder/removed/annotated all excluded)",
        R._untyped_public_defs(TYPING_NEGATIVES) == [], str(R._untyped_public_defs(TYPING_NEGATIVES)))
    chk("S1 _untyped_public_defs: a .tsx 'function' is never inspected (Python-only)",
        R._untyped_public_defs("+++ b/src/x.tsx\n+export function f() { return 1 }\n") == [])
    chk("S1 _untyped_public_defs: a removed untyped def is never counted",
        R._untyped_public_defs("+++ b/x.py\n+def ok(a: int) -> int:\n+    return a\n"
                               "-def removed_old(v):\n-    return v\n") == [])

    # EU-441 iter-2 — multi-line PUBLIC def signatures (the single-line checks above miss this).
    # (a) a fully-typed multi-line signature (-> on the ')' line) must NOT be flagged.
    chk("S1 _untyped_public_defs: fully-typed MULTI-LINE def (-> on the ')' line) is NOT flagged",
        R._untyped_public_defs(PROD_MULTILINE_TYPED_WITH_TEST) == [],
        str(R._untyped_public_defs(PROD_MULTILINE_TYPED_WITH_TEST)))
    # (b) a genuinely untyped multi-line signature (no -> anywhere in the span) IS flagged.
    chk("S1 _untyped_public_defs: genuinely untyped MULTI-LINE def IS flagged",
        any("classify_task" in u for u in R._untyped_public_defs(PROD_MULTILINE_UNTYPED_WITH_TEST)),
        str(R._untyped_public_defs(PROD_MULTILINE_UNTYPED_WITH_TEST)))


# ════════════════════════════════════════════════════════════════════════════════════════════
# stage-2 — _enforce_missing_tests (AC 1, 3)
# ════════════════════════════════════════════════════════════════════════════════════════════
def stage2() -> None:
    r = R._enforce_missing_tests(_pass_result(), PROD_TYPED_NO_TEST)
    chk("S2 missing-tests: production diff, no test -> verdict FAIL",
        r.verdict == Verdict.FAIL, str(r.verdict))
    tests_block = [q for q in r.quality_issues if q.area == "tests"]
    chk("S2 missing-tests: a tests quality issue is recorded",
        len(tests_block) >= 1, str([(q.severity, q.area) for q in r.quality_issues]))
    chk("S2 missing-tests: the tests block is blocking (blocker|major)",
        any(q.severity in ("blocker", "major") for q in tests_block),
        str([(q.severity, q.area) for q in tests_block]))
    chk("S2 missing-tests: the detail mentions missing tests",
        any("missing test" in q.detail.lower() for q in tests_block),
        str([q.detail for q in tests_block]))

    # Inverse (AC 1): same production diff that ALSO changes a test file -> NO missing-tests block.
    r_inv = R._enforce_missing_tests(_pass_result(), PROD_TYPED_WITH_TEST)
    chk("S2 missing-tests: production diff WITH a test file -> verdict stays PASS",
        r_inv.verdict == Verdict.PASS, str(r_inv.verdict))
    chk("S2 missing-tests: production diff WITH a test file -> no tests block added",
        not any(q.area == "tests" for q in r_inv.quality_issues),
        str([(q.severity, q.area) for q in r_inv.quality_issues]))

    # AC 3 — zero false positives on the four safe shapes.
    for label, diff in (("docs-only", DOCS_ONLY), ("config-only", CONFIG_ONLY),
                        ("tests-only", TESTS_ONLY), ("prod+test", PROD_TYPED_WITH_TEST)):
        rr = R._enforce_missing_tests(_pass_result(), diff)
        chk(f"S2 missing-tests: {label} diff -> NOT forced FAIL (no false positive)",
            rr.verdict == Verdict.PASS and not any(q.area == "tests" for q in rr.quality_issues),
            str(rr.verdict) + str([(q.severity, q.area) for q in rr.quality_issues]))

    # A genuine one-liner (< MIN added code lines) is exempt — A2.
    tiny = ("diff --git a/src/lib.py b/src/lib.py\n+++ b/src/lib.py\n"
            "@@ -1,1 +1,1 @@\n+def add(a: int, b: int) -> int:\n")
    r_tiny = R._enforce_missing_tests(_pass_result(), tiny)
    chk("S2 missing-tests: a <5-line production change is exempt (no false positive)",
        r_tiny.verdict == Verdict.PASS, str(r_tiny.verdict))


# ════════════════════════════════════════════════════════════════════════════════════════════
# stage-3 — _enforce_missing_typing (AC 2, 4)
# ════════════════════════════════════════════════════════════════════════════════════════════
def stage3() -> None:
    r = R._enforce_missing_typing(_pass_result(), PROD_UNTYPED_WITH_TEST)
    chk("S3 missing-typing: untyped public def -> verdict FAIL",
        r.verdict == Verdict.FAIL, str(r.verdict))
    typing_block = [q for q in r.quality_issues if q.area == "typing"]
    chk("S3 missing-typing: a typing quality issue is recorded",
        len(typing_block) >= 1, str([(q.severity, q.area) for q in r.quality_issues]))
    chk("S3 missing-typing: the typing block is blocking (blocker|major)",
        any(q.severity in ("blocker", "major") for q in typing_block))

    r_any = R._enforce_missing_typing(_pass_result(), PROD_ANY_WITH_TEST)
    chk("S3 missing-typing: explicit Any annotation -> verdict FAIL",
        r_any.verdict == Verdict.FAIL, str(r_any.verdict))
    chk("S3 missing-typing: explicit Any -> a typing block is recorded",
        any(q.area == "typing" for q in r_any.quality_issues))

    # AC 4 — fully-typed public code, private/dunder/removed/non-.py -> no block.
    r_neg = R._enforce_missing_typing(_pass_result(), TYPING_NEGATIVES)
    chk("S3 missing-typing: private/dunder/removed/fully-typed/.tsx -> NOT forced FAIL",
        r_neg.verdict == Verdict.PASS and not any(q.area == "typing" for q in r_neg.quality_issues),
        str(r_neg.verdict) + str([(q.severity, q.area) for q in r_neg.quality_issues]))

    r_clean = R._enforce_missing_typing(_pass_result(), PROD_TYPED_CLEAN_WITH_TEST)
    chk("S3 missing-typing: fully-annotated public def -> NOT forced FAIL",
        r_clean.verdict == Verdict.PASS and not any(q.area == "typing" for q in r_clean.quality_issues),
        str(r_clean.verdict))

    chk("S3 missing-typing: a .tsx 'function' is never inspected (Python-only)",
        R._enforce_missing_typing(_pass_result(),
                                  "+++ b/src/x.tsx\n+export function f() { return 1 }\n").verdict
        == Verdict.PASS)
    chk("S3 missing-typing: a removed untyped def is never counted",
        R._enforce_missing_typing(_pass_result(),
                                  "+++ b/x.py\n+def ok(a: int) -> int:\n+    return a\n"
                                  "-def removed_old(v):\n-    return v\n").verdict == Verdict.PASS)

    # EU-441 iter-2 — the multi-line regression through the full typing gate.
    # (a) fully-typed multi-line public def (-> on the ')' line) -> verdict stays PASS.
    r_ml_typed = R._enforce_missing_typing(_pass_result(), PROD_MULTILINE_TYPED_WITH_TEST)
    chk("S3 missing-typing: fully-typed MULTI-LINE public def -> verdict stays PASS",
        r_ml_typed.verdict == Verdict.PASS and not any(q.area == "typing" for q in r_ml_typed.quality_issues),
        str(r_ml_typed.verdict) + str([(q.severity, q.area) for q in r_ml_typed.quality_issues]))
    # (b) genuinely untyped multi-line public def (no -> in the span) -> verdict FAIL.
    r_ml_untyped = R._enforce_missing_typing(_pass_result(), PROD_MULTILINE_UNTYPED_WITH_TEST)
    chk("S3 missing-typing: genuinely untyped MULTI-LINE public def -> verdict FAIL",
        r_ml_untyped.verdict == Verdict.FAIL and any(q.area == "typing" for q in r_ml_untyped.quality_issues),
        str(r_ml_untyped.verdict) + str([(q.severity, q.area) for q in r_ml_untyped.quality_issues]))


# ════════════════════════════════════════════════════════════════════════════════════════════
# stage-4 — REVIEWER_SYSTEM REQUIRED-CHECKS block (AC 5)
# ════════════════════════════════════════════════════════════════════════════════════════════
def stage4() -> None:
    sys_txt = R.REVIEWER_SYSTEM
    chk("S4 REVIEWER_SYSTEM: contains a REQUIRED-CHECKS block",
        "REQUIRED-CHECKS" in sys_txt)
    for area in ("tests", "typing", "tenant-isolation", "error-handling"):
        chk(f"S4 REVIEWER_SYSTEM: REQUIRED-CHECKS names '{area}'", area in sys_txt)


# ════════════════════════════════════════════════════════════════════════════════════════════
# stage-5 — end-to-end review() (AC 1, 2, 6)
# ════════════════════════════════════════════════════════════════════════════════════════════
class _RR:
    def __init__(self, t):
        (self.final, self.text, self.is_error, self.cost_usd, self.num_turns, self.tools,
         self.provider, self.model_version) = t, t, False, 0.0, 1, [], "Anthropic", "claude-sonnet-4-6"
        self.input_tokens = self.output_tokens = 0


# The LLM's own JSON claims PASS, spec_met=true, zero issues — exactly the AUTO-14/AUTO-18 shape the
# deterministic gates must override.
_PASS_MET_JSON = ('```json\n{"verdict":"PASS","spec_conformance":{"met":true,"gaps":[]},'
                  '"quality":{"issues":[]},"required_changes":[],"summary":"all good"}\n```')

_captured: dict = {}


async def _fake_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None,
                          cfg=None, routing_tier=None):
    _captured["allowed_tools"] = getattr(options, "allowed_tools", None)
    _captured["disallowed_tools"] = getattr(options, "disallowed_tools", None)
    return _RR(_PASS_MET_JSON)


def stage5() -> None:
    _orig = R.run_agent_with_fallback
    R.run_agent_with_fallback = _fake_run_agent
    try:
        cfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                                     protected_branch="MAIN", backlog_backend="none")],
                     audit_path="/tmp/eu441-audit.jsonl", use_worktree=False, auto_model=False)
        app = cfg.app("automatixy")
        tk = Ticket(id="EU-441", key="EU-441", summary="harden reviewer gate",
                    description="d", acceptance_criteria=["Reviewer blocks missing tests"])

        # AC 1 end-to-end: production diff, no test, LLM said PASS -> forced FAIL with a tests block.
        r1 = asyncio.run(R.review(PROD_TYPED_NO_TEST, tk, app, cfg))
        chk("S5 review(): production diff, no test -> verdict FAIL (overrode LLM PASS)",
            r1.verdict == Verdict.FAIL, str(r1.verdict))
        chk("S5 review(): a blocking tests issue is present",
            any(q.area == "tests" and q.severity in ("blocker", "major") for q in r1.quality_issues),
            str([(q.severity, q.area) for q in r1.quality_issues]))

        # AC 1 inverse end-to-end: same production code WITH a test file -> no missing-tests block.
        r1b = asyncio.run(R.review(PROD_TYPED_WITH_TEST, tk, app, cfg))
        chk("S5 review(): production diff WITH a test file -> no missing-tests block",
            not any(q.area == "tests" for q in r1b.quality_issues),
            str([(q.severity, q.area) for q in r1b.quality_issues]))

        # AC 2 end-to-end: untyped public def (test file present) -> typing block.
        r2 = asyncio.run(R.review(PROD_UNTYPED_WITH_TEST, tk, app, cfg))
        chk("S5 review(): untyped public def -> a blocking typing issue is present",
            any(q.area == "typing" and q.severity in ("blocker", "major") for q in r2.quality_issues),
            str([(q.severity, q.area) for q in r2.quality_issues]))

        # AC 6 — tool permissions unchanged by this verdict-logic hardening.
        chk("S5 review(): allowed_tools == ['Read', 'Grep', 'Glob'] (unchanged)",
            _captured.get("allowed_tools") == ["Read", "Grep", "Glob"],
            str(_captured.get("allowed_tools")))
        chk("S5 review(): 'Bash' remains in disallowed_tools",
            "Bash" in (_captured.get("disallowed_tools") or []),
            str(_captured.get("disallowed_tools")))

        # A clean, fully-typed production diff WITH a test file still ships PASS.
        r_clean = asyncio.run(R.review(PROD_TYPED_CLEAN_WITH_TEST, tk, app, cfg))
        chk("S5 review(): fully-typed production + test file -> verdict stays PASS",
            r_clean.verdict == Verdict.PASS, str(r_clean.verdict))
    finally:
        R.run_agent_with_fallback = _orig


_STAGES = {"stage-1": stage1, "stage-2": stage2, "stage-3": stage3,
           "stage-4": stage4, "stage-5": stage5}


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    if arg in ("all", "stage-all"):
        for fn in (stage1, stage2, stage3, stage4, stage5):
            fn()
    elif arg in _STAGES:
        _STAGES[arg]()
    else:
        print(f"unknown stage: {arg!r} (expected stage-1..5 or all)")
        return 2
    passed = sum(1 for _, ok, _ in results if ok)
    print("\n================ EU-441 REVIEWER GATE HARDENING ================")
    for n, ok, det in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
    print("----------------------------------------------------------------")
    print(f"  {passed}/{len(results)} passed")
    print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
