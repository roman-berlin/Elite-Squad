"""EU-350 regression: classifier + fingerprint for reviewer findings on an unverifiable surface.

Mirrors the EU-268 execution-gate helper pattern (`_ac_requires_execution` /
`_has_execution_evidence`) but classifies the REVIEWER'S FINDING text (a `QualityIssue.detail` /
spec-gap string) rather than the ticket's AC text. A READ-ONLY reviewer (Bash disallowed, no
browser/renderer) structurally cannot verify a "looks like" / pixel / layout / browser-render
claim by reading a diff — this pins:

  1. `_classify_unverifiable_finding` — returns the lens key ('ui_visual', 'layout_pixel',
     'browser_behavior') for finding text on such a surface, `None` for ordinary logic/data/test
     findings.
  2. `_finding_fingerprint` — a stable, order-independent, whitespace/case-tolerant signature of
     (lens, detail); identical inputs modulo whitespace/case fingerprint identically, genuinely
     different (lens, detail) pairs fingerprint differently.
  3. Additive-only / behavior-neutral: `review()`'s returned Verdict/blocking_issues for the
     existing EU-268 execution-gate fixture is unchanged by this ticket.

All offline — SDK and agents stubbed; no network, no real models.
"""
import asyncio
import sys
import types

# ── SDK stub (no real model calls) — mirrors tests/reviewer_execution_ac_test.py ──────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): self.__dict__.update(k)
    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import reviewer as reviewer_mod        # noqa: E402
from orchestrator.config import Config, AppConfig        # noqa: E402
from orchestrator.contracts import BuildArtifact, Ticket, Verdict  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# ══════════════════════════════════════════════════════════════════════════════
# 1. _classify_unverifiable_finding — representative UI/visual/pixel/layout/browser-behavior text
# ══════════════════════════════════════════════════════════════════════════════
UI_VISUAL_CASES = [
    "the button looks misaligned",
    "screenshot shows wrong color",
]
LAYOUT_PIXEL_CASES = [
    "layout shifts by 4px on the viewport",
]
BROWSER_BEHAVIOR_CASES = [
    "renders incorrectly in Mobile Safari",
]

for _t in UI_VISUAL_CASES:
    chk(f"_classify_unverifiable_finding: {_t!r} -> 'ui_visual'",
        reviewer_mod._classify_unverifiable_finding(_t) == "ui_visual",
        str(reviewer_mod._classify_unverifiable_finding(_t)))

for _t in LAYOUT_PIXEL_CASES:
    chk(f"_classify_unverifiable_finding: {_t!r} -> 'layout_pixel'",
        reviewer_mod._classify_unverifiable_finding(_t) == "layout_pixel",
        str(reviewer_mod._classify_unverifiable_finding(_t)))

for _t in BROWSER_BEHAVIOR_CASES:
    chk(f"_classify_unverifiable_finding: {_t!r} -> 'browser_behavior'",
        reviewer_mod._classify_unverifiable_finding(_t) == "browser_behavior",
        str(reviewer_mod._classify_unverifiable_finding(_t)))

# ══════════════════════════════════════════════════════════════════════════════
# 2. _classify_unverifiable_finding — None for ordinary logic/data/test findings
# ══════════════════════════════════════════════════════════════════════════════
ORDINARY_CASES = [
    "missing null check on user_id",
    "off-by-one in the loop",
    "test does not assert the return value",
]
for _t in ORDINARY_CASES:
    chk(f"_classify_unverifiable_finding: {_t!r} -> None",
        reviewer_mod._classify_unverifiable_finding(_t) is None,
        str(reviewer_mod._classify_unverifiable_finding(_t)))

chk("_classify_unverifiable_finding: empty string -> None",
    reviewer_mod._classify_unverifiable_finding("") is None)
chk("_classify_unverifiable_finding: None text -> None",
    reviewer_mod._classify_unverifiable_finding(None) is None)

# ══════════════════════════════════════════════════════════════════════════════
# 3. _finding_fingerprint — stable across whitespace/case, differs for genuinely different findings
# ══════════════════════════════════════════════════════════════════════════════
fp_a = reviewer_mod._finding_fingerprint("ui_visual", "Button  Looks OFF")
fp_b = reviewer_mod._finding_fingerprint("ui_visual", "button looks off")
chk("_finding_fingerprint: whitespace/case variation -> identical fingerprint",
    fp_a == fp_b, f"{fp_a!r} vs {fp_b!r}")

fp_c = reviewer_mod._finding_fingerprint("ui_visual", "  button   looks\toff  ")
chk("_finding_fingerprint: tabs/leading/trailing whitespace variation -> identical fingerprint",
    fp_a == fp_c, f"{fp_a!r} vs {fp_c!r}")

fp_diff_detail = reviewer_mod._finding_fingerprint("ui_visual", "the header is the wrong color")
chk("_finding_fingerprint: genuinely different detail -> different fingerprint",
    fp_a != fp_diff_detail, f"{fp_a!r} vs {fp_diff_detail!r}")

fp_diff_lens = reviewer_mod._finding_fingerprint("layout_pixel", "Button  Looks OFF")
chk("_finding_fingerprint: same detail, different lens -> different fingerprint",
    fp_a != fp_diff_lens, f"{fp_a!r} vs {fp_diff_lens!r}")

chk("_finding_fingerprint: returns a string",
    isinstance(fp_a, str), str(type(fp_a)))

# ══════════════════════════════════════════════════════════════════════════════
# 4/5. Additive-only — the EU-268 execution-gate fixture's review() outcome is unchanged
# ══════════════════════════════════════════════════════════════════════════════
EXEC_AC = "All 6 tests pass on Desktop Chrome and Mobile Safari"

_captured_options: dict = {}


class _RR:
    def __init__(self, t):
        (self.final, self.text, self.is_error, self.cost_usd, self.num_turns, self.tools,
         self.provider, self.model_version) = t, t, False, 0.0, 1, [], "Anthropic", "claude-sonnet-4-6"
        self.input_tokens = self.output_tokens = 0


_PASS_MET_JSON = ('```json\n{"verdict":"PASS","spec_conformance":{"met":true,"gaps":[]},'
                  '"quality":{"issues":[]},"required_changes":[],"summary":"all 6 tests pass"}\n```')


async def _fake_run_agent(prompt, options, tag="", cfg=None, routing_tier=None):
    _captured_options["allowed_tools"] = getattr(options, "allowed_tools", None)
    return _RR(_PASS_MET_JSON)


_orig_run_agent_fb = reviewer_mod.run_agent_with_fallback
reviewer_mod.run_agent_with_fallback = _fake_run_agent

_rcfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                               protected_branch="MAIN", backlog_backend="none")],
              audit_path="/tmp/eu350-audit.jsonl", use_worktree=False, auto_model=False)
_rapp = _rcfg.app("automatixy")
_rtk_exec = Ticket(id="AUTO-109", key="AUTO-109", summary="Google Sync (2 of 2)",
                   description="d", acceptance_criteria=[EXEC_AC])

no_evidence_ba = BuildArtifact(files_changed=["e2e/google-sync.spec.ts"],
                               diff_digest="Implemented Google Sync per spec; all 6 tests pass on "
                                          "Desktop Chrome and Mobile Safari.",
                               decisions=[], open_questions=[])

_no_evidence_result = asyncio.run(reviewer_mod.review(
    "diff --git a/e2e/google-sync.spec.ts b/e2e/google-sync.spec.ts\n"
    "+// 6 Playwright specs for Google Sync\n",
    _rtk_exec, _rapp, _rcfg, build_artifact=no_evidence_ba))
chk("review(): EU-268 fixture — spec_met still forced False (unchanged by EU-350 addition)",
    _no_evidence_result.spec_met is False, str(_no_evidence_result.spec_met))
chk("review(): EU-268 fixture — verdict still forced FAIL (unchanged by EU-350 addition)",
    _no_evidence_result.verdict == Verdict.FAIL, str(_no_evidence_result.verdict))
chk("review(): EU-268 fixture — blocking_issues unchanged (empty, as before)",
    _no_evidence_result.blocking_issues == [], str(_no_evidence_result.blocking_issues))

reviewer_mod.run_agent_with_fallback = _orig_run_agent_fb

print("\n================= EU-350 REVIEWER UNVERIFIABLE-SURFACE CLASSIFIER =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
