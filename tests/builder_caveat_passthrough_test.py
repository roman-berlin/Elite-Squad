"""EU-267 — a builder caveat/limitation admission buried deep in the summary must survive into the
BuildArtifact handed to the Reviewer, independent of the 500-char diff_digest cap.

Regression case: AUTO-109's builder wrote "the e2e tests have broader environmental issues ... but
the edge function implementation is correct and ready for use" ~1.8k chars into its summary. Because
BuildArtifact.diff_digest is capped at 500 chars, that caveat was truncated away before the Reviewer
ever saw it. This harness proves:

  1. BuildArtifact.caveats is NOT subject to the 500-char diff_digest ceiling.
  2. builder._build_artifact / builder._caveats scan the FULL summary (not just the first 500 chars)
     and surface a caveat sitting at ~char 1800.
  3. reviewer._prompt includes that caveat text in the prompt handed to the Reviewer, even when
     diff_digest itself is truncated.
  4. A summary with no caveat/limitation language yields an empty caveats list (no false positives).
"""
import asyncio
import sys
sys.path.insert(0, ".")

from orchestrator.contracts import BuildArtifact, BuildResult, Ticket, Verdict
from orchestrator.config import Config, AppConfig
from orchestrator import builder, reviewer

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))


CAVEAT_TEXT = (
    "the e2e tests have broader environmental issues in this sandbox but the edge function "
    "implementation is correct and ready for use"
)

# -------------------------------------------------------------------------------------------- #
# 1. BuildArtifact.caveats is uncapped, independent of the 500-char diff_digest ceiling.
# -------------------------------------------------------------------------------------------- #
long_caveat = "x" * 1800 + " " + CAVEAT_TEXT
raised = False
try:
    ba_long = BuildArtifact(
        files_changed=[],
        diff_digest="short digest, well under 500 chars",
        decisions=[],
        open_questions=[],
        caveats=[long_caveat],
    )
except ValueError:
    raised = True
check("BuildArtifact accepts a >500-char caveats entry without raising (uncapped by design)",
      not raised)
check("BuildArtifact.caveats stores the long entry verbatim", not raised and ba_long.caveats == [long_caveat])


# -------------------------------------------------------------------------------------------- #
# 2. builder._build_artifact / _caveats scan the FULL summary — a caveat at ~char 1800 survives.
# -------------------------------------------------------------------------------------------- #
padding = " ".join(f"filler sentence number {i} about the change." for i in range(1, 60))
assert len(padding) > 1700, f"padding too short to place the caveat past char 1800: {len(padding)}"
summary = (
    "Implemented the requested edge function.\n\n"
    + padding
    + "\n\nHowever, " + CAVEAT_TEXT + ".\n\nTEST: (no UI — invoke the edge function directly)"
)
caveat_start = summary.index(CAVEAT_TEXT)
check("the caveat text in the fixture summary starts past char 1800 (matches AUTO-109 shape)",
      caveat_start > 1800, f"caveat starts at char {caveat_start}")

result = BuildResult(ok=True, summary=summary, raw="")
artifact = builder._build_artifact(result)
check("_build_artifact produces a BuildArtifact", isinstance(artifact, BuildArtifact))
check("_build_artifact.diff_digest is truncated to <= 500 chars (the cap still applies to digest)",
      len(artifact.diff_digest) <= 500)
check("_build_artifact.caveats contains the caveat text buried past char 1800 of the summary",
      any(CAVEAT_TEXT in c for c in artifact.caveats),
      detail=str(artifact.caveats))
check("the caveat text is ABSENT from the truncated diff_digest (proves it wasn't just riding along)",
      CAVEAT_TEXT not in artifact.diff_digest)


# -------------------------------------------------------------------------------------------- #
# 3. reviewer._prompt surfaces the caveat even though diff_digest is capped/truncated.
# -------------------------------------------------------------------------------------------- #
ticket = Ticket(id="AUTO-109", key="AUTO-109", summary="edge function",
                description="ship the edge function", acceptance_criteria=["works"])
prompt = reviewer._prompt("diff --git a/x b/x\n+change", ticket, build_artifact=artifact)
check("reviewer._prompt includes the caveat text reachable to the Reviewer",
      CAVEAT_TEXT in prompt)
check("reviewer._prompt labels the caveats section",
      "caveats/limitations:" in prompt)
check("reviewer._prompt's diff_digest line stays short (<=500 chars) even though caveats carry the caveat",
      len(artifact.diff_digest) <= 500 and CAVEAT_TEXT in prompt)


# -------------------------------------------------------------------------------------------- #
# 4. No false positives: a clean summary with no caveat/limitation language yields [].
# -------------------------------------------------------------------------------------------- #
clean_summary = (
    "Implemented the leads dashboard filter.\n\nDecisions:\n- used the existing query builder\n\n"
    "Open questions:\n- none\n\nTEST: /leads"
)
check("_caveats returns [] for a summary with no caveat/limitation language",
      builder._caveats(clean_summary) == [])
clean_artifact = builder._build_artifact(BuildResult(ok=True, summary=clean_summary, raw=""))
check("_build_artifact.caveats is [] for a clean summary (no false positives)",
      clean_artifact.caveats == [])
clean_prompt = reviewer._prompt("diff --git a/x b/x\n+change", ticket, build_artifact=clean_artifact)
check("reviewer._prompt omits the caveats line when there are none",
      "caveats/limitations:" not in clean_prompt)


# -------------------------------------------------------------------------------------------- #
# 5. Iteration-2: a benign use of 'however' is NOT flagged as a caveat (the weak marker needs
#    limitation-shaped context to fire; a plain aside must not trip it).
# -------------------------------------------------------------------------------------------- #
benign_however = "Implemented the filter. However, I also updated the README for clarity."
check("_caveats does NOT flag an ordinary benign 'however' aside (no limitation context)",
      builder._caveats(benign_however) == [],
      detail=str(builder._caveats(benign_however)))
# ...but a 'however' that DOES carry limitation context still surfaces (the marker isn't dead).
however_limited = "Implemented the filter. However, the dropdown test is not fully verified yet."
check("_caveats still flags a 'however' that carries genuine limitation context",
      any("not fully verified" in c for c in builder._caveats(however_limited)),
      detail=str(builder._caveats(however_limited)))


# -------------------------------------------------------------------------------------------- #
# 6. Iteration-2: an unresolved-test admission routed ONLY into caveats (diff_digest/decisions/
#    open_questions clean) must still force FAIL via the EU-249 deterministic backstop.
# -------------------------------------------------------------------------------------------- #
caveat_only_ba = BuildArtifact(
    files_changed=["src/x.test.tsx"],
    diff_digest="Implemented the edge function; summary is clean and green.",
    decisions=["used the existing query builder"],
    open_questions=[],
    caveats=["test files were created but encountered test infrastructure issues with localStorage mocking"],
)
check("_admitted_red_test_note fires when the ONLY unresolved-test admission is in caveats",
      reviewer._admitted_red_test_note(caveat_only_ba) is not None,
      detail=str(reviewer._admitted_red_test_note(caveat_only_ba)))

_PASS_JSON = ('```json\n{"verdict":"PASS","spec_conformance":{"met":true,"gaps":[]},'
             '"quality":{"issues":[]},"required_changes":[],"summary":"looks fine"}\n```')


class _RR:
    def __init__(self, t):
        (self.final, self.text, self.is_error, self.cost_usd, self.num_turns, self.tools,
         self.provider, self.model_version) = t, t, False, 0.0, 1, [], "Anthropic", "claude-sonnet-4-6"
        self.input_tokens = self.output_tokens = 0


# EU-258: widened to match the real agent.run_agent_with_fallback signature, which has always
# accepted ticket_id/pass_number — the reviewer call site now passes them (stub drift, not a
# contract change: production behaviour here is unchanged).
async def _fake_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None,
                          cfg=None, routing_tier=None):
    return _RR(_PASS_JSON)


_orig_fb = reviewer.run_agent_with_fallback
reviewer.run_agent_with_fallback = _fake_run_agent
try:
    _cfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  use_worktree=False, auto_model=False)
    _app = _cfg.app("automatixy")
    _res = asyncio.run(reviewer.review("diff", ticket, _app, _cfg, build_artifact=caveat_only_ba))
    check("review() forces FAIL when the caveats field admits an unresolved test (LLM said PASS)",
          _res.verdict == Verdict.FAIL and _res.blocking_issues, detail=str(_res.verdict))
finally:
    reviewer.run_agent_with_fallback = _orig_fb


# -------------------------------------------------------------------------------------------- #
# 7. Iteration-2: _caveats output is bounded (item count) so a pathologically verbose summary
#    can't grow the reviewer prompt without limit.
# -------------------------------------------------------------------------------------------- #
many = "\n".join(f"Caveat {i}: this edge case is not tested in the sandbox." for i in range(40))
bounded = builder._caveats(many)
check("_caveats bounds the number of returned items (<= max + elision marker)",
      len(bounded) <= builder._CAVEAT_MAX_ITEMS + 1,
      detail=f"got {len(bounded)} items")
check("_caveats appends an elision marker when items were dropped",
      any("elided to bound context" in c for c in bounded))
huge_item = "z" * 5000 + " this is unverified"
clipped = builder._caveats(huge_item)
check("_caveats clips an over-long single caveat entry",
      clipped and all(len(c) <= builder._CAVEAT_ITEM_MAX_CHARS for c in clipped),
      detail=f"max item len {max((len(c) for c in clipped), default=0)}")


# -------------------------------------------------------------------------------------------- #
if __name__ == "__main__":
    failed = [(n, d) for n, ok, d in results if not ok]
    for n, ok, d in results:
        print(("PASS" if ok else "FAIL") + f" - {n}" + (f" ({d})" if d and not ok else ""))
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)
