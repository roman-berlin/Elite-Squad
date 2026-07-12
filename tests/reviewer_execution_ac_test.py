"""EU-268 regression: the Reviewer must never self-report spec_met=true on an acceptance criterion
whose wording demands runtime/test execution ("all N tests pass", "on Desktop Chrome/Mobile
Safari") when the Reviewer is READ-ONLY (Bash disallowed — reviewer.py allowed_tools) and no
execution evidence (a passing test log / gate report) is present in the Builder's handoff narrative.

Grounded in AUTO-109: the GLM-4.6 reviewer set spec_met=true, blocking=0 for AC 3/4 ("All 6 Google
Sync tests pass on Desktop Chrome and Mobile Safari") from static inspection alone, and the ticket
merged to DEV self-admittedly unverified.

Pinned here:
  1. _ac_requires_execution — detects execution-dependent AC wording; a static AC ("function has a
     docstring") is not flagged.
  2. _has_execution_evidence — pure marker scan over the BuildArtifact NARRATIVE fields only
     (diff_digest/decisions/open_questions/caveats); the diff and committed-source paths in
     files_changed are NOT evidence sources (runtime output lives in the handoff, not in source).
  3. _enforce_execution_gate (pure) — no evidence -> forces spec_met=False, verdict FAIL, and a
     spec_gap naming the unverified AC; evidence present -> leaves the result unchanged.
  4. Reviewer tool permissions are unchanged: allowed_tools == [Read, Grep, Glob], Bash still
     disallowed — this is a verdict-logic fix, not new execution capability.
  5. End-to-end review(): the mock AC "All 6 tests pass on Desktop Chrome and Mobile Safari" with no
     evidence attached never comes back spec_met=true, even when the LLM's own JSON said met=true.

All offline — SDK and agents stubbed; no network, no real models.
"""
import asyncio
import sys
import types

# ── SDK stub (no real model calls) — mirrors tests/eu249_test_collectability_test.py ──────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): self.__dict__.update(k)
    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import reviewer as reviewer_mod        # noqa: E402
from orchestrator.config import Config, AppConfig        # noqa: E402
from orchestrator.contracts import BuildArtifact, ReviewResult, Ticket, Verdict  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


EXEC_AC = "All 6 tests pass on Desktop Chrome and Mobile Safari"
STATIC_AC = "function has a docstring"

# ══════════════════════════════════════════════════════════════════════════════
# 1. _ac_requires_execution
# ══════════════════════════════════════════════════════════════════════════════
chk("_ac_requires_execution: 'All 6 tests pass on Desktop Chrome and Mobile Safari' -> True",
    reviewer_mod._ac_requires_execution(EXEC_AC) is True)
chk("_ac_requires_execution: a static AC ('function has a docstring') -> False",
    reviewer_mod._ac_requires_execution(STATIC_AC) is False)
chk("_ac_requires_execution: 'all tests pass' (generic) -> True",
    reviewer_mod._ac_requires_execution("all tests pass") is True)
chk("_ac_requires_execution: 'verify in browser that the layout looks right' -> True",
    reviewer_mod._ac_requires_execution("verify in browser that the layout looks right") is True)
chk("_ac_requires_execution: empty string -> False",
    reviewer_mod._ac_requires_execution("") is False)

# ══════════════════════════════════════════════════════════════════════════════
# 2. _has_execution_evidence
# ══════════════════════════════════════════════════════════════════════════════
no_evidence_ba = BuildArtifact(files_changed=["e2e/google-sync.spec.ts"],
                               diff_digest="Implemented Google Sync per spec; all 6 tests pass on "
                                          "Desktop Chrome and Mobile Safari.",
                               decisions=[], open_questions=[])
chk("_has_execution_evidence: an AC's own claim echoed back in the digest is NOT evidence",
    reviewer_mod._has_execution_evidence(no_evidence_ba, "") is False)

evidence_ba = BuildArtifact(files_changed=["e2e/google-sync.spec.ts"],
                            diff_digest="Implemented Google Sync per spec.",
                            decisions=["Ran the gate report: 6/6 passed on Desktop Chrome and Mobile Safari"],
                            open_questions=[])
chk("_has_execution_evidence: a 'gate report' marker in decisions -> True",
    reviewer_mod._has_execution_evidence(evidence_ba, "") is True)

# Iter-2 tightened contract: execution evidence is runtime OUTPUT in the handoff, never committed
# SOURCE. The diff is no longer an evidence source, so a 'test log' marker sitting in the diff no
# longer unlocks the gate (previously this returned True — the assertion is inverted per the review).
evidence_diff = "test log: 6 passed, 0 failed\n"
chk("_has_execution_evidence: a 'test log' marker in the DIFF is NOT evidence (committed source is "
    "never runtime output) -> False",
    reviewer_mod._has_execution_evidence(None, evidence_diff) is False)

# ...and a committed report PATH in files_changed must not disable the gate either (a Playwright
# ticket legitimately commits a 'playwright-report/' path).
committed_source_ba = BuildArtifact(files_changed=["playwright-report/index.html", "test-results.json"],
                                    diff_digest="Implemented Google Sync per spec.",
                                    decisions=[], open_questions=[])
chk("_has_execution_evidence: a committed report path in files_changed is NOT evidence -> False",
    reviewer_mod._has_execution_evidence(committed_source_ba, "") is False)

# ...and untrusted PROSE ('verified/confirmed passing') no longer unlocks the gate — the Builder must
# attach a log/report/results file, not merely assert a sentence (prose alternations dropped).
prose_only_ba = BuildArtifact(files_changed=["e2e/google-sync.spec.ts"],
                              diff_digest="I verified passing all 6 tests and confirmed passing on Safari.",
                              decisions=[], open_questions=[])
chk("_has_execution_evidence: bare 'verified/confirmed passing' prose is NOT evidence -> False",
    reviewer_mod._has_execution_evidence(prose_only_ba, "") is False)

# Evidence attached in the uncapped caveats field is honoured (a real machine-shaped marker).
caveats_evidence_ba = BuildArtifact(files_changed=["e2e/google-sync.spec.ts"],
                                    diff_digest="Implemented Google Sync per spec.",
                                    decisions=[], open_questions=[],
                                    caveats=["Attached the passing test log for the 6 specs."])
chk("_has_execution_evidence: an attached 'test log' marker in caveats -> True",
    reviewer_mod._has_execution_evidence(caveats_evidence_ba, "") is True)

chk("_has_execution_evidence: no build_artifact, no diff -> False",
    reviewer_mod._has_execution_evidence(None, "") is False)

# ══════════════════════════════════════════════════════════════════════════════
# 3. _enforce_execution_gate (pure)
# ══════════════════════════════════════════════════════════════════════════════
_tk_exec = Ticket(id="AUTO-109", key="AUTO-109", summary="Google Sync",
                  description="d", acceptance_criteria=[EXEC_AC])
_tk_static = Ticket(id="EU-268", key="EU-268", summary="s", description="d",
                    acceptance_criteria=[STATIC_AC])

_pass_result = ReviewResult(verdict=Verdict.PASS, spec_met=True, spec_gaps=[], summary="looks fine")
_enforced = reviewer_mod._enforce_execution_gate(
    ReviewResult(verdict=_pass_result.verdict, spec_met=_pass_result.spec_met,
                spec_gaps=list(_pass_result.spec_gaps), summary=_pass_result.summary),
    _tk_exec, no_evidence_ba, "")
chk("_enforce_execution_gate: no evidence -> spec_met forced to False",
    _enforced.spec_met is False, str(_enforced.spec_met))
chk("_enforce_execution_gate: no evidence -> verdict forced to FAIL",
    _enforced.verdict == Verdict.FAIL, str(_enforced.verdict))
chk("_enforce_execution_gate: no evidence -> a spec_gap names the unverified AC",
    any(EXEC_AC in g for g in _enforced.spec_gaps), str(_enforced.spec_gaps))

_unaffected = reviewer_mod._enforce_execution_gate(
    ReviewResult(verdict=Verdict.PASS, spec_met=True, spec_gaps=[], summary="looks fine"),
    _tk_exec, evidence_ba, "")
chk("_enforce_execution_gate: execution evidence present -> spec_met left unchanged (True)",
    _unaffected.spec_met is True, str(_unaffected.spec_met))
chk("_enforce_execution_gate: execution evidence present -> verdict left unchanged (PASS)",
    _unaffected.verdict == Verdict.PASS, str(_unaffected.verdict))
chk("_enforce_execution_gate: execution evidence present -> no spec_gap added",
    _unaffected.spec_gaps == [], str(_unaffected.spec_gaps))

_no_exec_ac = reviewer_mod._enforce_execution_gate(
    ReviewResult(verdict=Verdict.PASS, spec_met=True, spec_gaps=[], summary="looks fine"),
    _tk_static, None, "")
chk("_enforce_execution_gate: ticket has no execution-dependent AC -> result untouched",
    _no_exec_ac.spec_met is True and _no_exec_ac.verdict == Verdict.PASS)

# ══════════════════════════════════════════════════════════════════════════════
# 4. Reviewer tool permissions unchanged (verdict-logic fix only, no new capability)
# ══════════════════════════════════════════════════════════════════════════════
_captured_options: dict = {}


class _RR:
    def __init__(self, t):
        (self.final, self.text, self.is_error, self.cost_usd, self.num_turns, self.tools,
         self.provider, self.model_version) = t, t, False, 0.0, 1, [], "Anthropic", "claude-sonnet-4-6"
        self.input_tokens = self.output_tokens = 0


# The LLM's own JSON claims spec_conformance.met=true, blocking=0 — exactly the AUTO-109 shape the
# deterministic gate must override.
_PASS_MET_JSON = ('```json\n{"verdict":"PASS","spec_conformance":{"met":true,"gaps":[]},'
                  '"quality":{"issues":[]},"required_changes":[],"summary":"all 6 tests pass"}\n```')


async def _fake_run_agent(prompt, options, tag="", cfg=None, routing_tier=None):
    _captured_options["allowed_tools"] = getattr(options, "allowed_tools", None)
    _captured_options["disallowed_tools"] = getattr(options, "disallowed_tools", None)
    return _RR(_PASS_MET_JSON)


_orig_run_agent_fb = reviewer_mod.run_agent_with_fallback
reviewer_mod.run_agent_with_fallback = _fake_run_agent

_rcfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                               protected_branch="MAIN", backlog_backend="none")],
              audit_path="/tmp/eu268-audit.jsonl", use_worktree=False, auto_model=False)
_rapp = _rcfg.app("automatixy")
_rtk_exec = Ticket(id="AUTO-109", key="AUTO-109", summary="Google Sync (2 of 2)",
                   description="d", acceptance_criteria=[EXEC_AC])

# ══════════════════════════════════════════════════════════════════════════════
# 5. End-to-end review(): no evidence -> the Reviewer does not emit spec_met=true, ever
# ══════════════════════════════════════════════════════════════════════════════
_no_evidence_result = asyncio.run(reviewer_mod.review(
    "diff --git a/e2e/google-sync.spec.ts b/e2e/google-sync.spec.ts\n"
    "+// 6 Playwright specs for Google Sync\n",
    _rtk_exec, _rapp, _rcfg, build_artifact=no_evidence_ba))
chk("reviewer: mock AC 'All 6 tests pass on Desktop Chrome and Mobile Safari' with NO evidence "
    "attached -> spec_met is NEVER true, even though the LLM verdict JSON said met=true",
    _no_evidence_result.spec_met is False, str(_no_evidence_result.spec_met))
chk("reviewer: the same unverified diff cannot be ship-ready",
    _no_evidence_result.is_ship_ready() is False)
chk("reviewer: verdict forced to FAIL on the unverified execution AC",
    _no_evidence_result.verdict == Verdict.FAIL, str(_no_evidence_result.verdict))

chk("reviewer permissions: allowed_tools == ['Read', 'Grep', 'Glob'] (unchanged — no new capability)",
    _captured_options.get("allowed_tools") == ["Read", "Grep", "Glob"],
    str(_captured_options.get("allowed_tools")))
chk("reviewer permissions: 'Bash' remains in disallowed_tools",
    "Bash" in (_captured_options.get("disallowed_tools") or []),
    str(_captured_options.get("disallowed_tools")))

# Evidence present (a gate report in decisions) -> the genuinely-verified execution AC still passes.
_evidenced_result = asyncio.run(reviewer_mod.review(
    "diff --git a/e2e/google-sync.spec.ts b/e2e/google-sync.spec.ts\n"
    "+// 6 Playwright specs for Google Sync\n",
    _rtk_exec, _rapp, _rcfg, build_artifact=evidence_ba))
chk("reviewer: execution evidence present (gate report in decisions) -> spec_met stays true",
    _evidenced_result.spec_met is True, str(_evidenced_result.spec_met))
chk("reviewer: execution evidence present -> verdict stays PASS",
    _evidenced_result.verdict == Verdict.PASS, str(_evidenced_result.verdict))

# A ticket with only static ACs is entirely unaffected.
_rtk_static = Ticket(id="EU-268", key="EU-268", summary="s", description="d",
                     acceptance_criteria=[STATIC_AC])
_static_result = asyncio.run(reviewer_mod.review("diff", _rtk_static, _rapp, _rcfg, build_artifact=None))
chk("reviewer: a ticket with only static ACs is unaffected (stays PASS)",
    _static_result.spec_met is True and _static_result.verdict == Verdict.PASS)

reviewer_mod.run_agent_with_fallback = _orig_run_agent_fb

print("\n================= EU-268 REVIEWER EXECUTION-AC GATE =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
