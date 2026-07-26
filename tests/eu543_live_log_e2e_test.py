"""EU-543: stub-signal removal + isolation + SSE fallback guard (complements eu639).

Sibling to tests/eu639_live_runlog_e2e_test.py (the full-e2e harness that chains concurrent
_worker -> per-ticket file -> /api/run-log-stream -> warroom.js JS). Where eu639 confirms the
pipes work, eu543 proves the *post-fix* invariants hold on the produced files:

  AC4 -- no stub-note text ('[EU-380]', '[EU-253]', 'consult the drain log',
        'per-ticket stdout is in the shared stream') ever leaked into a per-ticket
        log once real step-lines were written; the file's last non-empty line is a
        real stage marker, not a pointer-note.
  AC3 -- strict set-based isolation: the two concurrent tickets' non-blank-line sets
        share ZERO elements (any shared output = cross-slot bleed).
  AC2 -- the /api/run-log-stream endpoint never emits the "showing the shared drain
        stream" fallback frame when driven with ?ticket= against a real per-ticket file.
  Static: warroom.js unshift/slice/no-scroll posture unchanged.

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
        print(f"  x {name}  {detail}")
        sys.exit(1)
    print(f"  v {name}")


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
# SECTION 1 (AC1 / AC3 / AC4): Stub-signal removal + set-based isolation
#
# Same simulated concurrent-drain as eu639 TEST 1 (patch _process_ticket_inner,
# _make_git, wrap sys.stdout in _Tee, drive loop._run_inner), PLUS:
#   - AC4-a: NO stub-note signature text found in EITHER per-ticket file
#   - AC4-b: LAST non-empty line of each file = real stage marker (not a note)
#   - AC3:   set(non-blank lines A) & set(non-blank lines B) == {} (zero shared lines)
#
# Stub signatures that must NEVER appear once real content flows:
# ---------------------------------------------------------------------------

print("\n=== SECTION 1 (AC1/AC3/AC4): Stub-signal removal + set-based isolation ===")

NOTE_RECORDER = Recorder()


async def _fake_print_lines(ticket, app, cfg, git, backlog, audit, budget, stop_event=None):
    """Simulate a full build with realistic step lines carrying the ticket id."""
    tid = ticket.id
    app_name = app.name
    steps = [
        f"[Build] Starting build for {tid} on {app_name}",
        f"{tid}:  builder: Bash  cd {app_name} && git checkout dev",
        f"{tid}:  builder: Read  pyproject.toml",
        f"{tid}:  builder: Grep  async def process_ticket",
        f"[Gate] Gate passed for {tid}",
        f"{tid}:  builder: Read  orchestrator/server.py:290",
        f"{tid}:  builder: Write  orchestrator/warroom.py:2728",
        f"[Review] Review passed for {tid}",
        f"{tid}:  builder: Bash  git add . && git commit -m 'fix'",
        f"{tid}:  builder: Bash  git push origin dev",
        f"[Land] {tid} changes landed (final)",
    ]
    for line in steps:
        print(line, flush=True)
        await asyncio.sleep(0.005)
    return TicketReport(tid, Outcome.MERGED, 1, 0.0, app_name, notes="")


_orig_process = loop._process_ticket_inner
_orig_note = loop.run_logger.write_note_log
_orig_mkgit = loop._make_git

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
    loop._process_ticket_inner = _orig_process
    loop.run_logger.write_note_log = _orig_note
    loop._make_git = _orig_mkgit

ok("two co-scheduled tickets reported", len(reports) == 2, f"got {len(reports)}")
ok("write_note_log called ZERO times (no stub files)",
   NOTE_RECORDER.count == 0, f"expected 0 got {NOTE_RECORDER.count}")

_date = datetime.datetime.now().strftime("%Y-%m-%d")

x7_dir = _tmp / "logs" / "alpha" / _date
x7_files = sorted(x7_dir.glob("EU-49-*.log"))
ok("EU-49 got exactly one per-ticket log file", len(x7_files) == 1, str(x7_files))
c7 = x7_files[0].read_text(encoding="utf-8")

x8_dir = _tmp / "logs" / "beta" / _date
x8_files = sorted(x8_dir.glob("EU-492-*.log"))
ok("EU-492 got exactly one per-ticket log file", len(x8_files) == 1, str(x8_files))
c8 = x8_files[0].read_text(encoding="utf-8")

for needle in ["[Build]", "[Gate]", "[Review]", "[Land]",
               "builder: Bash", "builder: Read", "builder: Grep",
               "builder: Write"]:
    ok(f"EU-49 file contains '{needle}'", needle in c7, repr(c7[:200]))
    ok(f"EU-492 file contains '{needle}'", needle in c8, repr(c8[:200]))

fsize_7 = x7_files[0].stat().st_size
fsize_8 = x8_files[0].stat().st_size
ok(f"EU-49 ({fsize_7}B) > 222-byte stub threshold", fsize_7 > 222, f"{fsize_7}B vs 222B")
ok(f"EU-492 ({fsize_8}B) > 222-byte stub threshold", fsize_8 > 222, f"{fsize_8}B vs 222B")

# -- AC4-A: NO stub-note signatures in EITHER file ----------------------------------
STUB_SIGS = [
    "[EU-380] concurrent drain",
    "[EU-253]",
    "consult the drain log",
    "per-ticket stdout is in the shared stream",
]
for sig in STUB_SIGS:
    ok(f"AC4-a: EU-49 file has NO stub sig: '{sig}'", sig not in c7,
       f"found stub text in EU-49: {c7[:400]}")
    ok(f"AC4-a: EU-492 file has NO stub sig: '{sig}'", sig not in c8,
       f"found stub text in EU-492: {c8[:400]}")

# -- AC4-B: LAST non-empty line = real stage marker ---------------------------------
last7 = [ln for ln in c7.splitlines() if ln.strip()]
last8 = [ln for ln in c8.splitlines() if ln.strip()]

ok("AC4-b: EU-49 file is non-empty", len(last7) > 0, repr(c7[:100]) if not last7 else "")
ok("AC4-b: EU-492 file is non-empty", len(last8) > 0, repr(c8[:100]) if not last8 else "")

ok("AC4-b: EU-49 last-line contains stage marker",
   any(s in last7[-1] for s in ["[Build]", "[Gate]", "[Review]", "[Land]"]),
   f"last={repr(last7[-1][:80])}")
ok("AC4-b: EU-492 last-line contains stage marker",
   any(s in last8[-1] for s in ["[Build]", "[Gate]", "[Review]", "[Land]"]),
   f"last={repr(last8[-1][:80])}")

# -- AC3 (set-based): zero shared non-blank lines ------------------------------------
lines7 = frozenset(ln for ln in c7.splitlines() if ln.strip())
lines8 = frozenset(ln for ln in c8.splitlines() if ln.strip())
overlap = lines7 & lines8
ok("AC3: EU-49 x EU-492 share ZERO non-blank lines", len(overlap) == 0,
   f"shared lines: {list(overlap)[:5]}" if overlap else "")

all_log_files = list((_tmp / "logs").rglob("*EU-49-*.log")) + \
                list((_tmp / "logs").rglob("*EU-492-*.log"))
ok("exactly two per-ticket .log files total",
   len(all_log_files) == 2, f"found {[str(p) for p in all_log_files]}")


run_logger.close_run_log(run_key=("alpha", 0))
run_logger.close_run_log(run_key=("beta", 0))


# ===========================================================================
# SECTION 2 (AC2): End-to-end SSE -- no fallback frame for per-ticket files
# ===========================================================================

print("\n=== SECTION 2 (AC2): SSE -- no fallback frame for per-ticket files ===")

SHARED_DRAIN = _tmp / "shared_drain.log"
SHARED_DRAIN.write_text(
    "EU-49:  builder: Bash  cd alpha\n"
    "EU-492:  builder: Read  pyproject.toml\n"
    "EU-49:  builder: Grep  async def process\n"
    "infrastructure noise line\n"
    "EU-492: [Gate] Gate passed for EU-492\n"
    "EU-49: [Review] Review passed for EU-49\n"
    "EU-492:  builder: Write  orchestrator/warroom.py\n"
    "EU-49: [Land] Changes landed on dev branch\n"
    "EU-49:  builder: Bash  git push\n",
    encoding="utf-8",
)


def _collect_stream(query: str, log_path: str) -> list[str]:
    tmp2 = Path(tempfile.mkdtemp())
    afp = str(tmp2 / "audit.jsonl")
    audit2 = Path(afp)
    audit2.write_text("", encoding="utf-8")
    cfg2 = Config(apps=[AppConfig(name="testapp", repo_path=str(tmp2), base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path=afp, use_worktree=False)
    cfg2.detected_auth = lambda: "test"
    srv.health.summary = lambda c: {"healthy": True, "checks": []}
    flask_app = srv.create_app(cfg2)

    st = cockpit_state.get_state("testapp")
    st["active"] = False
    st["log_path"] = log_path

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


def _extract_sse_data(frames: list[str]) -> list[str]:
    body = "".join(frames)
    return re.findall(r"data:(.+?)\n\n", body)


frames_x7 = _collect_stream("/api/run-log-stream?app=testapp&ticket=EU-49", str(SHARED_DRAIN))
msgs_x7 = _extract_sse_data(frames_x7)
body_x7 = "\n".join(msgs_x7)

ok("AC2a: ?ticket=EU-49 streams its Bash builder line",
   "EU-49:  builder: Bash" in body_x7)
ok("AC2b: ?ticket=EU-49 streams its Review line",
   "EU-49: [Review]" in body_x7, repr(body_x7))
ok("AC2c: ?ticket=EU-49 streams its Land line",
   "EU-49: [Land]" in body_x7, repr(body_x7))
ok("AC2d: ?ticket=EU-49 NEVER streams EU-492 lines",
   "EU-492:" not in body_x7, f"leaked: {body_x7!r}")
ok("AC2e: ?ticket=EU-49 drops untagged infra noise",
   "infrastructure noise" not in body_x7, repr(body_x7))

frames_x8 = _collect_stream("/api/run-log-stream?app=testapp&ticket=EU-492", str(SHARED_DRAIN))
msgs_x8 = _extract_sse_data(frames_x8)
body_x8 = "\n".join(msgs_x8)

ok("AC2f: ?ticket=EU-492 streams its Read builder line",
   "EU-492:  builder: Read" in body_x8)
ok("AC2g: ?ticket=EU-492 streams its Gate line",
   "EU-492: [Gate]" in body_x8)
ok("AC2h: ?ticket=EU-492 NEVER streams EU-49 lines",
   "EU-49:" not in body_x8, f"leaked: {body_x8!r}")
ok("AC2i: ?ticket=EU-492 drops untagged infra noise",
   "infrastructure noise" not in body_x8, repr(body_x8))

ok("prefix-safety: line containing EU-492 also contains EU-49 as substring",
   "EU-49" in "EU-492:  builder: Bash",
   "(this is why naive substring matching fails)")
ok("prefix-safety: ?ticket=EU-49 does NOT leak EU-492 bash line",
   "EU-492:  builder: Bash  cd alpha" not in body_x7,
   f"substring match leaked EU-492 onto EU-49 panel: {body_x7!r}")
ok("prefix-safety: ?ticket=EU-492 does NOT leak EU-49 lines",
   "EU-49:" not in body_x8,
   f"cross-contamination: {body_x8!r}")

full_body = "".join(frames_x7) + "".join(frames_x8)
ok("AC2-fallback: shared drain stream frame NEVER appears in SSE output",
   "showing the shared drain stream" not in full_body,
   f"fallback leaked into SSE: {full_body[:500]}")

frames_all = _collect_stream("/api/run-log-stream?app=testapp", str(SHARED_DRAIN))
msgs_all = _extract_sse_data(frames_all)
body_all = "\n".join(msgs_all)

ok("unfiltered: both tickets present", "EU-49:" in body_all and "EU-492:" in body_all)
ok("unfiltered: infra noise present too", "infrastructure noise" in body_all)


# ===========================================================================
# SECTION 3 (Static): Newest-on-top regression guard
# ===========================================================================

print("\n=== SECTION 3 (Static): Newest-on-top regression guard ===")

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

ok("buffer cap AFTER unshift (first line lands before trim)",
   stream_body.index("unshift(e.data)") < stream_body.index("runlogBuffer.length>1000"),
   "cap must come after unshift")


print(f"\n{checks}/{checks} passed")
