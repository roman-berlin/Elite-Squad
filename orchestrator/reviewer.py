"""Reviewer driver — a second, READ-ONLY Claude that judges the diff.

Read-only is enforced at the permission layer (disallowed_tools covers every
mutating tool), not by prompt alone: the reviewer can read the repo for context
but physically cannot edit it. It judges BOTH spec conformance and quality.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator

from claude_agent_sdk import ClaudeAgentOptions

from . import filing, memory
from .agent import run_agent, run_agent_with_fallback
from .config import AppConfig, Config, normalize_effort
from .contracts import (BuildArtifact, PerTicketArtifactStore, QualityIssue,
                       ReviewResult, ReviewVerdict, Ticket, Verdict)

REVIEWER_SYSTEM = """\
You are the Reviewer in an automated dev pipeline. You are deliberately adversarial:
your job is to catch problems before they ship, not to be agreeable.

You judge a diff on TWO axes:
1. Spec conformance — does it satisfy every acceptance criterion of the ticket?
2. Quality — architecture, correctness, security, error handling, edge cases,
   tests, and consistency with the codebase, PLUS regressions, scope creep, and
   resource/memory leaks (unclosed DB/network handles, dangling listeners, tests
   that don't clean up in afterEach/afterAll).

Engage your specialist audit lenses on the diffs that warrant them (skip the rest):
- Security (Security Engineer): authz, secret leakage, input validation, injection, unsafe queries → area "security".
- Accessibility: UI changes — keyboard/focus, ARIA, contrast, labels, RTL → area "a11y".
- Performance: N+1 queries, hot loops, render thrash, unbounded memory → area "performance".
- Test-coverage: is the changed behaviour actually tested? → area "tests".
Report each finding as a quality issue with the matching "area".

You may read files in the repo for context, but you cannot modify anything.

You MUST end your response with a single fenced ```json block, and nothing after it,
matching exactly this schema:

```json
{
  "verdict": "PASS" | "FAIL",
  "spec_conformance": { "met": true, "gaps": ["..."] },
  "quality": { "issues": [ { "severity": "blocker|major|minor", "area": "...", "detail": "..." } ] },
  "required_changes": ["concrete instruction for the builder if FAIL"],
  "needs_human": false,
  "question": "",
  "summary": "≤5 tight bullets — lead with each blocking issue and what to fix, then a one-sentence overall rationale. Example: '• Missing tenant filter on /leads query\\n• No test for the error path\\n• Passes otherwise.'"
}
```

Rules for the verdict:
- PASS only if spec_conformance.met is true AND there are no blocker or major issues.
- Otherwise FAIL, and required_changes must be specific enough to act on directly.
- Severity calibration (2026-07-19 Commander order — review like a SENIOR lead, not a gatekeeper):
  "blocker"/"major" are reserved for defects that violate an acceptance criterion, break
  behaviour/security/data, or make the change unreleasable. Broader test coverage than the
  criteria demand, robustness/selector hardening, refactor preferences, docs polish and other
  beyond-the-ticket wishes are "minor" — minors are auto-filed as follow-up tickets on ship, so
  recording them as minor LOSES NOTHING and failing the build over them blocks a done deliverable.
- NEVER emit FAIL when spec_conformance.met is true and no blocker or major issue exists — that
  state IS a PASS (with advisory findings). A FAIL that contradicts your own findings is treated
  as inconsistent and reconciled to PASS mechanically.
- Set "needs_human": true with a clear "question" ONLY when the work is blocked on a
  product/scope DECISION the Commander must make (ambiguous requirement, a trade-off, a
  missing acceptance criterion) — not for ordinary code fixes. When needs_human is true,
  set verdict to FAIL.
"""

REVIEWER_SYSTEM += """
EU-249 hard rule — admitted-red tests are ALWAYS blocking: if the Builder's own handoff (the
diff_digest/decisions/open_questions you were given) admits that a test is failing, was skipped, or
hit a "test infrastructure issue" it didn't resolve, you MUST record that as a blocker/major quality
issue (area "tests") and the verdict CANNOT be PASS. Do not let confident prose about the rest of the
diff talk you into passing a self-reported red or skipped test — a diff whose own author says the
tests didn't run/pass ships nothing verified, regardless of how the rest of the diff reads.
"""

REVIEWER_SYSTEM += """
EU-441 REQUIRED-CHECKS — before you can return verdict PASS you MUST have actively considered, and
either emitted a finding for OR explicitly confirmed clean, EACH of these areas when the diff touches
its surface. You may not silently skip one because "the rest looks fine" — a diff that touches one of
these surfaces and emits NO finding for it is treated as having skipped it, and three of them
(tests/typing/tenant-isolation) are ALSO enforced deterministically (see _enforce_missing_tests /
_enforce_missing_typing / _enforce_tenant_query), so a wave-through there is overridden to FAIL
regardless of your verdict:
- tests: all changed production (.py/.tsx) behaviour MUST be covered by a test in the diff. If a test
  is missing, emit a blocker/major (area "tests") — do NOT pass on an untested behaviour change.
- typing: every NEW public def/async def (Python) must carry a "->" return annotation; an explicit
  use of the Any type (uppercase, or lowercase-any in TS-style) erodes the type surface. Note:
  ``if TYPE_CHECKING:`` imports backed by ``from __future__ import annotations`` ARE concrete types
  satisfying the requirement — resolved at static-analysis time, never executed, never runtime Any.
  Each typing demand must be checked against the CURRENT diff before being re-raised to prevent
  infinite loops. Missing/eroded typing on new public code -> finding (area "typing").
- tenant-isolation: each query/list/export that reads or writes user-scoped data MUST carry a tenant
  filter (Supabase RLS or an explicit where-clause); a missing tenant filter is a cross-tenant
  data-leak and a blocker (area "security" or "tenant-isolation"). Name the exact query missing it.
- error-handling: new I/O, network, DB, or parsing calls MUST handle their failure paths (raise/return/
  retry) and not swallow exceptions silently. A swallowed error or unhandled rejection -> finding
  (area "error-handling" or "correctness").
If the surface applies, emit the finding (even minor) rather than passing silently.
"""

# EU-42: give the Reviewer the out-of-scope findings channel. A real-but-off-spec issue it notices
# while judging the diff (a bug/risk outside THIS ticket's scope) is emitted as the shared
# ===TICKETS=== block, which loop._route_out_of_scope parses off review.raw and routes into the
# backlog (de-duped, labeled out-of-scope) instead of letting it evaporate. Reuses filing.py — the
# same machine block the QA/Security/Release officers already use.
REVIEWER_SYSTEM += filing.TICKET_BLOCK_RULE


def _prompt(diff: str, ticket: Ticket, build_artifact: BuildArtifact | None = None) -> str:
    ac = "\n".join(f"  - {c}" for c in ticket.acceptance_criteria) or "  (none specified)"
    parts = [
        f"TICKET {ticket.id}: {ticket.summary}",
        "",
        "DESCRIPTION:",
        ticket.description or "(none)",
        "",
        "ACCEPTANCE CRITERIA:",
        ac,
    ]
    # EU-72: lead with the Builder's structured handoff (read it first), THEN the full diff below —
    # artifact-first, full-context-on-demand. Falls back to the diff alone when no artifact was passed.
    if build_artifact is not None:
        parts.append("")
        parts.append("BUILDER'S STRUCTURED HANDOFF (read first, then verify against the diff):")
        if build_artifact.files_changed:
            parts.append("  files changed: " + ", ".join(build_artifact.files_changed))
        if build_artifact.diff_digest:
            parts.append("  summary: " + build_artifact.diff_digest)
        if build_artifact.decisions:
            parts.append("  decisions: " + "; ".join(build_artifact.decisions))
        if build_artifact.open_questions:
            parts.append("  open questions: " + "; ".join(build_artifact.open_questions))
        # EU-267: caveats/limitations the builder flagged, surfaced independent of diff_digest's
        # 500-char cap — a caveat buried past char 500 of the summary (e.g. AUTO-109's unverified-test
        # admission) must still reach the Reviewer here even when diff_digest truncated it out.
        if build_artifact.caveats:
            parts.append("  caveats/limitations: " + "; ".join(build_artifact.caveats))
    parts += [
        "",
        "DIFF UNDER REVIEW (feature branch vs base):",
        "```diff",
        diff if diff.strip() else "(empty diff — the builder produced no changes)",
        "```",
        "",
        "Review it now. Read any files you need for context, then emit the JSON verdict.",
    ]
    return "\n".join(parts)


# EU-249: a deterministic backstop for the AUTO-97 failure mode — the Builder's OWN handoff admitted
# "test files were created but encountered test infrastructure issues with localStorage mocking" and
# the LLM reviewer still emitted PASS with blocking:0. The system-prompt rule above is the primary
# defence; this pair of regexes is the safety net that doesn't depend on the model noticing, so it
# can't silently regress.
#
# Iteration-2 narrowing: the v1 regex was too broad — it flagged ordinary GREEN summaries like
# "Tests: 23 passed, 0 failed." and explicitly RESOLVED failures like "Fixed the failing test, now
# green", bouncing perfectly good diffs. The detector now fires only on a genuinely UNRESOLVED
# admission and deliberately ignores negation/resolution contexts:
#   • STRONG signals fire unconditionally — an unambiguous unresolved admission (test infra issue,
#     "still failing", "not passing", "couldn't get … to pass", "had to skip", "never ran").
#   • WEAK signals (a bare "failing/broken/skipped … test") fire ONLY when the same field carries NO
#     negation/resolution context ("no", "0", "zero", "none", "fixed", "resolved", "now pass/green",
#     "all tests pass", "nothing broke") — so "0 failed", "No tests failed", "Fixed the failing test,
#     now green" and friends stay PASS.
# A false positive costs one extra builder pass; a false negative ships an unverified diff — but the
# earlier over-broad version was itself parking valid diffs, so precision here IS the fix.
_UNRESOLVED_STRONG_RE = re.compile(
    r"(?i)("
    r"\btest(?:s|ing)?\s+infra(?:structure)?\b"                          # "test infrastructure issues"
    r"|\binfra(?:structure)?\s+issues?\b[^.\n]{0,40}\btests?\b"
    r"|\btests?\b[^.\n]{0,40}\binfra(?:structure)?\s+issues?\b"
    r"|\bstill\s+(?:fail\w*|broken|red|not\s+pass\w*)\b"                 # "still failing"
    r"|\bnot\s+passing\b|\bnot\s+green\b"
    r"|\b(?:won'?t|can'?t|cannot|couldn'?t|could\s+not|unable\s+to)\b[^.\n]{0,40}\bpass\w*\b"
    r"|\bhad\s+to\s+skip\b"
    r"|\btests?\b[^.\n]{0,40}\bnever\s+(?:ran|passed|run)\b"
    r")"
)
# A bare "failing/broken/skipped … test" — ambiguous on its own (could be "Fixed the failing test").
_WEAK_RED_TEST_RE = re.compile(
    r"(?i)("
    r"\b(?:fail(?:ing|ed|s)?|broken)\b[^.\n]{0,30}\btests?\b"
    r"|\btests?\b[^.\n]{0,30}\b(?:fail(?:ing|ed|s)?|broken)\b"
    r"|\bskip(?:ped|ping|s)?\b[^.\n]{0,30}\btests?\b"
    r"|\btests?\b[^.\n]{0,30}\bskip(?:ped|ping|s)?\b"
    r")"
)
# Negation / resolution wording that clears a WEAK signal (the failure was reported as GONE).
_RESOLVED_CTX_RE = re.compile(
    r"(?i)("
    r"\bfixed\b|\bresolved\b|\bcorrected\b"
    r"|\bnow\s+(?:pass\w*|green|work\w*)\b"
    r"|\ball\s+tests?\s+(?:pass\w*|green)\b"
    r"|\bnothing\s+(?:broke\w*|fail\w*)\b"
    r"|\bno\b|\bnone\b|\bzero\b|\b0\b"
    r")"
)


def _strip_quoted_context(text: str) -> str:
    """Remove markdown quoted-context spans from *text*, returning cleaned prose.

    Strips three kinds of quoted context (replacing each removed span with a single space):

    1. **Triple-backtick fenced code blocks** (`` ``` `` … `` ``` ``), including
       language-tagged fences like `` ```python ``. Non-greedy body so short fences
       don't swallow unrelated text. An UNTERMINATED opening fence (no closing `` ``` ``)
       strips from the fence to end-of-text — conservative, because unclosed fence
       content is still quoted context.
    2. **Inline single-backtick spans** (`` `…` ``). Run AFTER fences so a fence's own
       backticks aren't half-eaten by the inline pattern.
    3. **Markdown blockquote lines** (lines starting with optional whitespace then ``>``).

    Order matters: fences → inline backticks → blockquotes. Pure text-transform with no
    module-level state and no side effects.

    ``None`` and empty string return ``''`` (no-op guard on the first line)."""
    if not text:
        return ''
    # 1. Triple-backtick fenced blocks (language tag tolerated after opening fence).
    #    DOTALL so '.' matches newlines inside the body; non-greedy so short fences
    #    don't swallow past their closing triple-backtick. Un-ended fence strips
    #    conservatively to EOF.
    text = re.sub(r"```[^\n`]*\n?.*?```", ' ', text, flags=re.DOTALL)
    # 2. Inline single-backtick spans (one or more non-backtick chars).
    text = re.sub(r"`[^`\n]+`", ' ', text)
    # 3. Blockquote lines (optional leading whitespace + '>' + rest of line).
    text = re.sub(r"^[ \t]*>.*$", ' ', text, flags=re.MULTILINE)
    return text


def _admitted_red_test_note(build_artifact: BuildArtifact | None) -> str | None:
    """The offending sentence if the Builder's handoff (diff_digest/decisions/open_questions/caveats)
    admits a genuinely UNRESOLVED failing/skipped/broken test, else None. A STRONG signal fires on its
    own; a WEAK ("failing … test") signal fires only absent negation/resolution context in the same
    field — so ordinary green summaries and already-fixed failures are not flagged (EU-249 iteration 2).

    EU-267: ``caveats`` is scanned too. The builder now routes limitation/caveat language into its own
    field (uncapped by diff_digest's 500-char ceiling); an unresolved-test admission that lands there
    must still trip this backstop, not slip past it because it wasn't in diff_digest.

    EU-517: each field is passed through :py:func:`_strip_quoted_context` before regex matching, so
    quoted/example context (backtick-enclosed text, fenced blocks) no longer trips the scanner.
    Returns original ``text.strip()`` on a hit — never the stripped variant."""
    if build_artifact is None:
        return None
    fields = ([build_artifact.diff_digest] + list(build_artifact.decisions or [])
             + list(build_artifact.open_questions or []) + list(build_artifact.caveats or []))
    for text in fields:
        if not text:
            continue
        cleaned = _strip_quoted_context(text)
        if not cleaned:
            continue
        if _UNRESOLVED_STRONG_RE.search(cleaned):
            return text.strip()
        if _WEAK_RED_TEST_RE.search(cleaned) and not _RESOLVED_CTX_RE.search(cleaned):
            return text.strip()
    return None


def _enforce_admitted_red_tests(result: ReviewResult, build_artifact: BuildArtifact | None) -> ReviewResult:
    """EU-249: force FAIL (with a recorded blocker) when the Builder's own handoff admits a failing,
    skipped, or infrastructure-broken test — even if the LLM verdict said PASS with no blocking
    issues. A self-reported red/skipped test is categorically blocking, not prose the reviewer's
    overall impression can wave through (the exact AUTO-97 shape)."""
    note = _admitted_red_test_note(build_artifact)
    if note is None:
        return result
    if result.verdict != Verdict.PASS and result.blocking_issues:
        return result   # already failing on this diff for another reason — nothing to force
    forced = QualityIssue(
        severity="blocker", area="tests",
        detail=("Builder's own handoff admits a failing/skipped/broken test: "
                f"\"{note[:300]}\" — a self-reported red or skipped test is an automatic blocking "
                "finding (EU-249) and cannot ship PASS."),
    )
    result.quality_issues = list(result.quality_issues) + [forced]
    result.required_changes = list(result.required_changes) + [
        "Fix the failing/skipped test admitted in the build handoff (or remove the dead test) and "
        "confirm it actually runs green before resubmitting."
    ]
    result.verdict = Verdict.FAIL
    return result


# EU-441: the Builder convention is "tests with the code" + full typing, but until EU-441 the
# Reviewer ACCEPTED prose claims of coverage instead of enforcing them — tests (x276) and correctness
# (x114) are the top two recurring rework areas (49-ticket pattern logged 2026-06-30). These two
# deterministic diff-string backstops close the mechanically-enforceable half of that gap; the
# prompt block further down (REVIEWER_SYSTEM REQUIRED-CHECKS) covers the two halves a shared regex
# can't soundly judge (tenant-isolation, error-handling) per the EU-249 iter-2 false-positive
# doctrine (an over-broad regex "was itself parking valid diffs").

# Test-file matcher: /tests/ dir, _test.py, or the JS/TS .test./.spec. conventions. Broader than
# _classify_diff's inline check (which misses foo.test.tsx), and the single source of truth for both
# the production-file detector's test exclusion and _diff_has_test_file, so the two can't drift.
_TEST_FILE_RE = re.compile(r"(?:/tests/|_test\.py$|\.test\.|\.spec\.)")

# The "tests with the code" convention's smallest-exemption floor (surfaced assumption A2): a genuine
# one-line prod fix with no testable surface shouldn't trip the missing-tests gate.
_MISSING_TEST_MIN_LINES = 5

# A public def/async def signature (group 2 = the function name). Used by _untyped_public_defs.
_DEF_SIG_RE = re.compile(r"\b(async\s+def|def)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# Explicit typing erosion in an ADDED .py line: a return/var/parameterized Any, or a lowercase
# ': any' (usually a TS leak). Anchored to annotation position so a comment like "# Any value" or a
# sentence "...any of the above" doesn't fire (false-positive doctrine).
_EXPLICIT_ANY_RE = re.compile(r"(?:->\s*Any\b|:\s*Any\b|[\[,]\s*Any\b|:\s*any\b)")

# Import / "from" / "import" declaration — excluded from the explicit-Any scan so that a cumulative
# branch-vs-base diff containing a typing import of Any (e.g. alongside TYPE_CHECKING) does NOT
# trip as an explicit type annotation (the keyword is merely available, not *used*).
_IMPORT_LINE_RE = re.compile(r"^\s*(from|import)\b")


def _is_production_source(path: str) -> bool:
    """True for a non-test .py or .tsx source path — the single predicate behind the EU-441
    production-file detector and the added-line counter, so they agree on what 'production code'
    means (and so a foo.test.tsx is correctly NOT production)."""
    if not path:
        return False
    return (path.endswith(".py") or path.endswith(".tsx")) and not bool(_TEST_FILE_RE.search(path))


def _diff_changed_paths(diff: str) -> list[str]:
    """Return every file path named in the diff's headers (``+++ b/…`` / ``--- a/…``) as written
    (side prefix included), de-duplicated in first-seen order, with ``/dev/null`` dropped. The single
    header parser behind both `_diff_changed_production_files` and the self-referential enforcer skip
    further down, so the two can never disagree about what a diff touched."""
    seen: list[str] = []
    for line in (diff or "").split("\n"):
        if not (line.startswith("+++ b/") or line.startswith("--- a/")):
            continue
        parts = line.split()
        raw = parts[1] if len(parts) > 1 else ""
        if not raw or raw == "/dev/null":
            continue
        if raw not in seen:
            seen.append(raw)
    return seen


def _diff_changed_production_files(diff: str) -> list[str]:
    """Return every production source file changed in the diff (non-test .py/.tsx), as the b/-prefixed
    path. Mirrors the per-file detector in _classify_diff but uses the broader _TEST_FILE_RE. Empty
    for docs/config/test-only diffs — the exact precision the EU-441 gate needs: the coarse
    'production' CATEGORY also fires on ≥30-line config/docs diffs and would false-block."""
    return [p for p in _diff_changed_paths(diff) if _is_production_source(p)]


def _diff_has_test_file(diff: str) -> bool:
    """True when any file header in the diff is a test file (/tests/, _test.py, .test., .spec.) —
    the same test-file predicates _classify_diff uses, broadened to cover JS/TS test conventions."""
    for line in (diff or "").split("\n"):
        if not (line.startswith("+++ b/") or line.startswith("--- a/")):
            continue
        parts = line.split()
        raw = parts[1] if len(parts) > 1 else ""
        if raw and _TEST_FILE_RE.search(raw):
            return True
    return False


def _added_production_code_line_count(diff: str) -> int:
    """Count non-blank ADDED lines ('+' not '+++') that belong to a production source file. Scoped to
    production files so a one-line prod fix bundled with a large docs/config change stays under the
    _MISSING_TEST_MIN_LINES exemption (A2) — the threshold measures behaviour surface, not diff noise."""
    count = 0
    current = None
    for line in (diff or "").split("\n"):
        if line.startswith("diff --git"):
            current = None
            continue
        if line.startswith("+++ "):
            parts = line.split()
            current = parts[1] if len(parts) > 1 else ""
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if current and _is_production_source(current) and line[1:].strip():
                count += 1
    return count


def _added_python_production_lines(diff: str) -> Iterator[tuple[str, int, str]]:
    """Yield ``(file_path, run_id, added_content)`` for each ADDED line in a non-test .py
    production file — the single shared cursor for the typing scan.  ``run_id`` increments
    whenever a ``@@`` hunk header appears, a new file starts, or the cursor jumps away from the
    current file; this gives callers a cheap way to keep a def-span-walk inside one contiguous
    hunk (EU-463)."""
    current: str | None = None
    run_id = 0
    for line in (diff or "").split("\n"):
        if line.startswith("diff --git"):
            current = None
            run_id = 0
            continue
        if line.startswith("+++ "):
            parts = line.split()
            file_name = parts[1] if len(parts) > 1 else ""
            if file_name != current:         # new file → reset contiguity
                run_id += 1
            current = file_name
            continue
        if line.startswith("@@"):
            run_id += 1                      # hunk boundary
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if current and current.endswith(".py") and not _TEST_FILE_RE.search(current):
                yield current, run_id, line[1:]


def _sig_paren_depth(line: str) -> int:
    """Net open-paren delta (``(`` minus ``)``) of one line of a Python def signature, with string
    literals and trailing comments ignored so a default like ``s: str = "("`` or a ``# note`` can't
    hold the depth open. Used by ``_untyped_public_defs`` to locate the line that CLOSES a multi-line
    signature — the line where the depth opened by ``def name(`` returns to 0 (the matching ``)``)."""
    depth = 0
    quote: str | None = None
    for ch in line:
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "#":
            break
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    return depth


def _untyped_public_defs(diff: str) -> list[str]:
    """Return a ``file:name`` descriptor per ADDED public def/async def signature in a non-test .py
    file that lacks a ``->`` return annotation. Private (``_``-prefixed, which also covers dunders
    like ``__init__``) and already-annotated signatures are skipped; removed lines and non-.py files
    are never inspected. Matches the AUTO-85/AUTO-98 typing-erosion lessons.

    Multi-line signatures (EU-441 iter-2): a def split across lines — e.g.
    ``def classify_task(\\n    a: str = "",\\n) -> RoutingTier:`` — is NOT decided 'untyped' from the
    single ``def name(`` line. The full signature span is accumulated first: consecutive added lines
    for the SAME file are consumed until the parentheses opened by ``def name(`` balance (open-paren
    depth returns to 0, i.e. the matching closing ``)`` is reached), and the def is flagged ONLY if no
    ``->`` appears anywhere in that span. Without this, fully-typed multi-line signatures like
    orchestrator/routing.py's ``classify_task`` / ``should_route_to_local`` were false-flagged."""
    out: list[str] = []
    lines = list(_added_python_production_lines(diff))
    i, n = 0, len(lines)
    while i < n:
        file_path, run_id, content = lines[i]
        m = _DEF_SIG_RE.search(content)
        if not m:
            i += 1
            continue
        name = m.group(2)
        # Accumulate the full signature span until the '(' opened by 'def name(' closes. The '->'
        # return annotation may appear anywhere in the span — typically right after the closing
        # ')', e.g. ') -> RoutingTier:' — so it must be checked over the WHOLE span, not just the
        # 'def name(' line.
        # EU-463 guard: the span-walk stays strictly inside ONE contiguous added-run (same file,
        # same run_id). When paren depth doesn't close within that run, we stop silent — the closing
        # ')' (and possibly a '-> RetType:') likely lives on an unchanged context line outside our
        # visibility. We NEVER stitch across @@ hunk boundaries.
        span_parts: list[str] = [content]
        depth = _sig_paren_depth(content)
        j = i
        span_closed = depth == 0    # single-line def (parens balanced) counts as "closed"
        while (depth > 0
               and j + 1 < n
               and lines[j + 1][0] == file_path
               and lines[j + 1][1] == run_id):   # must stay in the same hunk/run
            j += 1
            nxt = lines[j][2]
            span_parts.append(nxt)
            depth += _sig_paren_depth(nxt)
            if depth <= 0:
                span_closed = True
        i = j + 1   # advance past the consumed continuation lines so they aren't rescanned
        if name.startswith("_"):     # private or dunder
            continue
        # If the span did NOT close within this run (depth > 0 after exhausting it), the closing
        # ')' and possible '-> RetType:' are outside our visibility — stay SILENT rather than
        # mis-classify partial text as an untyped def (EU-463).
        if not span_closed:
            continue
        if "->" in "\n".join(span_parts):   # return annotation present anywhere in the signature span
            continue
        out.append(f"{file_path}:{name}")
    return out


def _enforce_missing_tests(result: ReviewResult, diff: str) -> ReviewResult:
    """EU-441: force FAIL (blocker, area 'tests') when production source code changed with
    >= _MISSING_TEST_MIN_LINES added code lines but NO test file accompanies the diff — the
    AUTO-14/AUTO-18/AUTO-100 'tests with the code' gap that gate.py's collectability check does NOT
    cover (collectability only fires when a test was attempted and is non-runnable; a diff that
    simply omits the test entirely sailed through). The guard suppresses only an ALREADY-raised
    'tests' blocker, so a diff that is ALSO missing typing still gets that finding too (one pass
    surfaces every gap, not one per retry)."""
    if any(q.area == "tests" and q.severity in ("blocker", "major") for q in result.quality_issues):
        return result
    if not _diff_changed_production_files(diff):
        return result
    if _added_production_code_line_count(diff) < _MISSING_TEST_MIN_LINES:
        return result
    if _diff_has_test_file(diff):
        return result
    forced = QualityIssue(
        severity="blocker", area="tests",
        detail=(f"Production code changed (>= {_MISSING_TEST_MIN_LINES} added lines) but no test file "
                "accompanies the diff — missing tests for the new behaviour. 'Tests with the code' is a "
                "blocking convention (EU-441): add a *_test.py / *.test.* / *.spec.* file covering it."),
    )
    result.quality_issues = list(result.quality_issues) + [forced]
    result.required_changes = list(result.required_changes) + [
        "Add a test file (matching /tests/, _test.py, .test., or .spec.) that exercises the changed "
        "production code, and confirm it runs green before resubmitting."
    ]
    result.verdict = Verdict.FAIL
    return result


def _enforce_missing_typing(result: ReviewResult, diff: str) -> ReviewResult:
    """EU-441: force FAIL (major, area 'typing') when ADDED lines of a non-test .py file define a
    public def/async def with NO ``->`` return annotation, or introduce an explicit Any / ``: any``
    annotation — the AUTO-85/AUTO-98 typing-erosion pattern. Python-only: TS typing is already
    enforced by the tsc gate more reliably than a regex. Private/dunder names, removed lines, fully
    annotated signatures, and non-.py files are never flagged (EU-249 false-positive doctrine). The
    guard suppresses only an ALREADY-raised 'typing' blocker."""
    if any(q.area == "typing" and q.severity in ("blocker", "major") for q in result.quality_issues):
        return result
    untyped = _untyped_public_defs(diff)
    anys: list[str] = []
    for file_path, _run_id, content in _added_python_production_lines(diff):
        # Exclude import / "from" lines so a cumulative diff containing a typing import of Any
        # (e.g. alongside TYPE_CHECKING) doesn't trip as an explicit type annotation (the keyword
        # is merely available, not used at runtime).
        if _IMPORT_LINE_RE.match(content):
            continue
        m = _EXPLICIT_ANY_RE.search(content)
        if m:
            anys.append(f"{file_path}: {m.group(0).strip()}")
    if not untyped and not anys:
        return result
    findings = untyped + anys
    forced = QualityIssue(
        severity="major", area="typing",
        detail=("New Python code is missing return annotations or erodes typing with explicit Any "
                f"(EU-441): {', '.join(findings[:8])}. Add '-> <ReturnType>' to every public def and "
                "replace Any with a concrete type."),
    )
    result.quality_issues = list(result.quality_issues) + [forced]
    result.required_changes = list(result.required_changes) + [
        "Annotate every public def/async def in the diff with '-> <ReturnType>' and replace any "
        "explicit Any with a concrete type."
    ]
    result.verdict = Verdict.FAIL
    return result


# EU-446: the EU-441 REQUIRED-CHECKS block named tenant-isolation as one of the four surfaces, but
# until EU-446 it was enforced by the REVIEWER_SYSTEM PROMPT alone — which the LLM can still wave
# through (the exact AUTO-14/AUTO-18 unreliability EU-441 was created to eliminate). This backstop
# closes the mechanically-detectable half of that surface: query PRESENCE on a user-scoped table.
# The EU-249-safe core is 'detect query PRESENCE, never guess whether a tenant filter is MISSING' —
# the latter is the regex-infeasible part (and the over-broad shape EU-249 iter-2 explicitly
# rejected: an over-broad tenant-filter regex "was itself parking valid diffs"). EU itself has no
# Supabase, so the PRESENCE of a Supabase query-builder call in the diff IS the 'this is Automatixy
# user-data-access code' signal — no need to thread `app` into the signature, which keeps this gate
# byte-symmetric with the other two _enforce_* diff-string backstops.

# Supabase read/write ENTRY-POINT methods on an ADDED line establish query presence. `.from(` (JS) /
# `.from_(` (Python) double as the table source. `.eq`/`.filter` are deliberately NOT triggers: they
# always ride a read/write call AND `.eq('tenant_id', ...)` is itself the scoping signal, so treating
# them as triggers would conflate the trip with the clearance.
_QUERY_ENTRY_RE = re.compile(r"\.(?:select|insert|update|delete|upsert|from_?)\s*\(")

# Capture the table literal in a JS/Python `.from('X')` / `.from_('X')` call (group 1 = table name).
# Drives the user-scoped-table lever: only a query on a user-entity table trips the gate, so a global
# lookup (countries, app_config, ...) stays SILENT instead of false-blocking (EU-249).
_FROM_TABLE_RE = re.compile(r"\.from_?\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")

# User-entity noun set — a `.from(<table>)` whose table (tokenized by _/-/space) STARTS with one of
# these is user-scoped data. One commented, easy-to-tune alternation; an FP here (a user-scoped-
# looking table that is actually global) costs one builder pass and is the accepted backstop profile.
_USER_SCOPED_TABLE_RE = re.compile(
    r"(?:users?|tenants?|leads?|contacts?|deals?|accounts?|profiles?|workspaces?|members?|"
    r"customers?|subscriptions?|orgs?|organizations?|organisations?|compan(?:y|ies))"
)

# Tenant-column names whose explicit filter (a Supabase .eq / .filter, or raw SQL `where col =`)
# clears the gate. Kept as a shared fragment so the filter and the where-clause clauses stay in sync.
_TENANT_COLS = (
    "tenant_id|user_id|org_id|workspace_id|account_id|owner_id|company_id|customer_id|member_id"
)

# Scoping signals that CLEAR the gate (no false-block): ANY ADDED line carrying one means tenant
# scoping IS present somewhere in the diff — an explicit tenant-column filter on the query chain
# (Supabase .eq / .filter, or raw SQL `where col =`), OR an RLS marker (row-level-security enablement
# / `create policy` / `using (` / `with check`, or a deliberate `#`/`//`/`-- RLS` annotation). Scanned
# across all added lines rather than just the query line: a chained `.eq('tenant_id', ...)` can sit on
# its own line, and soundly associating it with a specific query across lines is not feasible — a
# read-only string gate has no other confirmation channel, so a real RLS annotation is accepted too.
_TENANT_SCOPING_RE = re.compile(
    r"(?i)("
    r"\.eq\s*\(\s*['\"](?:" + _TENANT_COLS + r")['\"]"
    r"|\.filter\s*\(\s*['\"].*(?:" + _TENANT_COLS + r")"
    r"|\bwhere\s+(?:[a-z_]+\.)?(?:" + _TENANT_COLS + r")\s*="
    r"|\brow\s+level\s+security\b"
    r"|\benable\s+row\s+level\s+security\b"
    r"|\bcreate\s+policy\b"
    r"|\brls\b"
    r"|\busing\s*\("
    r"|\bwith\s+check\b"
    r"|(?:#|//|--)\s*rls\b"
    r")"
)


def _added_diff_lines(diff: str) -> list[str]:
    """Return the content of every ADDED diff line ('+' but not the '+++' header), leading '+'
    stripped. The single shared cursor for the tenant-query scan: only newly-ADDED code is a leak
    surface, never context or removed lines."""
    out: list[str] = []
    for line in (diff or "").split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            out.append(line[1:])
    return out


def _is_user_scoped_table(table: str) -> bool:
    """True when the table literal names a user entity. The table is tokenized by ``_``/``-``/``/``/
    whitespace so snake_case names (``tenant_members``, ``app_users``) match while a leading prefix
    (``tbl_``) does not mask the noun, and the noun must START a token (so ``ideal`` is not flagged as
    ``deal``). A genuine FP (a user-scoped-looking table that is actually global) costs one builder
    pass — the accepted backstop profile per EU-249."""
    for tok in re.split(r"[_\-/\s]+", (table or "").lower()):
        if tok and _USER_SCOPED_TABLE_RE.match(tok):
            return True
    return False


def _diff_unscoped_tenant_queries(diff: str) -> list[str]:
    """EU-446: return the offending query lines — ADDED lines carrying a Supabase query on a
    USER-SCOPED table when the diff's added lines carry NO tenant scoping signal. Empty (no fire)
    when (a) no query entry-point method is present, (b) the query targets a non-user-scoped/lookup
    table (``countries``, ``app_config`` — EU-249: stay SILENT rather than guess), or (c) a scoping
    signal (``.eq('tenant_id', ...)`` / ``where tenant_id =`` / an RLS marker) appears anywhere in the
    added lines. Pure and deterministic; detects query PRESENCE on a user-scoped table, never guesses
    whether a filter is missing (the regex-infeasible half). Covers both JS (``.from(``) and Python
    (``.from_(``) Supabase clients."""
    added = _added_diff_lines(diff)
    if not added or not any(_QUERY_ENTRY_RE.search(ln) for ln in added):
        return []
    # User-scoped-table lever — any '.from(<table>)' / '.from_(<table>)' naming a user entity.
    user_scoped_lines = [
        ln for ln in added
        if any(_is_user_scoped_table(t) for t in _FROM_TABLE_RE.findall(ln))
    ]
    if not user_scoped_lines:
        return []
    # Scoping signal clears the gate — scan ALL added lines.
    if any(_TENANT_SCOPING_RE.search(ln) for ln in added):
        return []
    return user_scoped_lines


def _enforce_tenant_query(result: ReviewResult, diff: str) -> ReviewResult:
    """EU-446: force FAIL (major, area 'tenant-isolation') when the diff adds a Supabase query on a
    user-scoped table and carries NO tenant scoping signal — the AUTO-14/AUTO-18 cross-tenant
    data-leak shape the EU-441 prompt-only rule could still let through. Symmetric with
    ``_enforce_missing_tests`` / ``_enforce_missing_typing``: same ``(ReviewResult, diff) ->
    ReviewResult`` shape, same per-area suppress-guard, same force-major-then-FAIL. Detects query
    PRESENCE on a user-scoped table (mechanically sound); a global/lookup table and any RLS /
    explicit-tenant-filter signal stay SILENT so valid diffs are not false-blocked (EU-249)."""
    if any(q.area in ("tenant-isolation", "security") and q.severity in ("blocker", "major")
           for q in result.quality_issues):
        return result
    offenders = _diff_unscoped_tenant_queries(diff)
    if not offenders:
        return result
    forced = QualityIssue(
        severity="major", area="tenant-isolation",
        detail=("Supabase query on a user-scoped table with no tenant scoping signal in the diff "
                f"(EU-446): {offenders[0].strip()[:200]}. Confirm an explicit tenant filter "
                "(.eq('tenant_id', ...) / SQL 'where tenant_id =') or a Supabase row-level-security "
                "policy guards this query, otherwise it is a cross-tenant data-leak."),
    )
    result.quality_issues = list(result.quality_issues) + [forced]
    result.required_changes = list(result.required_changes) + [
        "Add tenant scoping to the flagged query — an explicit .eq('tenant_id', ...) / SQL "
        "'where tenant_id =' filter, or a Supabase row-level-security policy — and confirm it is in "
        "the diff before resubmitting."
    ]
    result.verdict = Verdict.FAIL
    return result


# 2026-07-28 (the EU-518/EU-520 false-fire class): a deterministic enforcer must not sit in judgment
# over a diff that edits the enforcer itself. All four backstops above decide by pattern-matching
# TEXT — the diff's added lines (missing-tests / missing-typing / tenant-query) or the Builder's
# free-text handoff (admitted-red-tests) — so a ticket whose whole PURPOSE is one of those detectors
# carries the trigger phrases in its own diff and its own handoff BY CONSTRUCTION, and the enforcer
# false-fires on the very change that fixes it. Measured cost: EU-518 and EU-520 both burned every
# pass and escalated exactly this way with the work ~80% done and the repo gate 501/502 green (the
# one red being a SUPERSEDED guard); EU-543 — a feature the Commander wanted — was lost downstream
# of that escalation.
#
# The relief is self-referential and nothing else: an enforcer is skipped ONLY when the diff modifies
# the source file that IMPLEMENTS that enforcer. Matching is an EXACT repo-relative path compare (not
# a suffix/substring), so no product-repo diff can reach it and every other ticket keeps byte-identical
# behaviour. What still judges a skipped diff: the LLM reviewer's own findings (untouched), the
# deterministic repo gate — which is already green, since loop.py runs it BEFORE review — and
# `_enforce_execution_gate`, deliberately absent from this map because it keys off the TICKET's
# acceptance criteria rather than the diff text, so a diff editing reviewer.py cannot fabricate its
# trigger. Every skip is recorded on `result.unverifiable_gaps`, which loop.py posts onto the ticket
# (EU-353), so the relaxation is visible rather than silent.
#
# Each entry lists the source that IMPLEMENTS that enforcer's detection, nothing adjacent. The
# red-admission entry has two, because the parser genuinely lives in both: `gate._red_test_admission`
# / `gate.gate_vs_builder_verdict` are the EU-442 verdict-mismatch half and import reviewer.py's
# regexes so "the orchestrator has ONE red-admission parser" (gate.py's own docstring, which cites
# EU-520 — the ticket this map exists for). The other three stay reviewer.py-only on purpose:
# gate.py's test-collectability check is a NEIGHBOURING behaviour, not the one `_enforce_missing_tests`
# enforces (see its docstring), and mapping it here would relax the tests-with-the-code gate for
# gate.py tickets that have nothing to do with the false-fire.
_ENFORCER_SOURCE_FILES: dict[str, tuple[str, ...]] = {
    "admitted-red-tests": ("orchestrator/reviewer.py", "orchestrator/gate.py"),
    "missing-tests": ("orchestrator/reviewer.py",),
    "missing-typing": ("orchestrator/reviewer.py",),
    "tenant-query": ("orchestrator/reviewer.py",),
}


def _strip_diff_path_prefix(path: str) -> str:
    """Drop git's ``a/``/``b/`` side prefix from a diff-header path, so a header path can be compared
    against a repo-relative source path."""
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def _self_referential_enforcer_skips(diff: str) -> list[str]:
    """Return the `_ENFORCER_SOURCE_FILES` keys of every deterministic enforcer whose OWN implementing
    source file this diff modifies — the tickets where the enforcer would be grading the change that
    fixes it. Pure and deterministic; an ordinary diff (anything that does not edit this orchestrator's
    own reviewer source) yields an empty list, so the enforcers run exactly as before."""
    changed = {_strip_diff_path_prefix(p) for p in _diff_changed_paths(diff)}
    if not changed:
        return []
    return [key for key, sources in _ENFORCER_SOURCE_FILES.items()
            if any(src in changed for src in sources)]


def _enforcer_skip_note(key: str) -> str:
    """The audit-visible reason line for one skipped enforcer, appended to `result.unverifiable_gaps`
    (surfaced on the ticket by EU-353, never blocking) so a self-referential skip is recorded rather
    than silent. Worded with NO UI/pixel/browser vocabulary on purpose, so
    `_classify_unverifiable_finding` returns None for it and the note is never mistaken for a finding
    by the EU-351/EU-352 bounce accumulator."""
    return (f"Deterministic '{key}' enforcer skipped on this diff: the diff modifies "
            f"{', '.join(_ENFORCER_SOURCE_FILES[key])}, the source that implements that enforcer, so "
            "its own trigger text is present by construction (the EU-518/EU-520 false-fire class). "
            "Judged by the reviewer's own findings and the deterministic repo gate instead.")


# EU-268: a deterministic backstop for the AUTO-109 failure mode — the Reviewer is READ-ONLY
# (allowed_tools=[Read, Grep, Glob], Bash disallowed — see the options built below) so it CANNOT run
# Playwright, pytest, or any other test suite. Despite that, on AUTO-109 the GLM-4.6 reviewer set
# spec_met=true/blocking=0 for an AC reading "All 6 Google Sync tests pass on Desktop Chrome and
# Mobile Safari" from static inspection alone, and the ticket merged to DEV self-admittedly
# unverified. This pair of pure helpers + the enforcement function below stop the Reviewer from
# self-reporting spec_met=true on an execution-dependent AC unless the Builder's own handoff (or the
# diff) carries actual execution evidence (a passing test log / gate report) — a genuinely verified
# execution AC still passes; an AC that merely READS as satisfied from the diff does not.
_EXECUTION_AC_RE = re.compile(
    r"(?i)("
    r"\ball\s+\d+\b[^.\n]{0,40}\btests?\b[^.\n]{0,10}\bpass"          # "all 6 ... tests ... pass"
    r"|\btests?\s+pass(?:es|ing)?\s+on\b"                              # "tests pass on Desktop Chrome"
    r"|\bdesktop\s+chrome\b|\bmobile\s+safari\b|\bmobile\s+chrome\b|\bdesktop\s+firefox\b|\bdesktop\s+safari\b"
    r"|\bverify\s+(?:in|on)\s+(?:the\s+)?(?:browser|device|mobile|desktop)\b"
    r"|\b(?:e2e|end-to-end|playwright|cypress)\b[^.\n]{0,30}\bpass"
    r"|\ball\s+tests?\s+pass\b"                                       # "all tests pass"
    r")"
)

# Markers that a passing-test-log / gate-report was actually ATTACHED to the handoff — the bar for
# "verified", not merely asserted. Iteration-2 tightening (EU-268): restricted to attachment /
# machine-shaped evidence only — a log/report citation, an "attached" artifact, or a machine-shaped
# result file. The bare "verified passing" / "confirmed passing" PROSE alternations were dropped: per
# unit doctrine the Builder's prose is untrusted, so a Builder must not be able to unlock the gate by
# writing a sentence — the evidence has to be an attached passing test log / gate report / results
# file. This also keeps an AC's own wording ("all tests pass") echoed back into the digest from
# counting as its own evidence.
_EXECUTION_EVIDENCE_RE = re.compile(
    r"(?i)("
    r"\btest\s*log\b"
    r"|\bgate\s*report\b"
    r"|\bplaywright[\s-]*report\b"
    r"|\btest[\s-]*report\b"
    r"|\bpassing\s+(?:test\s+)?log\b"
    r"|\b(?:log|report)\s+attached\b"
    r"|\battached\s+(?:the\s+)?(?:test\s+)?(?:log|report)\b"
    r"|\btest[\s-]*results?\.(?:log|txt|json|xml)\b"
    r")"
)


def _ac_requires_execution(ac: str) -> bool:
    """True when an acceptance criterion's wording demands runtime/test execution to verify (e.g.
    "all N tests pass", "tests pass on Desktop Chrome/Mobile Safari", "verify in browser/on device",
    an e2e/Playwright/Cypress pass) — the kind of claim a READ-ONLY reviewer can never confirm by
    reading the diff alone. A static AC ("function has a docstring") returns False."""
    if not ac:
        return False
    return bool(_EXECUTION_AC_RE.search(ac))


# EU-265: the sub-class of execution ACs that name a BROWSER/DEVICE surface — the exact AUTO-109
# failure mode ("All 6 Google Sync tests pass on Desktop Chrome and Mobile Safari"). A green REPO
# gate (pytest/run_all) proves nothing about a cross-browser matrix, so loop-supplied gate evidence
# must never satisfy these unless the gate itself ran that matrix (see _gate_evidence_covers).
_BROWSER_MATRIX_AC_RE = re.compile(
    r"(?i)("
    r"\bdesktop\s+chrome\b|\bmobile\s+safari\b|\bmobile\s+chrome\b|\bdesktop\s+firefox\b|\bdesktop\s+safari\b"
    r"|\bverify\s+(?:in|on)\s+(?:the\s+)?(?:browser|device|mobile|desktop)\b"
    r"|\b(?:e2e|end-to-end|playwright|cypress)\b"
    r")"
)

# Markers in the LOOP's gate evidence that the gate itself ran a browser/e2e suite (the commands
# are listed in the evidence, so "npx playwright test" shows up here when it actually ran).
_GATE_BROWSER_SUITE_RE = re.compile(r"(?i)\b(playwright|cypress)\b")


def _ac_requires_browser_matrix(ac: str) -> bool:
    """True when the execution AC is browser/device-scoped (see _BROWSER_MATRIX_AC_RE)."""
    if not ac:
        return False
    return bool(_BROWSER_MATRIX_AC_RE.search(ac))


def _gate_evidence_covers(ac: str, gate_evidence: str) -> bool:
    """EU-265: does the LOOP's machine-sourced gate evidence satisfy this execution AC?

    ``gate_evidence`` is authored by the ORCHESTRATOR (loop._gate_execution_evidence) from a real
    subprocess exit code — never by the Builder LLM — so unlike the handoff prose it is trusted. A
    green repo gate satisfies a generic "all tests pass" AC (the gate literally just ran those
    tests), but NOT a browser/device-matrix AC unless the gate commands actually ran that matrix
    (playwright/cypress named in the evidence) — AUTO-109 was precisely a cross-browser AC, and
    that hole must stay closed (EU-268)."""
    if not gate_evidence:
        return False
    if not _ac_requires_browser_matrix(ac):
        return True
    return bool(_GATE_BROWSER_SUITE_RE.search(gate_evidence))


def _has_execution_evidence(build_artifact: BuildArtifact | None, diff: str | None = None) -> bool:
    """True when the Builder's HANDOFF NARRATIVE carries a marker that a test suite was actually run
    and passed (a test log / gate report), as opposed to the Builder merely asserting the AC's own
    wording back at the Reviewer.

    Iteration-2 scope tightening (EU-268): scans ONLY the free-text narrative fields of the
    BuildArtifact (diff_digest / decisions / open_questions / caveats). The ``diff`` argument and
    ``files_changed`` are INTENTIONALLY NOT sources: execution evidence is runtime OUTPUT that
    belongs in the handoff, never in committed SOURCE. A Playwright ticket whose diff or config
    legitimately contains 'playwright-report' or 'test-results.json' — or that commits such a path
    into files_changed — must not silently disable the gate. ``diff`` is retained on the signature
    for call-site symmetry/back-compat but is deliberately ignored."""
    if build_artifact is None:
        return False
    fields: list[str] = [build_artifact.diff_digest or ""]
    fields.extend(build_artifact.decisions or [])
    fields.extend(build_artifact.open_questions or [])
    fields.extend(build_artifact.caveats or [])
    return any(_EXECUTION_EVIDENCE_RE.search(text) for text in fields if text)


# The fixed prefix every execution-gate spec gap starts with — the recognizer both
# collect_unverifiable_fingerprints and the escalate-once demotion below key on.
_EXEC_GATE_GAP_PREFIX = "AC requires test execution but no passing test log"


def _enforce_execution_gate(result: ReviewResult, ticket: Ticket, build_artifact: BuildArtifact | None,
                            diff: str, *, gate_evidence: str = "",
                            already_bounced: set[str] | None = None) -> ReviewResult:
    """EU-268: force spec_met=False + FAIL (with a named spec gap) when the ticket has an
    execution-dependent acceptance criterion and the handoff/diff carries no execution evidence —
    even if the LLM verdict said spec_met=true. Does NOT grant the Reviewer any new capability
    (Bash stays disallowed); it only stops the Reviewer's own verdict JSON from defaulting an
    unverifiable AC to true. A genuinely evidenced execution AC (a test log/gate report present) is
    left unchanged, so real verified passes still ship.

    EU-265: ``gate_evidence`` is the loop's own machine-sourced proof that a REAL verification gate
    ran green immediately before this review (loop._gate_execution_evidence — a subprocess exit
    code, not Builder prose). Without it, every ticket whose AC matches "all tests pass" was forced
    to FAIL on every pass DESPITE the gate having just run those tests, burned all max_passes, then
    escalated. (_enforce_bounce_once never relieves an execution-gate gap; since 2026-07-19 the
    relief lives HERE — a repeat of the same gap in `already_bounced` demotes, see below.)
    Scoped per _gate_evidence_covers: generic test-pass ACs are satisfied; browser/device-matrix
    ACs still force FAIL unless the gate itself ran that matrix. Defaults to '' (direct callers /
    older stubs), which keeps the EU-268 conservative behaviour byte-identical."""
    exec_acs = [ac for ac in (ticket.acceptance_criteria or []) if _ac_requires_execution(ac)]
    if not exec_acs or _has_execution_evidence(build_artifact, diff):
        return result
    unverified = [ac for ac in exec_acs if not _gate_evidence_covers(ac, gate_evidence)]
    if not unverified:
        return result
    # Escalate-once (2026-07-19, the AUTO-198 class): this gate used to be the ONE permanent FAIL
    # no rebuild could ever clear — an AC naming a browser/device matrix the repo's gate doesn't run
    # (e.g. "passes on Mobile Safari") re-FAILed every pass even when the LLM reviewer confirmed the
    # criteria met, burned max_passes, then parked a done deliverable on the Commander. A read-only
    # reviewer can NEVER produce the missing run, so the gap follows the EU-351 contract instead:
    # the FIRST raise still forces FAIL (the Builder gets one pass to actually run the suite and
    # attach the log); a REPEAT of the same gap demotes to `unverifiable_gaps` — surfaced, never
    # blocking — and the LLM's own verdict stands.
    bounced = already_bounced or set()
    fresh: list[str] = []
    for ac in unverified:
        gap = (f"{_EXEC_GATE_GAP_PREFIX}/gate report attached — "
               f"unverified: \"{ac}\"")
        if _finding_fingerprint("exec-gate", gap) in bounced:
            if gap not in result.unverifiable_gaps:
                result.unverifiable_gaps = list(result.unverifiable_gaps) + [gap]
            continue
        fresh.append(gap)
    for gap in fresh:
        if gap not in result.spec_gaps:
            result.spec_gaps = list(result.spec_gaps) + [gap]
    if fresh:
        result.spec_met = False
        result.verdict = Verdict.FAIL
    return result


# EU-350 (auto-split from EU-321): a classifier over the REVIEWER'S OWN FINDING text — a
# `QualityIssue.detail` / spec-gap string the reviewer is about to raise as blocking — not the
# ticket's AC text (that's `_ac_requires_execution` above). A READ-ONLY reviewer (no Bash, no
# browser/renderer) can never actually confirm a "looks like" / pixel-drift / cross-browser-render
# claim by reading a diff; this flags findings on that surface with a stable lens key so a future
# ticket can route them differently. Additive only in EU-350 — nothing in `review()` consumes this
# yet, so no verdict/blocking_issues behaviour changes.
_UNVERIFIABLE_SURFACE_RE = re.compile(
    r"(?i)("
    r"(?P<ui_visual>"
    r"\blooks?\s+(?:off|wrong|misaligned|broken|different|odd|weird)\b"
    r"|\bscreenshot\b"
    r"|\bvisually\b"
    r"|\bwrong\s+colou?r\b|\bcolou?r\s+(?:is|looks)\s+wrong\b"
    r"|\bmisaligned\b"
    r")"
    r"|(?P<layout_pixel>"
    r"\b\d+\s*px\b"
    r"|\bpixel[\s-]*(?:perfect|drift|diff)\b"
    r"|\blayout\s+(?:shifts?|breaks?|is\s+broken)\b"
    r"|\bviewport\b"
    r"|\bresponsive\b"
    r")"
    r"|(?P<browser_behavior>"
    r"\brenders?\s+(?:incorrectly|differently|wrong)\b"
    r"|\bin\s+(?:desktop\s+chrome|mobile\s+safari|mobile\s+chrome|desktop\s+firefox|desktop\s+safari)\b"
    r"|\bcross[\s-]*browser\b"
    r"|\bbrowser\s+render(?:ing)?\b"
    r")"
    r")"
)


def _classify_unverifiable_finding(text: str | None) -> str | None:
    """Classify a reviewer FINDING (a `QualityIssue.detail` / spec-gap string) as being on a surface
    a READ-ONLY reviewer structurally cannot execute or render — UI/visual ('looks off',
    'screenshot'), pixel/layout ('4px', 'viewport', 'responsive'), or browser-render
    ('Mobile Safari', 'renders incorrectly') — and return the matching stable lens key
    ('ui_visual' | 'layout_pixel' | 'browser_behavior'). Returns `None` for ordinary logic/data/test
    findings, or falsy input. Mirrors `_ac_requires_execution`, but over the reviewer's OWN finding
    text rather than the ticket's AC text."""
    if not text:
        return None
    m = _UNVERIFIABLE_SURFACE_RE.search(text)
    if not m:
        return None
    return next(name for name, val in m.groupdict().items() if val is not None)


def _finding_fingerprint(lens: str, detail: str) -> str:
    """A stable, order-independent, whitespace/case-tolerant fingerprint of a classified finding
    (lens, detail), so the SAME underlying finding re-raised with trivial wording drift can be
    recognised as identical. Reuses the normalization STYLE of `_changes_sig` in loop.py (lowercase,
    collapse whitespace, cap the detail to a prefix) without importing from loop.py — reviewer.py
    stays self-contained. Pure/deterministic: same (lens, detail) modulo whitespace/case always
    yields the same fingerprint; a genuinely different lens or detail yields a different one."""
    norm_lens = " ".join((lens or "").lower().split())
    norm_detail = " ".join((detail or "").lower().split())[:160]
    return hashlib.sha256(f"{norm_lens}|{norm_detail}".encode("utf-8")).hexdigest()


def _enforce_bounce_once(result: ReviewResult, already_bounced: set[str], *,
                         final_pass: bool = False) -> ReviewResult:
    """EU-351: escalate-once bounce gate — mirrors `_enforce_admitted_red_tests`/
    `_enforce_execution_gate` as a deterministic backstop, but this one RELAXES rather than forces
    a blocker. Walks the reviewer's own blocking findings (`quality_issues` blocker/major subset)
    and `spec_gaps`, classifying each via `_classify_unverifiable_finding` (EU-350). A finding that
    classifies as unverifiable AND whose fingerprint (`_finding_fingerprint`) is already present in
    `already_bounced` — i.e. it was raised and bounced on a PRIOR pass of this same ticket — is
    removed from `quality_issues`/`spec_gaps` and its detail is appended to
    `result.unverifiable_gaps` instead. A READ-ONLY reviewer that keeps re-raising the exact same
    unrenderable claim can never be satisfied by more builder iterations, so the second time is an
    escalate-once demotion, not a fresh blocker.

    First-time unverifiable findings (fingerprint not yet in `already_bounced`) and ordinary
    logic/data/test findings (returns None from the classifier) are left completely untouched —
    behavior is unchanged for both, and a first-pass unverifiable finding can still FAIL.

    After demotion, if nothing else keeps the ticket down (no blocking_issues, no spec_gaps left,
    and spec_met is True), the verdict is RECOMPUTED to PASS — an escalate-once demotion has to
    actually let a ticket reach ship-ready, not just relabel the same permanent FAIL.

    FINAL PASS (2026-07-28) — `final_pass=True` (threaded from loop.py's `iteration >= max_passes`)
    additionally demotes a NEW unverifiable finding, not only a repeat. The repeat-only condition was
    measured to be effectively unreachable for this class: 135/135 round-≥2 reviewer objections were
    textually NEW (2026-07-05 audit, recorded on `config.escalate_effort_on_retry`), so the
    fingerprint never matches and 29 of 128 escalations were still "max passes — PM escalated". A
    reviewer inventing a FRESH unverifiable objection every round is the same non-convergence wearing
    a new string. At the final pass the alternative is parking the ticket on the human, the
    deterministic gate is already green (it must be, to reach review at all), an
    unverifiable-BY-CONSTRUCTION finding cannot be satisfied by another builder pass, and there are no
    passes left — so surfacing it in `unverifiable_gaps` (visible on the ticket) beats escalating it.
    Scoped tightly: ONLY findings `_classify_unverifiable_finding` tags (it returns None for ordinary
    logic/data/test findings, which stay untouched and still FAIL), and NEVER an execution-gate spec
    gap — that gate keeps its own escalate-once contract inside `_enforce_execution_gate` and is
    excluded here by its `_EXEC_GATE_GAP_PREFIX`. `final_pass` defaults False, so every direct caller
    and every non-final pass behaves byte-identically to before.
    """
    if not already_bounced and not final_pass:
        return result

    kept_issues: list[QualityIssue] = []
    for q in result.quality_issues:
        if q.severity in ("blocker", "major"):
            lens = _classify_unverifiable_finding(q.detail)
            if lens is not None and (final_pass
                                     or _finding_fingerprint(lens, q.detail) in already_bounced):
                result.unverifiable_gaps = list(result.unverifiable_gaps) + [q.detail]
                continue
        kept_issues.append(q)
    result.quality_issues = kept_issues

    kept_gaps: list[str] = []
    removed_gaps: list[str] = []
    for gap in result.spec_gaps:
        lens = _classify_unverifiable_finding(gap)
        # The final-pass arm deliberately excludes an execution-gate gap: relieving THAT gate is
        # _enforce_execution_gate's own escalate-once business (its docstring pins the contract), and
        # an exec-gate gap quoting a browser/responsive AC can classify as unverifiable, so without
        # this guard the final pass would silently wave it through.
        if lens is not None and (_finding_fingerprint(lens, gap) in already_bounced
                                 or (final_pass and not gap.startswith(_EXEC_GATE_GAP_PREFIX))):
            result.unverifiable_gaps = list(result.unverifiable_gaps) + [gap]
            removed_gaps.append(gap)
            continue
        kept_gaps.append(gap)
    result.spec_gaps = kept_gaps

    # EU-351 iteration-2: recompute the SPEC channel so a ticket whose only spec blocker was a
    # previously-bounced — or, since 2026-07-28, a final-pass — unverifiable finding can actually
    # reach ship-ready, not just get relabelled. By construction every gap in `removed_gaps`
    # classified as unverifiable (that is the sole removal condition), so "all removed gaps were
    # demoted-unverifiable" always holds here; the only extra requirement is that NONE survive
    # (`not result.spec_gaps`). A real surviving gap keeps `spec_gaps` non-empty and leaves
    # `spec_met` False: an ordinary spec gap never classifies as unverifiable, and an execution-gate
    # gap — which CAN classify, when the AC it quotes names a browser/responsive surface — is held
    # back by the `_EXEC_GATE_GAP_PREFIX` guard above, so a genuine execution-gate FAIL is never
    # flipped.
    if removed_gaps and not result.spec_gaps:
        result.spec_met = True

    if result.spec_met and not result.blocking_issues and not result.spec_gaps:
        result.verdict = Verdict.PASS
    return result


def collect_unverifiable_fingerprints(result: ReviewResult) -> set[str]:
    """EU-352: collect the fingerprints of every unverifiable-surface finding CURRENTLY present on
    `result` — whether already demoted to `unverifiable_gaps` (a prior-pass bounce recognised by
    `_enforce_bounce_once`) or still sitting in `quality_issues` (blocker/major) / `spec_gaps` (a
    first-time raise this pass hasn't bounced yet, since `already_bounced` was empty or didn't
    contain it). The loop folds the returned set into its per-attempt `bounced_unverifiable`
    accumulator so the NEXT `review()` call's `already_bounced` recognises the SAME finding
    reappearing and demotes it via `_enforce_bounce_once` instead of blocking again. Ordinary
    logic/data/test findings (the classifier returns None) contribute nothing. Public and
    side-effect-free — kept in reviewer.py (not loop.py) so the fingerprint/classify logic stays
    self-contained per EU-351's design; loop.py never imports the private `_`-prefixed helpers."""
    fps: set[str] = set()
    for detail in result.unverifiable_gaps:
        lens = _classify_unverifiable_finding(detail)
        if lens is not None:
            fps.add(_finding_fingerprint(lens, detail))
    for q in result.quality_issues:
        if q.severity in ("blocker", "major"):
            lens = _classify_unverifiable_finding(q.detail)
            if lens is not None:
                fps.add(_finding_fingerprint(lens, q.detail))
    for gap in result.spec_gaps:
        lens = _classify_unverifiable_finding(gap)
        if lens is not None:
            fps.add(_finding_fingerprint(lens, gap))
    # Execution-gate gaps (recognized by their fixed prefix) carry the "exec-gate" lens so the
    # NEXT pass's _enforce_execution_gate sees a repeat in `already_bounced` and demotes it
    # (escalate-once) instead of re-forcing a FAIL the Builder can never clear. Scanned in both
    # channels: spec_gaps (a first-time raise this pass) and unverifiable_gaps (already demoted).
    for gap in list(result.spec_gaps) + list(result.unverifiable_gaps):
        if gap.startswith(_EXEC_GATE_GAP_PREFIX):
            fps.add(_finding_fingerprint("exec-gate", gap))
    return fps


def _classify_diff(diff: str) -> tuple[str, str]:
    """Classify a diff as 'trivial' or 'production' based on size and content.

    Returns (category, reason) where category is 'trivial' or 'production'.
    Trivial: tests-only changes AND <30 total changed lines.
    Production: any .tsx/.py production file changed, OR ≥30 lines, OR mixed changes.
    """
    if not diff or not diff.strip():
        return ("trivial", "empty diff")

    # Check for production file changes (.tsx or .py files, excluding test files)
    # Production files are those not under /tests/ or with names not ending in _test.py
    has_production_change = False
    production_reason = ""

    for line in diff.split("\n"):
        # Look for file paths in diff headers (e.g., "+++ b/src/components/Button.tsx")
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            file_path = line.split()[1] if len(line.split()) > 1 else ""
            # Check if it's a production file
            is_tsx = file_path.endswith(".tsx")
            is_py = file_path.endswith(".py")
            is_test = "/tests/" in file_path or file_path.endswith("_test.py")

            if (is_tsx or is_py) and not is_test:
                has_production_change = True
                production_reason = f"{'.tsx' if is_tsx else '.py'} production file"
                break

    # Count changed lines (lines starting with + or -)
    changed_lines = sum(1 for line in diff.split("\n") if line.startswith("+") or line.startswith("-"))

    # Classification logic
    if has_production_change:
        return ("production", production_reason)
    if changed_lines >= 30:
        return ("production", "≥30 lines")
    return ("trivial", "tests-only, <30 lines")


def _effort_for_diff(category: str, cfg: Config) -> tuple[str, int]:
    """Return (effort, max_turns) based on diff category.

    Trivial diffs → low effort, max_turns ≤ 10 (fast conformance check).
    Production diffs → cfg.reviewer_effort, max_turns = 30 (full-depth review).
    """
    if category == "trivial":
        return ("low", 10)
    return (normalize_effort(cfg.reviewer_effort), 30)


async def review(diff: str, ticket: Ticket, app: AppConfig, cfg: Config, iteration: int = 1,
                 *, store: PerTicketArtifactStore | None = None,
                 build_artifact: BuildArtifact | None = None,
                 already_bounced: set[str] | None = None,
                 gate_evidence: str = "",
                 final_pass: bool = False) -> ReviewResult:
    from . import models, provider as _provider
    # EU-72: read the Builder's BuildArtifact (passed by the loop, or from the shared pool) as the
    # primary handoff; the full diff is still under review below. After parsing, publish a typed
    # ReviewVerdict into the pool for the next iteration / audit. Both default None so direct callers
    # are unaffected.
    if build_artifact is None and store is not None:
        build_artifact = store.build
    # EU-52: thread the build iteration so the Reviewer escalates one tier per re-review — a small diff
    # is judged on Sonnet on pass 1 and on Opus when a rebuilt diff comes back (for_reviewer climbs a
    # tier per retry). Without this the iteration>1 escalation branch was dead and every review pinned
    # the ceiling. Defaults to 1 so direct/CLI callers are unaffected.
    rmodel, rreason = models.for_reviewer(cfg, diff, iteration)   # ceiling unless auto_model is on
    if getattr(cfg, "auto_model", False):
        print(f"  · reviewer model: {rreason}", flush=True)

    # EU-198: scale effort/turns based on diff size + nature
    diff_category, diff_reason = _classify_diff(diff)
    review_effort, review_turns = _effort_for_diff(diff_category, cfg)
    print(f"  · reviewer: {diff_category} diff → {review_effort} effort, {review_turns} turns ({diff_reason})", flush=True)

    options = ClaudeAgentOptions(
        model=rmodel,
        system_prompt=memory.preamble() + REVIEWER_SYSTEM,
        cwd=app.workdir or app.repo_path,   # the isolated worktree when enabled
        permission_mode="bypassPermissions",   # read-only audit; runs unattended in the build loop —
        allowed_tools=["Read", "Grep", "Glob"], # must never dead-stop on a tool prompt mid-cycle
        # Task/Agent (sub-agent spawn) is denied: allowed_tools does NOT gate it under bypassPermissions,
        # and a sub-agent burns its OWN turns outside this max_turns cap — the 2026-07-08 AUTO-93 review
        # ran ~75 min fanning out on a trivial diff. Denied at every officer site (class fix); a read-only
        # judge reasons over the diff, it never needs to delegate.
        disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash", "Task", "Agent"],
        setting_sources=["project"],
        max_turns=review_turns,
        effort=review_effort,
    )
    # Sonnet-cap → one-shot Opus retry for this pass (per-call, no weekly pin — see run_agent_with_fallback)
    # EU-174: determine routing tier based on task characteristics
    routing_tier = None
    try:
        from . import routing as _routing
        if _routing.is_routing_enabled():
            ticket_desc = ticket.description or ""
            ticket_size = ticket.size or ""
            effort = normalize_effort(cfg.reviewer_effort)
            tier = _routing.classify_task(
                ticket_description=ticket_desc,
                task_type="review",
                effort=effort,
                size=ticket_size,
            )
            routing_tier = tier.value
            if routing_tier == "local":
                print(f"      · routing → Tier 1 (Local Ollama)", flush=True)
            else:
                print(f"      · routing → Tier 2 (Cloud)", flush=True)
    except Exception:  # noqa: BLE001 — routing failure must not break the review
        routing_tier = None

    # EU-258: stamp the ticket id + pass number onto the ledger row (fields k/p), as the Builder has
    # since EU-38 — both are in scope here and were simply never passed, leaving 480/480 tag="reviewer"
    # rows unattributable ($430.50, 13.9% of window spend at triage 2026-07-16). Ledger/analytics only:
    # loop.py's _burn("reviewer", ...) already feeds the live per-ticket budget gates.
    run = await run_agent_with_fallback(_prompt(diff, ticket, build_artifact), options, tag="reviewer",
                                        ticket_id=ticket.id, pass_number=iteration, cfg=cfg,
                                        routing_tier=routing_tier)

    result = _parse(run.final or run.text)
    # 2026-07-28: a deterministic enforcer whose OWN implementing source this diff edits is skipped
    # for this diff — it would be grading the change that fixes it (see _ENFORCER_SOURCE_FILES).
    # Empty for every ordinary diff, so nothing else changes.
    skipped_enforcers = _self_referential_enforcer_skips(diff)
    if "admitted-red-tests" not in skipped_enforcers:
        result = _enforce_admitted_red_tests(result, build_artifact)   # EU-249 deterministic backstop
    # EU-441/EU-446 deterministic backstops: production code without an accompanying test, Python
    # code missing/eroding typing, and an unscoped Supabase query on a user-scoped table. Run before
    # the execution gate so these FAILs are recorded alongside (not suppressed by) any execution-AC
    # gap. Per-area guards keep each independent.
    if "missing-tests" not in skipped_enforcers:
        result = _enforce_missing_tests(result, diff)
    if "missing-typing" not in skipped_enforcers:
        result = _enforce_missing_typing(result, diff)
    if "tenant-query" not in skipped_enforcers:
        result = _enforce_tenant_query(result, diff)   # EU-446: unscoped user-scoped Supabase query
    # EU-268 deterministic backstop; EU-265 threads the loop's real green-gate proof into it.
    result = _enforce_execution_gate(result, ticket, build_artifact, diff,
                                     gate_evidence=gate_evidence,
                                     already_bounced=already_bounced or set())
    # EU-351 deterministic backstop; `final_pass` (2026-07-28) widens the demotion to a FRESH
    # unverifiable finding on the last pass, where the only other option is escalating to the human.
    result = _enforce_bounce_once(result, already_bounced or set(), final_pass=final_pass)
    # Record every self-referential skip where the ticket can see it (loop.py posts unverifiable_gaps
    # per EU-353), so the relaxation is never silent.
    for _key in skipped_enforcers:
        _note = _enforcer_skip_note(_key)
        if _note not in result.unverifiable_gaps:
            result.unverifiable_gaps = list(result.unverifiable_gaps) + [_note]
        print(f"  · reviewer: '{_key}' enforcer skipped — this diff edits the enforcer's own source",
              flush=True)
    result.cost_usd = run.cost_usd
    result.raw = run.final
    result.input_tokens = getattr(run, "input_tokens", 0)   # EU-96: expose for per-officer burn tracking
    result.output_tokens = getattr(run, "output_tokens", 0)  # getattr-guarded: stubs may omit these
    result.provider = getattr(run, "provider", "")         # EU-123: which provider served this run
    result.model_version = getattr(run, "model_version", "") # EU-123: clean model identifier
    # EU-123: show actual provider+model in the live feed
    if getattr(cfg, "auto_model", False):
        display = _provider.format_provider_model(run.provider, run.model_version)
        print(f"  · reviewer · {display}", flush=True)
    # EU-72: publish the typed ReviewVerdict into the shared pool — a small, auditable record of the
    # verdict + blockers the next iteration reads (the rich ReviewResult keeps its own enum/parser
    # surface for the loop's decision logic).
    if store is not None:
        store.put(ReviewVerdict(
            verdict=result.verdict,
            blocking=[q.detail for q in result.blocking_issues] + list(result.spec_gaps),
            notes=[q.detail for q in result.quality_issues if q.severity == "minor"],
        ))
    return result


def _parse(text: str) -> ReviewResult:
    """Extract the JSON verdict. Fails safe to FAIL if it can't be parsed."""
    data = _extract_json(text)
    if data is None:
        return ReviewResult(
            verdict=Verdict.FAIL,
            spec_met=False,
            summary="Could not parse reviewer verdict; failing safe.",
            required_changes=["Reviewer output was unparseable; re-run review."],
            raw=text,
            parse_failed=True,
        )
    spec = data.get("spec_conformance", {}) or {}
    quality = (data.get("quality", {}) or {}).get("issues", []) or []
    issues = [
        QualityIssue(
            severity=str(q.get("severity", "minor")).lower(),
            area=str(q.get("area", "")),
            detail=str(q.get("detail", "")),
        )
        for q in quality
    ]
    verdict = Verdict.PASS if str(data.get("verdict", "")).upper() == "PASS" else Verdict.FAIL
    return ReviewResult(
        verdict=verdict,
        spec_met=bool(spec.get("met", False)),
        spec_gaps=list(spec.get("gaps", []) or []),
        quality_issues=issues,
        required_changes=list(data.get("required_changes", []) or []),
        needs_human=bool(data.get("needs_human", False)),
        question=str(data.get("question", "")),
        summary=str(data.get("summary", "")),
    )


def _extract_json(text: str) -> dict | None:
    # Prefer a fenced ```json block; fall back to the last {...} span.
    fences = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = list(fences)
    if not candidates:
        brace = re.findall(r"(\{.*\})", text, re.DOTALL)
        candidates = brace[-1:] if brace else []
    for cand in reversed(candidates):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None


# EU-396: when the Builder makes NO changes and claims the ticket's acceptance criteria are already
# satisfied, that claim used to go straight to the Commander unverified ("verify and close it
# yourself" — the exact senior_pm-style mistake §2 was retired for). This mode runs the Reviewer
# read-only against the UNCHANGED tree (there is no diff) to check the claim itself before anyone
# escalates: per acceptance criterion, is it genuinely already met, cited to file:line?
NO_CHANGES_VERIFY_SYSTEM = """\
You are the Reviewer in an automated dev pipeline, called in a special AUDIT mode: the Builder made
NO code changes this pass and claims every acceptance criterion of this ticket is ALREADY satisfied
by the existing code. There is no diff to review — you are auditing the CURRENT (unchanged) tree.

You are deliberately adversarial: do not take the Builder's word for it. For EACH acceptance
criterion, read the actual code (Read/Grep/Glob) and decide whether it is genuinely already
satisfied, citing exact file:line evidence for what you saw.

This verdict can send the ticket straight to QA with NO further human review, so be conservative:
"confident": true is ONLY for the case where you have concrete file:line evidence that EVERY single
criterion is met. A criterion that is unproven, partially met, ambiguous, or needs a manual/visual/
runtime check you cannot perform from reading code means "confident": false — that is the safe,
common outcome; do not stretch for confidence.

You may read files in the repo for context, but you cannot modify anything.

You MUST end your response with a single fenced ```json block, and nothing after it, matching
exactly this schema:

```json
{
  "confident": true | false,
  "findings": [
    {
      "criterion": "<the acceptance criterion, verbatim or tightly summarized>",
      "satisfied": true | false,
      "evidence": "<file:line and a one-line explanation of what you saw there, or — if not satisfied — the gap>"
    }
  ],
  "summary": "≤5 tight bullets, one per criterion, each ending in its file:line citation or its gap"
}
```
"""


def _no_changes_prompt(ticket: Ticket, report: str) -> str:
    acs = list(ticket.acceptance_criteria or [])
    ac_block = "\n".join(f"{i}. {c}" for i, c in enumerate(acs, 1)) if acs else \
        "(none listed on the ticket — treat the description below as the acceptance criteria)"
    return (
        f"TICKET {ticket.id}: {ticket.summary}\n\n"
        f"DESCRIPTION:\n{(ticket.description or '(none)').strip()[:3000]}\n\n"
        f"ACCEPTANCE CRITERIA:\n{ac_block}\n\n"
        "THE BUILDER'S REPORT for why it made no changes:\n"
        f"{(report or '(no report given)').strip()[:2000]}\n\n"
        "Audit the CURRENT tree (there is no diff) and answer, per acceptance criterion: are these "
        "ACs already satisfied — cite file:line per AC."
    )


def _parse_no_changes_verdict(text: str) -> dict:
    """Pure parse of the AUDIT-mode reply. Fails CLOSED (confident=False) on anything unparseable or
    under-evidenced — never trusts the model's own "confident" flag at face value: it is downgraded
    to False unless every finding is satisfied=True AND carries non-empty evidence (deterministic
    backstop, same shape as _enforce_execution_gate elsewhere in this file)."""
    data = _extract_json(text)
    if data is None:
        return {"confident": False, "findings": [],
                "summary": "Could not parse the reviewer's verify output; failing closed (not confident).",
                "parse_failed": True}
    findings = []
    for f in (data.get("findings") or []):
        if not isinstance(f, dict):
            continue
        findings.append({
            "criterion": str(f.get("criterion", ""))[:400],
            "satisfied": bool(f.get("satisfied", False)),
            "evidence": str(f.get("evidence", ""))[:400],
        })
    all_cited = bool(findings) and all(f["satisfied"] and f["evidence"].strip() for f in findings)
    confident = bool(data.get("confident", False)) and all_cited
    return {
        "confident": confident,
        "findings": findings,
        "summary": str(data.get("summary", ""))[:1500],
        "parse_failed": False,
    }


async def verify_no_changes(ticket: Ticket, app: AppConfig, cfg: Config, report: str) -> dict:
    """EU-396: verify a Builder no-changes claim against the UNCHANGED tree before the loop escalates
    it to the Commander. Read-only Reviewer pass, no diff — per-AC file:line evidence.

    Returns {"confident", "findings", "summary", "cost_usd", "input_tokens", "output_tokens",
    "provider", "model_version", "raw", "parse_failed"}. Fails CLOSED (confident=False, zero cost) on
    any call error — an unverifiable claim is NEVER auto-closed, it just falls through to today's
    escalation, optionally with whatever findings this call did manage to gather.
    """
    from . import models
    model, mreason = models.for_officer(cfg, effort="high")
    if getattr(cfg, "auto_model", False):
        print(f"  · no-changes verify model: {mreason}", flush=True)
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=memory.preamble() + NO_CHANGES_VERIFY_SYSTEM,
        cwd=app.workdir or app.repo_path,   # the isolated worktree when enabled
        permission_mode="bypassPermissions",   # read-only audit; must never dead-stop on a prompt
        allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash", "Task", "Agent"],
        setting_sources=["project"],
        max_turns=18,
        effort="high",
    )
    try:
        run = await run_agent_with_fallback(_no_changes_prompt(ticket, report), options,
                                            tag="reviewer-verify", ticket_id=ticket.id, cfg=cfg)
    except Exception as exc:  # noqa: BLE001 — a verify-call crash must fail closed, never crash the caller
        return {"confident": False, "findings": [], "summary": f"Verification call failed: {exc}",
                "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "provider": "",
                "model_version": "", "raw": "", "parse_failed": True}
    result = _parse_no_changes_verdict(run.final or run.text or "")
    result["cost_usd"] = getattr(run, "cost_usd", 0.0)
    result["input_tokens"] = getattr(run, "input_tokens", 0)
    result["output_tokens"] = getattr(run, "output_tokens", 0)
    result["provider"] = getattr(run, "provider", "")
    result["model_version"] = getattr(run, "model_version", "")
    result["raw"] = run.final or run.text or ""
    return result
