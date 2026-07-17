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
import shutil
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

# Module fallbacks. Real deployments tune these through the Config fields of the same name (EU-271);
# an explicit kwarg to check() still wins over both, which is how the harness injects fast values.
_POLL_INTERVAL_SEC = 15.0
_DEFAULT_TIMEOUT_SEC = 300.0

# gh conclusions that are NOT a failure. "neutral"/"skipped" are how a path-filtered or conditional
# workflow reports "nothing to do" — treating them as red would make every land red on such repos.
_OK_CONCLUSIONS = ("success", "skipped", "neutral")

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


def _resolve_sec(explicit: Optional[float], cfg, field_name: str, fallback: float,
                 *, floor: float = 0.0) -> float:
    """Resolve a timing knob: explicit kwarg > cfg field > module default (EU-271). Reading cfg HERE
    rather than at the call site means the knob binds for every caller — the land path passes cfg
    through and gets the configured window without having to remember two more kwargs."""
    value = explicit
    if value is None:
        value = getattr(cfg, field_name, None)
    if value is None:
        value = fallback
    try:
        return max(floor, float(value))
    except (TypeError, ValueError):  # a garbled config value must not break a land
        return fallback


def _real_gh_runner(app: AppConfig) -> GhRunner:
    cwd = app.workdir or app.repo_path

    def _run(args: list) -> str:
        proc = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or f"gh {' '.join(args)} failed").strip()[:500])
        return proc.stdout

    return _run


def _list_runs(runner: GhRunner, sha: str) -> list:
    # workflowName (EU-270) so a failing job can be attributed to the workflow it came from —
    # "CodeQL / Analyze" vs a bare "Analyze" that could belong to any of the commit's runs.
    out = runner(["run", "list", "--commit", sha, "--json",
                  "databaseId,status,conclusion,url,headSha,workflowName", "--limit", "10"])
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
    """Fold EVERY completed run for the commit into one verdict. This used to read terminal_runs[0]
    under the comment "this is the merge commit's run" — a false premise (EU-270): `gh run list
    --commit` returns one run PER WORKFLOW FILE, so on any repo with a second push-triggered workflow
    (CodeQL/deploy beside ci.yml) the newest run was reported while a sibling was red — reintroducing
    the exact false-green EU-251 exists to prevent. Green only when EVERY run is green."""
    failed = [r for r in terminal_runs
              if (r.get("conclusion") or "").lower() not in _OK_CONCLUSIONS]
    if not failed:
        run = terminal_runs[0]
        return CIConclusion(status="green", conclusion=(run.get("conclusion") or "").lower(),
                            run_url=run.get("url", "") or "", raw=json.dumps(terminal_runs))
    failing: list = []
    for run in failed:
        wf = (run.get("workflowName") or "").strip()
        try:
            names = _failing_jobs(runner, run.get("databaseId"))
        except Exception as exc:  # noqa: BLE001 — job detail is best-effort; the conclusion still stands
            names = [f"(could not list failing jobs: {exc})"]
        failing.extend(f"{wf} / {n}" if wf else n for n in names)
    first = failed[0]
    return CIConclusion(status="red", conclusion=(first.get("conclusion") or "").lower() or "unknown",
                        failing_jobs=failing,
                        # the FAILING run's url — pointing the ticket at a green sibling would bury it
                        run_url=first.get("url", "") or "", raw=json.dumps(terminal_runs))


def check(cfg: Config, app: AppConfig, ticket: Ticket, merge_sha: str, audit=None, *,
          gh_runner: Optional[GhRunner] = None,
          poll_interval_sec: Optional[float] = None,
          timeout_sec: Optional[float] = None) -> CIConclusion:
    """Poll GitHub Actions for the merge commit until EVERY workflow run reaches a terminal
    conclusion or the bounded wait expires. Green/red are read from the REAL runs; a timeout returns
    status='timeout' (not success, not failure — unconfirmed). Never raises into the loop.

    Timing resolves explicit kwarg > cfg (ci_conclusion_{timeout,poll_interval}_sec) > module default,
    so the knob works even for a caller that just passes cfg through (EU-271)."""
    runner = gh_runner or _real_gh_runner(app)
    interval = _resolve_sec(poll_interval_sec, cfg, "ci_conclusion_poll_interval_sec",
                            _POLL_INTERVAL_SEC, floor=0.01)
    timeout = _resolve_sec(timeout_sec, cfg, "ci_conclusion_timeout_sec", _DEFAULT_TIMEOUT_SEC)
    tid = getattr(ticket, "id", "?")

    def _finish(res: CIConclusion) -> CIConclusion:
        print(f"  🔎 CI · {app.name}@{merge_sha[:12]} → {res.status}"
              + (f" ({res.conclusion})" if res.conclusion else ""), flush=True)
        if audit is not None:
            audit.record("ci_conclusion", ticket_id=tid, app=app.name, sha=merge_sha,
                         status=res.status, conclusion=res.conclusion, failing_jobs=res.failing_jobs)
        return res

    # EU-271: a `gh` that isn't installed cannot appear mid-poll, so the pre-fix behaviour — 20
    # identical failures over the full 300s window — was pure dead time on the SYNCHRONOUS loop.
    # Bail on the first probe instead. Still 'timeout' (unconfirmed), never green: fail-closed.
    if gh_runner is None and shutil.which("gh") is None:
        return _finish(CIConclusion(status="timeout",
                                    raw="gh not found on PATH — CI conclusion unchecked"))

    print(f"  🔎 CI · polling GH Actions for {app.name}@{merge_sha[:12]} (bounded {timeout:.0f}s)…",
          flush=True)
    result = CIConclusion(status="timeout",
                          raw=f"no terminal GH Actions run for {merge_sha[:12]} within {timeout:.0f}s")
    elapsed = 0.0
    while True:
        try:
            runs = _list_runs(runner, merge_sha)
        except FileNotFoundError as exc:  # EU-271: missing binary — terminal, never retry
            result.raw = f"gh not found: {exc}"
            break
        except Exception as exc:  # noqa: BLE001 — a transient gh hiccup keeps polling, never raises
            runs = []
            result.raw = f"gh error: {exc}"
        terminal = [r for r in runs if (r.get("status") or "").lower() == "completed"]
        # EU-270: a red run is terminal on its own — report it without waiting for a slow sibling.
        # Otherwise only a commit whose runs have ALL completed can be called green; a still-pending
        # sibling keeps us polling (and, on expiry, reports 'timeout' — never a green).
        if any((r.get("conclusion") or "").lower() not in _OK_CONCLUSIONS for r in terminal):
            result = _summarize(runner, terminal)
            break
        if runs and len(terminal) == len(runs):
            result = _summarize(runner, terminal)
            break
        elapsed += interval
        if elapsed >= timeout:
            break
        time.sleep(interval)

    return _finish(result)


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
