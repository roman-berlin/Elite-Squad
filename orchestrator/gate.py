"""Verification gate — runs an app's own tests/lint/typecheck.

Used twice: on the feature branch (before review) and again on dev after a merge
(to keep dev green). Cheap filter: never spend a review cycle on something the
test suite would catch.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .config import AppConfig
from .contracts import GateResult


def _subprocess_env(app: AppConfig) -> dict[str, str]:
    """EU-255: the env for a gate subprocess — it runs the PRODUCT's own (untrusted) test/build/
    lint commands, the same boundary the officer SDK seam minimizes via
    ``backends.secret_strip_overrides``. Inherits PATH/HOME/TMPDIR/toolchain and the native model
    auth (anything NOT flagged sensitive) plus ``app.gate_env``, but OMITS
    JIRA_*/TELEGRAM_*/GENERAL_COCKPIT_PROMOTE and (EU-371) GLM_AUTH_TOKEN — those live only in the
    orchestrator's own process and a gate never legitimately needs them (a gate command talks to no
    model endpoint, so the paid z.ai bearer has no business in its env). Unlike
    ``backends.secret_strip_overrides`` (which must BLANK because the SDK merges options.env OVER
    os.environ), here we build the dict ourselves, so the sensitive keys are simply left out."""
    from . import backends as _backends
    env = {k: v for k, v in os.environ.items() if not _backends.is_sensitive_key(k)}
    env.update(app.gate_env)
    return env


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
    full_failures: list[str] = []   # EU-342: untruncated, teed to a run-log for the Builder retry
    for cmd in commands:
        proc = None
        try:
            # EU-146: Use Popen with process group support for proper cleanup
            proc = subprocess.Popen(
                cmd, shell=True, cwd=where,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=_subprocess_env(app),   # EU-255: minimized — no Jira/Telegram creds
                start_new_session=True,  # Creates new session/group on Unix; ignored on Windows
            )
            try:
                stdout, stderr = proc.communicate(timeout=app.gate_timeout_sec)
            except subprocess.TimeoutExpired as texc:
                # EU-146: Kill entire process group to prevent orphaned children
                try:
                    os.killpg(os.getpgid(proc.pid), 9)  # SIGKILL
                except (ProcessLookupError, OSError):
                    proc.kill()
                # Reap the zombie and get partial output. The reap itself must be BOUNDED (EU-358):
                # a grandchild that re-daemonized into its own session survives the killpg and keeps
                # the pipe FDs open, so an unbounded communicate() here froze the whole drain.
                try:
                    stdout, stderr = proc.communicate(timeout=10)
                except Exception:  # noqa: BLE001 - TimeoutExpired again, or a closed-pipe race
                    stdout = texc.stdout or b""
                    stderr = texc.stderr or b""
                # Keep the partial output tail — a timeout report with no evidence made every
                # timeout look identical (EU-346 triage needs to see WHERE the suite stalled).
                tail = (stdout.decode("utf-8", errors="replace") + "\n"
                        + stderr.decode("utf-8", errors="replace")).strip()[-2000:]
                failures.append(f"$ {cmd}\n(timed out after {app.gate_timeout_sec}s)"
                                + (f"\n[partial output before the kill]\n{tail}" if tail else ""))
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
            combined = (stdout.decode("utf-8", errors="replace") + "\n"
                        + stderr.decode("utf-8", errors="replace")).strip()
            # EU-342: the concise `report` keeps the last-4000 tail (audit rows, Jira comments,
            # backward-compat); `full_report` keeps the COMPLETE output for the tee-on-failure log.
            failures.append(f"$ {cmd}\n(exit {proc.returncode})\n{combined[-4000:]}")
            full_failures.append(f"$ {cmd}\n(exit {proc.returncode})\n{combined}")
    if failures:
        return GateResult(passed=False, report="\n\n".join(failures),
                          full_report="\n\n".join(full_failures))
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
            env=_subprocess_env(app),   # EU-255: minimized — no Jira/Telegram creds
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

    Note (EU-85 / EU-326): the gate no longer carries a domain-gap note. Domain-gap
    classification was removed entirely in the Phase-2 collapse — it never belonged on the gate
    anyway (the advisory line was inaccurate whenever delegation was off or the ticket was too
    small to delegate).
    """
    # EU-54: fail fast and clearly if the gate interpreter can't even import its deps, before we
    # spend the whole suite producing a confusing mid-run ModuleNotFoundError.
    pf = preflight_imports(app)
    if pf is not None:
        return pf
    groups = select_gate_groups(app, changed_paths or [])
    if not groups:
        if not app.gate_commands:
            # EU-432: a build with NO configured gate is REFUSED, not passed vacuously. This used to
            # return passed=True ("(no gate commands configured)"), which let a build land UNGATED on
            # any host with no gate_commands — precisely the VPS's posture (config.server.example.yaml:
            # "It NEVER ... builds — no gate_commands"). A weekly builder scheduled there would have
            # shipped unverified. Refuse instead: an operator who wants to build MUST configure at
            # least one gate command (even a trivial `true`) so the gate is a deliberate act, never an
            # accident of an empty config. Defensive callers that only want a "did anything run?" probe
            # should pass explicit commands; the build-loop and off-loop paths are unaffected because
            # real build hosts (the Mac) always declare gate_commands.
            return GateResult(passed=False, report=(
                "REFUSED: no gate_commands are configured for this app and no per-app gate matched "
                "the changed paths. A build may not proceed without a verification gate — configure "
                "gate_commands (or gate_commands_by_app) first. (EU-432)"))
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


# 'timed out' isn't covered by _FAIL_LINE (gate.py's own timeout report reads
# "(timed out after Ns)" — no FAILED/ERROR/✗ token) — matched separately below.
_TIMED_OUT = re.compile(r"(?i)\btimed out\b")


def extract_failure_evidence(report: str, limit: int = 2500) -> str:
    """A failure-first slice of a (possibly huge) gate report, for audit/PM/Commander eyes.

    EU-217: a naive ``report[:2500]`` silently drops the failing suite whenever enough green
    suites' output precedes it to fill the budget (EU-204/EU-206: the recorded audit event
    showed nothing but green suites, so nobody downstream could see which suite actually
    failed). Keeps every line carrying failure signal (the ``_FAIL_LINE`` pattern, plus a
    'timed out' match it doesn't cover) and the run_all-style summary tail (from its first
    ``====...`` banner line to EOF — ``HARNESSES: … / FAILED: … / ALL GREEN``), then pads with
    as much head context as still fits, all capped at ``limit`` chars. Short reports pass
    through unchanged.
    """
    text = report or ""
    if len(text) <= limit:
        return text
    lines = text.splitlines()

    def _is_fail(ln: str) -> bool:
        return bool(_FAIL_LINE.search(ln)) or bool(_TIMED_OUT.search(ln))

    keep = {i for i, ln in enumerate(lines) if _is_fail(ln)}
    tail_start = next((i for i, ln in enumerate(lines)
                        if ln.strip() and set(ln.strip()) == {"="}), None)
    if tail_start is not None:
        keep.update(range(tail_start, len(lines)))
    if not keep:
        return text[:limit]   # no failure signal at all — fall back to the plain head slice

    evidence = "\n".join(lines[i] for i in sorted(keep))
    if len(evidence) >= limit:
        return evidence[:limit]

    # Pad with head context (the report's natural lead-in) until the char budget is spent.
    head: list[str] = []
    used = len(evidence)
    for i, ln in enumerate(lines):
        if i in keep:
            continue
        if used + len(ln) + 1 > limit:
            break
        head.append(ln)
        used += len(ln) + 1
    if not head:
        return evidence
    return "\n".join(head) + "\n" + evidence


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
    pass before any LLM sees the diff.

    A line carrying the pragma ``gate:allow-secret`` is exempt ONLY when it is provably
    pre-existing base content: the identical (whitespace-stripped) line also appears as a
    REMOVED (``-``) or context (`` ``) line of the same diff — what a move/reindent of an
    already-landed fixture looks like. Those provenance lines are generated by git from BASE
    content the diff's author cannot forge, so this needs no git shell-out and no base ref
    (the worktree HEAD may already contain the feature commits, so ``git show HEAD:`` would
    resurrect the bypass). EU-371(2), 2026-07-16 total audit: the pragma used to exempt ANY
    added line, letting untrusted product code whitelist its own secret in the very diff under
    review. A genuinely NEW credential-shaped fixture now red-gates and escalates to a human —
    the intended tradeoff; build fixtures by concatenation so the pattern never appears
    contiguously in source at all (the pre-2026-07-06 guidance, now the only self-serve path)."""
    lines = (diff or "").splitlines()
    # Stripped content of every base-provenance line ("-" removed / " " context; "---" headers
    # excluded, mirroring the "+++" exclusion below). Added ("+") lines are deliberately NOT
    # provenance — a duplicated added line is still two NEW lines, not a move.
    base_lines = {ln[1:].strip() for ln in lines
                  if (ln.startswith("-") and not ln.startswith("---")) or ln.startswith(" ")}
    hits: list[str] = []
    for ln in lines:
        if not ln.startswith("+") or ln.startswith("+++"):
            continue
        if "gate:allow-secret" in ln and ln[1:].strip() in base_lines:
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


def _nearest_lock(manifest_dir: str, lock_names: tuple[str, ...], repo_root: str) -> str | None:
    """Repo-relative path of the nearest existing lockfile at or ABOVE ``manifest_dir``, walking up to
    the repo root; ``None`` if none of ``lock_names`` exists anywhere in that ancestry. This makes drift
    detection WORKSPACE-AWARE: a bun/npm/pnpm/yarn monorepo hoists ONE lock to the repo root while each
    app keeps its own package.json, so an app-manifest change must be validated against the ROOT lock —
    a sibling-only lookup silently missed it and passed a broken build LIVE on AUTO-57 (a dep added to
    an app's package.json with the root bun.lock never regenerated). A nested package that vendors its
    OWN lock binds to that nearer lock first, so sub-package drift is never masked by a consistent root."""
    d = manifest_dir
    while True:
        for lk in lock_names:
            rel = os.path.join(d, lk) if d else lk
            if os.path.exists(os.path.join(repo_root, rel)):
                return rel
        parent = os.path.dirname(d)
        if parent == d:   # fixed point — "" for a relative path, "/" or a drive root for an absolute
            return None    # one; terminates the walk regardless of whether the input was relative
        d = parent


def _lockfile_candidates(changed_paths: list[str], repo_root: str) -> list[tuple[str, str]]:
    """Static manifest/lockfile drift candidates: ``(manifest_path, existing_lock_relpath)`` for each
    changed dependency manifest whose governing lockfile EXISTS in the repo but did NOT change. The lock
    is resolved to the NEAREST one at or above the manifest's directory (``_nearest_lock``) — the sibling
    when a package vendors its own lock, else the hoisted workspace-root lock. Manifests with no lockfile
    anywhere in their ancestry are never flagged (nothing to drift against)."""
    out: list[tuple[str, str]] = []
    changed = {p.replace("\\", "/") for p in (changed_paths or [])}
    for path in sorted(changed):
        d, base = os.path.split(path)
        for manifest, locks in _LOCKFILE_PAIRS:
            if base != manifest:
                continue
            lock = _nearest_lock(d, locks, repo_root)
            if lock is not None and lock not in changed:
                out.append((path, lock))
    return out


def _lockfile_problem(manifest_path: str, lock_relpath: str) -> str:
    return f"{manifest_path} changed but {lock_relpath} did not — regenerate the lockfile in the same commit"


def lockfile_sanity(changed_paths: list[str], repo_root: str) -> list[str]:
    """Detect manifest/lockfile drift in a diff: a dependency manifest changed while its sibling
    lockfile — which EXISTS in the repo — did not. Returns human-readable problems ([] = clean). This
    is a STATIC heuristic; ``run_deterministic_checks`` confirms REAL drift (``_lock_drift_confirmed``)
    before failing, so a lock-consistent manifest change (scripts edit, already-locked dep) is not a
    false failure."""
    return [_lockfile_problem(m, lk) for m, lk in _lockfile_candidates(changed_paths, repo_root)]


# Package managers whose manifest/lock consistency is verified NON-MUTATINGLY: --dry-run skips the
# node_modules write and the network; --ignore-scripts skips the project's OWN lifecycle scripts
# (preinstall/postinstall) — WITHOUT it bun still runs the manifest-under-review's scripts, so a
# hostile or buggy package.json in the diff could execute arbitrary shell in the worktree during the
# gate. The check still exits non-zero ONLY when the lock genuinely does not satisfy the manifest, so
# real-drift detection is unchanged. A manifest change that leaves it passing is not drift. Only bun is
# listed — other managers (npm/pnpm/yarn) keep the conservative static flag rather than shell out an
# installer we have not proven side-effect-free.
_LOCK_VERIFY: dict[str, str] = {
    "bun.lock": "bun install --frozen-lockfile --dry-run --ignore-scripts",
    "bun.lockb": "bun install --frozen-lockfile --dry-run --ignore-scripts",
}


def _lock_drift_confirmed(app: AppConfig, candidates: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep only the lockfile-drift candidates that are REAL. ``lockfile_sanity`` is a static heuristic
    that CANNOT be satisfied when a package.json change leaves the lock consistent (a scripts edit, or
    a dep already in the lock) — it false-fails and deadlocks a legitimate ticket (found live on
    AUTO-57). For a manifest whose lock has a verifier, run the manager's NON-MUTATING ``--dry-run``
    frozen check IN THE MANIFEST'S OWN DIRECTORY (monorepo-safe) via ``run_commands`` (shared timeout /
    process-group-kill / gate_env): passing → the lock is consistent → drop the flag; failing, a
    missing tool, or no verifier → keep it (conservative — REAL drift still fails, and nested drift in
    a sub-package is probed in that sub-package, never masked by a consistent repo root)."""
    if not candidates:
        return candidates
    root = app.workdir or app.repo_path
    verified: dict[tuple[str, str], bool] = {}   # (manifest_dir, lock_basename) -> consistent
    kept: list[tuple[str, str]] = []
    for manifest_path, lock_relpath in candidates:
        lock_base = os.path.basename(lock_relpath)
        cmd = _LOCK_VERIFY.get(lock_base)
        mdir = os.path.dirname(manifest_path)
        key = (mdir, lock_base)
        if cmd is None:
            kept.append((manifest_path, lock_relpath))   # no verifier → conservative keep
            continue
        if key not in verified:
            verified[key] = run_commands(app, [cmd], cwd=(os.path.join(root, mdir) if mdir else root)).passed
        if not verified[key]:
            kept.append((manifest_path, lock_relpath))   # the manager rejects the lock → REAL drift
    return kept


# --------------------------------------------------------------------------- #
# EU-249 — pre-merge, diff-scoped test-collectability + test-run gate.
#
# Grounded in the AUTO-97 -> AUTO-101 -> AUTO-95 audit chain: the automatixy gate was
# typecheck-only (no test step actually ran), so (a) a diff whose Builder self-reported failing
# tests merged with a green gate, and (b) two added test files landed at a doubled
# 'apps/zeltivo-crm/apps/zeltivo-crm/...' path that matches no vitest include glob — invisible to
# the test runner forever, so "tests pass" was a vacuous signal. This is the PRE-MERGE, diff-scoped
# complement to the dormant full-suite AUTO-57/AUTO-122 post-merge gate (it does not replace it):
# by running ONLY the files a diff actually ADDS, it is immune to the 22 pre-existing zeltivo-crm
# suite failures that keep that full-suite gate dormant. Opt-in per app via
# AppConfig.test_collectability_enabled — a no-op for any app that hasn't armed it.
# --------------------------------------------------------------------------- #

_TEST_FILE_RE = re.compile(r"\.(?:test|spec)\.[A-Za-z0-9]+$")
_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$")
_VITEST_CONFIG_NAMES = ("vitest.config.ts", "vitest.config.mts", "vitest.config.js", "vitest.config.mjs")


def added_test_files(diff: str) -> list[str]:
    """Repo-relative paths of ADDED or RENAMED-TO ``*.test.*``/``*.spec.*`` files in a unified diff.
    A test file merely MODIFIED in place is not returned — its collectability was already proven (or
    not) whenever it was first added, so re-flagging it on every touch would be noise."""
    out: list[str] = []
    lines = (diff or "").splitlines()
    i = 0
    while i < len(lines):
        m = _DIFF_GIT_HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        target = m.group(2)
        block_added = False
        j = i + 1
        while j < len(lines) and not _DIFF_GIT_HEADER_RE.match(lines[j]):
            ln = lines[j]
            if ln.startswith("new file mode") or ln.startswith("--- /dev/null"):
                block_added = True
            rn = re.match(r"^rename to (.+)$", ln)
            if rn:
                block_added = True
                target = rn.group(1).strip()
            j += 1
        if block_added and _TEST_FILE_RE.search(target):
            out.append(target)
        i = j
    return out


def phantom_nested_test_paths(paths: list[str]) -> list[str]:
    """Flag any path whose ``apps/<x>`` root segment is DUPLICATED later in the same path (e.g.
    ``apps/zeltivo-crm/apps/zeltivo-crm/src/foo.test.tsx``) — a phantom nested copy that matches no
    test runner's include glob no matter how the app is configured. Pure string check, zero runner
    cost (AUTO-101/AUTO-95: this exact shape landed on DEV twice with a green gate)."""
    hits: list[str] = []
    for p in paths:
        parts = p.replace("\\", "/").split("/")
        seen_roots: set[str] = set()
        for i in range(len(parts) - 1):
            if parts[i] == "apps" and parts[i + 1]:
                root = f"apps/{parts[i + 1]}"
                if root in seen_roots:
                    hits.append(p)
                    break
                seen_roots.add(root)
    return hits


def _owning_app_root(path: str) -> str | None:
    """The 'apps/<x>' directory owning a path, else None (a path not under apps/ at all)."""
    parts = path.replace("\\", "/").split("/")
    if len(parts) >= 2 and parts[0] == "apps" and parts[1]:
        return f"apps/{parts[1]}"
    return None


def _expand_braces(pattern: str) -> list[str]:
    """Expand ONE level of brace alternation at a time (recursing on the result), e.g.
    '*.{test,spec}.{ts,tsx}' -> the 4 concrete patterns micromatch/vitest would also expand."""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    out: list[str] = []
    for opt in m.group(1).split(","):
        out.extend(_expand_braces(pattern[:m.start()] + opt + pattern[m.end():]))
    return out


def _glob_to_regex(pattern: str) -> re.Pattern:
    """Translate ONE brace-free glob (as used in vitest ``include``: ``**`` = any depth, ``*`` = any
    chars within a path segment) into an anchored regex."""
    i, n = 0, len(pattern)
    out: list[str] = []
    while i < n:
        if pattern[i:i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif pattern[i:i + 2] == "**":
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_matches(pattern: str, relpath: str) -> bool:
    """Whether ``relpath`` matches a vitest/micromatch-style ``include`` glob (``**``, ``*``,
    ``{a,b}`` brace alternation)."""
    return any(_glob_to_regex(p).match(relpath) for p in _expand_braces(pattern))


def _parse_vitest_include(config_text: str) -> list[str]:
    """Pull the ``include: [...]`` string literals out of a vitest.config.ts source. Static/textual
    — this repo never executes an app's own config as code (zero runner cost, no supply-chain
    exposure to a hostile config in the diff under review)."""
    m = re.search(r"include\s*:\s*\[(.*?)\]", config_text, re.DOTALL)
    if not m:
        return []
    return re.findall(r"[\"']([^\"']+)[\"']", m.group(1))


def _vitest_includes(repo_root: str, app_root: str) -> list[str] | None:
    """The vitest ``include`` globs declared for ``app_root``, or ``None`` when no vitest config (or
    no ``include`` key) is found there — meaning there is nothing to validate a test path against,
    so the caller should skip rather than false-flag (e.g. a non-monorepo app, or one not on Vitest)."""
    for name in _VITEST_CONFIG_NAMES:
        p = Path(repo_root) / app_root / name
        if not p.exists():
            continue
        try:
            includes = _parse_vitest_include(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — an unreadable config just skips the check for this app
            return None
        return includes or None
    return None


_PLAYWRIGHT_CONFIG_NAMES = ("playwright.config.ts", "playwright.config.mts",
                            "playwright.config.js", "playwright.config.mjs")


def _playwright_testdir(repo_root: str, app_root: str) -> str | None:
    """The Playwright ``testDir`` declared for ``app_root`` (Playwright's own default 'tests' when
    a config exists without an explicit testDir), or None when the app has no playwright config.
    2026-07-20 (AUTO-182 live-fire): an e2e spec is collected by PLAYWRIGHT, not vitest — judging
    it by vitest include globs produced a deterministic false FAIL no rebuild could ever fix (both
    elite passes died at this gate without an LLM review, then max-passes escalated)."""
    for name in _PLAYWRIGHT_CONFIG_NAMES:
        p = Path(repo_root) / app_root / name
        if not p.exists():
            continue
        try:
            m = re.search(r"testDir\s*:\s*[\"']([^\"']+)[\"']", p.read_text(encoding="utf-8"))
        except OSError:
            return None
        d = (m.group(1) if m else "tests").strip().lstrip("./")
        return d.rstrip("/") or "tests"
    return None


def test_collectability_problems(app: AppConfig, test_files: list[str], repo_root: str) -> list[str]:
    """Human-readable collectability problems ([] = clean) for ADDED/RENAMED test files: a
    phantom duplicated-app-root path, or a path matching none of its owning app's vitest ``include``
    globs. A file whose owning app has no vitest config (nothing to validate against) is skipped —
    and so is a file under the app's Playwright ``testDir`` (an e2e spec is Playwright's to collect,
    never vitest's)."""
    problems: list[str] = []
    for f in test_files:
        if phantom_nested_test_paths([f]):
            problems.append(f"{f}: phantom nested path (a duplicated 'apps/<x>' root segment) — "
                            "this file matches no test runner's include glob and will never be collected")
            continue
        root = _owning_app_root(f)
        if root is None:
            continue
        relpath = f[len(root) + 1:] if f.startswith(root + "/") else f
        pw_dir = _playwright_testdir(repo_root, root)
        if pw_dir is not None and (relpath == pw_dir or relpath.startswith(pw_dir + "/")):
            continue
        includes = _vitest_includes(repo_root, root)
        if includes is None:
            continue
        if not any(glob_matches(pat, relpath) for pat in includes):
            problems.append(f"{f}: matches none of {root}'s vitest include globs {includes} "
                            f"(checked as '{relpath}' relative to {root}) — the test runner will never collect it")
    return problems


def run_scoped_vitest(app: AppConfig, test_files: list[str], repo_root: str) -> GateResult:
    """Run ONLY the given added test files via ``bunx vitest run`` in single-run mode (AUTO-57
    lesson: vitest, not a bare ``bun test``), grouped and cwd'd by owning app — diff-scoped so it is
    immune to pre-existing full-suite failures elsewhere in the app (AUTO-122's 22 zeltivo-crm
    failures). Reuses ``run_commands`` for the shared timeout / EU-146 process-group cleanup.

    Only files under an ``apps/<x>`` root are run; an unowned path is skipped, never run from the
    monorepo root (EU-263)."""
    by_root: dict[str, list[str]] = {}
    for f in test_files:
        root = _owning_app_root(f)
        if root is None:
            # EU-263: a path with no owning apps/<x> has no app to cwd into and no vitest config to
            # run against. The old grouping filed it under "" and fell back to cwd=repo_root, which
            # for the armed target (automatixy) has neither a root vitest config nor a root vitest
            # dep — so adding any of its 8 out-of-apps/ test files (packages/shared-consent/…,
            # scripts/ci/…, and 6 DENO tests under supabase/functions/*/index.test.ts that vitest
            # was never meant to collect) would have spuriously RED-ed the gate. The caller filters
            # and reports these; this guard is what makes the repo_root fallback unreachable.
            continue
        by_root.setdefault(root, []).append(f)
    failures: list[str] = []
    for root, files in by_root.items():
        rels = [f[len(root) + 1:] if f.startswith(root + "/") else f for f in files]
        cmd = ("bunx vitest run " + " ".join(shlex.quote(r) for r in rels)
              + " --pool=forks --poolOptions.forks.maxForks=2")
        res = run_commands(app, [cmd], cwd=os.path.join(repo_root, root))
        if not res.passed:
            failures.append(f"[{root}] scoped vitest FAILED\n{res.report}")
    if failures:
        return GateResult(passed=False, report="\n\n".join(failures))
    return GateResult(passed=True, report="scoped vitest run passed")


def test_collectability_gate(app: AppConfig, changed_paths: list[str], diff: str) -> GateResult:
    """EU-249 entry point. A no-op unless ``app.test_collectability_enabled`` is armed. For every
    ADDED/RENAMED ``*.test.*``/``*.spec.*`` file in the diff: flag phantom-nested or include-glob
    mismatches (zero runner cost), then run the survivors with a diff-scoped ``bunx vitest run`` and
    fail on red, returning the vitest report text. A diff touching no (added) test files is a
    zero-shell-out pass, unaffected by anything already broken elsewhere in the app."""
    if not getattr(app, "test_collectability_enabled", False):
        return GateResult(passed=True, report="(test-collectability gate not armed for this app)")
    test_files = added_test_files(diff or "")
    if not test_files:
        return GateResult(passed=True, report="no added/renamed test files in this diff")
    repo_root = app.workdir or app.repo_path
    problems = test_collectability_problems(app, test_files, repo_root)
    if problems:
        return GateResult(passed=False, report="\n".join(f"  · {p}" for p in problems))
    collectable = [f for f in test_files if not phantom_nested_test_paths([f])]
    # 2026-07-20 (AUTO-182): a Playwright e2e spec must not be fed to the scoped VITEST run either
    # — it is the app's playwright runner's job. Excluded but REPORTED (same honesty rule as the
    # unowned skips below), so an e2e spec never silently bypasses all verification.
    pw_skipped: list[str] = []
    non_pw: list[str] = []
    for f in collectable:
        _root = _owning_app_root(f)
        _pw = _playwright_testdir(repo_root, _root) if _root else None
        _rel = f[len(_root) + 1:] if _root and f.startswith(_root + "/") else f
        if _pw is not None and (_rel == _pw or _rel.startswith(_pw + "/")):
            pw_skipped.append(f)
        else:
            non_pw.append(f)
    # EU-263: only apps/<x>-owned files are runnable — the scoped run cwd's into the owning app, and
    # an unowned path has no config there to run against (see run_scoped_vitest). Skipping is the
    # honest outcome, but it is REPORTED rather than silent: a Deno/supabase test quietly never
    # running is the same invisibility class this gate exists to catch.
    runnable = [f for f in non_pw if _owning_app_root(f) is not None]
    skipped = [f for f in non_pw if _owning_app_root(f) is None]
    res = run_scoped_vitest(app, runnable, repo_root)
    notes: list[str] = []
    if pw_skipped:
        notes.append("Playwright e2e spec(s) — collected/run by the app's playwright runner, not "
                     "the scoped vitest (the Builder's own gate run covers them):\n"
                     + "\n".join(f"  · {s}" for s in pw_skipped))
    if skipped:
        notes.append("not run by the scoped vitest — no owning 'apps/<x>', so no vitest config to "
                     "run against (check this file's own runner):\n"
                     + "\n".join(f"  · {s}" for s in skipped))
    if not notes:
        return res
    return GateResult(passed=res.passed, report="\n".join([res.report] + notes))


def run_deterministic_checks(app: AppConfig, changed_paths: list[str], diff: str) -> GateResult:
    """§3 items 3–5, in order: lint → secret scan → lockfile sanity → (EU-249) diff-scoped
    test-collectability. Runs AFTER the test gate is green and BEFORE any LLM reviewer; a failure
    feeds the Builder as plain text. All are cheap relative to one review round, and none can
    hallucinate."""
    reports: list[str] = []
    if getattr(app, "lint_commands", None):
        lint = run_commands(app, app.lint_commands)
        if not lint.passed:
            reports.append("LINT gate failed:\n" + lint.report)
    hits = scan_diff_for_secrets(diff or "")
    if hits:
        reports.append("SECRET-LEAK gate failed — remove these from the diff (values masked):\n"
                       + "\n".join(f"  · {h}" for h in hits))
    candidates = _lockfile_candidates(changed_paths or [], app.workdir or app.repo_path)
    drift = _lock_drift_confirmed(app, candidates)   # verify REAL drift before failing (false-positive guard)
    if drift:
        reports.append("LOCKFILE gate failed:\n" + "\n".join(f"  · {_lockfile_problem(m, lk)}" for m, lk in drift))
    tc = test_collectability_gate(app, changed_paths or [], diff or "")
    if not tc.passed:
        reports.append("TEST-COLLECTABILITY gate failed:\n" + tc.report)
    if reports:
        return GateResult(passed=False, report="\n\n".join(reports))
    return GateResult(passed=True, report="deterministic checks passed")


# --------------------------------------------------------------------------- #
# EU-442 — gate-vs-Builder verdict cross-verification.
#
# EU-151: a Builder summary reported "all tests pass" while the verification gate recorded ERRORED,
# and nothing in the loop caught the contradiction — it silently looped to max-effort until a human
# triaged it. 85 gate failures / 30 errored / 147 hit-max-effort suggest that decoupling is systemic.
# This pure helper is the deterministic cross-check: parse an EXPLICIT test-outcome claim from the
# Builder's own self-report (a GREEN claim "all tests pass" vs a RED admission "tests still failing")
# and compare it to the authoritative gate verdict. A summary that makes NO test-outcome claim cannot
# contradict anything, so it returns None regardless of the gate — eliminating the EU-249-iter-2
# false-positive class. The RED-admission parser reuses reviewer.py's single source of truth (its
# compiled _UNRESOLVED_STRONG_RE / _WEAK_RED_TEST_RE / _RESOLVED_CTX_RE) rather than re-deriving it,
# so the two parsers can never drift apart.
# --------------------------------------------------------------------------- #

# An explicit GREEN (tests-passed) self-report. Broader than reviewer.py's resolution-context phrase
# on purpose — a Builder can claim success in several natural wordings, and each is a falsifiable
# assertion the gate can contradict. Negation is handled separately (_GREEN_NEGATION_RE) so "not all
# tests pass" never reads as a green claim.
_GREEN_TEST_CLAIM_RE = re.compile(
    r"(?i)("
    r"\ball\s+(?:the\s+)?tests?\s+(?:pass\w*|passed|green|succeed\w*)\b"   # "all tests pass"
    r"|\btests?\s+(?:pass\w*|passed|are\s+passing|green|succeed\w*)\b"     # "tests passed"
    r"|\btest\s+suite\s+(?:pass\w*|passed|green)\b"                        # "test suite passes"
    r"|\bevery\s+test\s+pass\w*\b"                                         # "every test passes"
    r")"
)
# A negation token immediately preceding a green phrase cancels it ("not all tests pass",
# "tests don't pass"). Checked against a short prefix window of the match.
_GREEN_NEGATION_RE = re.compile(
    r"(?i)\b(?:not|no|never|don'?t|doesn'?t|didn'?t|cannot|can'?t|won'?t|ain'?t|without)\b\s*"
    r"(?:\w+\s+){0,3}$"
)


def _green_test_claim(text: str) -> str | None:
    """The matched GREEN phrase if ``text`` makes an explicit tests-passed claim (not negated), else
    None. Returns the whole line carrying the claim so the mismatch reason names a readable sentence."""
    for m in _GREEN_TEST_CLAIM_RE.finditer(text or ""):
        prefix = text[max(0, m.start() - 28):m.start()]
        if _GREEN_NEGATION_RE.search(prefix):
            continue
        start = text.rfind("\n", 0, m.start()) + 1
        end = text.find("\n", m.end())
        if end == -1:
            end = len(text)
        return text[start:end].strip()[:300]
    return None


def _red_test_admission(text: str) -> str | None:
    """The offending line if ``text`` admits a genuinely UNRESOLVED failing/skipped/broken test, else
    None. Applies reviewer.py's exact STRONG / WEAK-vs-RESOLVED logic (imported, not re-derived) so
    the orchestrator has ONE red-admission parser and the two cannot drift."""
    from .reviewer import _RESOLVED_CTX_RE, _UNRESOLVED_STRONG_RE, _WEAK_RED_TEST_RE
    m = _UNRESOLVED_STRONG_RE.search(text or "")
    if not m:
        weak = _WEAK_RED_TEST_RE.search(text or "")
        if weak and not _RESOLVED_CTX_RE.search(text or ""):
            m = weak
    if not m:
        return None
    start = text.rfind("\n", 0, m.start()) + 1
    end = text.find("\n", m.end())
    if end == -1:
        end = len(text)
    return text[start:end].strip()[:300]


def gate_vs_builder_verdict(build_summary: str, gate_passed: bool) -> str | None:
    """Compare the Builder's self-reported test outcome against the authoritative gate verdict.

    Returns a short mismatch reason when the two CONTRADICT, else None:

      • Builder claims GREEN ("all tests pass") but the gate is RED  → the EU-151
        hallucination-against-an-errored-gate class.
      • Builder admits RED ("tests still failing") but the gate is GREEN → the Builder ran the
        wrong tests / didn't actually exercise the suite class.

    A summary that makes NO explicit test-outcome claim returns None regardless of the gate — a
    neutral report ("implemented the endpoint") cannot contradict anything, so it never fires (the
    EU-249-iter-2 false-positive guard). Both-agree-GREEN likewise returns None."""
    text = build_summary or ""
    green = _green_test_claim(text)
    if green and not gate_passed:
        return (f"Builder claims tests are GREEN (\"{green}\") but the verification gate "
                f"FAILED (red).")
    red = _red_test_admission(text)
    if red and gate_passed:
        return (f"Builder admits tests are RED (\"{red}\") but the verification gate PASSED (green).")
    return None


_RED_BASE_CACHE_MAX = 40   # (repo@sha) entries kept
_RED_BASE_RED_TTL_S = 30 * 60   # a cached RED is re-verified after this long (see below)
_BASE_GATE_TIMEOUT_MARKER = "(timed out after"   # written by run_commands on a command timeout


def base_gate_timed_out(report: str | None) -> bool:
    """True when a base-gate red is a runtime TIMEOUT — an environment/load verdict (the EU-228
    class), not a code-red. 2026-07-15 incident, twice in one day (04:20 and 17:41 waves): a box
    under load timed the 1800s base suite out, the red survived its confirmation re-run (sustained
    load reproduces), got cached, and the drain force-parked the whole To Do queue to needs_human
    one ticket per pick. A timeout must neither poison the red cache (see base_gate_check) nor
    park tickets (loop.py routes it to the EU-228 infra path instead)."""
    return _BASE_GATE_TIMEOUT_MARKER in (report or "")


def _base_gate_once(app: AppConfig, run) -> GateResult:
    """One base-tree gate pass: the verification gate plus the base's own lint (a red LINT base
    would otherwise burn every ticket's full pass budget at the deterministic stage — 2026-07-06
    review). Secrets/lockfile need a diff, so on the clean base only lint applies."""
    res = run(app, [])
    if not res.passed:
        return res
    return run_deterministic_checks(app, [], "")


def publish_base_green(app: AppConfig, cfg, sha: str) -> None:
    """EU-376: publish a green dev_gate verdict into the base-gate cache for ``sha``.

    A drain mints a new base sha on every land, and base_gate_check is the cache's ONLY writer —
    so the next ticket's base gate always MISSED and re-ran the full suite on the exact commit
    object the previous ticket's dev_gate proved green ~3 minutes earlier (measured 2026-07-16:
    all 14 cache entries were distinct shas = 14 misses; dev_gate green at 00:30:54, base gate
    re-proved the same 4831566 at 00:33:58 — 178s of pure duplication per ticket).

    Safety by construction: callers publish ONLY a green verdict (a published red would resurrect
    the EU-334/EU-228 false-red-halt class) for a sha that land_trial has already fast-forwarded
    to <base> (an honest ff means the trial commit IS the new base tip). Lint parity: the base
    gate runs the suite PLUS the base's lint (_base_gate_once), the dev_gate runs the suite only —
    so where lint_commands is armed the dev_gate proof is strictly weaker and must not be
    published. Best-effort: a cache write failure just restores the old re-run behaviour."""
    if not sha or getattr(app, "lint_commands", None):
        return
    cache_path = Path(cfg.audit_path).with_name("red_base_cache.json")
    key = f"{app.repo_path}@{sha}"
    try:
        from . import locking

        def _put(data):
            data = data if isinstance(data, dict) else {}
            data[key] = {"passed": True, "fp": "", "report": "", "ts": time.time()}
            if len(data) > _RED_BASE_CACHE_MAX:
                for old in sorted(data, key=lambda k: data[k].get("ts", 0))[:len(data) - _RED_BASE_CACHE_MAX]:
                    data.pop(old, None)
            return data
        locking.locked_rmw(cache_path, _put, default={}, corrupt_to_default=True)
    except Exception as exc:  # noqa: BLE001 — cache write failure must never block the land
        # 2026-07-19 stabilization: keep the fail-open contract, but a permanently dead cache
        # costs ~178s of duplicate gate work per ticket — make it visible in the run log.
        print(f"  · base-gate cache write failed ({exc}) — gate will re-run next time", flush=True)


def evict_base_green(app: AppConfig, cfg, sha: str) -> None:
    """EU-454: the inverse of publish_base_green — drop the green cache entry for ``sha``.

    publish_base_green (EU-376) publishes a green entry for merge_sha at _land time, BEFORE the
    post-merge dev-HEAD verify (EU-453) runs. If that verify then goes RED, the stale green would
    make the NEXT pick's base_gate_check HIT and skip re-running against dev's real (red) state —
    exactly the EU-447 false-green hazard (a clean combine that breaks dev stays SILENT). Called
    only from the post-merge verify RED branch, this drops the f"{app.repo_path}@{sha}" key from
    red_base_cache.json so the next pick's base gate MISSES and re-verifies against the real dev.
    Green verify leaves the entry untouched (today's behaviour, preserving EU-376's ~178s-per-
    ticket dedup win).

    Never raises (best-effort): the WHOLE body is guarded, so a malformed cfg/app (e.g. a stub
    missing repo_path) or a disk error just leaves the stale entry (restoring the old re-run
    behaviour on the next pick) instead of propagating into the post-merge RED branch and skipping
    its notify/tracker side effects. A no-op when the key (or the cache file) is absent — it never
    CREATES the file, because locked_rmw unconditionally os.replace-writes on a missing path
    (locking.py:222-227), so an evict on a cache that doesn't yet exist short-circuits first.
    Unconditional (no lint-armed refusal, unlike publish_base_green): evict is the inverse — a
    lint-armed app was never written, so the pop is a harmless no-op there."""
    if not sha:
        return
    try:
        cache_path = Path(cfg.audit_path).with_name("red_base_cache.json")
        if not cache_path.exists():
            return   # locked_rmw would os.replace-WRITE a missing file — short-circuit first
        key = f"{app.repo_path}@{sha}"
        from . import locking

        def _del(data):
            data = data if isinstance(data, dict) else {}
            data.pop(key, None)   # a no-op when the key is absent (the honest inverse of publish)
            return data

        locking.locked_rmw(cache_path, _del, default={}, corrupt_to_default=True)
    except Exception as exc:  # noqa: BLE001 — an evict failure must never block a landed merge
        print(f"  · base-gate cache evict failed ({exc}) — stale green may linger", flush=True)


def base_gate_check(app: AppConfig, cfg, git, runner=None) -> tuple[bool, str, str]:
    """§3 item 1 — the EU-174 killer. Run the verification gate (and the base's lint) against
    the CLEAN base tree, BEFORE the first build pass. Returns (passed, fingerprint, report).

    Flake honesty (2026-07-06 review; also this unit's own flaky-harness doctrine): a RED here
    can halt the whole unit, so a red verdict must survive a CONFIRMATION re-run — one flaky red
    never blocks. GREEN results are cached per (repo, base-sha) indefinitely (content-addressed;
    a false green just restores the old behaviour). RED results are cached with a TTL so an
    environmental red (venv missing from PATH, box under load) self-heals without the base
    moving; deleting red_base_cache.json or red_base_check=false remain the manual overrides.

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
                fresh_red = (not hit["passed"]
                             and time.time() - float(hit.get("ts", 0)) <= _RED_BASE_RED_TTL_S)
                if hit["passed"] or fresh_red:
                    return bool(hit["passed"]), str(hit.get("fp", "")), str(hit.get("report", ""))
                # expired red → fall through and re-verify
        except Exception:  # noqa: BLE001 — a missing/corrupt cache just re-runs the gate
            pass

    res = _base_gate_once(app, run)
    if not res.passed and not base_gate_timed_out(res.report):
        # Confirmation re-run: only a red that REPRODUCES blocks (a single timing flake on this
        # box must never park the whole queue). A green confirm wins — old behaviour proceeds.
        # A TIMEOUT red is exempt: it isn't cached, and doubling a gate_timeout_sec suite on an
        # already-loaded box is the harm, not the cure (2026-07-15: 2×1800s per re-check).
        confirm = _base_gate_once(app, run)
        if confirm.passed:
            res = confirm
    # Compute the infra verdict on the FULL report BEFORE truncation — run_commands appends one
    # entry per failing command, so a long genuine failure ahead of the timed-out command could
    # push the marker past the cut and make loop.py read the same red differently than we did.
    infra_red = (not res.passed) and base_gate_timed_out(res.report)
    fp = "" if res.passed else gate_fingerprint(res.report or "")
    report = "" if res.passed else (res.report or "")[:4000]
    if infra_red and not base_gate_timed_out(report):
        report = "(timed out after gate timeout — marker restored; truncation dropped it)\n" + report[:3900]

    # A timeout-shaped red is an environment verdict, not a code verdict — caching it would make
    # every pick for the next _RED_BASE_RED_TTL_S insta-block on a box that was merely busy.
    if sha and not infra_red:
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
        except Exception as exc:  # noqa: BLE001 — cache write failure must never block the pipeline
            print(f"  · base-gate cache write failed ({exc}) — gate will re-run next time", flush=True)
    return res.passed, fp, report


# Phase-2 §2 (2026-07-06): the EU-107/EU-134 Senior PM pre-build triage gate (prebuild_gate)
# was DELETED here. Off by default since 2026-06-29 (it closed [Feature] tickets as "answered");
# its ANSWER/CLOSE/REFILE verdicts fold into the Planner's single per-ticket decision, with the
# conservative overrides (AC / [Feature] / [Bug] ⇒ always build) as deterministic pre-checks.
