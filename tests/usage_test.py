"""Cost governor v2 QA: the token ledger records every call, rolls up today/7d/30d, enforces a daily
budget (auto-pause + 80% alert), and the cockpit /usage page renders — all best-effort, never crashing."""
import sys, types, tempfile, time, json
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class ClaudeAgentOptions:
    def __init__(self, **kw): self.__dict__.update(kw)
sdk.ClaudeAgentOptions = ClaudeAgentOptions
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import usage
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"
usage.configure(str(audit))

# --- ledger records + rolls up ---
usage.record("claude-opus-4-8", 1000, 200, 0.0, "builder")
usage.record("claude-sonnet-4-6", 500, 100, 0.0, "the-general")
usage.record("claude-haiku-4-5-20251001", 50, 10, 0.0, "smalltalk")
led = usage._path()
chk("ledger file created", led.exists())
today = usage.rollup(None, usage._day_start())
chk("today totals sum input+output", today["total"] == 1000 + 200 + 500 + 100 + 50 + 10, str(today["total"]))
chk("today counts all calls", today["calls"] == 3, str(today["calls"]))
chk("per-model breakdown keyed by short name",
    set(today["by_model"]) == {"opus", "sonnet", "haiku"}, str(set(today["by_model"])))
chk("today_tokens helper agrees", usage.today_tokens(None) == today["total"])

# --- windows: today / 7d / 30d, with an old line excluded from 'today' ---
old = {"t": time.time() - 10 * 86400, "m": "opus", "i": 9999, "o": 1, "c": 0.0, "g": "old"}
with led.open("a") as f:
    f.write(json.dumps(old) + "\n")
w = usage.windows(None)
chk("10-day-old burn NOT in today", w["today"]["total"] == 1860, str(w["today"]["total"]))
chk("10-day-old burn IS in last-30d", w["month"]["total"] == 1860 + 10000, str(w["month"]["total"]))
chk("10-day-old burn NOT in last-7d", w["week"]["total"] == 1860, str(w["week"]["total"]))

# --- F4: the daily budget is ARMED BY DEFAULT (no config.yaml override needed) ---
cfg_default = Config(apps=[], audit_path=str(audit))
chk("daily_token_budget default is a real positive ceiling", cfg_default.daily_token_budget > 0,
    str(cfg_default.daily_token_budget))
chk("budget_status 'on' by default (AC: budget_status(cfg)['on'] == True)",
    usage.budget_status(cfg_default)["on"] is True)

# --- budget status: off / under / alert / over ---
cfg_off = Config(apps=[], audit_path=str(audit), daily_token_budget=0)
chk("budget off when ceiling 0", usage.budget_status(cfg_off)["on"] is False and not usage.over_budget(cfg_off))
# F4 regression: ceiling 0 must NEVER read as over, no matter how big the burn (the original bug:
# daily_token_budget: 0 ⇒ over_budget always False ⇒ autopilot never pauses on a runaway).
chk("ceiling 0 never trips over_budget even on huge burn",
    usage.over_budget(Config(apps=[], audit_path=str(audit), daily_token_budget=0)) is False)

cfg = Config(apps=[], audit_path=str(audit), daily_token_budget=10000, budget_alert_pct=0.8)
st = usage.budget_status(cfg)   # today = 1860 of 10000 -> 18.6%
chk("under budget: not over, not alerting", not st["over"] and not st["alert"], str(st["pct"]))

# push today's burn past 80% then past 100%
usage.record("claude-opus-4-8", 7000, 0, 0.0, "builder")   # today now 8860 -> 88.6%
st2 = usage.budget_status(cfg)
chk("alert fires past 80%", st2["alert"] and not st2["over"], str(st2["pct"]))
usage.record("claude-opus-4-8", 3000, 0, 0.0, "builder")   # today now 11860 -> over
chk("over fires past 100%", usage.over_budget(cfg))

# --- record() no-ops when unconfigured (never raises) ---
usage._PATH = None
try:
    usage.record("m", 1, 1, 0.0, "x")
    chk("record() is a safe no-op when unconfigured", True)
except Exception as e:  # noqa: BLE001
    chk("record() is a safe no-op when unconfigured", False, str(e))
usage.configure(str(audit))

# --- cockpit /usage route renders ---
from orchestrator import server
repo = tmp / "app"; repo.mkdir()
scfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(repo), base_branch="DEV",
                              protected_branch="MAIN", backlog_backend="none")],
              audit_path=str(audit), use_worktree=False, daily_token_budget=10000)
scfg.detected_auth = lambda: "test"
client = server.create_app(scfg).test_client()
r = client.get("/usage"); body = r.get_data(as_text=True)
chk("/usage returns 200", r.status_code == 200, str(r.status_code))
chk("/usage shows the three windows", "Today" in body and "Last 7 days" in body and "Last 30 days" in body)
chk("/usage shows the daily budget bar", "Daily budget" in body and "tokens" in body)
chk("/usage shows a per-model breakdown", "opus" in body and "model" in body)
chk("/usage shows the model-ladder readout (measures the economy change)", "Model ladder today" in body)

# --- code_mix: the model-ladder readout (builder / reviewer / soldiers by tier) ---
mixfresh = Path(tempfile.mkdtemp()) / "audit.jsonl"
usage.configure(str(mixfresh))
usage.record("claude-sonnet-4-6", 100, 10, 0.0, "builder")            # cheap first-pass build
usage.record("claude-opus-4-8", 100, 10, 0.0, "builder")              # escalated build
usage.record("claude-sonnet-4-6", 100, 10, 0.0, "soldier·vanguard-fe")
usage.record("claude-opus-4-8", 100, 10, 0.0, "reviewer")
usage.record("claude-sonnet-4-6", 100, 10, 0.0, "the-general")        # NOT code → excluded
mx = usage.code_mix(None, usage._day_start())
chk("code_mix counts only code work (excludes the General)", mx["total"] == 4, str(mx["total"]))
chk("code_mix tiers: 2 sonnet, 2 opus", mx["by_tier"]["sonnet"] == 2 and mx["by_tier"]["opus"] == 2)
chk("code_mix cheap_pct = Sonnet/Haiku share", abs(mx["cheap_pct"] - 0.5) < 1e-9)
mx0 = usage.code_mix(None, usage._day_start() + 10 ** 12)             # window in the future → empty
chk("code_mix on an empty window → zeroes, no divide-by-zero", mx0["total"] == 0 and mx0["cheap_pct"] == 0.0)
usage.configure(str(audit))   # restore the suite's ledger

# --- agent.run_agent records to the ledger from its one choke-point ---
import asyncio
from orchestrator import agent
fresh = Path(tempfile.mkdtemp()) / "audit.jsonl"   # own dir → its own usage_ledger.jsonl
usage.configure(str(fresh))
class _AM:  # distinct stand-ins so isinstance routes Result vs Assistant correctly
    def __init__(s, **k): s.__dict__.update(k)
class _RM:
    def __init__(s, **k): s.__dict__.update(k)
async def fake_query(prompt, options):
    yield _RM(total_cost_usd=0.0, num_turns=1, is_error=False, result="done",
              usage={"input_tokens": 800, "output_tokens": 120, "cache_read_input_tokens": 200})
agent.query = lambda prompt, options: fake_query(prompt, options)
agent.ResultMessage = _RM
agent.AssistantMessage = _AM
async def _go():
    return await agent.run_agent("hi", ClaudeAgentOptions(model="claude-sonnet-4-6"), tag="t")
run = asyncio.run(_go())
chk("run_agent captured input tokens (incl cache)", run.input_tokens == 800 + 200, str(run.input_tokens))
chk("run_agent captured output tokens", run.output_tokens == 120, str(run.output_tokens))
chk("run_agent wrote one ledger line", usage.rollup(None, 0)["total"] == 800 + 200 + 120,
    str(usage.rollup(None, 0)["total"]))

# --- F4 dry verification: with a low temporary cap the autopilot logs budget_pause and HOLDS new
#     tickets (never reaches intake); raising the cap above the burn lets it resume past the gate. ---
from orchestrator import autopilot
ap_dir = Path(tempfile.mkdtemp())
ap_audit = ap_dir / "audit.jsonl"
usage.configure(str(ap_audit))                 # ledger → ap_dir/usage_ledger.jsonl
usage.record("claude-opus-4-8", 5000, 5000, 0.0, "builder")   # today = 10000 tokens

intake_calls = {"n": 0}
autopilot.notify.configured = lambda: False    # no Telegram / decisions thread in the test
autopilot.notify.send = lambda *a, **k: None
def _fake_drain(cfg, app_name, n):
    intake_calls["n"] += 1
    return []                                   # empty worklist → "queue clear" → once-break
autopilot.intake.from_drain = _fake_drain

# (1) OVER budget: cap below today's 10000 burn → pause, hold tickets (intake never called).
over_cfg = Config(apps=[], audit_path=str(ap_audit), daily_token_budget=100, dry_run=False)
asyncio.run(autopilot.autopilot(over_cfg, once=True))
ap_log = ap_audit.read_text(encoding="utf-8") if ap_audit.exists() else ""
chk("autopilot logs budget_pause when over the ceiling", '"budget_pause"' in ap_log)
chk("autopilot HOLDS new tickets when paused (intake not reached)", intake_calls["n"] == 0)

# (2) Raise the cap above the burn → resumes past the gate and pulls the worklist.
under_cfg = Config(apps=[], audit_path=str(ap_audit), daily_token_budget=10_000_000, dry_run=False)
asyncio.run(autopilot.autopilot(under_cfg, once=True))
chk("autopilot RESUMES once the ceiling is raised (intake reached)", intake_calls["n"] == 1)

print("\n=============== COST GOVERNOR v2 QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
