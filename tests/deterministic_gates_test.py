"""Phase-2 §3 — deterministic gates (2026-07-05 restructure, Commander-approved 2026-07-06).

Pinned here:
  1. gate_fingerprint — same failure with different timings/paths → SAME fingerprint; a
     different failing harness → different; a report with no failure signal → "" (never compared).
  2. scan_diff_for_secrets — credentials on ADDED lines are found and MASKED (the raw token never
     appears in the report); context/removed lines are ignored; clean diffs return [].
  3. lockfile_sanity — manifest changed while its existing sibling lockfile didn't → flagged;
     both changed → clean; repo without a lockfile → never flagged.
  4. run_deterministic_checks — lint failures + secret hits + lockfile drift aggregate into one
     failing GateResult; all-clean passes.
  5. base_gate_check — red/green results are cached per (repo, base-sha): the second ticket on
     the same sha never re-runs the suite; a git stub without current_sha still works (uncached).
  6. loop._attempt red-base short-circuit — a red base BLOCKS the ticket BEFORE any build
     (builder never invoked, red_base_block audited, ESCALATED with a 'red base' note);
     red_base_check=False restores the old behaviour (build proceeds → MERGED).
  7. loop._attempt identical-fingerprint escalation — the gate failing with the SAME fingerprint
     on consecutive passes parks the ticket (gate_fingerprint_stuck) instead of rebuilding.
  8. loop._attempt deterministic stage — a secret in the pass-1 diff bounces back to the builder
     (deterministic_gate failed), and the clean pass-2 diff proceeds to MERGED.

All offline — SDK and agents stubbed; no network, no real models.
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from pathlib import Path

# ── SDK stub (no real model calls) ───────────────────────────────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): self.__dict__.update(k)
    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.gate as gate_mod                   # noqa: E402
import orchestrator.loop as loop                       # noqa: E402
from orchestrator.config import Config, AppConfig      # noqa: E402
from orchestrator.contracts import (                   # noqa: E402
    BuildArtifact, BuildResult, GateResult, Outcome, ReviewResult, ReviewVerdict,
    TestEngineerResult, Ticket, TicketReport, Verdict,
)

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# ══════════════════════════════════════════════════════════════════════════════
# 1. gate_fingerprint
# ══════════════════════════════════════════════════════════════════════════════
REPORT_A1 = ("$ python3 tests/run_all.py\n(exit 1)\n"
             "  ✗ squad_test.py    FAILED in 12.3s\n  67 checks, 3 ERRORS\n"
             "  log: /tmp/runx1/out.log\n")
REPORT_A2 = ("$ python3 tests/run_all.py\n(exit 1)\n"
             "  ✗ squad_test.py    FAILED in 47.9s\n  67 checks, 3 ERRORS\n"
             "  log: /tmp/zzz42/out.log\n")
REPORT_B = ("$ python3 tests/run_all.py\n(exit 1)\n"
            "  ✗ provost_gate_test.py    FAILED in 3.3s\n")

fp_a1, fp_a2, fp_b = (gate_mod.gate_fingerprint(r) for r in (REPORT_A1, REPORT_A2, REPORT_B))
chk("fingerprint: same failure, different timings/paths → identical", fp_a1 == fp_a2 and fp_a1,
    f"{fp_a1} vs {fp_a2}")
chk("fingerprint: different failing harness → different", fp_a1 != fp_b, f"{fp_a1} vs {fp_b}")
chk("fingerprint: no failure signal → '' (non-comparable)",
    gate_mod.gate_fingerprint("all commands passed") == "" and gate_mod.gate_fingerprint("") == "")

# ══════════════════════════════════════════════════════════════════════════════
# 2. scan_diff_for_secrets
# ══════════════════════════════════════════════════════════════════════════════
# Fixtures are CONCATENATION-BUILT so the credential patterns never appear contiguously in this
# source file — otherwise any future self-ticket whose diff adds these lines would red-gate
# itself on the very scanner they test (2026-07-06 review).
RAW_KEY = "sk-" + "a1B2c3D4e5F6g7H8i9J0" + "k1L2m3N4"
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
DIRTY_DIFF = ("diff --git a/x.py b/x.py\n+++ b/x.py\n"
              f"+ANTHROPIC_KEY = \"{RAW_KEY}\"\n"
              f"+{AWS_KEY} is an aws id\n"
              f"-old_removed = \"{RAW_KEY}\"\n"
              " context_line = 1\n")
hits = gate_mod.scan_diff_for_secrets(DIRTY_DIFF)
chk("secrets: added sk- key + AWS id detected", len(hits) >= 2, str(hits))
chk("secrets: raw token never printed (masked)", all(RAW_KEY not in h for h in hits), str(hits))
chk("secrets: removed/context lines ignored, clean diff → []",
    gate_mod.scan_diff_for_secrets("+harmless = 1\n-junk\n") == []
    and gate_mod.scan_diff_for_secrets("") == [])
chk("secrets: the gate:allow-secret pragma exempts fixture lines",
    gate_mod.scan_diff_for_secrets(f"+key = \"{RAW_KEY}\"  # gate:allow-secret\n") == [])

# ══════════════════════════════════════════════════════════════════════════════
# 3. lockfile_sanity
# ══════════════════════════════════════════════════════════════════════════════
repo = Path(tempfile.mkdtemp())
(repo / "package.json").write_text("{}", encoding="utf-8")
(repo / "bun.lock").write_text("", encoding="utf-8")
chk("lockfile: manifest changed, existing lockfile didn't → flagged",
    gate_mod.lockfile_sanity(["package.json"], str(repo)) != [])
chk("lockfile: manifest + lockfile both changed → clean",
    gate_mod.lockfile_sanity(["package.json", "bun.lock"], str(repo)) == [])
chk("lockfile: repo without a lockfile → never flagged",
    gate_mod.lockfile_sanity(["pyproject.toml"], str(repo)) == [])
sub = repo / "apps" / "web"
sub.mkdir(parents=True)
(sub / "package.json").write_text("{}", encoding="utf-8")
(sub / "package-lock.json").write_text("{}", encoding="utf-8")
chk("lockfile: nested manifest resolved against its own directory",
    gate_mod.lockfile_sanity(["apps/web/package.json"], str(repo)) != []
    and gate_mod.lockfile_sanity(["apps/web/package.json", "apps/web/package-lock.json"], str(repo)) == [])

# ══════════════════════════════════════════════════════════════════════════════
# 4. run_deterministic_checks
# ══════════════════════════════════════════════════════════════════════════════
_lint_app = AppConfig(name="a", repo_path=str(repo), backlog_backend="none",
                      lint_commands=["exit 3"], gate_timeout_sec=30)
det = gate_mod.run_deterministic_checks(_lint_app, ["package.json"], DIRTY_DIFF)
chk("deterministic: lint + secrets + lockfile aggregate into one failing report",
    not det.passed and "LINT" in det.report and "SECRET-LEAK" in det.report
    and "LOCKFILE" in det.report, det.report[:200])
_clean_app = AppConfig(name="a", repo_path=str(repo), backlog_backend="none")
chk("deterministic: all-clean passes",
    gate_mod.run_deterministic_checks(_clean_app, ["src/ok.py"], "+x = 1\n").passed)

# ══════════════════════════════════════════════════════════════════════════════
# 5. base_gate_check — per-sha cache
# ══════════════════════════════════════════════════════════════════════════════
cache_dir = Path(tempfile.mkdtemp())
_cfg_cache = Config(apps=[], audit_path=str(cache_dir / "audit.jsonl"), use_worktree=False)
_bapp = AppConfig(name="a", repo_path="/repo/x", backlog_backend="none",
                  gate_commands=["true"])


class _ShaGit:
    def __init__(self, sha): self._sha = sha
    def current_sha(self): return self._sha


calls = {"n": 0}


def _red_runner(app, changed=None, **_):
    calls["n"] += 1
    return GateResult(passed=False, report=REPORT_A1)


ok1, fp1, rep1 = gate_mod.base_gate_check(_bapp, _cfg_cache, _ShaGit("abc123"), runner=_red_runner)
ok2, fp2, rep2 = gate_mod.base_gate_check(_bapp, _cfg_cache, _ShaGit("abc123"), runner=_red_runner)
chk("base gate: red base detected with a fingerprint (after a confirmation re-run)",
    ok1 is False and fp1 == fp_a1 and calls["n"] == 2, f"{ok1},{fp1},calls={calls['n']}")
chk("base gate: second ticket on the same sha hits the cache (suite not re-run)",
    calls["n"] == 2 and (ok2, fp2) == (ok1, fp1), f"runner calls={calls['n']}")
ok3, _, _ = gate_mod.base_gate_check(_bapp, _cfg_cache, _ShaGit("def456"), runner=_red_runner)
chk("base gate: a NEW sha re-runs the gate (red + confirm)", calls["n"] == 4,
    f"runner calls={calls['n']}")


class _NoShaGit:
    pass


calls["n"] = 0
gate_mod.base_gate_check(_bapp, _cfg_cache, _NoShaGit(), runner=_red_runner)
chk("base gate: git without current_sha still works (uncached; red + confirm)",
    calls["n"] == 2, f"runner calls={calls['n']}")

# Flake honesty: a red that does NOT reproduce on the confirmation re-run is treated as green
# (one timing flake must never park the whole queue) — and the GREEN verdict is what gets cached.
flake = {"n": 0}


def _flaky_runner(app, changed=None, **_):
    flake["n"] += 1
    if flake["n"] == 1:
        return GateResult(passed=False, report=REPORT_A1)
    return GateResult(passed=True, report="")


okf, fpf, _ = gate_mod.base_gate_check(_bapp, _cfg_cache, _ShaGit("flake01"), runner=_flaky_runner)
okf2, _, _ = gate_mod.base_gate_check(_bapp, _cfg_cache, _ShaGit("flake01"), runner=_flaky_runner)
chk("base gate: one flaky red → confirmation wins, verdict GREEN and cached",
    okf is True and fpf == "" and okf2 is True and flake["n"] == 2, f"{okf},{flake['n']}")

# A cached RED expires after _RED_BASE_RED_TTL_S and is re-verified (an environmental red —
# missing venv, loaded box — must self-heal without the base sha moving).
import json as _json
_cache_file = Path(_cfg_cache.audit_path).with_name("red_base_cache.json")
_data = _json.loads(_cache_file.read_text(encoding="utf-8"))
_data[f"{_bapp.repo_path}@abc123"]["ts"] = 1.0        # ancient red
_cache_file.write_text(_json.dumps(_data), encoding="utf-8")
calls["n"] = 0
gate_mod.base_gate_check(_bapp, _cfg_cache, _ShaGit("abc123"), runner=_red_runner)
chk("base gate: an EXPIRED cached red is re-verified, not served stale",
    calls["n"] == 2, f"runner calls={calls['n']}")

# §3.1+§3.3: the base check covers the base's own LINT too — a red lint base would otherwise
# burn every ticket's full pass budget at the deterministic stage.
_lint_base_app = AppConfig(name="a", repo_path="/repo/lint", backlog_backend="none",
                           gate_commands=["true"], lint_commands=["exit 3"], gate_timeout_sec=30)
okl, fpl, repl = gate_mod.base_gate_check(_lint_base_app, _cfg_cache, _ShaGit("lint01"),
                                          runner=lambda app, changed=None, **_: GateResult(passed=True, report=""))
chk("base gate: a red LINT base short-circuits too", okl is False and "LINT" in repl, repl[:120])

# ══════════════════════════════════════════════════════════════════════════════
# loop integration harness (run_cost/eu105 pattern)
# ══════════════════════════════════════════════════════════════════════════════
_aud_dir = Path(tempfile.mkdtemp())
_APP = AppConfig(name="automatixy", repo_path="/tmp", base_branch="DEV",
                 protected_branch="MAIN", backlog_backend="none", gate_commands=["true"])


def _ticket(tid: str) -> Ticket:
    return Ticket(id=tid, key=tid, summary="s", description="d",
                  acceptance_criteria=["one"], app="automatixy", ephemeral=True)


class _Audit:
    def __init__(self): self.events = []
    def record(self, e, **k): self.events.append({"event": e, **k})


class _Backlog:
    def set_status(self, *a, **k): pass
    def add_comment(self, *a, **k): pass


class _Git:
    """At-base by default (current == base): the red-base check's provable-base guard passes."""
    def __init__(self, diffs=None, head="base0001", base="base0001"):
        self._diffs = list(diffs or [])
        self._head, self._base = head, base
        self.default_diff = "diff --git a/x b/x\n+clean_line"
    def has_changes(self): return True
    def current_sha(self): return self._head
    def base_sha(self): return self._base
    def diff_against_base(self):
        return self._diffs[0] if self._diffs else self.default_diff
    def changed_paths(self): return ["orchestrator/loop.py"]


BUILD_CALLS = {"n": 0}


class _StubBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")

    @staticmethod
    async def build(req, app, cfg, audit=None, store=None, spec=None):
        BUILD_CALLS["n"] += 1
        if store is not None:
            store.put(BuildArtifact(files_changed=[], diff_digest="d",
                                    decisions=[], open_questions=[]))
        return BuildResult(ok=True, summary="built", cost_usd=0.1, num_turns=2)


class _StubTE:
    @staticmethod
    async def ensure_coverage(ticket, app, cfg, store=None, build_artifact=None):
        return TestEngineerResult(ok=True, coverage="all green", cost_usd=0.0)


class _StubReviewer:
    @staticmethod
    async def review(diff, ticket, app, cfg, iteration=1, store=None, build_artifact=None):
        if store is not None:
            store.put(ReviewVerdict(verdict=Verdict.PASS, blocking=[], notes=[]))
        return ReviewResult(verdict=Verdict.PASS, spec_met=True, cost_usd=0.1)


def _fake_land(tk, app, cfg, git, backlog, audit, branch, iteration, cost, build, review,
               security_block=None, coverage=""):
    return TicketReport(tk.id, Outcome.MERGED, iteration, cost, app.name, branch)


_orig = (loop.builder_mod, loop.test_engineer_mod, loop.reviewer_mod,
         loop.run_gate, loop._land, loop._notify)
loop.builder_mod = _StubBuilder
loop.test_engineer_mod = _StubTE
loop.reviewer_mod = _StubReviewer
loop._land = _fake_land
loop._notify = lambda c, t: None


def _mkcfg(**kw) -> Config:
    d = Path(tempfile.mkdtemp())
    base = dict(apps=[_APP], audit_path=str(d / "audit.jsonl"), use_worktree=False,
                security_gate=False, pm_enabled=False, test_gate=False, max_iterations=2)
    base.update(kw)
    return Config(**base)


def _attempt(cfg, git, audit, tid="EU-1"):
    return asyncio.run(loop._attempt(_ticket(tid), _APP, cfg, git, _Backlog(), audit,
                                     loop.Budget(0), f"autodev/{tid}"))


try:
    # ── 6. red-base short-circuit ────────────────────────────────────────────
    loop.run_gate = _red_runner            # base gate (and any pass gate) is red
    calls["n"] = 0
    BUILD_CALLS["n"] = 0
    au = _Audit()
    rep = _attempt(_mkcfg(), _Git(), au, "EU-RB1")
    chk("red base: ticket ESCALATED before any build",
        rep.outcome == Outcome.ESCALATED and "red base" in (rep.notes or ""),
        f"{rep.outcome},{rep.notes}")
    chk("red base: builder never invoked", BUILD_CALLS["n"] == 0, str(BUILD_CALLS))
    chk("red base: red_base_block audited with the fingerprint",
        any(e["event"] == "red_base_block" and e.get("fingerprint") == fp_a1 for e in au.events),
        str([e["event"] for e in au.events]))

    # red_base_check=False → old behaviour (build proceeds; everything green → MERGED)
    loop.run_gate = lambda app, changed=None, **_: GateResult(passed=True, report="")
    BUILD_CALLS["n"] = 0
    rep2 = _attempt(_mkcfg(red_base_check=False), _Git(), _Audit(), "EU-RB2")
    chk("red_base_check=False: build proceeds and lands",
        rep2.outcome == Outcome.MERGED and BUILD_CALLS["n"] == 1,
        f"{rep2.outcome},{BUILD_CALLS}")

    # NOT provably at base (resumed WIP feature branch: HEAD ≠ base) → the check is SKIPPED —
    # the 2026-07-06 review's HIGH: gating the ticket's own red WIP as 'the base' permanently
    # deadlocked every requeue in non-isolated mode.
    loop.run_gate = _red_runner
    BUILD_CALLS["n"] = 0
    au_wip = _Audit()
    rep_wip = _attempt(_mkcfg(), _Git(head="wipsha99", base="base0001"), au_wip, "EU-WIP1")
    chk("resumed WIP branch (HEAD≠base): red-base check skipped — the build still runs",
        BUILD_CALLS["n"] >= 1
        and not any(e["event"] == "red_base_block" for e in au_wip.events)
        and "red base" not in (rep_wip.notes or ""),
        f"builds={BUILD_CALLS},notes={rep_wip.notes}")

    # Per-app-gated monorepo → skipped (its in-pass gate runs only the touched component; the
    # repo-wide fallback would block green-app tickets on a red sibling).
    _mono = AppConfig(name="automatixy", repo_path="/tmp", base_branch="DEV",
                      protected_branch="MAIN", backlog_backend="none",
                      gate_commands=["true"], gate_commands_by_app={"web": ["true"]})
    BUILD_CALLS["n"] = 0
    au_mono = _Audit()
    rep_mono = asyncio.run(loop._attempt(_ticket("EU-MONO1"), _mono, _mkcfg(), _Git(),
                                         _Backlog(), au_mono, loop.Budget(0), "autodev/EU-MONO1"))
    chk("per-app-gated monorepo: red-base check skipped — the build still runs",
        BUILD_CALLS["n"] >= 1
        and not any(e["event"] == "red_base_block" for e in au_mono.events),
        f"builds={BUILD_CALLS}")

    # ── 7. identical-fingerprint escalation ─────────────────────────────────
    seq = {"n": 0}

    def _green_then_red(app, changed=None, **_):
        seq["n"] += 1
        if seq["n"] == 1:                      # the base check
            return GateResult(passed=True, report="")
        return GateResult(passed=False, report=REPORT_A1 if seq["n"] == 2 else REPORT_A2)

    loop.run_gate = _green_then_red
    BUILD_CALLS["n"] = 0
    au3 = _Audit()
    rep3 = _attempt(_mkcfg(pm_enabled=False), _Git(), au3, "EU-FP1")
    chk("stuck fingerprint: identical gate failure two gates running → parked via exhaustion "
        "(PM triage path kept — run_all only exposes harness granularity)",
        rep3.outcome == Outcome.ESCALATED, f"{rep3.outcome},{rep3.notes}")
    chk("stuck fingerprint: exactly two builds (no third identical pass anywhere)",
        BUILD_CALLS["n"] == 2, str(BUILD_CALLS))
    chk("stuck fingerprint: gate_fingerprint_stuck audited",
        any(e["event"] == "gate_fingerprint_stuck" for e in au3.events),
        str([e["event"] for e in au3.events]))

    # ── 8. deterministic stage bounces a dirty diff, clean retry lands ──────
    loop.run_gate = lambda app, changed=None, **_: GateResult(passed=True, report="")

    class _GitDirtyThenClean(_Git):
        def __init__(self):
            super().__init__()
            self.calls = 0
        def diff_against_base(self):
            # pass 1 carries a secret; once the deterministic gate bounced it, pass 2 is clean.
            return DIRTY_DIFF if BUILD_CALLS["n"] <= 1 else self.default_diff

    BUILD_CALLS["n"] = 0
    au4 = _Audit()
    rep4 = _attempt(_mkcfg(), _GitDirtyThenClean(), au4, "EU-DET1")
    det_events = [e for e in au4.events if e["event"] == "deterministic_gate"]
    chk("deterministic stage: dirty pass-1 diff bounced, clean pass-2 lands",
        rep4.outcome == Outcome.MERGED and BUILD_CALLS["n"] == 2, f"{rep4.outcome},{BUILD_CALLS}")
    chk("deterministic stage: audited failed then passed",
        [e["passed"] for e in det_events] == [False, True], str(det_events))
    chk("deterministic stage: the failure report masks the secret",
        all(RAW_KEY not in json.dumps(e) for e in det_events), "raw secret leaked into audit")
finally:
    (loop.builder_mod, loop.test_engineer_mod, loop.reviewer_mod,
     loop.run_gate, loop._land, loop._notify) = _orig

# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════
passed = [n for n, ok, _ in results if ok]
failed = [(n, d) for n, ok, d in results if not ok]
print(f"\ndeterministic_gates_test: {len(passed)}/{len(results)} passed")
for n, d in failed:
    print(f"  FAIL: {n}" + (f" — {d}" if d else ""))
sys.exit(1 if failed else 0)
