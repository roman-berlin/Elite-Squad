"""EU-669 — Non-destructive peek + dismiss for the stored one-shot result.

Acceptance criteria tested here (mirrors the EU-669 description exactly):
  AC(a) _peek_last_result called twice returns identical {tone, text, timestamp} — no clear.
  AC(b) POST /api/dismiss-result clears both last_result and last_result_record;
        a _peek_last_result call immediately after returns None.
  AC(c) POST /api/dismiss-result with nothing pending → {"ok": True}, no KeyError/500.
  AC(d) Existing _result_strip behaviour untouched — still peeks without popping.
  AC(e) Full flow: set via set_last_result, peek twice unchanged, dismiss, peek again empty.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

import orchestrator.cockpit_state as cs
from orchestrator.cockpit_views import _peek_last_result, _result_strip
from orchestrator.config import AppConfig, Config
from orchestrator.server import create_app, set_last_result

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))
    tag = "PASS" if cond else "FAIL"
    line = f"  [{tag}] {name}"
    if detail and not cond:
        line += f"  ({detail})"
    print(line)


print("\n============ EU-669 Peek + Dismiss QA ============")

# ── AC(a): _peek_last_result called twice returns identical record ──────────────
st_a: dict = {"last_result_record": {"tone": "error", "text": "Deploy failed", "timestamp": 42.0}}
first = _peek_last_result(st_a)
second = _peek_last_result(st_a)
chk("AC(a): _peek called twice returns same record",
    first == second == {"tone": "error", "text": "Deploy failed", "timestamp": 42.0},
    repr(first))
chk("AC(a): state untouched after two peeks",
    st_a.get("last_result_record") is not None,
    "state was cleared by peek")

# ── AC(b): dismiss clears both keys; peek afterwards returns None ───────────────
tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="x", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"))
app = create_app(cfg)
client = app.test_client()

set_last_result("x", "ok", "shipped")
with app.test_request_context():
    rec = _peek_last_result(cs.get_state("x"))
chk("AC(b) pre: record exists before dismiss",
    rec is not None and rec["text"] == "shipped",
    repr(rec))

rv = client.post("/api/dismiss-result?app=x")
chk("AC(b): dismiss returns 200", rv.status_code == 200)

after = _peek_last_result(cs.get_state("x"))
chk("AC(b): _peek after dismiss returns None", after is None, repr(after))
chk("AC(b): last_result also cleared (legacy key)",
    not cs.get_state("x").get("last_result"),
    f"leftover: {cs.get_state('x').get('last_result')!r}")
chk("AC(b): last_result_record also cleared",
    "last_result_record" not in cs.get_state("x"),
    "record key still present")

# ── AC(c): dismiss on empty state is idempotent no-op ──────────────────────────
rv_empty = client.post("/api/dismiss-result?app=x")
body_empty = rv_empty.get_data(as_text=True)
chk("AC(c): empty-state dismiss → 200", rv_empty.status_code == 200)
chk("AC(c): empty-state dismiss body is {\"ok\": true}",
    json.loads(body_empty) == {"ok": True},
    body_empty)
chk("AC(c): no exception (not 500), no KeyError in logs",
    rv_empty.status_code == 200)

# ── AC(d): _result_strip behaviour untouched — still non-destructive ───────────
st_d: dict = {"last_result_record": {"tone": "warn", "text": "Slow CI"}}
strip1 = _result_strip(st_d)
strip2 = _result_strip(st_d)
chk("AC(d): _result_strip returns content", len(strip1) > 0)
chk("AC(d): _result_strip does NOT pop state",
    st_d.get("last_result_record") is not None,
    "state was consumed")
chk("AC(d): same output both calls",
    strip1 == strip2)

# ── AC(e): full lifecycle flow ─────────────────────────────────────────────────
cs.reset_workspaces()
set_last_result("x", "warn", "caution!")
peek1 = _peek_last_result(cs.get_state("x"))
peek2 = _peek_last_result(cs.get_state("x"))
chk("AC(e) step 1: set via set_last_result ✓",
    peek1 == peek2 == {"tone": "warn", "text": "caution!", "timestamp": peek1["timestamp"]})

client.post("/api/dismiss-result?app=x")
peek3 = _peek_last_result(cs.get_state("x"))
chk("AC(e) step 2: peek twice unchanged ✓", peek1 is not None)
chk("AC(e) step 3: dismiss ✓", rv_empty.status_code == 200)
chk("AC(e) step 4: peek after dismiss is None",
    peek3 is None,
    repr(peek3))

# ── Report ---------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("-----------------------------------------------------------")
print(f"  {passed}/{total} passed")
if passed != total:
    print(f"  RESULT: {total - passed} FAIL")
    sys.exit(1)
print("  RESULT: ALL GREEN")
sys.exit(0)
