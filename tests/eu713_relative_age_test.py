"""EU-713 — Live relative timestamp on the result strip (reconciled scope).

PM decision on EU-713 (resolves the autosplit collision with EU-670/675): the parallel
/api/last-result poll and second strip were dropped as redundant — the server-rendered
strip already covers dismissible + tone-styled + present-when-result + no-reappear-after-
dismiss via POST /api/dismiss-result. The ONE net-new behaviour kept: the strip's
timestamp renders as a relative string that ages purely client-side.

Acceptance criteria covered:
  AC(a) _result_strip emits the record's timestamp as data-ts on an empty .result-age
        span; records without a usable timestamp (legacy writers) render no span.
  AC(b) Tone styling still varies after the markup change (error→--bad, ok→--ok,
        warn→not red) and the EU-675 dismiss hook is intact.
  AC(c) The board JS carries the client-side ticker: paints .result-age[data-ts],
        repaints inside applyBoard (SSE/poll swaps), refreshes on a 30s interval, and
        makes no endpoint calls for it (page never references /api/last-result).
  AC(d) Live round-trip: the served board carries data-ts → POST /api/dismiss-result →
        the next board render shows no strip and no data-ts (does not reappear).
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

# ── SDK / network stubs (same pattern as eu672/eu675 cockpit tests) ────────────
_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = _sdk

_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = _req

sys.path.insert(0, ".")

from orchestrator import cockpit_state, server, warroom  # noqa: E402
from orchestrator.cockpit_views import _result_strip  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    line = f"  [{tag}] {name}"
    if detail and not cond:
        line += f"  ({detail})"
    print(line)
    results.append((name, bool(cond), str(detail)))


print("\n================ EU-713 Relative-Age QA ================")

# --- AC(a): data-ts + .result-age span emitted from the record's timestamp ----
TS = 1_753_618_800.5
s = _result_strip({"last_result_record": {"tone": "ok", "text": "shipped", "timestamp": TS}})
chk("AC(a): strip carries an empty .result-age span",
    'class=result-age' in s and "result-age data-ts='" in s, s[:300])
chk("AC(a): data-ts holds the record's epoch seconds (ms precision)",
    "data-ts='1753618800.500'" in s, s[:300])
chk("AC(a): age span styled dim + nowrap (sits beside the message)",
    "color:var(--dim)" in s and "white-space:nowrap" in s, s[:300])

# Records without a usable timestamp render no span (legacy / partial writers)
s = _result_strip({"last_result_record": {"tone": "error", "text": "boom"}})
chk("AC(a): no timestamp → no .result-age span", "result-age" not in s, s[:300])
for bad_ts, label in ((None, "None"), ("garbage", "non-numeric"), (0, "zero")):
    s = _result_strip({"last_result_record": {"tone": "ok", "text": "x", "timestamp": bad_ts}})
    chk(f"AC(a): unusable timestamp ({label}) → no span, no crash",
        "result-age" not in s and ">x<" in s, s[:200])

# Legacy plain-string fallback path is untouched (no timestamp source there)
s = _result_strip({"last_result": "plain text"})
chk("AC(a): legacy string fallback renders without a span",
    "plain text" in s and "result-age" not in s, s[:200])

# --- AC(b): tone styling and the EU-675 dismiss hook survive the change -------
s = _result_strip({"last_result_record": {"tone": "error", "text": "Deploy failed", "timestamp": TS}})
chk("AC(b): error tone → var(--bad), never var(--ok)",
    "var(--bad)" in s and "var(--ok)" not in s, s[:200])
s = _result_strip({"last_result_record": {"tone": "ok", "text": "All good", "timestamp": TS}})
chk("AC(b): ok tone → var(--ok), never var(--bad)",
    "var(--ok)" in s and "var(--bad)" not in s, s[:200])
s = _result_strip({"last_result_record": {"tone": "warn", "text": "Slow CI", "timestamp": TS}})
chk("AC(b): warn tone not red", "var(--bad)" not in s, s[:200])
s = _result_strip({"last_result_record": {"tone": "ok", "text": "All good", "timestamp": TS}})
chk("AC(b): dismiss hook + message intact",
    "data-dismiss-result" in s and "All good" in s, s[:300])

# --- AC(c): the board JS ticker (static assertions on the served page) --------
tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp / "app"), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()
page = client.get("/").get_data(as_text=True)

chk("AC(c): ticker paints .result-age[data-ts] spans",
    'querySelectorAll(".result-age[data-ts]")' in page)
chk("AC(c): refresh interval is 30s",
    "setInterval(paintResultAges,30000)" in page)
chk("AC(c): painted on load AND repainted inside applyBoard (SSE/poll swaps)",
    page.count("paintResultAges()") >= 2
    and "function applyBoard" in page and page.index("paintResultAges();") < page.rindex("paintResultAges()"),
    f"call sites={page.count('paintResultAges()')}")
chk("AC(c): relative buckets present (just now / m / h / d)",
    '"just now"' in page and '"m ago"' in page and '"h ago"' in page and '"d ago"' in page)
chk("AC(c): purely client-side — page never calls /api/last-result",
    "/api/last-result" not in page)
chk("AC(c): no second strip — the EU-670 strip markup is the only result surface",
    page.count("data-dismiss-result") >= 1 and "rsdis" not in page and "rstime" not in page)

# --- AC(d): live round-trip — strip carries data-ts, dismiss removes it -------
MSG = "✓ EU-713 relative age shipped DEV→MAIN"


def _clear() -> None:
    server._state.pop("last_result", None)
    server._state.pop("last_result_record", None)
    app_st = cockpit_state.get_state("automatixy")
    app_st.pop("last_result", None)
    app_st.pop("last_result_record", None)


_clear()
server.set_last_result("automatixy", "ok", MSG)   # writer stamps timestamp=time.time()
b1 = client.get("/api/board?app=automatixy").get_data(as_text=True)
chk("AC(d) pre: board shows the strip with message + data-ts",
    MSG in b1 and "data-ts='" in b1 and "result-age" in b1, b1[:300])

r = client.post("/api/dismiss-result?app=automatixy")
chk("AC(d): POST /api/dismiss-result → 200 + {ok: true}",
    r.status_code == 200 and json.loads(r.get_data(as_text=True)) == {"ok": True},
    f"{r.status_code} {r.get_data(as_text=True)[:80]}")

b2 = client.get("/api/board?app=automatixy").get_data(as_text=True)
chk("AC(d): next board render has no strip and no data-ts (does not reappear)",
    MSG not in b2 and "data-ts='" not in b2 and "data-dismiss-result" not in b2)
_clear()

# --- report -------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("-----------------------------------------------------------")
print(f"  {passed}/{total} passed")
if passed != total:
    print(f"  RESULT: {total - passed} FAIL")
    sys.exit(1)
print("  RESULT: ALL GREEN")
sys.exit(0)
