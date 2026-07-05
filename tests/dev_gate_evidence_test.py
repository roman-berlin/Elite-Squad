"""Dev-gate evidence pin — a red post-merge gate must leave diagnosable evidence.

Evidence (EU-139 run, 2026-07-05 22:33): the post-merge dev gate failed in `loop._land`
(`reason = "dev gate fails after merge"`) and the run recorded ONLY the terse `pr_opened` audit
event; the failing harness names appeared nowhere — not in state/audit.jsonl, not in the fallback
PR's body — so diagnosing the (false-red) failure meant re-running the whole suite.

This harness drives `loop._land` (fakes, no git/network — the eu51 pattern) and pins:

  1. a red gate records a `dev_gate` audit event {ticket_id, passed=False, failing, report_tail}
     BEFORE the pr_opened event, with the failing-harness NAMES parsed from the gate report;
  2. the fallback PR body carries the same evidence (section heading + names + report tail);
  3. the shapes stay additive: a green gate records dev_gate passed=True with an empty failure
     payload; an unclean merge (gate never ran) records NO dev_gate event and keeps the PR body
     free of the gate section; pr_opened keeps its pre-existing reason/pr_url fields;
  4. dry-run keeps the evidence too (the gate genuinely ran on the trial merge);
  5. `_gate_failures` parses `✗ <name>` lines + the `FAILED: a b` summary, de-dupes, caps at 12,
     and returns [] for reports whose runner doesn't name its failures.
"""
import sys, types, tempfile
from pathlib import Path
from types import SimpleNamespace

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import loop
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, Outcome

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
app = AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev",
                protected_branch="main", backlog_backend="none")  # no postmerge_commands -> SRE skipped
live = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), dry_run=False)
dry = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), dry_run=True)
tkt = Ticket(id="EU-139", key="EU-139", summary="gate evidence", description="")

# A gate report shaped like run_commands() wrapping a red tests/run_all.py: per-harness ✗ lines
# plus the trailing `FAILED: …` summary naming the same two harnesses (the parser must de-dupe).
RED_REPORT = """$ .venv/bin/python3 tests/run_all.py
(exit 1)
  ✓ eu41_changelog_test.py           12/12 passed
  ✗ cli_entry_smoke_test.py          soft-tally FAIL: only 3/4 of its own checks passed (harness exited 0)
  ✗ sync_guard_test.py               (crashed before a result line)
================================================================
  HARNESSES: 41 passed / 43     TOTAL CHECKS: 612
  FAILED: cli_entry_smoke_test.py sync_guard_test.py
================================================================"""


class _FakeBacklog:
    def set_status(self, *a, **k): pass
    def add_comment(self, *a, **k): pass
    def attach_pr(self, *a, **k): pass

class _FakeCommenter:
    def summarize_gate_event(self, *a, **k): return None
    def post_comment(self, *a, **k): pass

class _CapAudit:
    def __init__(self): self.events = []
    def record(self, event, **k): self.events.append((event, k))
    def of(self, event): return [k for e, k in self.events if e == event]

class _Git:            # clean trial merge; captures the PR the fallback opens
    def __init__(self): self.prs = []
    def commit_all(self, *a, **k): pass
    def trial_merge(self, *a, **k): return True
    def changed_paths(self, *a, **k): return []
    def current_sha(self, *a, **k): return "deadbeef"
    def land_trial(self, *a, **k): pass
    def abandon_trial(self, *a, **k): pass
    def delete_local_branch(self, *a, **k): pass
    def delete_remote_branch(self, *a, **k): pass
    def sync_main_base(self, *a, **k): return ""
    def push(self, *a, **k): pass
    def open_pr(self, branch, title, body):
        self.prs.append((title, body))
        return "https://example.test/pr/4"

class _GitUnclean(_Git):   # trial merge fails -> the gate must never run
    def trial_merge(self, *a, **k): return False


def land(cfg, git, audit, gate_result):
    loop.run_gate = lambda *a, **k: gate_result
    return loop._land(tkt, app, cfg, git, _FakeBacklog(), audit,
                      branch="autodev/EU-139", iteration=1, cost=0.0,
                      build=SimpleNamespace(summary="build did the thing"),
                      review=SimpleNamespace(summary="review confirmed it"),
                      commenter=_FakeCommenter())


orig_gate, orig_notify, orig_changelog = loop.run_gate, loop._notify, loop._record_changelog
loop._notify = lambda *a, **k: None
loop._record_changelog = lambda *a, **k: None
try:
    # 1) LIVE + red gate -> PR_OPENED with a dev_gate event carrying the failure detail.
    g1, a1 = _Git(), _CapAudit()
    rep1 = land(live, g1, a1, SimpleNamespace(passed=False, report=RED_REPORT))
    chk("red gate lands as PR_OPENED with the dev-gate reason",
        rep1.outcome == Outcome.PR_OPENED and rep1.notes == "dev gate fails after merge",
        f"{rep1.outcome} / {rep1.notes}")
    dg1 = a1.of("dev_gate")
    chk("red gate records exactly one dev_gate event", len(dg1) == 1, str(a1.events))
    ev = dg1[0] if dg1 else {}
    chk("dev_gate event carries ticket_id and passed=False",
        ev.get("ticket_id") == "EU-139" and ev.get("passed") is False, str(ev))
    chk("dev_gate.failing names BOTH failing harnesses, de-duped, in order",
        ev.get("failing") == ["cli_entry_smoke_test.py", "sync_guard_test.py"],
        str(ev.get("failing")))
    chk("dev_gate.report_tail carries the gate output tail",
        "soft-tally FAIL" in ev.get("report_tail", "") and "FAILED:" in ev.get("report_tail", ""),
        str(ev.get("report_tail"))[:200])
    names1 = [e for e, _ in a1.events]
    chk("dev_gate is recorded BEFORE pr_opened (evidence survives a PR-opening crash)",
        "pr_opened" in names1 and names1.index("dev_gate") < names1.index("pr_opened"), str(names1))
    po = a1.of("pr_opened")
    chk("pr_opened keeps its pre-existing shape (reason + pr_url — additive change)",
        po and po[0].get("reason") == "dev gate fails after merge"
        and po[0].get("pr_url") == "https://example.test/pr/4", str(po))
    body1 = g1.prs[0][1] if g1.prs else ""
    chk("PR body gains the dev-gate failure section",
        "## Dev gate — FAILED after merge" in body1, body1[:300])
    chk("PR body names the failing harnesses",
        "cli_entry_smoke_test.py" in body1 and "sync_guard_test.py" in body1, body1[:300])
    chk("PR body carries the report tail", "soft-tally FAIL" in body1, body1[:300])
    chk("PR body still carries the reviewer summary (nothing dropped)",
        "## Reviewer summary" in body1 and "review confirmed it" in body1, body1[-300:])

    # 2) LIVE + green gate -> MERGED; dev_gate recorded with passed=True and an EMPTY failure payload.
    g2, a2 = _Git(), _CapAudit()
    rep2 = land(live, g2, a2, SimpleNamespace(passed=True, report="all commands passed"))
    dg2 = a2.of("dev_gate")
    chk("green gate still merges", rep2.outcome == Outcome.MERGED, str(rep2.outcome))
    chk("green gate records dev_gate passed=True with no failure payload",
        len(dg2) == 1 and dg2[0].get("passed") is True
        and dg2[0].get("failing") == [] and dg2[0].get("report_tail") == "", str(dg2))
    chk("green land opens no PR (unchanged behaviour)", g2.prs == [], str(g2.prs))

    # 3) LIVE + unclean merge -> the gate never runs: no dev_gate event, no gate section in the PR.
    calls = {"n": 0}
    def _counting_gate(*a, **k):
        calls["n"] += 1
        return SimpleNamespace(passed=True, report="")
    g3, a3 = _GitUnclean(), _CapAudit()
    loop.run_gate = _counting_gate
    rep3 = loop._land(tkt, app, live, g3, _FakeBacklog(), a3,
                      branch="autodev/EU-139", iteration=1, cost=0.0,
                      build=SimpleNamespace(summary="b"), review=SimpleNamespace(summary="r"),
                      commenter=_FakeCommenter())
    chk("unclean merge keeps its reason", rep3.outcome == Outcome.PR_OPENED
        and rep3.notes == "could not merge cleanly into dev", f"{rep3.outcome} / {rep3.notes}")
    chk("unclean merge never runs the gate (short-circuit preserved)", calls["n"] == 0, str(calls))
    chk("unclean merge records NO dev_gate event", a3.of("dev_gate") == [], str(a3.events))
    body3 = g3.prs[0][1] if g3.prs else ""
    chk("unclean-merge PR body has NO gate-failure section",
        "Dev gate — FAILED" not in body3, body3[:300])

    # 4) DRY-RUN + red gate -> still SKIPPED, but the dev_gate evidence is kept (the gate ran).
    g4, a4 = _Git(), _CapAudit()
    rep4 = land(dry, g4, a4, SimpleNamespace(passed=False, report=RED_REPORT))
    dg4 = a4.of("dev_gate")
    chk("dry-run red gate still reports SKIPPED", rep4.outcome == Outcome.SKIPPED, str(rep4.outcome))
    chk("dry-run red gate keeps the dev_gate evidence",
        len(dg4) == 1 and dg4[0].get("passed") is False
        and dg4[0].get("failing") == ["cli_entry_smoke_test.py", "sync_guard_test.py"], str(dg4))
finally:
    loop.run_gate, loop._notify, loop._record_changelog = orig_gate, orig_notify, orig_changelog

# 5) _gate_failures parser unit checks.
chk("parser: ✗ lines + FAILED: summary de-dupe to two ordered names",
    loop._gate_failures(RED_REPORT) == ["cli_entry_smoke_test.py", "sync_guard_test.py"],
    str(loop._gate_failures(RED_REPORT)))
chk("parser: empty report -> []", loop._gate_failures("") == [])
chk("parser: a runner that doesn't name failures -> [] (tail still carries the detail)",
    loop._gate_failures("$ bun vitest\n(exit 1)\nTypeError: boom\n  at foo.ts:3") == [])
many = "\n".join(f"  ✗ h{i:02d}_test.py  broke" for i in range(20))
chk("parser: caps at 12 names", len(loop._gate_failures(many)) == 12,
    str(len(loop._gate_failures(many))))

print("\n============ DEV-GATE EVIDENCE PIN (red gate leaves a diagnosable trail) ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
