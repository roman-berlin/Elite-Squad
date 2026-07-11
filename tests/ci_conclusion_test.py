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

    def create_task(self, summary, description, labels=None, issue_type="Task"):
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


passed = sum(1 for _, c, _ in results if c)
for n, c, d in results:
    print(f"  {'✓' if c else '✗'} {n}" + (f"  [{d}]" if (not c and d) else ""))
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
