"""Automode QA: with automode ON the PM never escalates — an ESCALATE (or unclear) reply is coerced to
DECIDE so the unit keeps building, the PM is told to decide, and the call is logged for async review.
With automode OFF behaviour is unchanged (escalate = wait for the Commander)."""
import sys, types, tempfile, asyncio
from pathlib import Path

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

ESC = "I recommend splitting billing into four routes; this is a pricing-semantics call.\nPM VERDICT: ESCALATE"
DEC = "Group them under one parent route — reversible, matches the nav.\nPM VERDICT: DECIDE"
UNCLEAR = "Some musings with no verdict line at all."

# --- parse_verdict: normal (fail-safe to ESCALATE) ---
chk("normal: ESCALATE stays ESCALATE", pm.parse_verdict(ESC)["verdict"] == "ESCALATE")
chk("normal: DECIDE stays DECIDE", pm.parse_verdict(DEC)["verdict"] == "DECIDE")
chk("normal: unclear -> ESCALATE (fail-safe)", pm.parse_verdict(UNCLEAR)["verdict"] == "ESCALATE")

# --- parse_verdict: automode coerces to DECIDE, keeps the body as the decision ---
a_esc = pm.parse_verdict(ESC, auto_mode=True)
chk("automode: ESCALATE -> DECIDE", a_esc["verdict"] == "DECIDE", a_esc["verdict"])
chk("automode: the recommendation becomes the decision body", "four routes" in a_esc["body"])
chk("automode: unclear -> DECIDE (never wait)", pm.parse_verdict(UNCLEAR, auto_mode=True)["verdict"] == "DECIDE")
chk("automode: a real DECIDE stays DECIDE", pm.parse_verdict(DEC, auto_mode=True)["verdict"] == "DECIDE")

# --- review() threads automode: prompt gets the AUTOMODE addendum, verdict comes back DECIDE ---
captured = {}
async def fake_run_officer(**kw):
    captured["system"] = kw.get("system", "")
    return ESC   # the PM "wanted" to escalate
recon.run_officer = fake_run_officer

app = AppConfig(name="automatixy", repo_path="/tmp", base_branch="DEV", protected_branch="MAIN", backlog_backend="none")
cfg_on = Config(apps=[app], audit_path="/tmp/x.jsonl", auto_mode=True)
cfg_off = Config(apps=[app], audit_path="/tmp/x.jsonl", auto_mode=False)

r_on = asyncio.run(pm.review(cfg_on, "automatixy", "AUTO-14", question="billing routes?"))
chk("review automode ON -> verdict DECIDE (no Commander wait)", r_on["verdict"] == "DECIDE", r_on["verdict"])
chk("review automode ON -> PM prompt carries the AUTOMODE directive", "AUTOMODE IS ON" in captured["system"])

r_off = asyncio.run(pm.review(cfg_off, "automatixy", "AUTO-14", question="billing routes?"))
chk("review automode OFF -> verdict ESCALATE (unchanged)", r_off["verdict"] == "ESCALATE", r_off["verdict"])
chk("review automode OFF -> no AUTOMODE directive in prompt", "AUTOMODE IS ON" not in captured["system"])

# --- config default is OFF (opt-in; out of the box the unit still asks) ---
chk("auto_mode defaults OFF", Config(apps=[], audit_path="/tmp/x.jsonl").auto_mode is False)

# --- the floors are NOT the PM gate: parse_verdict only touches the product call, nothing else ---
chk("automode does not invent a verdict from empty", pm.parse_verdict("", auto_mode=True)["verdict"] == "DECIDE")
chk("body falls back cleanly when empty", "(the PM gave no detail)" in pm.parse_verdict("", auto_mode=True)["body"])

print("\n================== AUTOMODE QA ==================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
