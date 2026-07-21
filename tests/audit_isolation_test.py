"""Audit-ledger isolation guard (2026-07-21).

Live incident: `jira._audit()` lazily opens ``Config.audit_path`` — the process's REAL
``state/audit.jsonl`` — whenever a caller has not substituted the module singleton. The contract
was "each harness remembers to stub ``jira._AUDIT_LOG``", and ``state_routing_test.py`` did not.
So every ``python3 tests/run_all.py`` appended 5 fabricated ``ticket_transition`` rows for the
real ticket AUTO-73 to the operational ledger — 15 of the 29 rows on record, one of them claiming
the ticket landed in a German column ``Fertig`` that exists on no board here. ``dashboard``,
``forensics`` and the daily brief all read that file, so the unit's own record of what it did to
tickets was more than half fiction.

The fix is structural, mirroring GENERAL_PID_FILE (EU-355): ``run_all`` points GENERAL_AUDIT_PATH
at a per-run temp file and ``_audit()`` honours it ahead of the config default, so no harness has
to remember anything. These pins keep it that way.

Pins:
  1. _audit() honours GENERAL_AUDIT_PATH over the config default.
  2. A real set_status() call under that env writes to the temp ledger, NOT the repo's live one.
  3. The repo's live state/audit.jsonl gains ZERO bytes from a transition driven by a harness.
  4. (source) run_all sets GENERAL_AUDIT_PATH for every child, so the guarantee is suite-wide.
"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

req = types.ModuleType("requests")
req.Session = lambda *a, **k: None
req.RequestException = Exception
sys.modules["requests"] = req
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

_TMP = Path(tempfile.mkdtemp(prefix="audit-isolation-")) / "audit.jsonl"
os.environ["GENERAL_AUDIT_PATH"] = str(_TMP)

from orchestrator.backlog import jira

# 1) the env override wins over Config.audit_path
jira._AUDIT_LOG = None
chk("_audit() honours GENERAL_AUDIT_PATH over the config default",
    Path(jira._audit().path).resolve() == _TMP.resolve(),
    f"{jira._audit().path} != {_TMP}")

# ---------- drive a REAL set_status through the adapter, as a harness would ---------- #
class _Resp:
    def __init__(s, payload): s._p = payload
    def raise_for_status(s): return None
    def json(s): return s._p

class _Sess:
    """Serves canned transitions; records posts. Same shape state_routing_test.py uses."""
    def __init__(s): s.posts = []
    def get(s, url, params=None):
        if url.endswith("/transitions"):
            return _Resp({"transitions": [{"id": "1", "to": {"name": "Done"}}]})
        return _Resp({"fields": {"status": {"name": "In Progress"}}})
    def post(s, url, json=None):
        s.posts.append((url, json))
        return _Resp({})

_live = Path("state/audit.jsonl")
_before = _live.stat().st_size if _live.exists() else 0

adapter = types.SimpleNamespace(
    session=_Sess(),
    status_map={},
    status_fallbacks={"QA": ["Done", "Closed"]},
    _url=lambda path: f"https://x/rest/api/3/{path}",
    _current_status=lambda key: "In Progress",
    add_comment=lambda t, body: None,
)
ticket = types.SimpleNamespace(key="AUTO-73", id="AUTO-73")
jira.JiraAdapter.set_status(adapter, ticket, "QA")

# 2) the fabricated row landed in the temp ledger
rows = [json.loads(l) for l in _TMP.read_text(encoding="utf-8").splitlines() if l.strip()] \
    if _TMP.exists() else []
chk("a harness-driven transition writes to the temp ledger",
    any(r.get("event") == "ticket_transition" and r.get("ticket_id") == "AUTO-73" for r in rows),
    rows)

# 3) …and the LIVE ledger did not grow. This is the pin that would have caught the incident.
_after = _live.stat().st_size if _live.exists() else 0
chk("the repo's live state/audit.jsonl gained nothing from it",
    _after == _before, f"{_before} -> {_after} bytes")

# 4) (source) run_all guarantees this for every child process, not just this harness
_ra = Path("tests/run_all.py").read_text(encoding="utf-8")
chk("run_all sets GENERAL_AUDIT_PATH in the child env for the whole suite",
    'GENERAL_AUDIT_PATH' in _ra and '_CHILD_ENV["GENERAL_AUDIT_PATH"]' in _ra)

print("\n========== AUDIT-LEDGER ISOLATION GUARD ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
