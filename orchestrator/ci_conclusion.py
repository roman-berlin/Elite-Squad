"""CI-conclusion check — the land path's remote-CI reality check (EU-251).

Every existing post-merge check in the land path is LOCAL: the SRE's ``postmerge_commands``
(sentinel.py) and the smoke runner's single ``smoke_command`` (smoke.py) both re-run tests on the
Mac worktree, and the Builder's own "CI will go green" claim is never observed, just repeated.
AUTO-112 landed on exactly that gap: the commit that shipped claimed "CI will go green immediately"
while the real GitHub Actions run for that merge commit concluded FAILURE — and nothing in
``orchestrator/`` ever consulted GitHub Actions to catch it.

This module is that consultation. For CI-relevant tickets only (see ``should_run``) the land path
shells out to ``gh`` right after a live land, polls (bounded) the run for the merge commit until it
reaches a terminal conclusion, and reports the REAL per-job outcome — never the Builder's self-report.
``report()`` then posts that reality to the ticket: on green, the real conclusion; on red, an explicit
"AC not met" flag (never phrased as success) plus a de-duped residual-failure follow-up so a repeat
run doesn't double-file; on timeout, a "not confirmed" note — the land itself is never unwound.

Deliberately distinct from the SRE / smoke local re-run gates (AUTO-57/83): a local re-run cannot
catch a CI-environment-only failure (missing secret, a checkout/cleanup step, runner-only env) — the
exact failure class AUTO-112 was filed for. This reads the ACTUAL remote run.

Best-effort, like sentinel/smoke: never raises into the loop, and a non-CI ticket makes ZERO ``gh``
calls (``should_run`` gates everything below it).
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .config import AppConfig, Config
from .contracts import Ticket

# "CI" as a standalone token (word-bounded so it doesn't fire on ordinary prose), or an explicit
# mention of GitHub Actions / gh actions / workflow status / the workflows directory.
_CI_KEYWORDS = re.compile(
    r"\bCI\b|github\s*actions|gh\s*actions|workflow\s*status|\.github/workflows",
    re.IGNORECASE,
)

_POLL_INTERVAL_SEC = 15.0
_DEFAULT_TIMEOUT_SEC = 300.0

GhRunner = Callable[[list], str]


@dataclass
class CIConclusion:
    """The result of polling GitHub Actions for one merge commit."""
    status: str                                    # "green" | "red" | "timeout"
    conclusion: str = ""                           # gh's raw overall conclusion ("success", "failure", ...)
    failing_jobs: list = field(default_factory=list)
    run_url: str = ""
    raw: str = ""                                   # last raw payload/error, kept for the audit trail

    @property
    def confirmed_success(self) -> bool:
        return self.status == "green"


def should_run(cfg: Config, ticket: Ticket) -> bool:
    """True only for a CI-relevant ticket — labelled 'ci', or its summary/description/acceptance
    criteria mention CI / GitHub Actions / workflow status. False for an ordinary ticket, and when
    False the caller MUST NOT call check() — zero `gh` subprocesses for a non-CI ticket."""
    if not bool(getattr(cfg, "ci_conclusion_enabled", True)):
        return False
    labels = [str(lab).strip().lower() for lab in (getattr(ticket, "labels", None) or [])]
    if "ci" in labels:
        return True
    haystack = " ".join(filter(None, [
        getattr(ticket, "summary", "") or "",
        getattr(ticket, "description", "") or "",
        " ".join(getattr(ticket, "acceptance_criteria", None) or []),
    ]))
    return bool(_CI_KEYWORDS.search(haystack))


def _real_gh_runner(app: AppConfig) -> GhRunner:
    cwd = app.workdir or app.repo_path

    def _run(args: list) -> str:
        proc = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or f"gh {' '.join(args)} failed").strip()[:500])
        return proc.stdout

    return _run


def _list_runs(runner: GhRunner, sha: str) -> list:
    out = runner(["run", "list", "--commit", sha, "--json",
                  "databaseId,status,conclusion,url,headSha", "--limit", "10"])
    data = json.loads(out) if out and out.strip() else []
    return data if isinstance(data, list) else []


def _failing_jobs(runner: GhRunner, run_id) -> list:
    if not run_id:
        return []
    out = runner(["run", "view", str(run_id), "--json", "jobs"])
    data = json.loads(out) if out and out.strip() else {}
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    return [j.get("name", "?") for j in jobs
            if (j.get("conclusion") or "").lower() not in ("success", "skipped", "")]


def _summarize(runner: GhRunner, terminal_runs: list) -> CIConclusion:
    run = terminal_runs[0]  # `gh run list --commit` is newest-first; this is the merge commit's run
    conclusion = (run.get("conclusion") or "").lower()
    url = run.get("url", "") or ""
    if conclusion == "success":
        return CIConclusion(status="green", conclusion=conclusion, run_url=url, raw=json.dumps(run))
    try:
        failing = _failing_jobs(runner, run.get("databaseId"))
    except Exception as exc:  # noqa: BLE001 — job detail is best-effort; the conclusion itself still stands
        failing = [f"(could not list failing jobs: {exc})"]
    return CIConclusion(status="red", conclusion=conclusion or "unknown",
                        failing_jobs=failing, run_url=url, raw=json.dumps(run))


def check(cfg: Config, app: AppConfig, ticket: Ticket, merge_sha: str, audit=None, *,
          gh_runner: Optional[GhRunner] = None,
          poll_interval_sec: Optional[float] = None,
          timeout_sec: Optional[float] = None) -> CIConclusion:
    """Poll GitHub Actions for the merge commit's run until it reaches a terminal conclusion or the
    bounded wait expires. Green/red are read from the REAL run; a timeout returns status='timeout'
    (not success, not failure — unconfirmed). Never raises into the loop: a `gh` hiccup (including the
    binary being missing) keeps polling until the timeout, then reports as 'timeout'."""
    runner = gh_runner or _real_gh_runner(app)
    interval = max(0.01, _POLL_INTERVAL_SEC if poll_interval_sec is None else poll_interval_sec)
    timeout = max(0.0, _DEFAULT_TIMEOUT_SEC if timeout_sec is None else timeout_sec)
    tid = getattr(ticket, "id", "?")
    print(f"  🔎 CI · polling GH Actions for {app.name}@{merge_sha[:12]} (bounded {timeout:.0f}s)…",
          flush=True)

    result = CIConclusion(status="timeout",
                          raw=f"no terminal GH Actions run for {merge_sha[:12]} within {timeout:.0f}s")
    elapsed = 0.0
    while True:
        try:
            runs = _list_runs(runner, merge_sha)
            terminal = [r for r in runs if (r.get("status") or "").lower() == "completed"]
        except Exception as exc:  # noqa: BLE001 — a gh hiccup keeps polling till timeout, never raises
            terminal = []
            result.raw = f"gh error: {exc}"
        if terminal:
            result = _summarize(runner, terminal)
            break
        elapsed += interval
        if elapsed >= timeout:
            break
        time.sleep(interval)

    print(f"  🔎 CI · {app.name}@{merge_sha[:12]} → {result.status}"
          + (f" ({result.conclusion})" if result.conclusion else ""), flush=True)
    if audit is not None:
        audit.record("ci_conclusion", ticket_id=tid, app=app.name, sha=merge_sha,
                     status=result.status, conclusion=result.conclusion,
                     failing_jobs=result.failing_jobs)
    return result


def _file_followup(app: AppConfig, ticket: Ticket, result: CIConclusion):
    """De-duped residual-failure follow-up, reusing filing.file_findings' existing de-dup
    (find_open_by_summary) so a repeat run against the same still-red commit does not re-file."""
    from . import filing
    tid = getattr(ticket, "id", "?")
    jobs = ", ".join(result.failing_jobs) if result.failing_jobs else "unknown job(s)"
    title = f"{tid}: CI still red on {app.base_branch} after merge (failing: {jobs})"
    body = (f"{tid} landed on {app.base_branch} but GitHub Actions concluded "
            f"'{result.conclusion or 'failure'}' for that merge commit. Failing jobs: {jobs}. "
            f"{result.run_url}\n\n"
            f"AC not met — {tid}'s promised 'CI will go green' outcome is false. Fix the failing "
            f"job(s) before the ticket is considered done.")
    synthetic_report = ("===TICKETS===\n"
                        + json.dumps([{"title": title, "type": "Bug", "severity": "HIGH", "body": body}])
                        + "\n===END===")
    return filing.file_findings(app, "ci-conclusion", synthetic_report)


def report(cfg: Config, app: AppConfig, ticket: Ticket, backlog, result: CIConclusion,
          audit=None) -> str:
    """Post the REAL CI conclusion to the ticket — never phrase a red run as success — and, when
    red, file the de-duped follow-up. Best-effort: a Jira/filing hiccup here must never unwind an
    already-successful land. Returns a short suffix for the land note ('' when green)."""
    tid = getattr(ticket, "id", "?")
    note = ""
    if result.status == "green":
        comment = (f"✅ CI conclusion confirmed: {result.conclusion or 'success'} for the merge "
                   f"commit.{f' {result.run_url}' if result.run_url else ''}")
    elif result.status == "red":
        jobs = ", ".join(result.failing_jobs) if result.failing_jobs else "(job names unavailable)"
        comment = (f"⚠️ AC not met: CI still red (failing jobs: {jobs})."
                   f"{f' {result.run_url}' if result.run_url else ''}")
        note = " · ⚠️ CI RED (AC not met)"
    else:  # timeout
        comment = ("⏱️ CI status not confirmed within the poll window — GitHub Actions had not "
                   "reached a final result for the merge commit. Land stands (not unwound); "
                   f"re-check {app.base_branch} manually.")
        note = " · ⚠️ CI status not confirmed"

    if backlog is not None and not getattr(ticket, "ephemeral", False):
        try:
            backlog.add_comment(ticket, comment)
        except Exception as exc:  # noqa: BLE001 — a Jira hiccup must never unwind a successful land
            print(f"  🔎 CI · ticket comment skipped ({exc})", flush=True)

    if result.status == "red":
        try:
            _file_followup(app, ticket, result)
        except Exception as exc:  # noqa: BLE001 — filing is best-effort
            print(f"  🔎 CI · follow-up filing skipped ({exc})", flush=True)

    if audit is not None:
        audit.record("ci_conclusion_reported", ticket_id=tid, app=app.name, status=result.status)
    return note
