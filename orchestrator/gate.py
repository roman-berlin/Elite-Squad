"""Verification gate — runs an app's own tests/lint/typecheck.

Used twice: on the feature branch (before review) and again on dev after a merge
(to keep dev green). Cheap filter: never spend a review cycle on something the
test suite would catch.
"""
from __future__ import annotations

import os
import subprocess
import sys

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
