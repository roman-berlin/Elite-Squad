"""Officers must not spawn sub-agents (the AUTO-93 reviewer runaway, 2026-07-08).

Live incident: reviewing AUTO-93 (a trivial no-production-change, 2-test-file diff) the Reviewer ran
~75 minutes without converging. Root cause: officers run under permission_mode="bypassPermissions"
where disallowed_tools is the operative block, but they left the SDK sub-agent spawn tool ("Task";
the older Claude Code alias is "Agent") in NEITHER allowed_tools nor disallowed_tools. Each spawned
sub-agent burns its OWN turn budget OUTSIDE the officer's max_turns cap, so a review/plan/council pass
can fan out unbounded. It is a CLASS bug: the same profile lived at ~20 ClaudeAgentOptions sites.

The fix denies the sub-agent tool at every construction site (the central agent.py seam was avoided
to stay disjoint from concurrent EU-189 work). This test enforces the invariant STRUCTURALLY so a NEW
officer cannot silently re-open the gap:

  INVARIANT: EVERY ClaudeAgentOptions/_Opts construction (in orchestrator/*.py AND scripts/*.py) that
             sets permission_mode="bypassPermissions" must deny "Task" somewhere in that same
             construction block.

This is block-based (not a per-literal grep and not a hardcoded officer list), so it catches a brand
new bypassPermissions officer that forgot the deny-list — the exact failure mode that let three sites
slip the first pass. It also tolerates a DRY refactor (a deny constant that includes "Task" still
satisfies "Task in block"). "Task" is the load-bearing SDK tool name; "Agent" is kept as a belt.
"""
import sys, types, asyncio, tempfile, re, glob
from pathlib import Path

# ── Stub the Agent SDK so ClaudeAgentOptions(**k) captures kwargs ──
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.reviewer as reviewer_mod
from orchestrator import models
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Construction call spellings in the codebase: the direct name and squad.py's `as _Opts` alias.
_CALL_KEYS = ("ClaudeAgentOptions(", "_Opts(")

def _option_blocks(src):
    """Yield the text of each ClaudeAgentOptions(...) / _Opts(...) construction (paren-balanced)."""
    for key in _CALL_KEYS:
        idx = 0
        while True:
            i = src.find(key, idx)
            if i < 0:
                break
            j = i + len(key) - 1          # index of the opening '('
            depth = 0
            while j < len(src):
                if src[j] == "(":
                    depth += 1
                elif src[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            yield src[i:j + 1]
            idx = j + 1


# ============ INVARIANT: every bypassPermissions construction denies "Task" ============ #
_files = sorted(glob.glob("orchestrator/*.py") + glob.glob("scripts/*.py"))
_bypass_seen = 0
for path in _files:
    src = Path(path).read_text(encoding="utf-8")
    for k, block in enumerate(_option_blocks(src)):
        if 'permission_mode="bypassPermissions"' in block or "permission_mode='bypassPermissions'" in block:
            _bypass_seen += 1
            chk(f"{path}: bypassPermissions construction #{k} denies Task",
                '"Task"' in block or "'Task'" in block,
                block.replace(chr(10), " ")[:110])
# sanity: we actually found the population we expect to guard (guards against the scan silently going empty)
chk("scan found the bypassPermissions construction population (>=15)", _bypass_seen >= 15, f"seen={_bypass_seen}")


# ============ BEHAVIOUR: reviewer.review builds options that deny "Task" ============ #
class RR:
    def __init__(s, t):
        s.final = s.text = t
        s.is_error = False; s.cost_usd = 0.0; s.num_turns = 1; s.tools = []
        s.provider = "Anthropic"; s.model_version = "claude-sonnet-5"
        s.input_tokens = s.output_tokens = 0
        s.is_plan_limit = False; s.plan_limit_kind = ""

captured = {}
async def fake_run_agent(prompt, options, tag="", cfg=None, routing_tier=None, **kw):
    captured["options"] = options
    return RR('{"verdict": "PASS", "spec_conformance": {"met": true}, "quality": {"issues": []}}')

reviewer_mod.run_agent = fake_run_agent
reviewer_mod.run_agent_with_fallback = fake_run_agent
models.for_reviewer = lambda cfg, diff="", iteration=1: (models.SONNET, "sonnet")

d = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)
app = cfg.app("automatixy"); app.workdir = str(d)
tk = Ticket(id="AUTO-93", key="AUTO-93", summary="s", description="x", acceptance_criteria=["a"])
asyncio.run(reviewer_mod.review("diff --git a/x b/x\n+y", tk, app, cfg))
opts = captured.get("options")
disallowed = list(getattr(opts, "disallowed_tools", []) or [])
chk("reviewer options captured", opts is not None)
chk("reviewer.review options deny Task (load-bearing sub-agent tool)", "Task" in disallowed, f"disallowed={disallowed}")
for _t in ("Write", "Edit", "Bash"):
    chk(f"reviewer still denies {_t} (read-only preserved)", _t in disallowed, f"disallowed={disallowed}")

print("\n========== OFFICER SUB-AGENT DISALLOW (CLASS-BUG SWEEP) QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
