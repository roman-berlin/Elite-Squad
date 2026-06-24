"""EU-37: the Test Engineer runs after the Builder, before the Reviewer — it loads its officer
doctrine, owns the coverage artifact, and that artifact flows into the PR description."""
import sys, types, asyncio, pathlib

# Stub the Agent SDK (no network / real models) — same shim the other driver tests use.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import test_engineer as te
from orchestrator import agent as agent_mod
from orchestrator.agent import AgentRun
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, TestEngineerResult
import orchestrator.loop as loop

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ---- doctrine: loads officers/test-engineer.md (slice 1's prompt file) ----
doc = te.doctrine()
chk("doctrine loads the Test Engineer officer file", "Test Engineer" in doc and "coverage" in doc.lower())
chk("system_prompt wraps doctrine + the pipeline driver",
    "Test Engineer" in te.system_prompt() and "COVERAGE:" in te.system_prompt())

# ---- EU-37 iter-2: the runner is repo-derived, NOT hardcoded Bun (rejection point 1) ----
# The absolute Bun-only directive is gone from BOTH the officer doctrine and the pipeline driver
# (assert on doctrine()/_DRIVER directly — system_prompt() also carries the unit-memory preamble,
# whose Standing Orders legitimately say "Bun-only" about the automatixy product).
doc_l, drv = te.doctrine().lower(), te._DRIVER
chk("officer doctrine drops the absolute Bun-only / never-other-runner constraint",
    "bun-only" not in doc_l and "never npm/jest/vitest" not in doc_l)
chk("driver drops the absolute Bun-only / never-other-runner constraint",
    "Bun-only" not in drv and "never npm/jest/vitest" not in drv.lower())
chk("driver tells the officer to use THIS repo's CLAUDE.md test command",
    "CLAUDE.md" in drv)
chk("driver offers both the Python EU and the Bun runners as examples",
    "tests/run_all.py" in drv and "bun test --coverage" in drv)
chk("officer doctrine names the Python EU runner too (not just Bun)",
    "tests/run_all.py" in te.doctrine())

# ---- EU-37 iter-2: COVERAGE artifact is runner-shaped — a Python pass/fail line parses fine ----
chk("extract_coverage handles a non-Bun (Python pass/fail) artifact shape",
    te.extract_coverage("ran the suite\nCOVERAGE: 148/148 checks across 7 harnesses; "
                        "n/a (run_all.py reports no line coverage)")
    == "148/148 checks across 7 harnesses; n/a (run_all.py reports no line coverage)")

# ---- EU-37 iter-2: the post-TE re-gate comment is honest about what it catches (rejection pt 2) ----
loop_src = pathlib.Path(loop.__file__).read_text(encoding="utf-8")
loop_norm = " ".join(loop_src.split())
chk("post-TE re-gate comment drops the misleading 'cheap gate so a broken test is caught here' claim",
    "cheap gate so a broken test is caught here" not in loop_norm)
chk("post-TE re-gate comment ties the test signal to the app's configured gate_commands",
    "configured gate_commands" in loop_norm and "ONLY when" in loop_norm)

# ---- extract_coverage: parses the artifact line, tolerant of bold, keeps the LAST line ----
chk("extract_coverage parses a plain COVERAGE line",
    te.extract_coverage("did stuff\nCOVERAGE: lines 80%→88%, funcs 75%→90%, branches 60%→72%")
    == "lines 80%→88%, funcs 75%→90%, branches 60%→72%")
chk("extract_coverage tolerates bold markup",
    te.extract_coverage("**COVERAGE: n/a (config-only change)**") == "n/a (config-only change)")
chk("extract_coverage keeps the final summary line",
    te.extract_coverage("COVERAGE: draft\nmore text\nCOVERAGE: lines 50%→70%") == "lines 50%→70%")
chk("extract_coverage empty when no artifact line", te.extract_coverage("no coverage reported here") == "")

# ---- ensure_coverage: returns a TestEngineerResult with the parsed artifact (stubbed run_agent) ----
captured = {}
async def fake_run_agent(prompt, options, tag=""):
    captured["tag"] = tag
    captured["prompt"] = prompt
    return AgentRun(
        text="added happy-path + regression tests\nCOVERAGE: lines 82%→91%, funcs 78%→95%, branches 64%→80%",
        final="added happy-path + regression tests\nCOVERAGE: lines 82%→91%, funcs 78%→95%, branches 64%→80%",
        cost_usd=0.5, num_turns=4, is_error=False, tools=["Write tests/x.test.ts"])
agent_mod.run_agent = fake_run_agent
te.run_agent = fake_run_agent

def mkcfg(**kw):
    return Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path="/tmp/x.jsonl", use_worktree=False, **kw)
cfg = mkcfg()
app = cfg.app("automatixy")
ticket = Ticket(id="EU-37", key="EU-37", summary="Recruit Test Engineer",
                description="wire it in", acceptance_criteria=["coverage gate runs each ticket"])
res = asyncio.run(te.ensure_coverage(ticket, app, cfg))
chk("ensure_coverage returns a TestEngineerResult", isinstance(res, TestEngineerResult))
chk("ensure_coverage reports ok", res.ok is True)
chk("ensure_coverage surfaces the coverage artifact", res.coverage == "lines 82%→91%, funcs 78%→95%, branches 64%→80%")
chk("ensure_coverage tags the run 'test-engineer'", captured.get("tag") == "test-engineer")
chk("ensure_coverage feeds the ticket + acceptance criteria into the prompt",
    "EU-37" in captured.get("prompt", "") and "coverage gate runs each ticket" in captured.get("prompt", ""))

# ---- process error fails safe (ok=False, empty artifact) — pipeline keeps moving to review ----
async def err_run_agent(prompt, options, tag=""):
    return AgentRun(text="", final="", cost_usd=0.1, num_turns=1, is_error=True, tools=[])
te.run_agent = err_run_agent
agent_mod.run_agent = err_run_agent
res_err = asyncio.run(te.ensure_coverage(ticket, app, cfg))
chk("ensure_coverage fails safe on a process error", res_err.ok is False and res_err.coverage == "")

# ---- the coverage artifact flows into the PR description ----
class _Rev:
    summary = "looks good"
body = loop._pr_body(ticket, app, _Rev(), "lines 82%→91%, funcs 78%→95%, branches 64%→80%")
chk("PR body carries the coverage section", "## Coverage" in body and "lines 82%→91%" in body)
no_cov = loop._pr_body(ticket, app, _Rev(), "")
chk("PR body omits the coverage section when there's no artifact", "## Coverage" not in no_cov)

# ---- the config gate flag is armed by default ----
chk("test_gate is armed by default", mkcfg().test_gate is True)

print("\n============ TEST ENGINEER (EU-37) ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
