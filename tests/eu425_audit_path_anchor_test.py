"""EU-425: the JiraAdapter transition-audit sink is anchored to the LIVE cfg.audit_path, not Config's
class default; and a failure in the failure-path audit write can never mask the re-raised transition
exception.

Two acceptance criteria:

1. The adapter's audit sink derives from the same resolved audit_path the live process uses. The
   adapter is built from `app` alone (base.make_backlog), so it can't see cfg; configure_audit_path()
   hands it the resolved path once at boot. Pins:
     - configure_audit_path sets the resolved path and invalidates a stale cached sink.
     - with an OVERRIDDEN path configured, _audit() resolves to THAT path, not Config.audit_path
       (the class default './state/audit.jsonl') — this is exactly the bug EU-425 fixes.
     - a real set_status transition event lands in the overridden file; the class-default file gains
       nothing.
     - GENERAL_AUDIT_PATH still WINS over the configured path (the test-isolation contract preserved).
     - configure_audit_path is idempotent (a repeat with the same path keeps the cached sink).

2. The audit write inside set_status's except path cannot mask/replace the re-raised transition
   exception: a record() that itself raises must not survive past the `raise` — the caller sees the
   REAL transition error, never an audit-write error standing in for it.

run_all sets GENERAL_AUDIT_PATH for every child (suite-wide ledger isolation, see run_all.py), so
this harness pops it before the resolution checks and drives _audit() with only the configured path
in play, then restores it.
"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

# --- stub the network/SDK so importing the orchestrator never leaves the process ------------- #
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
    results.append((n, bool(c), str(d) if not c else ""))

# run_all sets GENERAL_AUDIT_PATH for every child; pop it so the configured path is the ONLY signal
# in the resolution checks (restored at the end).
_prev_gap = os.environ.pop("GENERAL_AUDIT_PATH", None)

from orchestrator.backlog import jira                  # noqa: E402
from orchestrator.config import Config                 # noqa: E402
from orchestrator.contracts import Ticket              # noqa: E402

CLASS_DEFAULT = Config.audit_path   # './state/audit.jsonl' — the dataclass class-level default


class _Resp:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): return None
    def json(self): return self._p


class _Sess:
    """Canned current-status GET + transitions GET; records every POST."""
    def __init__(self, post_boom=None):
        self.posts = []
        self._post_boom = post_boom
    def get(self, url, params=None):
        if url.endswith("/transitions"):
            return _Resp({"transitions": [{"id": "1", "to": {"name": "Done"}}]})
        return _Resp({"fields": {"status": {"name": "In Progress"}}})
    def post(self, url, json=None):
        self.posts.append((url, json))
        if url.endswith("/transitions") and self._post_boom:
            raise self._post_boom
        return _Resp({})


def _adapter(session):
    return types.SimpleNamespace(
        session=session,
        status_map={},
        status_fallbacks={"QA": ["Done", "Closed"]},
        _url=lambda path: f"https://x/rest/api/3/{path}",
        _current_status=lambda key: "In Progress",
        add_comment=lambda t, body: None,
    )


TICKET = Ticket(id="EU-425", key="EU-425", summary="s", description="", acceptance_criteria=[])


# ============================================================================ #
# AC1: the sink follows the resolved (overridden) audit_path, not the class default
# ============================================================================ #
_custom = Path(tempfile.mkdtemp(prefix="eu425-audit-")) / "custom-ledger.jsonl"
assert str(_custom) != CLASS_DEFAULT, "test fixture must override the default to have teeth"

# (a) configure_audit_path records the resolved path and drops a stale cached sink
jira._AUDIT_LOG = "stale-marker"   # pretend a sink was cached against the old/default path
jira.configure_audit_path(str(_custom))
chk("configure_audit_path sets _RESOLVED_AUDIT_PATH to the resolved cfg path",
    jira._RESOLVED_AUDIT_PATH == str(_custom), jira._RESOLVED_AUDIT_PATH)
chk("configure_audit_path invalidates a previously-cached sink (the path may have changed)",
    jira._AUDIT_LOG is None, repr(jira._AUDIT_LOG))

# (b) _audit() now resolves to the OVERRIDDEN path, not the class default — the core EU-425 fix
resolved = Path(jira._audit().path).resolve()
chk("_audit() resolves to the configured (overridden) path, not Config.audit_path",
    resolved == _custom.resolve(), f"{resolved} != {_custom}")
chk("…and that path genuinely differs from the class default (so the pin has teeth)",
    str(_custom) != CLASS_DEFAULT, f"{_custom} == class default {CLASS_DEFAULT}")

# (c) + (d) a real transition lands in the OVERRIDDEN file; the class-default file gains nothing
_default = Path(CLASS_DEFAULT)
_before = _default.stat().st_size if _default.exists() else 0
jira._AUDIT_LOG = None   # rebuild against the configured path
jira.JiraAdapter.set_status(_adapter(_Sess()), TICKET, "QA")
rows = []
if _custom.exists():
    rows = [json.loads(l) for l in _custom.read_text(encoding="utf-8").splitlines() if l.strip()]
chk("the transition event lands in the OVERRIDDEN audit file",
    any(r.get("event") == "ticket_transition" and r.get("ticket_id") == "EU-425" for r in rows), rows)
_after = _default.stat().st_size if _default.exists() else 0
chk("the class-default state/audit.jsonl gained nothing from the configured-path transition",
    _after == _before, f"{_before} -> {_after} bytes")

# (e) GENERAL_AUDIT_PATH still WINS over the configured path (isolation contract preserved)
_env_ledger = Path(tempfile.mkdtemp(prefix="eu425-env-")) / "env-ledger.jsonl"
os.environ["GENERAL_AUDIT_PATH"] = str(_env_ledger)
jira._AUDIT_LOG = None
jira.configure_audit_path(str(_custom))   # configured path is _custom, yet env must win
_env_resolved = Path(jira._audit().path).resolve()
chk("GENERAL_AUDIT_PATH wins over the configured resolved path",
    _env_resolved == _env_ledger.resolve(), f"{_env_resolved} != {_env_ledger}")
os.environ.pop("GENERAL_AUDIT_PATH", None)

# (f) configure_audit_path is idempotent — a repeat with the SAME path keeps the cached sink
jira._AUDIT_LOG = None
jira.configure_audit_path(str(_custom))
_cached = jira._audit()                     # builds + caches the sink against _custom
jira.configure_audit_path(str(_custom))     # SAME path → must NOT drop the cache
chk("configure_audit_path is idempotent (same path keeps the cached sink)",
    jira._AUDIT_LOG is _cached, "a repeat call with the same path rebuilt the sink")


# ============================================================================ #
# AC2: a failure-path audit write cannot mask the re-raised transition exception
# ============================================================================ #
class _BoomAudit:
    """An audit sink whose record() always raises — simulates a dead ledger (disk full / perms)."""
    def record(self, event, **fields):
        raise OSError("audit disk full")


# The transition POST blows up so set_status enters its except path; the boom audit then lives there.
_real_err = RuntimeError("503 from Jira")
ad2 = _adapter(_Sess(post_boom=_real_err))
jira._AUDIT_LOG = _BoomAudit()
_raised = None
try:
    jira.JiraAdapter.set_status(ad2, TICKET, "QA")
except Exception as e:   # noqa: BLE001 - captured to assert which exception survives
    _raised = e
chk("AC2: the ORIGINAL transition error is what reaches the caller",
    isinstance(_raised, RuntimeError) and "503 from Jira" in str(_raised), repr(_raised))
chk("AC2: the audit-write OSError did NOT mask/replace the re-raised transition exception",
    _raised is not None and not isinstance(_raised, OSError), repr(_raised))


# restore the suite-wide isolation var for any later import in this process
if _prev_gap is not None:
    os.environ["GENERAL_AUDIT_PATH"] = _prev_gap

print("\n============= EU-425 AUDIT-PATH ANCHOR QA =============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
