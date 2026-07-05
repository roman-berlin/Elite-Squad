"""Verification gate — runs an app's own tests/lint/typecheck.

Used twice: on the feature branch (before review) and again on dev after a merge
(to keep dev green). Cheap filter: never spend a review cycle on something the
test suite would catch.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .config import AppConfig
from .contracts import GateResult


def run_commands(app: AppConfig, commands: list[str], cwd: str | None = None) -> GateResult:
    """Run a list of shell commands in the app's worktree; fail on the first non-zero exit.
    Shared by the pre-review gate and the post-merge SRE.

    EU-146: Uses process groups to prevent orphaned child processes (e.g. vitest worker forks).
    On Unix, processes are created in a new session via start_new_session=True, ensuring
    all children can be terminated together via os.killpg on timeout."""
    if not commands:
        return GateResult(passed=True, report="(no commands configured)")
    where = cwd or app.workdir or app.repo_path
    failures: list[str] = []
    for cmd in commands:
        proc = None
        try:
            # EU-146: Use Popen with process group support for proper cleanup
            proc = subprocess.Popen(
                cmd, shell=True, cwd=where,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, **app.gate_env},
                start_new_session=True,  # Creates new session/group on Unix; ignored on Windows
            )
            try:
                stdout, stderr = proc.communicate(timeout=app.gate_timeout_sec)
            except subprocess.TimeoutExpired:
                # EU-146: Kill entire process group to prevent orphaned children
                try:
                    os.killpg(os.getpgid(proc.pid), 9)  # SIGKILL
                except (ProcessLookupError, OSError):
                    proc.kill()
                # Reap the zombie and get partial output
                stdout, stderr = proc.communicate()
                failures.append(f"$ {cmd}\n(timed out after {app.gate_timeout_sec}s)")
                continue
        except Exception as exc:
            failures.append(f"$ {cmd}\n({exc!r})")
            continue
        finally:
            # EU-146: Final cleanup to ensure no orphaned processes remain
            if proc and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), 9)
                except (ProcessLookupError, OSError):
                    try:
                        proc.kill()
                    except Exception:
                        pass
                try:
                    proc.communicate(timeout=1)
                except Exception:
                    pass
        if proc.returncode != 0:
            tail = (stdout.decode("utf-8", errors="replace") + "\n" + stderr.decode("utf-8", errors="replace")).strip()[-4000:]
            failures.append(f"$ {cmd}\n(exit {proc.returncode})\n{tail}")
    if failures:
        return GateResult(passed=False, report="\n\n".join(failures))
    return GateResult(passed=True, report="all commands passed")


def gate_interpreter(commands: list[str] | None) -> str | None:
    """The python interpreter a gate will use: the leading token of the first command that
    points at a python executable (e.g. an absolute `.venv/bin/python`). None when the gate
    isn't python-invoked (e.g. a Bun `tsc` typecheck). (EU-54)"""
    for cmd in commands or []:
        toks = (cmd or "").strip().split()
        if toks and os.path.basename(toks[0]).startswith("python"):
            return toks[0]
    return None


def preflight_imports(app: AppConfig, commands: list[str] | None = None) -> GateResult | None:
    """EU-54 health check. Before running the suite, confirm the gate's python interpreter can
    import every module in ``app.gate_preflight``. Returns a FAILED GateResult (with an actionable
    venv hint) when one is missing; None when there's nothing to check or all imports resolve.

    Guards the unit's most fragile path — an EU self-build whose bare ``python3`` gate inherits a
    PATH without the project virtualenv and dies on ``import requests``, turning the gate red for a
    reason unrelated to the ticket and burning retries until it parks."""
    mods = list(getattr(app, "gate_preflight", None) or [])
    if not mods:
        return None
    interp = gate_interpreter(commands if commands is not None else app.gate_commands) or sys.executable
    try:
        proc = subprocess.run(
            [interp, "-c", "import " + ", ".join(mods)],
            capture_output=True, text=True, timeout=min(app.gate_timeout_sec, 120),
            env={**os.environ, **app.gate_env},
        )
    except Exception as exc:  # noqa: BLE001 - a broken/missing interpreter IS the finding
        return GateResult(passed=False, report=f"gate health check: cannot run interpreter '{interp}': {exc}")
    if proc.returncode != 0:
        miss = (proc.stderr.strip().splitlines() or ["import failed"])[-1]
        return GateResult(passed=False, report=(
            f"gate health check FAILED — interpreter '{interp}' cannot import required modules "
            f"({', '.join(mods)}).\n{miss}\n"
            "The gate is not running under the project virtualenv. Pin gate_commands to the venv "
            "interpreter (an absolute .venv/bin/python path) or launch autopilot with the venv on PATH."))
    return None


def touched_components(changed_paths: list[str]) -> list[str]:
    """Component (app/package) directory names a diff touches, in first-seen order.
    A path like 'apps/landing-page/src/x.ts' -> 'landing-page'; 'packages/ui/y.ts' -> 'ui'.
    Paths outside apps/ and packages/ are ignored. (EU-19)"""
    seen: list[str] = []
    for p in changed_paths:
        parts = p.replace("\\", "/").split("/")
        if len(parts) >= 2 and parts[0] in ("apps", "packages") and parts[1]:
            if parts[1] not in seen:
                seen.append(parts[1])
    return seen


def select_gate_groups(app: AppConfig, changed_paths: list[str]) -> list[tuple[str, list[str]]]:
    """Pick the per-app gate command groups for the components a diff touched.

    Returns a list of (component_name, commands). An empty list means detection was
    AMBIGUOUS — the caller falls back to the repo-wide `gate_commands` default. (EU-19)

      • No per-app config, or no changed paths under apps/ or packages/  -> [] (fall back).
      • A changed package listed in gate_shared_packages also pulls in the apps that
        depend on it, so a shared dependency re-gates its consumers.
      • Only touched components that HAVE a per-app entry contribute a group; if none of
        the touched components are configured -> [] (fall back, don't silently skip).
    """
    by_app = app.gate_commands_by_app or {}
    if not by_app or not changed_paths:
        return []
    touched = touched_components(changed_paths)
    if not touched:
        return []
    # Expand shared packages to the apps that depend on them.
    shared = app.gate_shared_packages or {}
    selected: list[str] = list(touched)
    for name in touched:
        for dep in shared.get(name, []):
            if dep not in selected:
                selected.append(dep)
    groups = [(name, by_app[name]) for name in selected if by_app.get(name)]
    return groups


def run_gate(app: AppConfig, changed_paths: list[str] | None = None) -> GateResult:
    """Run the verification gate. When per-app gate commands are configured and the diff's
    changed paths map to one or more configured components, run ONLY those components' gates
    (naming each in the failure report). Otherwise fall back to the repo-wide `gate_commands`
    (the original single-command behaviour). (EU-19)

    Args:
        app: App configuration (gate commands, env, timeout, etc.).
        changed_paths: Paths modified by the diff; used to select per-app gate groups.

    Note (EU-85): the gate no longer carries a domain-gap note. Domain-gap classification
    lives in ONE place — the squad delegation path (``squad._plan`` → ``detect_domain_gap``),
    where it actually routes provisioning — so the gate doesn't re-run the classifier just to
    attach an advisory line (which was inaccurate whenever delegation was off or the ticket was
    too small to delegate).
    """
    # EU-54: fail fast and clearly if the gate interpreter can't even import its deps, before we
    # spend the whole suite producing a confusing mid-run ModuleNotFoundError.
    pf = preflight_imports(app)
    if pf is not None:
        return pf
    groups = select_gate_groups(app, changed_paths or [])
    if not groups:
        if not app.gate_commands:
            return GateResult(passed=True, report="(no gate commands configured)")
        return run_commands(app, app.gate_commands)

    failures: list[str] = []
    passed_apps: list[str] = []
    for name, commands in groups:
        res = run_commands(app, commands)
        if res.passed:
            passed_apps.append(name)
        else:
            # Name the app that failed so the audit report points at the right component.
            failures.append(f"[{name}] gate FAILED\n{res.report}")
    if failures:
        return GateResult(passed=False, report="\n\n".join(failures))
    return GateResult(passed=True, report="app gates passed: " + ", ".join(passed_apps))


# --------------------------------------------------------------------------- #
# Phase-2 §3 — deterministic gates (2026-07-05 restructure, Commander-approved 2026-07-06).
# Everything here is a script, not an LLM: failures feed the Builder as plain text (a free
# review round) and no LLM reviewer runs until they are green.
# --------------------------------------------------------------------------- #

# Lines that carry the SIGNAL of a failure (which harness/command failed, which error class) —
# everything else in a gate report is noise for identity purposes.
_FAIL_LINE = re.compile(r"(?i)(\bFAILED\b|\bFAILURES?\b|✗|✘|\bERRORS?\b|Traceback|exit \d+|"
                        r"AssertionError|\bFAIL\b)")
# Volatile fragments that differ between two runs of the SAME failure: durations, hex addresses,
# tmp paths, line numbers.
_NOISE = re.compile(r"\b\d+(\.\d+)?s\b|\b0x[0-9a-f]+\b|/(?:tmp|var|private)/\S+|:\d+\b")


def gate_fingerprint(report: str) -> str:
    """Stable identity of WHAT failed in a gate report — the EU-174 lever: the same red base
    produced byte-different reports every pass (timings, tmp paths), so nothing could see that
    4 max-effort builds were fighting one unchanged failure. Extracts only failure-signal lines,
    strips volatile fragments, order-independent. '' when the report carries no failure signal
    (callers must treat '' as non-comparable, never as 'identical')."""
    lines: set[str] = set()
    for ln in (report or "").splitlines():
        if _FAIL_LINE.search(ln):
            lines.add(_NOISE.sub("", " ".join(ln.lower().split()))[:200])
    if not lines:
        return ""
    sig = "|".join(sorted(lines))[:8000]
    return hashlib.sha1(sig.encode("utf-8")).hexdigest()[:16]


# Deterministic secret patterns over ADDED diff lines — replaces the secrets half of the
# per-diff Opus provost-gate call (161 calls / 8.0M tokens on the audited corpus). Matches are
# MASKED in the report (provost doctrine: never print a real secret).
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("secret key (sk-…)", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("assigned secret literal",
     re.compile(r"(?i)\b(api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*[\"'][^\"'\s]{16,}[\"']")),
]


def scan_diff_for_secrets(diff: str) -> list[str]:
    """Scan a unified diff's ADDED lines for credential patterns. Returns masked findings
    ('label: sk-abc…wxyz'); an empty list means clean. Deterministic and cheap — runs on every
    pass before any LLM sees the diff."""
    hits: list[str] = []
    for ln in (diff or "").splitlines():
        if not ln.startswith("+") or ln.startswith("+++"):
            continue
        for label, pat in _SECRET_PATTERNS:
            m = pat.search(ln)
            if m:
                tok = m.group(0)
                masked = (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else "…masked…"
                hits.append(f"{label}: {masked}")
    return hits


# manifest filename -> lockfile siblings that must move with it (only pairs with a real
# lockfile convention; requirements.txt has none, so it is deliberately absent).
_LOCKFILE_PAIRS: list[tuple[str, tuple[str, ...]]] = [
    ("package.json", ("bun.lock", "bun.lockb", "package-lock.json", "pnpm-lock.yaml", "yarn.lock")),
    ("pyproject.toml", ("uv.lock", "poetry.lock")),
    ("Cargo.toml", ("Cargo.lock",)),
    ("Gemfile", ("Gemfile.lock",)),
]


def lockfile_sanity(changed_paths: list[str], repo_root: str) -> list[str]:
    """Detect manifest/lockfile drift in a diff: a dependency manifest changed while its sibling
    lockfile — which EXISTS in the repo — did not. Returns human-readable problems ([] = clean).
    Repos without a lockfile for that manifest are never flagged (nothing to drift against)."""
    problems: list[str] = []
    changed = {p.replace("\\", "/") for p in (changed_paths or [])}
    for path in sorted(changed):
        d, base = os.path.split(path)
        for manifest, locks in _LOCKFILE_PAIRS:
            if base != manifest:
                continue
            siblings = [(os.path.join(d, lk) if d else lk) for lk in locks]
            existing = [s for s in siblings if os.path.exists(os.path.join(repo_root, s))]
            if existing and not any(s in changed for s in existing):
                problems.append(f"{path} changed but {existing[0]} did not — regenerate the lockfile "
                                "in the same commit")
    return problems


def run_deterministic_checks(app: AppConfig, changed_paths: list[str], diff: str) -> GateResult:
    """§3 items 3–5, in order: lint → secret scan → lockfile sanity. Runs AFTER the test gate is
    green and BEFORE any LLM reviewer; a failure feeds the Builder as plain text. All three are
    cheap relative to one review round, and none can hallucinate."""
    reports: list[str] = []
    if getattr(app, "lint_commands", None):
        lint = run_commands(app, app.lint_commands)
        if not lint.passed:
            reports.append("LINT gate failed:\n" + lint.report)
    hits = scan_diff_for_secrets(diff or "")
    if hits:
        reports.append("SECRET-LEAK gate failed — remove these from the diff (values masked):\n"
                       + "\n".join(f"  · {h}" for h in hits))
    problems = lockfile_sanity(changed_paths or [], app.workdir or app.repo_path)
    if problems:
        reports.append("LOCKFILE gate failed:\n" + "\n".join(f"  · {p}" for p in problems))
    if reports:
        return GateResult(passed=False, report="\n\n".join(reports))
    return GateResult(passed=True, report="deterministic checks passed")


_RED_BASE_CACHE_MAX = 40   # (repo@sha) entries kept; content-addressed so entries never go stale


def base_gate_check(app: AppConfig, cfg, git, runner=None) -> tuple[bool, str, str]:
    """§3 item 1 — the EU-174 killer. Run the verification gate against the CLEAN base tree
    (call BEFORE the first build pass, while the worktree still equals base). Returns
    (passed, fingerprint, report).

    Results are cached per (repo, base-sha) beside the audit log (locked RMW — many worktrees
    can hit this concurrently), so one broken base blocks every queued ticket at the cost of ONE
    suite run, and a green base is re-proven once per base commit, not once per ticket. The
    cache is content-addressed by sha — no TTL needed; a flaky false-red for a sha clears when
    the base moves, or delete red_base_cache.json / set red_base_check=false to override.

    ``runner`` lets loop.py pass ITS run_gate symbol so existing harness monkeypatching keeps
    working (tests stub loop.run_gate, and the base check must honor that stub)."""
    run = runner or run_gate
    sha = ""
    try:
        sha = (getattr(git, "current_sha", lambda: "")() or "").strip()
    except Exception:  # noqa: BLE001 — a stub git without sha support just skips the cache
        sha = ""
    cache_path = Path(cfg.audit_path).with_name("red_base_cache.json")
    key = f"{app.repo_path}@{sha}"

    if sha:
        try:
            import json
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            hit = data.get(key)
            if isinstance(hit, dict) and "passed" in hit:
                return bool(hit["passed"]), str(hit.get("fp", "")), str(hit.get("report", ""))
        except Exception:  # noqa: BLE001 — a missing/corrupt cache just re-runs the gate
            pass

    res = run(app, [])
    fp = "" if res.passed else gate_fingerprint(res.report or "")
    report = "" if res.passed else (res.report or "")[:4000]

    if sha:
        try:
            from . import locking

            def _put(data):
                data = data if isinstance(data, dict) else {}
                data[key] = {"passed": res.passed, "fp": fp, "report": report, "ts": time.time()}
                if len(data) > _RED_BASE_CACHE_MAX:
                    for old in sorted(data, key=lambda k: data[k].get("ts", 0))[:len(data) - _RED_BASE_CACHE_MAX]:
                        data.pop(old, None)
                return data
            locking.locked_rmw(cache_path, _put, default={}, corrupt_to_default=True)
        except Exception:  # noqa: BLE001 — cache write failure must never block the pipeline
            pass
    return res.passed, fp, report


# --------------------------------------------------------------------------- #
# Pre-build gate integration (EU-107)
# --------------------------------------------------------------------------- #

async def prebuild_gate(cfg, worklist, audit):
    """Run the pre-build triage gate (Senior PM) before tickets reach the Builder.

    This gate filters tickets that can be resolved without a build:
    - ANSWER: questions answerable from docs/context → reply and close
    - CLOSE: invalid/duplicate tickets → close with reason
    - REFILE: misrouted tickets → re-file as new tickets and close original
    - CONTINUE: tickets that need a build → pass through to Builder

    The gate also checks worktree locks to skip in-flight tickets.

    Args:
        cfg: Config object
        worklist: List of (AppConfig, Ticket) tuples
        audit: AuditLog object

    Returns:
        Filtered worklist with only tickets that need a build.
    """
    # EU-134: Check if gate is enabled; if not, pass all tickets through
    if not cfg.prebuild_gate_enabled:
        print("  · pre-build gate: disabled (prebuild_gate_enabled=False) — passing all tickets to Builder", flush=True)
        return worklist
    from . import loop, senior_pm
    from .backlog.base import make_backlog

    filtered_worklist = []
    triage_results = []

    for app, ticket in worklist:
        ticket_id = ticket.id or ticket.key or "unknown"

        # Check if worktree is locked (in-flight ticket)
        worktree_path = loop._worktree_path(app, cfg)
        if loop.is_worktree_locked(worktree_path):
            print(f"  · {ticket_id}: skipping — worktree locked (build in progress)", flush=True)
            audit.record("prebuild_skip", ticket_id=ticket_id, reason="worktree_locked")
            continue

        # Run Senior PM triage
        print(f"  · {ticket_id}: running pre-build triage...", flush=True)
        try:
            pm_audit = await senior_pm.triage_async(cfg, ticket, auto_mode=cfg.auto_mode)
            verdict = pm_audit.verdict

            # Record the triage decision
            audit.record("prebuild_triage",
                        ticket_id=ticket_id,
                        verdict=verdict,
                        citations=pm_audit.citations,
                        raw=pm_audit.raw)

            triage_results.append((ticket_id, verdict))

            # EU-134: Conservative post-triage check — override to CONTINUE for tickets that need a build
            # Tickets with acceptance criteria ALWAYS continue (never ANSWER/CLOSE/REFILE them).
            # [Feature] or [Bug] labeled tickets ALWAYS continue (they require implementation).
            if ticket.acceptance_criteria or any(label in ["Feature", "Bug"] for label in (ticket.labels or [])):
                if verdict != "CONTINUE":
                    print(f"  · {ticket_id}: overriding {verdict} → CONTINUE (ticket has AC or [Feature]/[Bug] tag)", flush=True)
                    audit.record("prebuild_override", ticket_id=ticket_id, from_verdict=verdict, to_verdict="CONTINUE",
                               reason="conservative: ticket with AC or Feature/Bug tag always continues")
                    verdict = "CONTINUE"

            # Act on the verdict
            backlog = make_backlog(app)

            if verdict == "ANSWER":
                # Senior PM answered the question → close ticket (if auto_mode is on, else Needs-you)
                if cfg.auto_mode:
                    print(f"  · {ticket_id}: ANSWER → closing with answer", flush=True)
                    backlog.add_comment(ticket, pm_audit.raw)
                    backlog.set_status(ticket, "Done")
                    audit.record("prebuild_close", ticket_id=ticket_id, reason="answered")
                else:
                    print(f"  · {ticket_id}: ANSWER → Needs-you (auto_mode off)", flush=True)
                    backlog.add_comment(ticket, pm_audit.raw)
                    backlog.set_status(ticket, "Needs you")
                    audit.record("prebuild_needs_you", ticket_id=ticket_id, reason="answered_but_auto_mode_off")

            elif verdict == "CLOSE":
                # Invalid/duplicate → close with reason (if auto_mode is on, else Needs-you)
                if cfg.auto_mode:
                    print(f"  · {ticket_id}: CLOSE → closing", flush=True)
                    backlog.add_comment(ticket, pm_audit.raw)
                    backlog.set_status(ticket, "Done")
                    audit.record("prebuild_close", ticket_id=ticket_id, reason="invalid")
                else:
                    print(f"  · {ticket_id}: CLOSE → Needs-you (auto_mode off)", flush=True)
                    backlog.add_comment(ticket, pm_audit.raw)
                    backlog.set_status(ticket, "Needs you")
                    audit.record("prebuild_needs_you", ticket_id=ticket_id, reason="close_but_auto_mode_off")

            elif verdict == "REFILE":
                # Misrouted → re-file as new tickets and close original (if auto_mode is on, else Needs-you)
                if cfg.auto_mode:
                    print(f"  · {ticket_id}: REFILE → creating {len(pm_audit.refile_targets)} new ticket(s)", flush=True)
                    backlog.add_comment(ticket, pm_audit.raw)

                    # Create the new tickets
                    filed_keys = []
                    for new_ticket in pm_audit.refile_targets:
                        try:
                            new_key = backlog.create_task(
                                summary=new_ticket.get("title", ""),
                                description=new_ticket.get("body", ""),
                                labels=["autodev", "refiled"],
                                issue_type=new_ticket.get("type", "Task")
                            )
                            if new_key:
                                filed_keys.append(new_key)
                                audit.record("prebuild_refile",
                                           ticket_id=ticket_id,
                                           new_ticket_key=new_key,
                                           title=new_ticket.get("title", ""))
                        except Exception as e:
                            print(f"  · {ticket_id}: failed to create refile ticket: {e}", flush=True)
                            audit.record("prebuild_refile_failed",
                                       ticket_id=ticket_id,
                                       error=str(e))

                    # Close the original ticket with reference to new tickets
                    comment = pm_audit.raw
                    if filed_keys:
                        comment += f"\n\nRefiled as: {', '.join(filed_keys)}"
                    backlog.add_comment(ticket, comment)
                    backlog.set_status(ticket, "Done")
                else:
                    print(f"  · {ticket_id}: REFILE → Needs-you (auto_mode off)", flush=True)
                    backlog.add_comment(ticket, pm_audit.raw)
                    backlog.set_status(ticket, "Needs you")
                    audit.record("prebuild_needs_you", ticket_id=ticket_id, reason="refile_but_auto_mode_off")

            else:
                # CONTINUE or unknown → pass through to Builder
                print(f"  · {ticket_id}: {verdict} → continuing to build", flush=True)
                filtered_worklist.append((app, ticket))

        except Exception as e:
            print(f"  · {ticket_id}: triage failed → continuing to build: {e}", flush=True)
            audit.record("prebuild_error", ticket_id=ticket_id, error=str(e))
            # On error, pass through to build rather than dropping the ticket
            filtered_worklist.append((app, ticket))

    # Log summary
    if triage_results:
        answered = sum(1 for _, v in triage_results if v == "ANSWER")
        closed = sum(1 for _, v in triage_results if v == "CLOSE")
        refiled = sum(1 for _, v in triage_results if v == "REFILE")
        continued = sum(1 for _, v in triage_results if v not in ("ANSWER", "CLOSE", "REFILE"))
        print(f"  · pre-build gate: {answered} answered, {closed} closed, {refiled} refiled, {continued} continuing", flush=True)

    return filtered_worklist
