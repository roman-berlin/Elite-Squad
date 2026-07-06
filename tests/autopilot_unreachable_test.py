"""Autopilot idle-message QA: an empty worklist caused by an UNREACHABLE board (e.g. a dead
JIRA_API_TOKEN) must NOT be reported as 'queue clear — nothing of yours'. That misleading line is
exactly what hid EU-20..EU-36 (Jira answers an unauthenticated search with HTTP 200 + no issues, so
the drain comes back empty). When intake.LAST_DRAIN_ERRORS is populated, the autopilot must say the
backlog is HIDDEN/UNREACHABLE (and ping Telegram), once per idle stretch — not pretend it's clear."""
import sys, types, tempfile, asyncio, io, contextlib, threading
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import autopilot
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
# The real autopilot() runs below write their PID file. Keep it off the machine-global
# /tmp/general-autopilot.pid, which is shared with a live daemon and every other checkout's suite.
autopilot._PID_FILE = tmp / "general-autopilot.pid"
cfg = Config(apps=[], audit_path=str(tmp / "audit.jsonl"))

AUTH_MSG = ("Jira auth failed for app 'Elite-Unit' (X-Seraph-LoginReason=AUTHENTICATED_FAILED) - the "
            "API token is invalid/expired or its account can't access https://toibis.atlassian.net. "
            "Rotate JIRA_API_TOKEN")

autopilot.usage.budget_status = lambda c: {"over": False, "alert": False, "used": 0, "cap": 1, "pct": 0.0}
autopilot.intake.from_drain = lambda c, app, n: []          # empty worklist...
autopilot.intake.LAST_DRAIN_ERRORS.clear()
autopilot.intake.LAST_DRAIN_ERRORS["Elite-Unit"] = AUTH_MSG  # ...because the board was UNREACHABLE
async def _ac(c, reports, audit, blocked): return None
autopilot.notify.configured = lambda: False
sent = []
autopilot.notify.send = lambda *a, **k: sent.append(a[0] if a else "")

ev = threading.Event()
sleeps = {"n": 0}
def _sleep_stub(seconds, stop_event=None):
    sleeps["n"] += 1
    if sleeps["n"] >= 3:
        ev.set()
autopilot._sleep = _sleep_stub

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    asyncio.run(autopilot.autopilot(cfg, once=False, interval=1, stop_event=ev))
out = buf.getvalue()

chk("idled several cycles (>=3 sleeps)", sleeps["n"] >= 3, f"sleeps={sleeps['n']}")
chk("does NOT lie with 'queue clear' when a board is unreachable", "queue clear" not in out, out[:200])
chk("surfaces the board as UNREACHABLE / HIDDEN", "UNREACHABLE" in out and "HIDDEN" in out)
chk("names the failing board + the actionable fix", "Elite-Unit" in out and "Rotate JIRA_API_TOKEN" in out)
chk("announced ONCE per idle stretch (no spam)", out.count("UNREACHABLE") == 1, f"count={out.count('UNREACHABLE')}")
chk("pinged Telegram about the unreachable backlog", any("unreachable" in s.lower() for s in sent), str(sent)[:200])

# EU-51: the dark backlog must also leave a DURABLE trail in audit.jsonl (not just a print + notify),
# naming the unreachable board(s) — else a queue that went dark at 3am has zero forensic record.
import json
audit_rows = [json.loads(ln) for ln in Path(cfg.audit_path).read_text().splitlines() if ln.strip()]
bu = [r for r in audit_rows if r.get("event") == "backlog_unreachable"]
chk("recorded a 'backlog_unreachable' audit event", len(bu) == 1, f"rows={len(bu)}")
chk("the audit event names the dead board + the actionable fix",
    bool(bu) and "Elite-Unit" in bu[0].get("boards", {}) and "Rotate" in str(bu[0].get("boards", {})),
    str(bu[:1])[:200])
chk("did NOT record a 'queue_clear' while a board was unreachable",
    not any(r.get("event") == "queue_clear" for r in audit_rows))

# control: with NO drain errors the old honest 'queue clear' must still appear
autopilot.intake.LAST_DRAIN_ERRORS.clear()
ev2 = threading.Event(); sleeps["n"] = 0
def _sleep2(seconds, stop_event=None):
    sleeps["n"] += 1
    if sleeps["n"] >= 2:
        ev2.set()
autopilot._sleep = _sleep2
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    asyncio.run(autopilot.autopilot(cfg, once=False, interval=1, stop_event=ev2))
out2 = buf2.getvalue()
chk("a genuinely empty queue still says 'queue clear'", "queue clear" in out2 and "UNREACHABLE" not in out2)

# EU-51: the benign empty queue records 'queue_clear' (so the log distinguishes "genuinely clear" from
# "hidden behind a dead board"), distinct from the unreachable case.
rows2 = [json.loads(ln) for ln in Path(cfg.audit_path).read_text().splitlines() if ln.strip()]
chk("a genuinely empty queue records a 'queue_clear' audit event",
    any(r.get("event") == "queue_clear" for r in rows2))

print("\n============ AUTOPILOT UNREACHABLE-BACKLOG QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
