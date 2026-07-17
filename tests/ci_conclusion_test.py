"""EU-251: the land path's remote-CI reality check. AUTO-112 landed on a verbatim "CI will go
green" claim (verified with `bun test`, not the vitest CI actually runs) while the real GitHub
Actions run for that merge commit was RED, and nothing in orchestrator/ ever consulted GitHub
Actions to catch it. Exercises orchestrator.ci_conclusion against a STUBBED `gh` (no network, no
real GitHub Actions calls) for green / red / timed-out merge-commit runs, plus the gating
(`should_run`) that keeps a non-CI ticket from spawning any `gh` subprocess at all.
"""
import sys, types
sys.path.insert(0, ".")

# Stub the Agent SDK so importing the orchestrator package never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

import json

from orchestrator import ci_conclusion, filing

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace


class Audit:
    def __init__(s): s.events = []
    def record(s, kind, **kw): s.events.append((kind, kw))


class StubBacklog:
    """Mirrors the filing.py contract used elsewhere (eu42_filing_test.py): create_task records
    the new ticket, find_open_by_summary de-dups against a seeded set of open summaries. Also
    spies on add_comment / set_status so a test can assert the land is never unwound."""
    def __init__(self, open_summaries=None):
        self._open = {s.strip().lower(): k for s, k in (open_summaries or {}).items()}
        self.created = []
        self.comments = []
        self.status_calls = []

    def find_open_by_summary(self, summary):
        return self._open.get(str(summary).strip().lower())

    def create_task(self, summary, description, labels=None, issue_type="Task", priority=None):
        key = f"EU-{900 + len(self.created)}"
        self.created.append((summary, list(labels or []), issue_type))
        self._open[summary.strip().lower()] = key  # so a re-run of the SAME title de-dups
        return key

    def add_comment(self, ticket, body):
        self.comments.append(body)

    def set_status(self, ticket, status):
        self.status_calls.append(status)


def app(name="automatixy"):
    return ns(name=name, base_branch="DEV", repo_path="/tmp", workdir="/tmp")


def ticket(id="AUTO-112", labels=None, summary="Fix leaky RLS check", description="",
          acceptance_criteria=None, ephemeral=False):
    return ns(id=id, labels=labels or [], summary=summary, description=description,
              acceptance_criteria=acceptance_criteria or [], ephemeral=ephemeral)


cfg = ns()  # should_run/check/report don't dereference cfg beyond a single optional attr


# --- 1) should_run gating -------------------------------------------------------------------- #
chk("should_run True for label 'ci'", ci_conclusion.should_run(cfg, ticket(labels=["ci"])))
chk("should_run True for AC mentioning GitHub Actions",
    ci_conclusion.should_run(cfg, ticket(acceptance_criteria=["the GitHub Actions run must be green"])))
chk("should_run True for description mentioning CI",
    ci_conclusion.should_run(cfg, ticket(description="Fix the CI workflow status check")))
chk("should_run False for an ordinary ticket",
    not ci_conclusion.should_run(cfg, ticket(labels=[], summary="Fix leaky RLS check",
                                             description="tighten the tenant_id filter",
                                             acceptance_criteria=["queries scoped to tenant_id"])))


def land_ci_step(cfg, app, tkt, backlog, merge_sha, audit, gh_runner, **kw):
    """Mirrors the loop.py `_land` wiring: should_run gates check()+report() entirely — for a
    non-CI ticket NEITHER is ever invoked, so gh_runner is never called."""
    calls = []
    def counting(args):
        calls.append(args)
        return gh_runner(args)
    if not ci_conclusion.should_run(cfg, tkt):
        return None, "", calls
    # A red conclusion files its follow-up via filing.file_findings, which resolves its OWN
    # backlog handle through filing.make_backlog(app) — point it at the SAME stub `backlog` so
    # the test can see both the ticket comment and any filed follow-up on one object (this mirrors
    # real code, where make_backlog(app) and the loop's `backlog` both resolve to the same adapter).
    filing.make_backlog = lambda a: backlog
    result = ci_conclusion.check(cfg, app, tkt, merge_sha, audit, gh_runner=counting, **kw)
    note = ci_conclusion.report(cfg, app, tkt, backlog, result, audit)
    return result, note, calls


def _boom(args):
    raise AssertionError(f"gh must never be called for a non-CI ticket (got {args})")

au0 = Audit()
bl0 = StubBacklog()
res0, note0, calls0 = land_ci_step(cfg, app(), ticket(labels=[]), bl0, "abc123", au0, _boom)
chk("non-CI ticket: check() never invoked", res0 is None)
chk("non-CI ticket: zero gh subprocess calls", calls0 == [])
chk("non-CI ticket: no ticket comment posted", bl0.comments == [])


# --- 2) GREEN: gh reports the merge commit's run as success ----------------------------------- #
def gh_green(args):
    if args[:2] == ["run", "list"]:
        return json.dumps([{"databaseId": 1, "status": "completed", "conclusion": "success",
                            "url": "https://github.com/x/y/actions/runs/1", "headSha": "9943818"}])
    raise AssertionError(f"unexpected gh call on green run: {args}")

au1 = Audit()
bl1 = StubBacklog()
res1, note1, calls1 = land_ci_step(cfg, app(), ticket(labels=["ci"]), bl1, "9943818", au1, gh_green,
                                   poll_interval_sec=0.01, timeout_sec=0.05)
chk("green: status == 'green'", res1.status == "green", res1.status)
chk("green: at least one gh call made", len(calls1) >= 1)
chk("green: hand-off comment states the real conclusion (success)",
    len(bl1.comments) == 1 and "success" in bl1.comments[0].lower(), bl1.comments)
chk("green: no follow-up ticket filed", bl1.created == [], bl1.created)
chk("green: audit recorded ci_conclusion status=green",
    any(k == "ci_conclusion" and kw.get("status") == "green" for k, kw in au1.events))


# --- 3) RED: gh reports failure with named failing jobs, exactly-once de-duped filing ---------- #
_red_calls = []
def gh_red(args):
    _red_calls.append(args)
    if args[:2] == ["run", "list"]:
        return json.dumps([{"databaseId": 42, "status": "completed", "conclusion": "failure",
                            "url": "https://github.com/x/y/actions/runs/42", "headSha": "sha42"}])
    if args[:2] == ["run", "view"]:
        return json.dumps({"jobs": [
            {"name": "Test (vitest) — node18", "conclusion": "failure"},
            {"name": "Test (vitest) — node20", "conclusion": "success"},
            {"name": "Lint", "conclusion": "cancelled"},
        ]})
    raise AssertionError(f"unexpected gh call on red run: {args}")

au2 = Audit()
bl2 = StubBacklog()
res2, note2, calls2 = land_ci_step(cfg, app(), ticket(id="AUTO-112", labels=["ci"]), bl2, "sha42",
                                   au2, gh_red, poll_interval_sec=0.01, timeout_sec=0.05)
chk("red: status == 'red'", res2.status == "red", res2.status)
chk("red: failing_jobs names the failing job(s)",
    "Test (vitest) — node18" in res2.failing_jobs and "Test (vitest) — node20" not in res2.failing_jobs,
    res2.failing_jobs)
chk("red: comment contains 'AC not met: CI still red'",
    len(bl2.comments) == 1 and "AC not met: CI still red" in bl2.comments[0], bl2.comments)
chk("red: comment names the failing job",
    len(bl2.comments) == 1 and "Test (vitest) — node18" in bl2.comments[0], bl2.comments)
chk("red: exactly one residual-failure follow-up filed", len(bl2.created) == 1, bl2.created)
chk("red: land itself is not unwound (no status transition)", bl2.status_calls == [], bl2.status_calls)

# Second run against the SAME still-red commit -> dedupes instead of re-filing.
res2b, note2b, _ = land_ci_step(cfg, app(), ticket(id="AUTO-112", labels=["ci"]), bl2, "sha42",
                                Audit(), gh_red, poll_interval_sec=0.01, timeout_sec=0.05)
chk("red rerun: no second follow-up created (de-duped)", len(bl2.created) == 1, bl2.created)
chk("red rerun: still posts an 'AC not met' comment", "AC not met: CI still red" in bl2.comments[-1])


# --- 4) TIMEOUT: gh never reaches a terminal conclusion within the bounded wait ---------------- #
def gh_timeout(args):
    if args[:2] == ["run", "list"]:
        return json.dumps([{"databaseId": 7, "status": "in_progress", "conclusion": None,
                            "url": "https://github.com/x/y/actions/runs/7", "headSha": "shaT"}])
    raise AssertionError(f"unexpected gh call on timeout run: {args}")

au3 = Audit()
bl3 = StubBacklog()
res3, note3, calls3 = land_ci_step(cfg, app(), ticket(id="AUTO-3", labels=["ci"]), bl3, "shaT", au3,
                                   gh_timeout, poll_interval_sec=0.01, timeout_sec=0.03)
chk("timeout: status == 'timeout'", res3.status == "timeout", res3.status)
chk("timeout: hand-off says status was not confirmed",
    len(bl3.comments) == 1 and "not confirmed" in bl3.comments[0].lower(), bl3.comments)
chk("timeout: hand-off is NOT phrased as success",
    len(bl3.comments) == 1 and "success" not in bl3.comments[0].lower(), bl3.comments)
chk("timeout: land itself is not unwound (no status transition)", bl3.status_calls == [], bl3.status_calls)
chk("timeout: no follow-up filed", bl3.created == [], bl3.created)


# --- 5) a gh runner that always raises never propagates into the loop -------------------------- #
def gh_boom(args):
    raise RuntimeError("gh: command not found")

au4 = Audit()
bl4 = StubBacklog()
try:
    res4, note4, _ = land_ci_step(cfg, app(), ticket(id="AUTO-4", labels=["ci"]), bl4, "shaX", au4,
                                  gh_boom, poll_interval_sec=0.01, timeout_sec=0.03)
    raised = False
except Exception:
    raised = True
chk("gh failure never raises out of check()/report()", not raised)
chk("gh failure -> reported as timeout (unconfirmed), not success",
    not raised and res4.status == "timeout", getattr(res4, "status", "raised"))


# --- 6) EU-270: `gh run list --commit` returns one run PER WORKFLOW FILE ----------------------- #
# The module used to read terminal_runs[0] under the comment "this is the merge commit's run", which
# is false for any repo with more than one push-triggered workflow (CodeQL/deploy beside ci.yml). A
# read-only probe against the pre-fix module returned 'green' for a green+FAILURE sibling pair —
# reintroducing the exact false-green EU-251 exists to prevent.
def _runs_json(runs):
    return json.dumps(runs)

# (a) newest run green, sibling workflow RED -> the commit is RED, never green.
def gh_multi_red(args):
    if args[:2] == ["run", "list"]:
        return _runs_json([
            {"databaseId": 1, "status": "completed", "conclusion": "success", "workflowName": "ci",
             "url": "https://github.com/x/y/actions/runs/1", "headSha": "multi1"},
            {"databaseId": 2, "status": "completed", "conclusion": "failure", "workflowName": "CodeQL",
             "url": "https://github.com/x/y/actions/runs/2", "headSha": "multi1"},
        ])
    if args[:2] == ["run", "view"] and "2" in args:
        return json.dumps({"jobs": [{"name": "Analyze", "conclusion": "failure"}]})
    if args[:2] == ["run", "view"]:
        return json.dumps({"jobs": []})
    raise AssertionError(f"unexpected gh call: {args}")

au5 = Audit()
bl5 = StubBacklog()
res5, note5, _ = land_ci_step(cfg, app(), ticket(id="AUTO-270", labels=["ci"]), bl5, "multi1", au5,
                              gh_multi_red, poll_interval_sec=0.01, timeout_sec=0.05)
chk("multi-workflow: green newest + red sibling -> status == 'red' (no false green)",
    res5.status == "red", res5.status)
chk("multi-workflow: the RED sibling's job is named in failing_jobs",
    any("Analyze" in j for j in res5.failing_jobs), res5.failing_jobs)
chk("multi-workflow: failing job is prefixed with its workflow name",
    any("CodeQL" in j for j in res5.failing_jobs), res5.failing_jobs)
chk("multi-workflow: run_url points at the FAILING run, not the green sibling",
    res5.run_url.endswith("/2"), res5.run_url)
chk("multi-workflow: exactly one follow-up filed", len(bl5.created) == 1, bl5.created)

# (b) green run + still-in-progress sibling -> must NOT be confirmed green.
def gh_multi_pending(args):
    if args[:2] == ["run", "list"]:
        return _runs_json([
            {"databaseId": 3, "status": "completed", "conclusion": "success", "workflowName": "ci",
             "url": "https://github.com/x/y/actions/runs/3", "headSha": "multi2"},
            {"databaseId": 4, "status": "in_progress", "conclusion": None, "workflowName": "deploy",
             "url": "https://github.com/x/y/actions/runs/4", "headSha": "multi2"},
        ])
    raise AssertionError(f"unexpected gh call: {args}")

au6 = Audit()
bl6 = StubBacklog()
res6, note6, _ = land_ci_step(cfg, app(), ticket(id="AUTO-270b", labels=["ci"]), bl6, "multi2", au6,
                              gh_multi_pending, poll_interval_sec=0.01, timeout_sec=0.03)
chk("multi-workflow: in-progress sibling -> not confirmed green",
    res6.status != "green" and not res6.confirmed_success, res6.status)
chk("multi-workflow: in-progress sibling -> reported as timeout (unconfirmed)",
    res6.status == "timeout", res6.status)

# (c) every workflow green -> still green (the happy path must not regress).
def gh_multi_green(args):
    if args[:2] == ["run", "list"]:
        return _runs_json([
            {"databaseId": 5, "status": "completed", "conclusion": "success", "workflowName": "ci",
             "url": "https://github.com/x/y/actions/runs/5", "headSha": "multi3"},
            {"databaseId": 6, "status": "completed", "conclusion": "skipped", "workflowName": "deploy",
             "url": "https://github.com/x/y/actions/runs/6", "headSha": "multi3"},
        ])
    raise AssertionError(f"unexpected gh call: {args}")

au7 = Audit()
bl7 = StubBacklog()
res7, note7, _ = land_ci_step(cfg, app(), ticket(id="AUTO-270c", labels=["ci"]), bl7, "multi3", au7,
                              gh_multi_green, poll_interval_sec=0.01, timeout_sec=0.05)
chk("multi-workflow: all runs green/skipped -> status == 'green'", res7.status == "green", res7.status)
chk("multi-workflow: green path files no follow-up", bl7.created == [], bl7.created)

# A red sibling is terminal: it must be caught WITHOUT waiting for a pending sibling to finish.
def gh_red_plus_pending(args):
    if args[:2] == ["run", "list"]:
        return _runs_json([
            {"databaseId": 8, "status": "completed", "conclusion": "failure", "workflowName": "ci",
             "url": "https://github.com/x/y/actions/runs/8", "headSha": "multi4"},
            {"databaseId": 9, "status": "in_progress", "conclusion": None, "workflowName": "deploy",
             "url": "https://github.com/x/y/actions/runs/9", "headSha": "multi4"},
        ])
    if args[:2] == ["run", "view"]:
        return json.dumps({"jobs": [{"name": "Test", "conclusion": "failure"}]})
    raise AssertionError(f"unexpected gh call: {args}")

res8, _, _ = land_ci_step(cfg, app(), ticket(id="AUTO-270d", labels=["ci"]), StubBacklog(), "multi4",
                          Audit(), gh_red_plus_pending, poll_interval_sec=0.01, timeout_sec=0.05)
chk("multi-workflow: an already-red run is reported red without awaiting a pending sibling",
    res8.status == "red", res8.status)


# --- 7) EU-271: config knobs + fast-bail when `gh` is unavailable ------------------------------ #
import time as _time
from dataclasses import fields as _dc_fields

from orchestrator import config as config_mod

_cfg_fields = {f.name for f in _dc_fields(config_mod.Config)}
chk("config declares a real ci_conclusion_enabled field", "ci_conclusion_enabled" in _cfg_fields)
chk("config declares a real ci_conclusion_timeout_sec field", "ci_conclusion_timeout_sec" in _cfg_fields)
chk("config declares a real ci_conclusion_poll_interval_sec field",
    "ci_conclusion_poll_interval_sec" in _cfg_fields)
# The teeth: Config.load routes YAML through _known_only, which DROPS undeclared keys with a warning
# — so before EU-271 the documented off-switch was silently discarded at load time.
_kept = config_mod._known_only(config_mod.Config, {"ci_conclusion_enabled": False}, where="config")
chk("ci_conclusion_enabled survives _known_only (the off-switch is actually reachable)",
    _kept == {"ci_conclusion_enabled": False}, _kept)
chk("ci_conclusion_enabled=False disables the check via should_run",
    not ci_conclusion.should_run(ns(ci_conclusion_enabled=False), ticket(labels=["ci"])))

# check() must honour the cfg fields with NO explicit kwargs — otherwise the knob is decorative.
_cfg_fast = ns(ci_conclusion_poll_interval_sec=0.01, ci_conclusion_timeout_sec=0.03)
_t0 = _time.perf_counter()
res9 = ci_conclusion.check(_cfg_fast, app(), ticket(labels=["ci"]), "shaCfg", Audit(),
                           gh_runner=gh_timeout)
_elapsed9 = _time.perf_counter() - _t0
chk("cfg timeout/poll are honoured without explicit kwargs (no 300s block)",
    res9.status == "timeout" and _elapsed9 < 1.0, f"{res9.status} in {_elapsed9:.2f}s")

# An explicit kwarg must still win over cfg — the injection contract the rest of this harness uses.
_cfg_slow = ns(ci_conclusion_poll_interval_sec=99.0, ci_conclusion_timeout_sec=99.0)
_t0 = _time.perf_counter()
res10 = ci_conclusion.check(_cfg_slow, app(), ticket(labels=["ci"]), "shaKw", Audit(),
                            gh_runner=gh_timeout, poll_interval_sec=0.01, timeout_sec=0.03)
chk("explicit kwargs still override cfg", res10.status == "timeout"
    and (_time.perf_counter() - _t0) < 1.0)

# gh missing: a binary that isn't on PATH can NEVER appear mid-poll, so burning the whole 300s
# window on 20 identical failures is pure dead time on the synchronous loop.
_missing_calls = []
def gh_missing(args):
    _missing_calls.append(args)
    raise FileNotFoundError(2, "No such file or directory: 'gh'")

_t0 = _time.perf_counter()
res11 = ci_conclusion.check(ns(), app(), ticket(labels=["ci"]), "shaGone", Audit(),
                            gh_runner=gh_missing)  # DEFAULT timeout on purpose: pins the 300s burn
_elapsed11 = _time.perf_counter() - _t0
chk("gh missing: bails after at most one probe (no 20-poll burn)",
    len(_missing_calls) <= 1, len(_missing_calls))
chk("gh missing: returns fast at the DEFAULT timeout", _elapsed11 < 1.0, f"{_elapsed11:.2f}s")
chk("gh missing: reported as unconfirmed, never green",
    res11.status == "timeout" and not res11.confirmed_success, res11.status)
chk("gh missing: raw explains why the check was skipped", "gh" in res11.raw.lower(), res11.raw)

# A transient gh hiccup (RuntimeError) must STILL retry — only a missing binary is terminal.
_flaky_calls = []
def gh_flaky(args):
    _flaky_calls.append(args)
    raise RuntimeError("gh: API rate limit exceeded")

ci_conclusion.check(ns(), app(), ticket(labels=["ci"]), "shaFlaky", Audit(), gh_runner=gh_flaky,
                    poll_interval_sec=0.01, timeout_sec=0.05)
chk("transient gh error still retries (not treated as terminal)", len(_flaky_calls) > 1, len(_flaky_calls))


passed = sum(1 for _, c, _ in results if c)
for n, c, d in results:
    print(f"  {'✓' if c else '✗'} {n}" + (f"  [{d}]" if (not c and d) else ""))
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
