"""EU-446 regression: add a deterministic tenant-scope/query-presence heuristic to the EU-441
Reviewer gate.

EU-441 hardened the Reviewer to mechanically block missing tests and missing/eroded typing, but left
tenant-isolation to the REVIEWER_SYSTEM prompt alone — which the LLM can still wave through (the exact
AUTO-14/AUTO-18 unreliability EU-441 was created to eliminate). This adds a third deterministic
backstop, _enforce_tenant_query, symmetric with _enforce_missing_tests / _enforce_missing_typing,
that fires on query PRESENCE (mechanically sound) rather than guessing whether a tenant filter is
MISSING (regex-infeasible, and the over-broad shape EU-249 iter-2 explicitly rejected).

Stages (each leaves the tree green): run a single stage with `python3 tests/eu446_..._test.py
stage-N`, or everything with no arg / `all`.

  stage-1 — pure helper _diff_unscoped_tenant_queries(diff) -> offending lines (JS .from( AND Python
            .from_(, user-scoped table noun lever, scoping-signal clearance, global-table silence).
  stage-2 — _enforce_tenant_query (AC 1–5): unscoped user-table query -> forced FAIL (major, area
            'tenant-isolation'); explicit .eq('tenant_id',...) / RLS marker clears; non-user-scoped
            lookup table (countries/app_config) no-op; per-area suppress-guard (no dup).
  stage-3 — end-to-end review() (SDK + run_agent_with_fallback stubbed) forced FAIL + unchanged
            tool permissions (AC 6).

All offline — SDK and agents stubbed; no network, no real models.
"""
import asyncio
import sys
import types

# ── SDK stub (no real model calls) — mirrors tests/eu441_reviewer_hardening_test.py ─────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): self.__dict__.update(k)
    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import reviewer as R       # noqa: E402
from orchestrator.config import Config, AppConfig  # noqa: E402
from orchestrator.contracts import QualityIssue, ReviewResult, Ticket, Verdict  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def _pass_result() -> ReviewResult:
    """A fresh PASS/spec_met result — the deterministic gates mutate the object they receive, so
    every call site needs its own instance (mirrors eu441_reviewer_hardening_test.py)."""
    return ReviewResult(verdict=Verdict.PASS, spec_met=True, spec_gaps=[], summary="looks fine")


# ── fixture diffs ────────────────────────────────────────────────────────────────────────────
# UN-scoped Supabase query on a user-scoped table ('leads'): a .from('leads').select('*') call with
# NO .eq('tenant_id'...) and NO RLS marker anywhere in the diff -> the cross-tenant leak the gate
# must block (AC 1).
UNSCOPED_USER_QUERY = (
    "diff --git a/src/leads.ts b/src/leads.ts\n"
    "+++ b/src/leads.ts\n"
    "@@ -0,0 +1,4 @@\n"
    "+import { supabase } from './db'\n"
    "+export async function list() {\n"
    "+  const { data } = await supabase.from('leads').select('*')\n"
    "+  return data\n"
    "+}\n"
)

# Python form (.from_) of the same unscoped query — proves BOTH clients are covered.
PY_UNSCOPED_USER_QUERY = (
    "diff --git a/svc/leads_svc.py b/svc/leads_svc.py\n"
    "+++ b/svc/leads_svc.py\n"
    "@@ -0,0 +1,1 @@\n"
    "+rows = supabase.from_('leads').select('*').execute()\n"
)

# Same user-table query WITH an explicit tenant filter on the chain -> clears (AC 2).
SCOPED_USER_QUERY = (
    "diff --git a/src/leads.ts b/src/leads.ts\n"
    "+++ b/src/leads.ts\n"
    "@@ -0,0 +1,4 @@\n"
    "+export async function list(session) {\n"
    "+  const { data } = await supabase.from('leads').select('*')\n"
    "+    .eq('tenant_id', session.tenantId)\n"
    "+  return data\n"
    "+}\n"
)

# User-table query diff that ALSO carries an RLS signal (enable + create policy + using/check) ->
# clears (AC 3): a clean (RLS-wrapped) query diff is NOT false-blocked.
RLS_WRAPPED_QUERY = (
    "diff --git a/migrations/0042_leads_rls.sql b/migrations/0042_leads_rls.sql\n"
    "+++ b/migrations/0042_leads_rls.sql\n"
    "@@ -0,0 +1,3 @@\n"
    "+alter table leads enable row level security;\n"
    "+create policy tenant_iso on leads\n"
    "+  using (tenant_id = current_setting('app.tenant_id')) with check (tenant_id = current_setting('app.tenant_id'));\n"
    "diff --git a/src/leads.ts b/src/leads.ts\n"
    "+++ b/src/leads.ts\n"
    "@@ -0,0 +1,1 @@\n"
    "+const { data } = await supabase.from('leads').select('*')\n"
)

# Query on a non-user-scoped LOOKUP table -> SILENT (AC 4): EU-249 — never false-block a table the
# lever cannot classify as user-scoped.
GLOBAL_LOOKUP_QUERY = (
    "diff --git a/src/geo.ts b/src/geo.ts\n"
    "+++ b/src/geo.ts\n"
    "@@ -0,0 +1,1 @@\n"
    "+const { data } = await supabase.from('countries').select('*')\n"
)
GLOBAL_CONFIG_QUERY = (
    "diff --git a/src/config.ts b/src/config.ts\n"
    "+++ b/src/config.ts\n"
    "@@ -0,0 +1,1 @@\n"
    "+const row = await supabase.from('app_config').select('value')\n"
)

# A diff with NO Supabase query at all -> SILENT (no entry-point method present).
NO_QUERY_DIFF = (
    "diff --git a/src/util.ts b/src/util.ts\n"
    "+++ b/src/util.ts\n"
    "@@ -0,0 +1,1 @@\n"
    "+export const add = (a: number, b: number) => a + b\n"
)


# ════════════════════════════════════════════════════════════════════════════════════════════
# stage-1 — pure helper _diff_unscoped_tenant_queries(diff) -> offending lines
# ════════════════════════════════════════════════════════════════════════════════════════════
def stage1() -> None:
    off = R._diff_unscoped_tenant_queries(UNSCOPED_USER_QUERY)
    chk("S1 unscoped user-table query -> offender list is non-empty", off != [], str(off))
    chk("S1 unscoped user-table query -> offender cites the leads query line",
        any("leads" in ln and "from(" in ln.replace(" ", "") for ln in off), str(off))

    py_off = R._diff_unscoped_tenant_queries(PY_UNSCOPED_USER_QUERY)
    chk("S1 Python .from_('leads') unscoped query -> offender list is non-empty (both clients)",
        py_off != [], str(py_off))

    chk("S1 scoped query (.eq('tenant_id',...)) -> no offenders (clears)",
        R._diff_unscoped_tenant_queries(SCOPED_USER_QUERY) == [],
        str(R._diff_unscoped_tenant_queries(SCOPED_USER_QUERY)))
    chk("S1 RLS-wrapped query -> no offenders (clears)",
        R._diff_unscoped_tenant_queries(RLS_WRAPPED_QUERY) == [],
        str(R._diff_unscoped_tenant_queries(RLS_WRAPPED_QUERY)))
    chk("S1 global lookup table ('countries') -> no offenders (EU-249 silence)",
        R._diff_unscoped_tenant_queries(GLOBAL_LOOKUP_QUERY) == [],
        str(R._diff_unscoped_tenant_queries(GLOBAL_LOOKUP_QUERY)))
    chk("S1 global config table ('app_config') -> no offenders (EU-249 silence)",
        R._diff_unscoped_tenant_queries(GLOBAL_CONFIG_QUERY) == [],
        str(R._diff_unscoped_tenant_queries(GLOBAL_CONFIG_QUERY)))
    chk("S1 diff with no Supabase query -> no offenders",
        R._diff_unscoped_tenant_queries(NO_QUERY_DIFF) == [],
        str(R._diff_unscoped_tenant_queries(NO_QUERY_DIFF)))

    # Snake_case user-scoped tables still match the noun lever (tokenized by _).
    snake = R._diff_unscoped_tenant_queries(
        "+++ b/svc.py\n@@ -0,0 +1,1 @@\n+db.from_('tenant_members').select('*')\n")
    chk("S1 snake_case user-scoped table ('tenant_members') -> offender detected",
        snake != [], str(snake))
    # A user-scoped noun as a non-leading token still matches (app_users).
    app_users = R._diff_unscoped_tenant_queries(
        "+++ b/svc.py\n@@ -0,0 +1,1 @@\n+db.from_('app_users').select('*')\n")
    chk("S1 'app_users' (noun in a later token) -> offender detected", app_users != [], str(app_users))


# ════════════════════════════════════════════════════════════════════════════════════════════
# stage-2 — _enforce_tenant_query (AC 1–5)
# ════════════════════════════════════════════════════════════════════════════════════════════
def stage2() -> None:
    # AC 1 — unscoped user-table query on a fresh PASS/spec_met result -> FAIL + blocking finding.
    r = R._enforce_tenant_query(_pass_result(), UNSCOPED_USER_QUERY)
    chk("S2 AC1 unscoped user-table query -> verdict FAIL", r.verdict == Verdict.FAIL, str(r.verdict))
    tenant = [q for q in r.quality_issues if q.area in ("tenant-isolation", "security")]
    chk("S2 AC1 a tenant-isolation/security quality issue is recorded", len(tenant) >= 1,
        str([(q.severity, q.area) for q in r.quality_issues]))
    chk("S2 AC1 the tenant finding is blocking (blocker|major)",
        any(q.severity in ("blocker", "major") for q in tenant),
        str([(q.severity, q.area) for q in tenant]))
    chk("S2 AC1 the detail names the offending query + the scoping requirement",
        any(("leads" in q.detail or "query" in q.detail.lower()) and
            ("tenant" in q.detail.lower() or "rls" in q.detail.lower()) for q in tenant),
        str([q.detail for q in tenant]))
    chk("S2 AC1 a required_change was appended",
        len(r.required_changes) >= 1, str(r.required_changes))

    # AC 2 — same user-table query WITH an explicit tenant filter -> PASS, no finding.
    r2 = R._enforce_tenant_query(_pass_result(), SCOPED_USER_QUERY)
    chk("S2 AC2 scoped query (.eq('tenant_id',...)) -> verdict stays PASS",
        r2.verdict == Verdict.PASS, str(r2.verdict))
    chk("S2 AC2 scoped query -> no tenant-isolation/security finding added",
        not any(q.area in ("tenant-isolation", "security") for q in r2.quality_issues),
        str([(q.severity, q.area) for q in r2.quality_issues]))

    # AC 3 — query diff that ALSO carries an RLS signal -> PASS, no finding.
    r3 = R._enforce_tenant_query(_pass_result(), RLS_WRAPPED_QUERY)
    chk("S2 AC3 RLS-wrapped query -> verdict stays PASS (not false-blocked)",
        r3.verdict == Verdict.PASS, str(r3.verdict))
    chk("S2 AC3 RLS-wrapped query -> no tenant-isolation/security finding added",
        not any(q.area in ("tenant-isolation", "security") for q in r3.quality_issues),
        str([(q.severity, q.area) for q in r3.quality_issues]))

    # AC 4 — query on a non-user-scoped/lookup table, no scoping signal -> PASS (EU-249 silence).
    for label, diff in (("countries", GLOBAL_LOOKUP_QUERY), ("app_config", GLOBAL_CONFIG_QUERY),
                        ("no-query", NO_QUERY_DIFF)):
        rr = R._enforce_tenant_query(_pass_result(), diff)
        chk(f"S2 AC4 {label} diff -> NOT forced FAIL (no false positive)",
            rr.verdict == Verdict.PASS and
            not any(q.area in ("tenant-isolation", "security") for q in rr.quality_issues),
            str(rr.verdict) + str([(q.severity, q.area) for q in rr.quality_issues]))

    # AC 5 — a result that ALREADY carries a blocker/major tenant-isolation finding -> no duplicate.
    pre = ReviewResult(verdict=Verdict.FAIL, spec_met=False, spec_gaps=[],
                       quality_issues=[QualityIssue(
                           severity="blocker", area="tenant-isolation",
                           detail="LLM already named the missing tenant filter on the leads query")],
                       summary="caught it")
    r5 = R._enforce_tenant_query(pre, UNSCOPED_USER_QUERY)
    chk("S2 AC5 pre-existing tenant-isolation blocker -> verdict unchanged (no force)",
        r5.verdict == Verdict.FAIL, str(r5.verdict))
    chk("S2 AC5 pre-existing tenant-isolation blocker -> exactly ONE such issue (no duplicate)",
        sum(1 for q in r5.quality_issues if q.area == "tenant-isolation") == 1,
        str([(q.severity, q.area) for q in r5.quality_issues]))
    # The 'security' area also trips the per-area suppress-guard.
    pre_sec = ReviewResult(verdict=Verdict.FAIL, spec_met=False, spec_gaps=[],
                           quality_issues=[QualityIssue(
                               severity="major", area="security",
                               detail="LLM flagged a data-leak on the leads query")],
                           summary="caught it")
    r5s = R._enforce_tenant_query(pre_sec, UNSCOPED_USER_QUERY)
    chk("S2 AC5 pre-existing 'security' major -> no tenant-isolation finding added",
        not any(q.area == "tenant-isolation" for q in r5s.quality_issues),
        str([(q.severity, q.area) for q in r5s.quality_issues]))
    chk("S2 AC5 pre-existing 'security' major -> the security issue is preserved",
        any(q.area == "security" for q in r5s.quality_issues),
        str([(q.severity, q.area) for q in r5s.quality_issues]))


# ════════════════════════════════════════════════════════════════════════════════════════════
# stage-3 — end-to-end review() (AC 6)
# ════════════════════════════════════════════════════════════════════════════════════════════
class _RR:
    def __init__(self, t):
        (self.final, self.text, self.is_error, self.cost_usd, self.num_turns, self.tools,
         self.provider, self.model_version) = t, t, False, 0.0, 1, [], "Anthropic", "claude-sonnet-4-6"
        self.input_tokens = self.output_tokens = 0


# The LLM's own JSON claims PASS, spec_met=true, zero issues — exactly the AUTO-14/AUTO-18 shape the
# deterministic gate must override.
_PASS_MET_JSON = ('```json\n{"verdict":"PASS","spec_conformance":{"met":true,"gaps":[]},'
                  '"quality":{"issues":[]},"required_changes":[],"summary":"all good"}\n```')

_captured: dict = {}


async def _fake_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None,
                          cfg=None, routing_tier=None):
    _captured["allowed_tools"] = getattr(options, "allowed_tools", None)
    _captured["disallowed_tools"] = getattr(options, "disallowed_tools", None)
    return _RR(_PASS_MET_JSON)


def stage3() -> None:
    _orig = R.run_agent_with_fallback
    R.run_agent_with_fallback = _fake_run_agent
    try:
        cfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                                     protected_branch="MAIN", backlog_backend="none")],
                     audit_path="/tmp/eu446-audit.jsonl", use_worktree=False, auto_model=False)
        app = cfg.app("automatixy")
        tk = Ticket(id="EU-446", key="EU-446", summary="tenant-scope reviewer gate",
                    description="d", acceptance_criteria=["Reviewer blocks unscoped tenant queries"])

        # AC 6a — unscoped user-table query, LLM said PASS -> forced FAIL with the tenant finding.
        r1 = asyncio.run(R.review(UNSCOPED_USER_QUERY, tk, app, cfg))
        chk("S3 AC6 unscoped user-table query -> verdict FAIL (overrode LLM PASS)",
            r1.verdict == Verdict.FAIL, str(r1.verdict))
        chk("S3 AC6 a blocking tenant-isolation/security issue is present",
            any(q.area in ("tenant-isolation", "security") and q.severity in ("blocker", "major")
                for q in r1.quality_issues),
            str([(q.severity, q.area) for q in r1.quality_issues]))

        # AC 6b — clean (RLS-wrapped) query diff -> verdict stays PASS (not false-blocked).
        r2 = asyncio.run(R.review(RLS_WRAPPED_QUERY, tk, app, cfg))
        chk("S3 AC6 clean RLS-wrapped query diff -> verdict stays PASS",
            r2.verdict == Verdict.PASS, str(r2.verdict))
        chk("S3 AC6 clean RLS-wrapped query diff -> no tenant finding",
            not any(q.area in ("tenant-isolation", "security") for q in r2.quality_issues),
            str([(q.severity, q.area) for q in r2.quality_issues]))

        # AC 6c — tool permissions unchanged by this verdict-logic hardening (read-only audit surface).
        chk("S3 AC6 allowed_tools == ['Read', 'Grep', 'Glob'] (unchanged)",
            _captured.get("allowed_tools") == ["Read", "Grep", "Glob"],
            str(_captured.get("allowed_tools")))
        chk("S3 AC6 'Bash' remains in disallowed_tools",
            "Bash" in (_captured.get("disallowed_tools") or []),
            str(_captured.get("disallowed_tools")))
    finally:
        R.run_agent_with_fallback = _orig


_STAGES = {"stage-1": stage1, "stage-2": stage2, "stage-3": stage3}


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    if arg in ("all", "stage-all"):
        for fn in (stage1, stage2, stage3):
            fn()
    elif arg in _STAGES:
        _STAGES[arg]()
    else:
        print(f"unknown stage: {arg!r} (expected stage-1..3 or all)")
        return 2
    passed = sum(1 for _, ok, _ in results if ok)
    print("\n================ EU-446 TENANT-SCOPE QUERY GATE ================")
    for n, ok, det in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
    print("----------------------------------------------------------------")
    print(f"  {passed}/{len(results)} passed")
    print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
