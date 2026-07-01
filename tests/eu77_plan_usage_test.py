"""EU-77 QA: the live Claude Max subscription-limit probe (plan_usage) + its brand-styled /usage panel.

The probe reads Claude Code's own `rate_limit_event` stream (session / weekly · all models / per-model),
caches it 5 min, is best-effort (never raises → falls back to the EU-75 own-ledger gauge), and fires ONLY
from a /usage render — never from the cockpit board. Every test stubs the CLI probe; no real `claude`
process is ever spawned."""
import sys, types, tempfile, time
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import usage, warroom, server
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

NOW = 1_000_000.0
INFOS = [
    {"status": "allowed", "resetsAt": NOW + 4 * 3600 + 12 * 60,
     "rateLimitType": "five_hour", "utilization": 0.12, "isUsingOverage": False},
    {"status": "allowed_warning", "resetsAt": NOW + 5 * 86400 + 4 * 3600,
     "rateLimitType": "seven_day", "utilization": 0.61, "isUsingOverage": False},
    {"status": "rejected", "resetsAt": NOW + 90,
     "rateLimitType": "seven_day_opus", "utilization": 0.99, "isUsingOverage": True},
]

def _stub(infos):
    usage._plan_cache.clear()
    usage._probe_plan_limits = lambda: list(infos)

# ── (a) happy path: probe parsed into ordered, branded, tone-mapped limit rows ──────────────────
_stub(INFOS)
pu = usage.plan_usage(now=NOW, force=True)
chk("available True when the probe returns data", pu.get("available") is True, str(pu))
lims = pu.get("limits", [])
chk("one row per emitted limit", len(lims) == 3, str(len(lims)))
chk("rows ordered session → weekly → per-model",
    [l["key"] for l in lims] == ["session", "weekly", "weekly_opus"], str([l["key"] for l in lims]))
by = {l["key"]: l for l in lims}
chk("session label is 'Current session'", by["session"]["label"] == "Current session")
chk("weekly label is 'Weekly · All models'", by["weekly"]["label"] == "Weekly · All models")
chk("per-model label is 'Weekly · Opus'", by["weekly_opus"]["label"] == "Weekly · Opus")
chk("utilisation → integer pct (0.61 → 61)", by["weekly"]["pct"] == 61, str(by["weekly"]["pct"]))
chk("session tone green (ok) at 12%", by["session"]["tone"] == "ok", by["session"]["tone"])
chk("weekly tone amber (warn) via allowed_warning status", by["weekly"]["tone"] == "warn", by["weekly"]["tone"])
chk("opus tone red (bad) via rejected status", by["weekly_opus"]["tone"] == "bad", by["weekly_opus"]["tone"])
chk("reset countdown 'd h' formatting", by["weekly"]["resets_in"] == "5d 4h", by["weekly"]["resets_in"])
chk("reset countdown 'h m' formatting", by["session"]["resets_in"] == "4h 12m", by["session"]["resets_in"])
chk("overage flag carried through", by["weekly_opus"]["overage"] is True)

# ── (b) empty probe → unavailable (so the cockpit shows the own-ledger fallback note) ────────────
_stub([])
pu_empty = usage.plan_usage(now=NOW, force=True)
chk("empty probe → available False", pu_empty.get("available") is False, str(pu_empty))
chk("empty probe carries a reason", bool(pu_empty.get("reason")))

# ── (c) probe failure is non-fatal (best-effort, never raises) ──────────────────────────────────
usage._plan_cache.clear()
def _boom():
    raise RuntimeError("claude exploded")
usage._probe_plan_limits = _boom
try:
    pu_err = usage.plan_usage(now=NOW, force=True)
    chk("probe exception never propagates", True)
except Exception as e:  # noqa: BLE001
    chk("probe exception never propagates", False, str(e))
chk("failed probe → available False with reason", pu_err.get("available") is False and "exploded" in pu_err.get("reason", ""))

# ── (d) 5-minute cache: a good read is reused; force re-probes; failures expire sooner ───────────
_stub(INFOS)
r1 = usage.plan_usage(now=NOW)                      # populates cache (available True)
usage._probe_plan_limits = _boom                    # next real probe would blow up
r2 = usage.plan_usage(now=NOW + 120)                # within 300s → served from cache, no re-probe
chk("good read cached for 5 min (no re-probe within TTL)", r2.get("available") is True, str(r2))
r3 = usage.plan_usage(now=NOW + 120, force=True)    # force bypasses cache → hits _boom → unavailable
chk("force=True bypasses the cache", r3.get("available") is False)
r4 = usage.plan_usage(now=NOW + 120 + 30)           # failed read cached only ~60s → still cached
chk("failed read cached briefly (no re-probe within 60s)", r4.get("available") is False)
usage._probe_plan_limits = lambda: list(INFOS)
r5 = usage.plan_usage(now=NOW + 120 + 75)           # past the 60s error TTL → re-probes → recovers
chk("failed read expires after ~60s and recovers", r5.get("available") is True, str(r5))

# ── (e) unit helpers: reset formatting + tone thresholds ────────────────────────────────────────
chk("_fmt_reset_in days", usage._fmt_reset_in(NOW + 2 * 86400 + 3 * 3600, NOW) == "2d 3h")
chk("_fmt_reset_in minutes", usage._fmt_reset_in(NOW + 9 * 60, NOW) == "9m")
chk("_fmt_reset_in sub-minute", usage._fmt_reset_in(NOW + 20, NOW) == "<1m")
chk("_fmt_reset_in past → 'now'", usage._fmt_reset_in(NOW - 5, NOW) == "now")
chk("_fmt_reset_in junk → ''", usage._fmt_reset_in(None, NOW) == "")
chk("_plan_tone green below 0.8", usage._plan_tone(0.5, "allowed") == "ok")
chk("_plan_tone amber at/above 0.8", usage._plan_tone(0.82, "allowed") == "warn")
chk("_plan_tone red at/above 0.95", usage._plan_tone(0.97, "allowed") == "bad")
chk("_plan_tone red on rejected status regardless of pct", usage._plan_tone(0.1, "rejected") == "bad")

# ── (f) the cockpit BOARD must never fire the probe (only /usage does) ───────────────────────────
tmp = Path(tempfile.mkdtemp()); audit = tmp / "audit.jsonl"
usage.configure(str(audit))
usage.record("claude-sonnet-4-6", 500, 100, 0.0, "builder")
usage._plan_cache.clear()
usage._probe_plan_limits = _boom                    # if the board probes, this raises inside its try
board_cfg = Config(apps=[], audit_path=str(audit), daily_token_budget=10_000)
cards = warroom.kpis(board_cfg, [], None)
labels = {c.get("label") for c in cards}
# EU-145: merged "Tokens today" + "Tokens this week" into a single "Tokens" card
chk("board renders 'Tokens' card (merged today + week)", "Tokens" in labels, str(labels))
# These are now expected to FAIL because we merged the cards
chk("board does NOT render separate 'Tokens today' card (EU-145 merged)", "Tokens today" not in labels, str(labels))
chk("board does NOT render separate 'Tokens this week' card (EU-145 merged)", "Tokens this week" not in labels, str(labels))

# ── (g) /usage route — available: brand panel with gauges, %s, resets, a11y progressbar ─────────
repo = tmp / "app"; repo.mkdir()
scfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(repo), base_branch="DEV",
                              protected_branch="MAIN", backlog_backend="none")],
              audit_path=str(audit), use_worktree=False, daily_token_budget=10_000)
scfg.detected_auth = lambda: "test"
client = server.create_app(scfg).test_client()

now = time.time()
_stub([
    {"status": "allowed", "resetsAt": now + 3 * 3600, "rateLimitType": "five_hour",
     "utilization": 0.12, "isUsingOverage": False},
    {"status": "allowed_warning", "resetsAt": now + 5 * 86400, "rateLimitType": "seven_day",
     "utilization": 0.61, "isUsingOverage": False},
    {"status": "rejected", "resetsAt": now + 5 * 86400, "rateLimitType": "seven_day_opus",
     "utilization": 0.99, "isUsingOverage": True},
])
body = client.get("/usage").get_data(as_text=True)
chk("panel title rendered", "Claude Max — subscription limits" in body)
chk("session row labelled 'Current session'", "Current session" in body)
chk("weekly row labelled 'Weekly · All models'", "Weekly · All models" in body)
chk("per-model row labelled 'Weekly · Opus'", "Weekly · Opus" in body)
chk("rows ordered session-before-weekly in the HTML",
    body.index("Current session") < body.index("Weekly · All models"))
chk("shows a percentage", "61%" in body)
chk("shows a reset countdown", "resets in" in body)
chk("green/amber/red fills all present (green→amber→red)",
    "pf g" in body and "pf a" in body and "pf r" in body)
chk("accessible progressbar markup", "role=progressbar" in body and "aria-valuenow" in body)

# ── (h) /usage route — unavailable: own-ledger fallback note, page otherwise intact ─────────────
_stub([])
body2 = client.get("/usage").get_data(as_text=True)
chk("fallback note when probe unavailable", "machine-readable" in body2.lower())
chk("page still renders the own-ledger windows under fallback",
    "Last 7 days" in body2 and "Daily budget" in body2)

# ── summary ──────────────────────────────────────────────────────────────────
print("\n============ EU-77 PLAN USAGE QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
