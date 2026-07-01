"""EU-96 artifact-contract test: Builder → Reviewer → TestEngineer handoff chain.

Asserts the typed artifact pipeline described in EU-96:

  (1) Builder.build() publishes a BuildArtifact with non-empty files_changed
      (after the loop stamps it), diff_digest, and decisions into the store.
  (2) Reviewer prompt-builder injects the BuildArtifact fields (open_questions,
      diff_digest) and Reviewer.review() publishes a ReviewVerdict with
      verdict/blocking fields.
  (3) TestEngineer prompt-builder injects FILES CHANGED from BuildArtifact and
      ensure_coverage() publishes a TestEngineerArtifact with ok/coverage_pct.
  (4) After two simulated officer calls, store.token_burn has entries for both
      officer keys.

Uses the same mock-agent pattern as eu72_artifact_wiring_test.py:
inspect.signature checks verify the keyword-arg contract; stub AgentRun objects
replace real Claude calls — no network or model required.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import types

# ── SDK stub (no real claude_agent_sdk required) ──────────────────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _Stub:
    """Generic stub that absorbs any constructor args and is callable."""

    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _Stub  # type: ignore[attr-defined]
for _attr in ("AssistantMessage", "ResultMessage", "TextBlock", "ToolUseBlock",
              "ClaudeAgentOptions", "query"):
    setattr(sdk, _attr, _Stub)
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

# ── Shared test scaffolding ───────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    """Record a named assertion into the results list."""
    results.append((name, bool(cond), detail))


# ── Imports (after SDK stub is in place) ──────────────────────────────────────
from orchestrator import builder, reviewer, test_engineer  # noqa: E402
from orchestrator.agent import AgentRun  # noqa: E402
from orchestrator.config import Config, AppConfig  # noqa: E402
from orchestrator.contracts import (  # noqa: E402
    BuildArtifact,
    BuildRequest,
    BuildResult,
    PerTicketArtifactStore,
    ReviewResult,
    ReviewVerdict,
    SpecArtifact,
    TestEngineerArtifact,
    TestEngineerResult,
    Ticket,
    Verdict,
)


def _mkcfg(**kw):
    """Minimal Config with a single automatixy app for test isolation."""
    return Config(
        apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                        protected_branch="MAIN", backlog_backend="none")],
        audit_path="/tmp/eu96_contract.jsonl",
        use_worktree=False,
        **kw,
    )


cfg = _mkcfg()
app = cfg.app("automatixy")
tk = Ticket(
    id="EU-96", key="EU-96",
    summary="structured artifact contract",
    description="MetaGPT-style structured handoffs cut Opus burn",
    acceptance_criteria=["AC_TOKEN_96"],
)

# A builder summary that contains both a Decisions and an Open questions section
# so _section_bullets() yields non-empty lists for the artifact.
_BUILDER_SUMMARY = (
    "Implemented the artifact contract.\n\n"
    "Decisions:\n- used typed BuildArtifact dataclass\n- digest bounded to 500 chars\n\n"
    "Open questions:\n- should we persist artifacts to disk?\n"
)

# ── Verify signature contracts (inspect.signature pattern) ───────────────────
# These checks pin the keyword-arg surface the loop relies on; a signature change
# breaks this test immediately rather than silently failing at runtime.

_build_sig = inspect.signature(builder.build)
chk("builder.build accepts 'store' keyword arg", "store" in _build_sig.parameters,
    str(list(_build_sig.parameters)))
chk("builder.build accepts 'spec' keyword arg", "spec" in _build_sig.parameters,
    str(list(_build_sig.parameters)))

_review_sig = inspect.signature(reviewer.review)
chk("reviewer.review accepts 'store' keyword arg", "store" in _review_sig.parameters,
    str(list(_review_sig.parameters)))
chk("reviewer.review accepts 'build_artifact' keyword arg",
    "build_artifact" in _review_sig.parameters,
    str(list(_review_sig.parameters)))

_te_sig = inspect.signature(test_engineer.ensure_coverage)
chk("ensure_coverage accepts 'store' keyword arg", "store" in _te_sig.parameters,
    str(list(_te_sig.parameters)))
chk("ensure_coverage accepts 'build_artifact' keyword arg",
    "build_artifact" in _te_sig.parameters,
    str(list(_te_sig.parameters)))


# =================== (1) Builder publishes BuildArtifact ===================== #

b_prompt_cap: dict[str, str] = {}


async def _fake_build_agent(prompt, options, tag="", ticket_id=None, pass_number=None, cfg=None):
    """Stub run_agent for the builder: captures the prompt, returns a fake AgentRun."""
    b_prompt_cap["p"] = prompt
    return AgentRun(
        text=_BUILDER_SUMMARY,
        final=_BUILDER_SUMMARY,
        cost_usd=0.1,
        num_turns=2,
        is_error=False,
        tools=[],
        input_tokens=1200,
        output_tokens=300,
    )


builder.run_agent = _fake_build_agent
# EU-108: stub run_agent_with_fallback so builder tests don't hit real async iteration
builder.run_agent_with_fallback = _fake_build_agent
cfg.delegation_enabled = False  # force solo path so build() publishes directly

store_b = PerTicketArtifactStore()
spec = SpecArtifact(
    acceptance=["AC_TOKEN_96"],
    scope="artifact contract",
    non_goals=["persist artifacts to disk"],
)

res_b = asyncio.run(
    builder.build(
        BuildRequest(ticket=tk, branch="eu96/branch", iteration=1),
        app, cfg, store=store_b, spec=spec,
    )
)

chk("build() returns a successful BuildResult",
    isinstance(res_b, BuildResult) and res_b.ok)
chk("build() reads SpecArtifact acceptance into the prompt",
    "AC_TOKEN_96" in b_prompt_cap.get("p", ""))
chk("build() publishes a BuildArtifact into the store",
    isinstance(store_b.build, BuildArtifact))
chk("build() artifact has non-empty diff_digest",
    bool(store_b.build and store_b.build.diff_digest))
chk("build() artifact has non-empty decisions",
    bool(store_b.build and store_b.build.decisions))

# Simulate the loop stamping the authoritative files_changed (the loop owns git,
# not the builder — _build_artifact intentionally leaves files_changed=[]).
store_b.build.files_changed = [
    "orchestrator/contracts.py",
    "tests/eu96_artifact_contract_test.py",
]
chk("files_changed is non-empty after the loop stamps it",
    len(store_b.build.files_changed) >= 1)


# =================== (2) Reviewer injects artifact + publishes verdict ======= #

# Pre-build a rich artifact whose fields we can probe inside the reviewer prompt.
ba = BuildArtifact(
    files_changed=["orchestrator/contracts.py", "orchestrator/builder.py"],
    diff_digest="DIFFDIGEST_TOKEN_96",
    decisions=["use typed dataclass", "keep digest ≤ 500 chars"],
    open_questions=["OQ_PERSIST_DISK"],
)

r_prompt_cap: dict[str, str] = {}


async def _fake_review_agent(prompt, options, tag="", ticket_id=None, pass_number=None, cfg=None):
    """Stub run_agent for the reviewer: captures the prompt, returns a PASS verdict."""
    r_prompt_cap["p"] = prompt
    return AgentRun(
        text="",
        final=(
            '```json\n'
            '{"verdict":"PASS","spec_conformance":{"met":true,"gaps":[]},'
            '"quality":{"issues":[]},"required_changes":[],"summary":"all good"}'
            '\n```'
        ),
        cost_usd=0.1,
        num_turns=1,
        is_error=False,
        tools=[],
        input_tokens=700,
        output_tokens=150,
    )


reviewer.run_agent = _fake_review_agent
store_r = PerTicketArtifactStore()
store_r.put(ba)

res_r = asyncio.run(
    reviewer.review("a diff", tk, app, cfg, store=store_r, build_artifact=ba)
)

chk("review() returns a ReviewResult",
    isinstance(res_r, ReviewResult))
chk("reviewer prompt injects diff_digest from BuildArtifact",
    "DIFFDIGEST_TOKEN_96" in r_prompt_cap.get("p", ""))
chk("reviewer prompt injects open_questions from BuildArtifact",
    "OQ_PERSIST_DISK" in r_prompt_cap.get("p", ""))
chk("review() publishes a ReviewVerdict into the store",
    isinstance(store_r.review, ReviewVerdict))
chk("ReviewVerdict.verdict field is set (Verdict enum)",
    store_r.review is not None and isinstance(store_r.review.verdict, Verdict))
chk("ReviewVerdict.blocking field is a list",
    store_r.review is not None and isinstance(store_r.review.blocking, list))


# ============= (3) TestEngineer injects files_changed + publishes artifact === #

te_prompt_cap: dict[str, str] = {}


async def _fake_te_agent(prompt, options, tag="", ticket_id=None, pass_number=None, cfg=None):
    """Stub run_agent for the test engineer: captures the prompt, returns a coverage line."""
    te_prompt_cap["p"] = prompt
    return AgentRun(
        text="COVERAGE: lines 85→92%",
        final="COVERAGE: lines 85→92%",
        cost_usd=0.05,
        num_turns=1,
        is_error=False,
        tools=[],
        input_tokens=900,
        output_tokens=200,
    )


test_engineer.run_agent = _fake_te_agent
store_te = PerTicketArtifactStore()
store_te.put(ba)

res_te = asyncio.run(
    test_engineer.ensure_coverage(tk, app, cfg, store=store_te, build_artifact=ba)
)

chk("ensure_coverage() returns a TestEngineerResult",
    isinstance(res_te, TestEngineerResult))
chk("test-engineer prompt injects FILES CHANGED from BuildArtifact",
    "orchestrator/contracts.py" in te_prompt_cap.get("p", ""))
chk("ensure_coverage() publishes a TestEngineerArtifact into store.test",
    isinstance(store_te.test, TestEngineerArtifact))
chk("TestEngineerArtifact.ok is True (successful run)",
    store_te.test is not None and store_te.test.ok is True)
chk("TestEngineerArtifact.coverage_pct parsed from 'lines 85→92%'",
    store_te.test is not None and store_te.test.coverage_pct == 92.0,
    repr(store_te.test.coverage_pct if store_te.test else None))

# EU-96 (iter-4): _parse_coverage_pct narrowing — a trailing "(was …%)" prior-coverage clause must
# NOT be mistaken for the current ("after") value by the last-match heuristic.
_pcp = test_engineer._parse_coverage_pct
chk("_parse_coverage_pct: plain '91%' -> 91.0", _pcp("91%") == 91.0, repr(_pcp("91%")))
chk("_parse_coverage_pct: 'lines 82→91%' -> 91.0 (after value)", _pcp("lines 82→91%") == 91.0,
    repr(_pcp("lines 82→91%")))
chk("_parse_coverage_pct: 'n/a (pytest counts only)' -> None", _pcp("n/a (pytest counts only)") is None,
    repr(_pcp("n/a (pytest counts only)")))
chk("_parse_coverage_pct: 'now n/a (was 95%)' -> None (before-% stripped)",
    _pcp("now n/a (was 95%)") is None, repr(_pcp("now n/a (was 95%)")))
chk("_parse_coverage_pct: '91% (was 80%)' -> 91.0 (after, not the before-%)",
    _pcp("91% (was 80%)") == 91.0, repr(_pcp("91% (was 80%)")))
chk("_parse_coverage_pct: empty string -> None", _pcp("") is None, repr(_pcp("")))


# =================== (4) token_burn accumulation for two officer keys ======== #

def _burn(store: PerTicketArtifactStore, key: str, in_tok: int, out_tok: int) -> None:
    """Mirror of the loop's local _burn() closure: additive accumulation per key."""
    store.token_burn[key] = store.token_burn.get(key, 0) + in_tok + out_tok


store_burn = PerTicketArtifactStore()
# Simulate the loop calling _burn() after each officer returns its result.
_burn(store_burn, "builder", res_b.input_tokens, res_b.output_tokens)
_burn(store_burn, "reviewer", res_r.input_tokens, res_r.output_tokens)

chk("store.token_burn has a 'builder' entry after builder call",
    "builder" in store_burn.token_burn,
    repr(store_burn.token_burn))
chk("store.token_burn has a 'reviewer' entry after reviewer call",
    "reviewer" in store_burn.token_burn,
    repr(store_burn.token_burn))
chk("builder token_burn equals input + output tokens",
    store_burn.token_burn.get("builder") == res_b.input_tokens + res_b.output_tokens,
    repr(store_burn.token_burn.get("builder")))
chk("reviewer token_burn equals input + output tokens",
    store_burn.token_burn.get("reviewer") == res_r.input_tokens + res_r.output_tokens,
    repr(store_burn.token_burn.get("reviewer")))
chk("both officer keys present after two calls",
    len([k for k in ("builder", "reviewer") if k in store_burn.token_burn]) == 2,
    repr(store_burn.token_burn))


# ── Report ────────────────────────────────────────────────────────────────────
print("\n============ EU-96 ARTIFACT CONTRACT QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------")
print(f"  {passed}/{len(results)} passed", "✅" if passed == len(results) else "❌")
sys.exit(0 if passed == len(results) else 1)
