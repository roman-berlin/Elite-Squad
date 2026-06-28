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
# Explicit ESCALATE WITH the mandatory WHY line → preserved as ESCALATE; why captured.
_esc_text = ("WHY PM CANNOT RESOLVE: Only the Commander holds the billing-tier contract.\n"
             "Recommend X; critical.\nPM VERDICT: ESCALATE")
_esc = pm.parse_verdict(_esc_text)
chk("ESCALATE parsed (with WHY)", _esc["verdict"] == "ESCALATE")
chk("ESCALATE captures why text", "Commander" in _esc.get("why", ""), str(_esc.get("why")))
# Explicit ESCALATE WITHOUT the WHY line → the escalation is PRESERVED (a genuine can't-decide is
# never silently auto-decided away); the missing line is logged as possible noise. The PM *prompt*
# is what enforces the WHY line — parse-time coercion would only bury the critical calls this surfaces.
_esc_no_why = pm.parse_verdict("Recommend X; critical.\nPM VERDICT: ESCALATE")
chk("ESCALATE without WHY -> still ESCALATE (never silently auto-decided)", _esc_no_why["verdict"] == "ESCALATE")
chk("why absent when the WHY line is missing", "why" not in _esc_no_why)
# Unclear reply → ESCALATE fail-safe (no WHY enforcement on the fallback path).
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

# --- escalate path (with mandatory WHY line) ---
async def fake_esc(**kw):
    return ("WHY PM CANNOT RESOLVE: Only the Commander can approve a tier rename that changes billing copy.\n"
            "Recommend: keep 'Premium' label for now.\nPM VERDICT: ESCALATE")
recon.run_officer = fake_esc
r2 = asyncio.run(pm.review(cfg, "automatixy", "AUTO-14", "rename Premium tier?"))
chk("review parses ESCALATE on a critical call", r2["verdict"] == "ESCALATE" and "Premium" in r2["body"], str(r2))
chk("review captures why on ESCALATE", "Commander" in r2.get("why", ""), str(r2.get("why")))

print("\n================ PRODUCT MANAGER OFFICER QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
