"""Product Manager officer: decide/escalate parse + fail-safe + it recruits soldiers via the recon squad."""
import sys, types, asyncio
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import pm, recon
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- parse_verdict (pure) ---
chk("DECIDE parsed", pm.parse_verdict("Group billing under one parent.\nPM VERDICT: DECIDE")["verdict"] == "DECIDE")
chk("ESCALATE parsed", pm.parse_verdict("Recommend X; critical.\nPM VERDICT: ESCALATE")["verdict"] == "ESCALATE")
chk("unclear -> ESCALATE (fail-safe: ask the Commander)", pm.parse_verdict("hmm, not sure")["verdict"] == "ESCALATE")
d = pm.parse_verdict("The call: use 4 groups.\nPM VERDICT: DECIDE")
chk("body excludes the verdict line", "PM VERDICT" not in d["body"] and "4 groups" in d["body"])

cfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path="/tmp/x.jsonl", use_worktree=False)

# --- review() delegates through recon.run_officer (so the PM can field a squad) + parses ---
captured = {}
async def fake_run_officer(**kw):
    captured.update(kw)
    return "Decision: name the group 'Observe'.\nPM VERDICT: DECIDE"
recon.run_officer = fake_run_officer
r = asyncio.run(pm.review(cfg, "automatixy", "AUTO-14", "what to name the audit/flags section?"))
chk("review parses DECIDE", r["verdict"] == "DECIDE" and "Observe" in r["body"], str(r))
chk("PM goes through the recon squad path (officer=pm)", captured.get("officer") == "pm" and captured.get("label") == "Product Manager", str(captured.get("officer")))
chk("PM soldiers are read-only", captured.get("soldier_tools") == ["Read", "Grep", "Glob"], str(captured.get("soldier_tools")))

# --- escalate path ---
async def fake_esc(**kw):
    return "Recommend: keep 'Premium' label for now.\nWhy critical: changes billing semantics.\nPM VERDICT: ESCALATE"
recon.run_officer = fake_esc
r2 = asyncio.run(pm.review(cfg, "automatixy", "AUTO-14", "rename Premium tier?"))
chk("review parses ESCALATE on a critical call", r2["verdict"] == "ESCALATE" and "Premium" in r2["body"], str(r2))

print("\n================ PRODUCT MANAGER OFFICER QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
