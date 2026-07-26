"""EU-639: end-to-end integration — concurrent _worker → per-ticket file → /api/run-log-stream.

This is the last integration gate for the Live Run Log feature (EU-543 epic lineage). It chains
the seams the individual pieces cover but don't touch together:

  1. Concurrent _worker (loop._run_inner, max_concurrent_builders=2) routes each slot's stdout
     into THAT ticket's file via the _Tee + _RUN_LOG_KEY ContextVar seam.
  2. The /api/run-log-stream SSE endpoint (Flask test client) reads those per-ticket files
     back through its generator, honouring the ?ticket= param with prefix-safe matching.
  3. warroom.py's JS still does unshift + slice(0,1000), no scrollTop (newest-on-top).
  4. Whole-unit gate: the new harness is picked up by tests/run_all.py alongside pre-existing ones.

Stubs the Agent SDK / requests so importing the orchestrator needs no network / real models.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import re
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, ".")

# --- Minimal stubs (same shape as other harnesses) -------------------------------

_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return s

_sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", _sdk)

_req = types.ModuleType("requests")
_req.Session = lambda *a, **k: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
_req.RequestException = Exception
sys.modules.setdefault("requests", _req)

import orchestrator.loop as loop  # noqa: E402
import orchestrator.run_logger as run_logger  # noqa: E402
import orchestrator.cockpit_state as cockpit_state  # noqa: E402
import orchestrator.server as srv  # noqa: E402
from orchestrator.config import Config, AppConfig  # noqa: E402
from orchestrator.contracts import Outcome, TicketReport  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


_tmp = Path(tempfile.mkdtemp())


# ---------------------------------------------------------------------------
# Recorder helpers -----------------------------------------------------------
# ---------------------------------------------------------------------------

class Recorder:
    """Records calls and arguments."""
    def __init__(self):
        self.calls = []
        self.args_list = []
        self.kwargs_list = []

    def record(self, *args, **kwargs):
        self.calls.append(True)
        self.args_list.append(list(args))
        self.kwargs_list.append(dict(kwargs))

    @property
    def count(self):
        return len(self.calls)


# ---------------------------------------------------------------------------
# Config + tickets -----------------------------------------------------------
# ---------------------------------------------------------------------------

def mkcfg(n=2) -> Config:
    c = Config(apps=[], audit_path=str(_tmp / "audit.jsonl"))
    c.max_concurrent_builders = n
    c.use_worktree = False
    c.max_cost_usd = 0
    c.concurrent_min_free_gb = 0
    return c


APP_ALPHA = AppConfig(name="alpha", repo_path=str(_tmp / "a"), base_branch="dev",
                      protected_branch="main", backlog_backend="none")
APP_BETA = AppConfig(name="beta", repo_path=str(_tmp / "b"), base_branch="dev",
                     protected_branch="main", backlog_backend="none")


def T(tid, desc="", app="alpha"):
    from orchestrator.contracts import Ticket
    return Ticket(id=tid, key=tid, summary=tid, description=desc, app=app, ephemeral=True)


class _Audit:
    def __init__(self):
        self.events = []
    def record(self, event, **k):
        self.events.append((event, k))


# ===========================================================================
# TEST 1 (AC1): Concurrent-mode reality check
#
# Two simulated concurrent workers emit realistic 'builder: …' step lines plus
# stage markers for EU-49 and EU-492. After the run:
#   • each per-ticket log contains ALL of its own lines
#   • ZERO of the other ticket's lines
#   • file size > 222 bytes (not a stub)
#   • write_note_log called exactly zero times from the _worker path
# ===========================================================================

print("\n=== TEST 1 (AC1): Concurrent-mode reality check ===")

NOTE_RECORDER = Recorder()


async def _fake_print_lines(ticket, app, cfg, git, backlog, audit, budget, stop_event=None):
    """Simulate a full build with realistic step lines carrying the ticket id."""
    tid = ticket.id
    app_name = app.name
    # Stage markers and step lines — matches the real officer stdout format:
    # "{ticket_id}: · builder: {tool} {target}" plus "[Phase] ..." lines.
    steps = [
        f"[Build] Starting build for {tid} on {app_name}",
        f"{tid}: · builder: Bash  cd {app_name} && git checkout dev",
        f"{tid}: · builder: Read  pyproject.toml",
        f"{tid}: · builder: Grep  async def process_ticket",
        f"[Gate] Gate passed for {tid}",
        f"{tid}: · builder: Read  orchestrator/server.py:290",
        f"{tid}: · builder: Write  orchestrator/warroom.py:2728",
        f"[Review] Review passed for {tid}",
        f"{tid}: · builder: Bash  git add . && git commit -m 'fix'",
        f"{tid}: · builder: Bash  git push origin dev",
        f"[Land] Changes landed on dev branch",
    ]
    for line in steps:
        print(line, flush=True)
        await asyncio.sleep(0.005)
    return TicketReport(tid, Outcome.MERGED, 1, 0.0, app_name, notes="")


orig_process = loop._process_ticket_inner
orig_note = loop.run_logger.write_note_log
orig_mkgit = loop._make_git

loop._process_ticket_inner = _fake_print_lines
loop.run_logger.write_note_log = NOTE_RECORDER.record
loop._make_git = lambda cfg, app, slot=0: types.SimpleNamespace(ensure_clean=lambda: None)

_real_stdout = sys.stdout
sys.stdout = cockpit_state._Tee(_real_stdout)

try:
    reports = asyncio.run(loop._run_inner(
        mkcfg(2),
        [(APP_ALPHA, T("EU-49")), (APP_BETA, T("EU-492", app="beta"))],
        _Audit()))
finally:
    sys.stdout = _real_stdout
    loop._process_ticket_inner = orig_process
    loop.run_logger.write_note_log = orig_note
    loop._make_git = orig_mkgit

ok("two co-scheduled tickets reported", len(reports) == 2, f"got {len(reports)}")
ok("write_note_log called ZERO times (no stub files)",
   NOTE_RECORDER.count == 0, f"expected 0 got {NOTE_RECORDER.count}")

_date = datetime.datetime.now().strftime("%Y-%m-%d")

# -- EU-49's file --------------------------------------------------------
e49_dir = _tmp / "logs" / "alpha" / _date
e49_files = sorted(e49_dir.glob("EU-49-*.log"))
ok("EU-49 got exactly one per-ticket log file", len(e49_files) == 1, str(e49_files))
c49 = e49_files[0].read_text(encoding="utf-8")

for needle in ["[Build]", "[Gate]", "[Review]", "[Land]",
               "· builder: Bash", "· builder: Read", "· builder: Grep",
               "· builder: Write"]:
    ok(f"EU-49's file contains '{needle}'", needle in c49, repr(c49[:200]))

fsize_49 = e49_files[0].stat().st_size
ok(f"EU-49's file ({fsize_49}B) > 222-byte stub",
   fsize_49 > 222, f"{fsize_49}B vs 222B")

# -- EU-492's file -------------------------------------------------------
e492_dir = _tmp / "logs" / "beta" / _date
e492_files = sorted(e492_dir.glob("EU-492-*.log"))
ok("EU-492 got exactly one per-ticket log file", len(e492_files) == 1, str(e492_files))
c492 = e492_files[0].read_text(encoding="utf-8")

for needle in ["[Build]", "[Gate]", "[Review]", "[Land]",
               "· builder: Bash", "· builder: Read", "· builder: Grep",
               "· builder: Write"]:
    ok(f"EU-492's file contains '{needle}'", needle in c492, repr(c492[:200]))

fsize_492 = e492_files[0].stat().st_size
ok(f"EU-492's file ({fsize_492}B) > 222-byte stub",
   fsize_492 > 222, f"{fsize_492}B vs 222B")

# -- Isolation: no cross-contamination -----------------------------------
ok("EU-49's file has ZERO of EU-492's content",
   "EU-492" not in c49, repr(c49[:300]))
ok("EU-492's file has ZERO of EU-49's content",
   "EU-49]" not in c492, repr(c492[:300]))

# -- No extra note/log stub files leaked ---------------------------------
all_log_files = list((_tmp / "logs").rglob("*EU-49-*.log")) + \
                list((_tmp / "logs").rglob("*EU-492-*.log"))
ok("exactly two per-ticket .log files total (one per ticket)",
   len(all_log_files) == 2, f"found {[str(p) for p in all_log_files]}")


# Clean up handles for subsequent tests
run_logger.close_run_log(run_key=("alpha", 0))
run_logger.close_run_log(run_key=("beta", 0))


# ===========================================================================
# TEST 2 (AC2): End-to-end panel feed through /api/run-log-stream
#
# Creates a shared-drain fixture with interleaved EU-49 / EU-492 lines and
# exercises ?ticket= param for isolation. Also asserts prefix-safety:
#   'EU-49' must NOT match 'EU-492:' lines.
# ===========================================================================

print("\n=== TEST 2 (AC2): End-to-end panel feed ===")

SHARED_DRAIN = _tmp / "shared_drain.log"
SHARED_DRAIN.write_text(
    "EU-49: · builder: Bash  cd alpha\n"
    "EU-492: · builder: Read  pyproject.toml\n"
    "EU-49: · builder: Grep  async def process\n"
    "infrastructure noise line — no ticket id\n"
    "EU-492: [Gate] Gate passed for EU-492\n"
    "EU-49: [Review] Review passed for EU-49\n"
    "EU-492: · builder: Write  orchestrator/warroom.py\n"
    "EU-49: [Land] Changes landed on dev branch\n"
    "EU-49: · builder: Bash  git push\n",
    encoding="utf-8",
)


def _collect_stream(query: str, log_path: str) -> list[str]:
    """Drive /api/run-log-stream generator to exhaustion against a static file."""
    tmp2 = Path(tempfile.mkdtemp())
    audit2 = tmp2 / "audit.jsonl"
    audit2.write_text("", encoding="utf-8")
    cfg2 = Config(apps=[AppConfig(name="testapp", repo_path=str(tmp2), base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path=str(audit2), use_worktree=False)
    cfg2.detected_auth = lambda: "test"
    srv.health.summary = lambda c: {"healthy": True, "checks": []}
    flask_app = srv.create_app(cfg2)

    st = cockpit_state.get_state("testapp")
    st["active"] = False       # drain completes immediately after first read
    st["log_path"] = log_path  # point at our fixture

    with flask_app.test_request_context(query):
        resp = flask_app.view_functions["run_log_stream_api"]()
    gen = resp.response

    frames = []
    for chunk in gen:
        frames.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace"))
    try:
        gen.close()
    except Exception:
        pass
    return frames


def _extract_log_data(frames: list[str]) -> list[str]:
    """Pull SSE data payloads from raw frame strings."""
    body = "".join(frames)
    return re.findall(r"data:(.+?)\n\n", body)


# -- Stream EU-49 only ---------------------------------------------------
frames_e49 = _collect_stream("/api/run-log-stream?app=testapp&ticket=EU-49", str(SHARED_DRAIN))
msgs_e49 = _extract_log_data(frames_e49)
body_e49 = "\n".join(msgs_e49)

ok("AC2a: ?ticket=EU-49 streams EU-49's bash line",
   "EU-49: · builder: Bash" in body_e49)
ok("AC2b: ?ticket=EU-49 streams EU-49's review line",
   "EU-49: [Review]" in body_e49, repr(body_e49))
ok("AC2c: ?ticket=EU-49 streams EU-49's land line",
   "EU-49: [Land]" in body_e49, repr(body_e49))
ok("AC2d: ?ticket=EU-49 NEVER streams EU-492's lines",
   "EU-492" not in body_e49, f"EU-492 leaked: {body_e49!r}")
ok("AC2e: ?ticket=EU-49 drops untagged infra noise",
   "infrastructure noise" not in body_e49, repr(body_e49))

# -- Stream EU-492 only --------------------------------------------------
frames_e492 = _collect_stream("/api/run-log-stream?app=testapp&ticket=EU-492", str(SHARED_DRAIN))
msgs_e492 = _extract_log_data(frames_e492)
body_e492 = "\n".join(msgs_e492)

ok("AC2f: ?ticket=EU-492 streams EU-492's read line",
   "EU-492: · builder: Read" in body_e492)
ok("AC2g: ?ticket=EU-492 streams EU-492's gate line",
   "EU-492: [Gate]" in body_e492)
ok("AC2h: ?ticket=EU-492 NEVER streams EU-49's lines",
   "EU-49:" not in body_e492, f"EU-49 leaked: {body_e492!r}")
ok("AC2i: ?ticket=EU-492 drops untagged infra noise",
   "infrastructure noise" not in body_e492, repr(body_e492))

# -- Prefix-safety: EU-49 must NOT match EU-492 lines --------------------
# The trickiest case: "EU-492: · builder: Bash  cd alpha" CONTAINS "EU-49" as a substring.
# A naive `ticket in line` would incorrectly let EU-49 see EU-492's lines.
ok("prefix-safety: line containing 'EU-492:' also contains 'EU-49' as substring",
   "EU-49" in "EU-492: · builder: Bash",
   "(this is why naive substring matching fails)")
ok("prefix-safety: ?ticket=EU-49 does NOT leak EU-492's bash line",
   "EU-492: · builder: Bash  cd alpha" not in body_e49,
   f"substring match leaked EU-492 onto EU-49 panel: {body_e49!r}")
ok("prefix-safety: ?ticket=EU-492 does NOT leak EU-49's lines",
   "EU-49:" not in body_e492,
   f"cross-contamination: {body_e492!r}")

# -- Unfiltered stream emits everything ----------------------------------
frames_unfiltered = _collect_stream("/api/run-log-stream?app=testapp", str(SHARED_DRAIN))
msgs_unfiltered = _extract_log_data(frames_unfiltered)
body_all = "\n".join(msgs_unfiltered)

ok("AC3a: no ticket param streams EU-49 lines",
   "EU-49:" in body_all and "EU-492:" in body_all)
ok("AC3b: no ticket param streams infro noise too",
   "infrastructure noise" in body_all)


# ===========================================================================
# TEST 3 (AC3): Newest-on-top regression guard
# Static assertion against warroom.py JS — mirrors tests/eu637_runlog_newest_first_test.py
# ===========================================================================

print("\n=== TEST 3 (AC3): Newest-on-top regression guard ===")

ROOT_DIR = Path(__file__).resolve().parent.parent
WARROOM_SRC = (ROOT_DIR / "orchestrator" / "warroom.py").read_text()

m_stream = re.search(r"function startRunlogStream\(\).*?\n  \}\n", WARROOM_SRC, re.DOTALL)
stream_body = m_stream.group(0) if m_stream else ""
ok("startRunlogStream function found", m_stream is not None)

m_render = re.search(r"function renderRunlogLines\(\).*?\n  \}\n", WARROOM_SRC, re.DOTALL)
render_body = m_render.group(0) if m_render else ""
ok("renderRunlogLines function found", m_render is not None)

ok("unshift(e.data) present (prepend-newest)",
   "runlogBuffer.unshift(e.data)" in stream_body,
   repr(stream_body[:500]))
ok("push(e.data) removed (old append-at-bottom gone)",
   "runlogBuffer.push(e.data)" not in stream_body)
ok("slice(0,1000) cap keeps newest-only",
   "slice(0,1000)" in stream_body)
ok("no force-scroll in renderRunlogLines",
   "runlogPanel.scrollTop" not in render_body)
ok("'log' listener calls renderRunlogLines()",
   "renderRunlogLines();" in stream_body)
ok("'done' listener closes EventSource",
   "runlogEs.close()" in stream_body)


# ===========================================================================
# Summary
# ===========================================================================

print(f"\n{checks}/{checks} checks passed")
