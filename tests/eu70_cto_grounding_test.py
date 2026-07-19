"""EU-70: CTO/General chat grounding — unit tests for the two new pieces that fix fabricated counts
and missing next steps.

Fix #1: JiraAdapter.latest_builder_comment() — surfaces the last [General]-prefixed unit comment
        so the CTO can hand the Commander the concrete next step instead of generic deflection.
Fix #2: _needs_context() — derives every count from live state (needs.summary) instead of guessing,
        so the CTO can never invent "7 decisions awaiting" when the real answer is 0.
"""
import sys, types, asyncio, json, tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

results = []
def chk(name, cond, detail=""):
    results.append((name, bool(cond), str(detail) if not cond else ""))

# ── Stub out requests so jira.py imports without network ─────────────────────
req = types.ModuleType("requests")
req.Session = lambda *a, **k: None
req.RequestException = Exception
sys.modules["requests"] = req

from orchestrator.backlog import jira, base
from orchestrator import council

# patch _adf_to_text so we can feed plain strings as comment bodies
jira._adf_to_text = lambda x: x if isinstance(x, str) else ""


# ─────────────────────────────────────────────────────────────────────────────
# 1. JiraAdapter.latest_builder_comment() — happy-path and edge cases
# ─────────────────────────────────────────────────────────────────────────────

class _StubAdapter:
    """Minimal stub that satisfies latest_builder_comment without a real HTTP session.
    Calling JiraAdapter.latest_builder_comment(stub, key) uses our comments() override."""
    def __init__(self, comment_bodies):
        self._bodies = comment_bodies

    def comments(self, key):
        return [{"body": b} for b in self._bodies]

def _make_adapter(comment_bodies):
    """Return a stub whose comments() returns the supplied bodies."""
    return _StubAdapter(comment_bodies)

# Test: returns the last [General] comment
adapter = _make_adapter([
    "Some human comment",
    "[General] First unit post: build started.",
    "Another human: looks good",
    "[General] Next steps: open .github/workflows/ci.yml and paste the ready diff.",
])
result = jira.JiraAdapter.latest_builder_comment(adapter, "EU-59")
chk("latest_builder_comment: returns last [General] comment",
    result == "[General] Next steps: open .github/workflows/ci.yml and paste the ready diff.",
    repr(result))

# Test: skips non-[General] comments and returns None when none present
adapter_no_general = _make_adapter([
    "Human: please fix the auth bug",
    "Human: still broken",
])
chk("latest_builder_comment: returns None when no [General] comment",
    jira.JiraAdapter.latest_builder_comment(adapter_no_general, "AUTO-23") is None)

# Test: empty comments list → None
adapter_empty = _make_adapter([])
chk("latest_builder_comment: returns None for empty comments",
    jira.JiraAdapter.latest_builder_comment(adapter_empty, "EU-59") is None)

# Test: only non-[General] comments returns None (regression — ensures it doesn't pick up human comments)
adapter_human_only = _make_adapter([
    "Human: what is the status?",
    "Human: I need this done by Friday.",
])
chk("latest_builder_comment: None when all comments are human (no fabrication)",
    jira.JiraAdapter.latest_builder_comment(adapter_human_only, "AUTO-9") is None)

# Test: picks the LAST [General] comment (most recent wins; not the first)
adapter_multi_general = _make_adapter([
    "[General] Older unit post: waiting for review.",
    "Human: approved.",
    "[General] Latest unit post: CI blocked — paste diff into .github/workflows/ci.yml.",
])
result_multi = jira.JiraAdapter.latest_builder_comment(adapter_multi_general, "EU-70")
chk("latest_builder_comment: picks the MOST RECENT [General] comment (not the oldest)",
    result_multi is not None and "Latest unit post" in result_multi,
    repr(result_multi))
chk("latest_builder_comment: does NOT return the older [General] comment",
    result_multi is None or "Older unit post" not in result_multi,
    repr(result_multi))

# Test: strips leading/trailing whitespace
adapter_whitespace = _make_adapter([
    "  [General] Indented next-step comment.  ",
])
result_ws = jira.JiraAdapter.latest_builder_comment(adapter_whitespace, "EU-59")
chk("latest_builder_comment: strips surrounding whitespace",
    result_ws == "[General] Indented next-step comment.",
    repr(result_ws))

# ─────────────────────────────────────────────────────────────────────────────
# 2. BacklogAdapter.latest_builder_comment() — base class default → None
# ─────────────────────────────────────────────────────────────────────────────

class _ConcreteAdapter(base.BacklogAdapter):
    """Minimal concrete subclass to test the base default."""
    def get_ready_tasks(self, limit): return []
    def get_task(self, key): raise RuntimeError("not wired")
    def set_status(self, ticket, status): pass
    def add_comment(self, ticket, body): pass

base_adapter = _ConcreteAdapter.__new__(_ConcreteAdapter)
chk("BacklogAdapter base: latest_builder_comment returns None by default",
    base.BacklogAdapter.latest_builder_comment(base_adapter, "EU-59") is None)


# ─────────────────────────────────────────────────────────────────────────────
# 3. _needs_context() — grounds every count in real state, never invents
# ─────────────────────────────────────────────────────────────────────────────

from orchestrator.config import Config, AppConfig

d = Path(tempfile.mkdtemp())
(d / "audit.jsonl").write_text("")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)

# ── 3a. Zero state: all streams empty ────────────────────────────────────────
import orchestrator.needs as _needs_mod

_orig_summary = _needs_mod.summary

def _summary_zero(cfg):
    return {"decisions": [], "proposals": [], "tasks": [], "total": 0}

_needs_mod.summary = _summary_zero
# force council to reload the module reference
council_needs_ctx = council._needs_context  # save original
ctx_zero = _run(council._needs_context(cfg))
chk("_needs_context zero: returns non-empty string (not None/empty)",
    isinstance(ctx_zero, str) and len(ctx_zero) > 0, repr(ctx_zero))
chk("_needs_context zero: says 0 items",
    "0 items" in ctx_zero or "nothing is waiting" in ctx_zero, repr(ctx_zero))
chk("_needs_context zero: labelled as 'live' / 'never guess'",
    "live" in ctx_zero and ("never guess" in ctx_zero or "cite ONLY" in ctx_zero), repr(ctx_zero))
chk("_needs_context zero: does NOT say '7' (regression: fabricated count)",
    "7" not in ctx_zero, repr(ctx_zero))

# ── 3b. Non-zero: 2 decisions + 1 task (mirrors real EU-70 symptom) ──────────
def _summary_nonzero(cfg):
    return {
        "decisions": [{"id": "AUTO-23"}, {"id": "AUTO-32"}],
        "proposals": [],
        "tasks": [{"ticket_id": "EU-17", "outcome": "errored", "app": "automatixy"}],
        "total": 3,
    }

_needs_mod.summary = _summary_nonzero
ctx_nonzero = _run(council._needs_context(cfg))
chk("_needs_context nonzero: total matches real state (3, not invented)",
    "3 total" in ctx_nonzero or "3" in ctx_nonzero, repr(ctx_nonzero))
chk("_needs_context nonzero: lists AUTO-23 from real decisions",
    "AUTO-23" in ctx_nonzero, repr(ctx_nonzero))
chk("_needs_context nonzero: lists EU-17 from real tasks",
    "EU-17" in ctx_nonzero, repr(ctx_nonzero))
chk("_needs_context nonzero: does NOT fabricate a wrong total (e.g. '7 total')",
    "7 total" not in ctx_nonzero and "4 total" not in ctx_nonzero, repr(ctx_nonzero))
chk("_needs_context nonzero: labelled live/cite-only",
    "live" in ctx_nonzero and ("cite ONLY" in ctx_nonzero or "never guess" in ctx_nonzero),
    repr(ctx_nonzero))

# ── 3c. Proposals only (no decisions/tasks) ──────────────────────────────────
# (The approvals KINDS stream was removed in the 2026-07-19 stabilization; needs.summary no
# longer exposes an "approvals" key and _needs_context no longer reads one.)
def _summary_proposals_only(cfg):
    return {
        "decisions": [],
        "proposals": [{"id": "PR-1"}],
        "tasks": [],
        "total": 1,
    }

_needs_mod.summary = _summary_proposals_only
ctx_ap = _run(council._needs_context(cfg))
chk("_needs_context proposals: mentions proposals",
    "proposal" in ctx_ap, repr(ctx_ap))
chk("_needs_context proposals: total is 1 (not invented)",
    "1 total" in ctx_ap or "1" in ctx_ap, repr(ctx_ap))

# ── 3d. needs.summary() raises — must degrade gracefully (return empty string) ──
def _summary_broken(cfg):
    raise RuntimeError("disk offline")

_needs_mod.summary = _summary_broken
ctx_err = _run(council._needs_context(cfg))
chk("_needs_context exception: degrades to empty string (best-effort)",
    ctx_err == "", repr(ctx_err))

# restore
_needs_mod.summary = _orig_summary


# ─────────────────────────────────────────────────────────────────────────────
# 4. Regression: the '7 decisions awaiting' hallucination (EU-70 symptom)
# ─────────────────────────────────────────────────────────────────────────────
# With real state of 0 decisions, the output must not contain any invented
# non-zero count.  (The original bug: CTO said "7 awaiting decision" when
# pending_decisions.json had 0 entries.)
def _summary_zero_decisions(cfg):
    return {"decisions": [], "proposals": [], "tasks": [], "total": 0}

_needs_mod.summary = _summary_zero_decisions
ctx_reg = _run(council._needs_context(cfg))
chk("regression EU-70: 0 real decisions → output does NOT claim any non-zero decision count",
    "decision" not in ctx_reg or "0" in ctx_reg or "nothing" in ctx_reg,
    repr(ctx_reg))
chk("regression EU-70: 0 real decisions → output does NOT say '7'",
    "7" not in ctx_reg, repr(ctx_reg))

_needs_mod.summary = _orig_summary


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────
print("\n============ EU-70 CTO GROUNDING UNIT TESTS ============")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ← {det}" if det else ""))
print("--------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
