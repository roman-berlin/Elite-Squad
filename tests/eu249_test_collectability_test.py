"""EU-249 regression: the pre-merge gate must actually RUN added tests and verify they're
COLLECTABLE, and the Reviewer must never PASS a diff whose own handoff admits red/skipped tests.

Grounded in the AUTO-97 -> AUTO-101 -> AUTO-95 audit chain: a diff whose Builder self-reported
"test files were created but encountered test infrastructure issues" merged with a green gate and
a PASS review, and two test files landed at a doubled 'apps/zeltivo-crm/apps/zeltivo-crm/...' path
that matches no vitest include glob — invisible to the test runner forever.

Pinned here:
  1. added_test_files — extracts ADDED/RENAMED-TO *.test.*/*.spec.* paths from a unified diff;
     modified-in-place test files are not re-flagged.
  2. phantom_nested_test_paths — a duplicated 'apps/<x>' root segment is flagged, pure-string,
     zero runner cost.
  3. test_collectability_problems — a phantom-nested path is flagged by name; a path matching none
     of its owning app's vitest `include` globs is flagged citing the mismatch; a path that matches
     is clean.
  4. test_collectability_gate — end to end: phantom-nested fails naming the path; matching-include
     passes; a diff with no added test files never shells out; the scoped `bunx vitest run` only
     ever receives the ADDED files (never a bare full-suite invocation), and a red scoped run fails
     the gate with the vitest report text while a green one passes — both independent of a stubbed
     "22 pre-existing failures elsewhere" signal.
  5. Config gating — the check is a per-app opt-in (AppConfig.test_collectability_enabled); an app
     that hasn't armed it (e.g. the EU python gate) never triggers ANY of this, even on a diff that
     would otherwise fail collectability.
  6. Reviewer — a Builder self-report of a failing/skipped/infra-issue test in the BuildArtifact
     handoff forces verdict FAIL with a blocking issue, even when the LLM's own JSON said PASS with
     no blocking issues; a clean handoff is unaffected.

All offline — SDK and agents stubbed; no network, no real models.
"""
import asyncio
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

# ── SDK stub (no real model calls) ───────────────────────────────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): self.__dict__.update(k)
    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.gate as gate_mod                    # noqa: E402
from orchestrator import reviewer as reviewer_mod        # noqa: E402
from orchestrator.config import Config, AppConfig        # noqa: E402
from orchestrator.contracts import BuildArtifact, GateResult, Ticket, Verdict  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# ══════════════════════════════════════════════════════════════════════════════
# Fixture repo: apps/zeltivo-crm with a real-shaped vitest.config.ts
# ══════════════════════════════════════════════════════════════════════════════
REPO = Path(tempfile.mkdtemp())
ZC = REPO / "apps" / "zeltivo-crm"
ZC.mkdir(parents=True)
(ZC / "vitest.config.ts").write_text(
    "import { defineConfig } from 'vitest/config'\n"
    "export default defineConfig({\n"
    "  test: {\n"
    "    include: [\"src/**/*.{test,spec}.{ts,tsx}\"],\n"
    "  },\n"
    "})\n",
    encoding="utf-8",
)

PHANTOM_PATH = "apps/zeltivo-crm/apps/zeltivo-crm/src/foo.test.tsx"
GOOD_PATH = "apps/zeltivo-crm/src/foo.test.tsx"
OUTSIDE_INCLUDE_PATH = "apps/zeltivo-crm/e2e/foo.test.tsx"   # not nested, but not under src/


def _added_diff(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..e69de29\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1,3 @@\n"
        "+import { describe, it, expect } from 'vitest'\n"
        "+describe('foo', () => { it('works', () => { expect(1).toBe(1) }) })\n"
    )


def _modified_diff(path: str) -> str:
    # a pure modification (no 'new file mode', no '--- /dev/null') must NOT be treated as added.
    return (
        f"diff --git a/{path} b/{path}\n"
        "index abc123..def456 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,2 @@\n"
        " describe('foo', () => {})\n"
        "+it('more', () => {})\n"
    )


def app(**kw) -> AppConfig:
    base = dict(name="automatixy", repo_path=str(REPO), base_branch="DEV", workdir=str(REPO),
                gate_timeout_sec=30, backlog_backend="none")
    base.update(kw)
    return AppConfig(**base)


# ══════════════════════════════════════════════════════════════════════════════
# 1. added_test_files
# ══════════════════════════════════════════════════════════════════════════════
added = gate_mod.added_test_files(_added_diff(GOOD_PATH))
chk("added_test_files: detects a newly-added *.test.tsx", added == [GOOD_PATH], str(added))

not_added = gate_mod.added_test_files(_modified_diff(GOOD_PATH))
chk("added_test_files: a pure modification of an EXISTING test file is not flagged as added",
    not_added == [], str(not_added))

chk("added_test_files: a diff with no test files -> []",
    gate_mod.added_test_files("diff --git a/x.ts b/x.ts\nnew file mode 100644\n"
                              "--- /dev/null\n+++ b/x.ts\n") == [])

rename_diff = ("diff --git a/apps/zeltivo-crm/src/old.test.tsx b/apps/zeltivo-crm/src/new.test.tsx\n"
              "similarity index 100%\n"
              "rename from apps/zeltivo-crm/src/old.test.tsx\n"
              "rename to apps/zeltivo-crm/src/new.test.tsx\n")
chk("added_test_files: a RENAMED-TO test file is flagged (rename target, not source)",
    gate_mod.added_test_files(rename_diff) == ["apps/zeltivo-crm/src/new.test.tsx"])

# ══════════════════════════════════════════════════════════════════════════════
# 2. phantom_nested_test_paths — pure string check
# ══════════════════════════════════════════════════════════════════════════════
chk("phantom: apps/<x>/apps/<x>/... is flagged", gate_mod.phantom_nested_test_paths([PHANTOM_PATH]) == [PHANTOM_PATH])
chk("phantom: a normal apps/<x>/... path is clean", gate_mod.phantom_nested_test_paths([GOOD_PATH]) == [])
chk("phantom: two different apps/<x> roots in one path is clean (no duplicate root)",
    gate_mod.phantom_nested_test_paths(["apps/zeltivo-crm/apps/landing-page/x.test.tsx"]) == [])

# ══════════════════════════════════════════════════════════════════════════════
# 3. test_collectability_problems — include-glob matching against the real vitest.config.ts
# ══════════════════════════════════════════════════════════════════════════════
prob_good = gate_mod.test_collectability_problems(app(), [GOOD_PATH], str(REPO))
chk("collectability: a path matching the include glob is clean", prob_good == [], str(prob_good))

prob_phantom = gate_mod.test_collectability_problems(app(), [PHANTOM_PATH], str(REPO))
chk("collectability: the phantom-nested path is flagged BY NAME",
    len(prob_phantom) == 1 and PHANTOM_PATH in prob_phantom[0], str(prob_phantom))

prob_outside = gate_mod.test_collectability_problems(app(), [OUTSIDE_INCLUDE_PATH], str(REPO))
chk("collectability: a non-nested path outside the include globs is flagged, citing the mismatch",
    len(prob_outside) == 1 and OUTSIDE_INCLUDE_PATH in prob_outside[0]
    and "include" in prob_outside[0].lower(), str(prob_outside))

# an app whose owning dir has no vitest.config.ts at all -> nothing to validate against, skip clean
prob_unconfigured = gate_mod.test_collectability_problems(app(), ["apps/unknown-app/src/x.test.tsx"], str(REPO))
chk("collectability: an app with no vitest.config.ts is skipped (nothing to validate against)",
    prob_unconfigured == [], str(prob_unconfigured))

# ══════════════════════════════════════════════════════════════════════════════
# 4. test_collectability_gate — end to end, config-gated
# ══════════════════════════════════════════════════════════════════════════════
armed = app(test_collectability_enabled=True)
unarmed = app(test_collectability_enabled=False)

# AC1: phantom-nested path fails, naming the path.
r1 = gate_mod.test_collectability_gate(armed, [PHANTOM_PATH], _added_diff(PHANTOM_PATH))
chk("gate AC1: phantom-nested diff FAILS, naming the phantom path",
    not r1.passed and PHANTOM_PATH in r1.report, r1.report)

# AC1 (positive half) + AC4: a matching-include added file runs the scoped vitest and passes when green.
_orig_rc = gate_mod.run_commands
_captured: dict = {}


def _rc_stub_pass(app_, commands, cwd=None):
    _captured["cmd"] = commands
    _captured["cwd"] = cwd
    return GateResult(passed=True, report="1 passed")


try:
    gate_mod.run_commands = _rc_stub_pass
    r2 = gate_mod.test_collectability_gate(armed, [GOOD_PATH], _added_diff(GOOD_PATH))
finally:
    gate_mod.run_commands = _orig_rc
chk("gate AC1: a diff adding apps/zeltivo-crm/src/foo.test.tsx (matches include) PASSES",
    r2.passed, r2.report)
chk("gate: the scoped vitest run targets ONLY the added file, relative to the app dir "
    "(never a bare full-suite invocation)",
    _captured.get("cmd") and "vitest run" in _captured["cmd"][0] and "src/foo.test.tsx" in _captured["cmd"][0]
    and "apps/zeltivo-crm" not in _captured["cmd"][0].split("vitest run", 1)[1],
    str(_captured.get("cmd")))
chk("gate: the scoped vitest run's cwd is the OWNING app dir",
    (_captured.get("cwd") or "").endswith(os.path.join("apps", "zeltivo-crm")), str(_captured.get("cwd")))

# AC2: a *.test.tsx outside the include globs (not nested) fails citing the mismatch.
r3 = gate_mod.test_collectability_gate(armed, [OUTSIDE_INCLUDE_PATH], _added_diff(OUTSIDE_INCLUDE_PATH))
chk("gate AC2: a non-nested path outside vitest's include globs FAILS, citing the mismatch",
    not r3.passed and OUTSIDE_INCLUDE_PATH in r3.report and "include" in r3.report.lower(), r3.report)

# AC3: an added test that fails/errors at the scoped vitest run fails the gate and returns the report.
def _rc_stub_fail(app_, commands, cwd=None):
    _captured["cmd"] = commands
    return GateResult(passed=False, report="FAIL src/foo.test.tsx\n1 failed, 0 passed\nAssertionError: boom")


try:
    gate_mod.run_commands = _rc_stub_fail
    r4 = gate_mod.test_collectability_gate(armed, [GOOD_PATH], _added_diff(GOOD_PATH))
finally:
    gate_mod.run_commands = _orig_rc
chk("gate AC3: a red scoped vitest run FAILS the gate and returns the vitest report to the builder",
    not r4.passed and "AssertionError" in r4.report, r4.report)

# AC3 (unaffected-by-pre-existing-failures half): a diff touching NO test files never shells out to
# vitest at all — it is categorically unaffected by any pre-existing (e.g. the 22 zeltivo-crm) failures.
_calls = {"n": 0}


def _rc_stub_count(app_, commands, cwd=None):
    _calls["n"] += 1
    return GateResult(passed=False, report="should never run")


try:
    gate_mod.run_commands = _rc_stub_count
    r5 = gate_mod.test_collectability_gate(armed, ["apps/zeltivo-crm/src/Button.tsx"],
                                           "diff --git a/apps/zeltivo-crm/src/Button.tsx b/apps/zeltivo-crm/src/Button.tsx\n"
                                           "index 1..2 100644\n--- a/apps/zeltivo-crm/src/Button.tsx\n"
                                           "+++ b/apps/zeltivo-crm/src/Button.tsx\n@@ -1 +1 @@\n-x\n+y\n")
finally:
    gate_mod.run_commands = _orig_rc
chk("gate AC3: a diff with no added/renamed test files never runs vitest (0 shell-outs) and passes",
    r5.passed and _calls["n"] == 0, f"calls={_calls['n']}, report={r5.report}")

# AC4: the gate is a per-app opt-in — an UNARMED app (e.g. the EU python gate) never triggers ANY
# check, even on a diff that would otherwise fail collectability.
_calls["n"] = 0
try:
    gate_mod.run_commands = _rc_stub_count
    r6 = gate_mod.test_collectability_gate(unarmed, [PHANTOM_PATH], _added_diff(PHANTOM_PATH))
finally:
    gate_mod.run_commands = _orig_rc
chk("gate AC4: an app with test_collectability_enabled unset/False never runs the check (opt-in)",
    r6.passed and _calls["n"] == 0, f"passed={r6.passed}, calls={_calls['n']}")

eu_app = AppConfig(name="Elite-Unit", repo_path=str(REPO), backlog_backend="none")
chk("gate AC4: default AppConfig (no flag set, e.g. the EU python gate) is unarmed by default",
    eu_app.test_collectability_enabled is False)

# ══════════════════════════════════════════════════════════════════════════════
# 5. run_deterministic_checks wires the gate in (aggregated report, same convention as LINT/SECRET/LOCKFILE)
# ══════════════════════════════════════════════════════════════════════════════
try:
    gate_mod.run_commands = _rc_stub_fail
    det = gate_mod.run_deterministic_checks(armed, [GOOD_PATH], _added_diff(GOOD_PATH))
finally:
    gate_mod.run_commands = _orig_rc
chk("run_deterministic_checks: a red scoped vitest run surfaces as a TEST-COLLECTABILITY failure",
    not det.passed and "TEST-COLLECTABILITY" in det.report, det.report[:300])

det_clean = gate_mod.run_deterministic_checks(unarmed, ["src/ok.py"], "+x = 1\n")
chk("run_deterministic_checks: an unarmed app / clean diff still passes (unchanged behaviour)",
    det_clean.passed, det_clean.report)

shutil.rmtree(REPO, ignore_errors=True)

# ══════════════════════════════════════════════════════════════════════════════
# 6. Reviewer — admitted red/skipped tests in the Builder handoff force FAIL
# ══════════════════════════════════════════════════════════════════════════════
class _RR:
    def __init__(self, t):
        (self.final, self.text, self.is_error, self.cost_usd, self.num_turns, self.tools,
         self.provider, self.model_version) = t, t, False, 0.0, 1, [], "Anthropic", "claude-sonnet-4-6"
        self.input_tokens = self.output_tokens = 0


_PASS_JSON = ('```json\n{"verdict":"PASS","spec_conformance":{"met":true,"gaps":[]},'
             '"quality":{"issues":[]},"required_changes":[],"summary":"looks fine"}\n```')


async def _fake_run_agent(prompt, options, tag="", cfg=None, routing_tier=None):
    return _RR(_PASS_JSON)


_orig_run_agent_fb = reviewer_mod.run_agent_with_fallback
reviewer_mod.run_agent_with_fallback = _fake_run_agent

_rcfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                               protected_branch="MAIN", backlog_backend="none")],
              audit_path=str(Path(tempfile.mkdtemp()) / "audit.jsonl"), use_worktree=False,
              auto_model=False)
_rapp = _rcfg.app("automatixy")
_rtk = Ticket(id="AUTO-97", key="AUTO-97", summary="s", description="d", acceptance_criteria=["a"])

admitted_ba = BuildArtifact(
    files_changed=["src/x.test.tsx"],
    diff_digest="Added tests; test files were created but encountered test infrastructure issues "
               "with localStorage mocking",
    decisions=[], open_questions=[],
)
try:
    admitted_result = asyncio.run(reviewer_mod.review("diff", _rtk, _rapp, _rcfg, build_artifact=admitted_ba))
finally:
    pass
chk("reviewer: LLM said PASS/blocking:0, but the handoff admits a test infra issue -> forced FAIL",
    admitted_result.verdict == Verdict.FAIL, str(admitted_result.verdict))
chk("reviewer: the forced failure is recorded as a BLOCKING issue",
    len(admitted_result.blocking_issues) >= 1, str(admitted_result.blocking_issues))
chk("reviewer: cannot emit PASS with blocking:0 on an admitted-red-test diff (is_ship_ready is False)",
    admitted_result.is_ship_ready() is False)

clean_ba = BuildArtifact(files_changed=["src/x.tsx"], diff_digest="Implemented the button per spec.",
                         decisions=["used existing Card component"], open_questions=[])
clean_result = asyncio.run(reviewer_mod.review("diff", _rtk, _rapp, _rcfg, build_artifact=clean_ba))
chk("reviewer: a clean handoff (no admitted test issues) is UNAFFECTED — stays PASS",
    clean_result.verdict == Verdict.PASS and not clean_result.blocking_issues, str(clean_result.verdict))

no_ba_result = asyncio.run(reviewer_mod.review("diff", _rtk, _rapp, _rcfg, build_artifact=None))
chk("reviewer: no build_artifact at all -> unaffected (back-compat, e.g. direct/CLI callers)",
    no_ba_result.verdict == Verdict.PASS)

skip_ba = BuildArtifact(files_changed=["src/x.test.tsx"], diff_digest="",
                        decisions=["Skipped the failing localStorage test for now to unblock the rest."],
                        open_questions=[])
skip_result = asyncio.run(reviewer_mod.review("diff", _rtk, _rapp, _rcfg, build_artifact=skip_ba))
chk("reviewer: a self-reported SKIPPED test (in decisions, not diff_digest) also forces FAIL",
    skip_result.verdict == Verdict.FAIL and skip_result.blocking_issues, str(skip_result.verdict))

# ── Iteration-2 regression: the admitted-red detector must NOT fire on ordinary SUCCESS handoffs.
# The v1 regex was broad enough to match 'Tests: 23 passed, 0 failed.' (a green summary) and
# 'Fixed the failing test, now green' (an explicitly RESOLVED failure) — bouncing perfectly good
# diffs. It must require a genuinely UNRESOLVED admission (infra issue / still failing / had to
# skip / couldn't get it to pass) and ignore negation/resolution contexts (no, 0, zero, none,
# fixed, resolved, now pass/green).
_MUST_NOT_FIRE = [
    "Tests: 23 passed, 0 failed.",
    "No tests failed.",
    "No tests failed; suite green.",
    "0 failing",
    "all tests pass, nothing broken",
    "Fixed the failing test, now green",
    "Fixed the previously failing localStorage test, now passing.",
    "all tests pass, nothing broke",
]
for _txt in _MUST_NOT_FIRE:
    _ba = BuildArtifact(files_changed=["src/x.test.tsx"], diff_digest=_txt, decisions=[], open_questions=[])
    chk(f"admitted-red: a normal success handoff is NOT flagged — {_txt!r}",
        reviewer_mod._admitted_red_test_note(_ba) is None,
        str(reviewer_mod._admitted_red_test_note(_ba)))

# The genuinely-unresolved admissions the detector MUST still catch (the AUTO-97 shapes).
_MUST_FIRE = [
    "test files were created but encountered test infrastructure issues with localStorage mocking",
    "Skipped the failing localStorage test for now to unblock the rest.",
    "couldn't get the localStorage tests to pass",
    "the suite is still failing after my change",
    "had to skip the broken dropdown test",
]
for _txt in _MUST_FIRE:
    _ba = BuildArtifact(files_changed=["src/x.test.tsx"], diff_digest=_txt, decisions=[], open_questions=[])
    chk(f"admitted-red: a genuinely unresolved failure/skip IS flagged — {_txt!r}",
        reviewer_mod._admitted_red_test_note(_ba) is not None, "not flagged")

# EU-267: the same admission routed into the NEW `caveats` field (diff_digest/decisions/open_questions
# clean) must still trip the backstop — caveats is scanned too, so it can't be a bypass channel.
for _txt in _MUST_FIRE:
    _ba = BuildArtifact(files_changed=["src/x.test.tsx"], diff_digest="clean green summary",
                        decisions=[], open_questions=[], caveats=[_txt])
    chk(f"admitted-red: an unresolved failure in caveats (only) IS flagged — {_txt!r}",
        reviewer_mod._admitted_red_test_note(_ba) is not None, "not flagged")
_caveat_only_ba = BuildArtifact(files_changed=["src/x.test.tsx"], diff_digest="clean green summary",
                                decisions=[], open_questions=[],
                                caveats=["test files were created but encountered test infrastructure "
                                         "issues with localStorage mocking"])
_caveat_only_res = asyncio.run(reviewer_mod.review("diff", _rtk, _rapp, _rcfg, build_artifact=_caveat_only_ba))
chk("reviewer: an unresolved-test admission living ONLY in caveats forces FAIL (EU-267)",
    _caveat_only_res.verdict == Verdict.FAIL and _caveat_only_res.blocking_issues,
    str(_caveat_only_res.verdict))

# Full review path: each MUST-NOT-FIRE success handoff stays PASS (not forced to FAIL).
for _txt in ("Tests: 23 passed, 0 failed.", "No tests failed; suite green.",
             "Fixed the previously failing localStorage test, now passing."):
    _ba = BuildArtifact(files_changed=["src/x.test.tsx"], diff_digest=_txt, decisions=[], open_questions=[])
    _res = asyncio.run(reviewer_mod.review("diff", _rtk, _rapp, _rcfg, build_artifact=_ba))
    chk(f"reviewer: success handoff stays PASS (not forced FAIL) — {_txt!r}",
        _res.verdict == Verdict.PASS and not _res.blocking_issues, str(_res.verdict))

reviewer_mod.run_agent_with_fallback = _orig_run_agent_fb

print("\n================= EU-249 TEST-COLLECTABILITY GATE QA =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
