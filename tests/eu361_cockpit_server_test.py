"""EU-361 — cockpit server correctness batch (2026-07-16 total audit, Documentation/TOTAL_AUDIT_2026-07-16.md).

Seven independent defects in ``orchestrator/server.py``, one pin each. Every one was re-verified
live on dev before this harness was written; each check below FAILS against the pre-fix code for
the stated reason, not for an import error.

  1. ``/api/run-log-stream`` captured ``state["log_path"]`` ONCE at generator start. During a drain
     the next ticket opens its own log file, so the stream kept tailing the FINISHED ticket's log
     and the cockpit looked frozen from ticket #2 on. -> the path is re-read every tick.
  2. Stop-button race: ``stop_event`` was minted INSIDE the worker thread, so a Stop click landing
     in the window between "POST /api/run returns" and "the thread gets scheduled" found no event
     and silently no-op'd. -> the event is claimed in the REQUEST thread (the autopilot Start path
     at server.py already did it this way — the in-repo precedent).
  3. Check-then-act on the 7 one-shot ceremony flags (standup/council/scribe/meeting/patrol/
     approve/group): the flag was set inside ``_bg()``, so under ``app.run(threaded=True)`` two
     clicks a few ms apart BOTH passed ``if not _state.get(X)`` and started two ceremonies.
     Modelled here with a Thread stub that does not run its target — precisely the window.
  4. ``report_api``'s background run omitted the EU-175 ``run_start``/``run_end`` bracketing that
     ``run_api``/``run_selected_api`` carry -> unpaired boundaries / ghost sessions in forensics.
  5. ``security_reply_api`` recorded a ``security_reply_ticket_created`` audit event on a path where
     the code's own comment says "not yet implemented" and no ticket is created — a false trail.
  6. The EU-254 CSRF/Host guard hardcoded port 8787 while ``general serve --port N`` is a supported
     flag (main.py), so on any other port every legitimate POST 403s.
  7. ``/api/open-logs`` is a state-changing GET (spawns ``open`` via subprocess) and was therefore
     exempt from the EU-254 POST-only guard.

Stubs the Agent SDK / requests so importing the orchestrator needs no network / real models. Soft
``k/n passed`` tally so ``tests/run_all.py`` (the EU-44 gate) judges it honestly.
"""
import inspect
import json
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import cockpit_state  # noqa: E402
import orchestrator.server as srv  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

results: list[tuple[str, bool, str]] = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


tmp = Path(tempfile.mkdtemp())
AUDIT = tmp / "audit.jsonl"
AUDIT.write_text("", encoding="utf-8")
cfg = Config(apps=[AppConfig(name="alpha", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(AUDIT), use_worktree=False)
cfg.detected_auth = lambda: "test"
srv.health.summary = lambda c: {"healthy": True, "checks": []}

_real_thread = srv.threading.Thread


class _NoStartThread:
    """A Thread that is CONSTRUCTED but never runs its target.

    This is the whole point of checks 2 and 3: under ``app.run(threaded=True)`` the request thread
    returns the redirect long before the worker is scheduled, so anything the old code only did
    *inside* ``_bg()`` (mint the stop_event, set the busy flag) did not exist yet for the next
    click to see. Freezing the worker at "constructed, not started" makes that window deterministic
    instead of a sleep-and-hope race.
    """
    made: list = []

    def __init__(s, target=None, daemon=None, **kw):
        s.target = target
        _NoStartThread.made.append(s)

    def start(s):
        pass


class _SyncThread:
    """Runs the worker INLINE on .start() — for the checks that need the run to complete."""
    def __init__(s, target=None, daemon=None, **kw): s.t = target
    def start(s):
        if s.t:
            s.t()


def _stub_threading(thread_cls):
    """Swap ONLY ``threading.Thread`` for the server module; the real ``Event`` stays reachable so
    the stub can't accidentally decide the outcome of a check about who mints the stop_event."""
    return types.SimpleNamespace(Thread=thread_cls, Event=threading.Event, Lock=threading.Lock)


def _audit_events() -> list[dict]:
    out = []
    for line in AUDIT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _reset():
    # The ceremony flags are popped BEFORE reset_run_state(): that seam rebuilds each key from
    # _new_state(), which has no entry for the ceremony flags, so it KeyErrors on any it finds.
    for k in ("standuping", "councilling", "scribing", "meeting", "patrolling",
              "grouping", "shipreview", "qa"):
        cockpit_state._state.pop(k, None)
    cockpit_state.reset_run_state()
    _NoStartThread.made = []


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 1) /api/run-log-stream re-reads log_path — it must roll onto the NEXT ticket's log mid-drain
# ══════════════════════════════════════════════════════════════════════════════════════════════
_reset()
app = srv.create_app(cfg)

log_a = tmp / "ticket_a.log"
log_b = tmp / "ticket_b.log"
log_a.write_text("", encoding="utf-8")
log_b.write_text("", encoding="utf-8")

st = cockpit_state.get_state("alpha")
st["active"] = True
st["log_path"] = str(log_a)

with app.test_request_context("/api/run-log-stream?app=alpha"):
    resp = app.view_functions["run_log_stream_api"]()
gen = resp.response

frames: list[str] = []
stop = threading.Event()

def _consume():
    try:
        for chunk in gen:
            frames.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace"))
            if stop.is_set():
                break
    except Exception:  # noqa: BLE001 — the generator is closed from the main thread below
        pass

t = threading.Thread(target=_consume, daemon=True)
t.start()

# Ticket A's log gets a line — the stream must tail it.
log_a.write_text("A-LINE-ONE\n", encoding="utf-8")
time.sleep(1.2)

# The drain finishes ticket A and opens ticket B's log — exactly the mid-drain rollover.
log_b.write_text("B-LINE-ONE\n", encoding="utf-8")
st["log_path"] = str(log_b)
time.sleep(1.6)

stop.set()
try:
    gen.close()
except Exception:  # noqa: BLE001
    pass
t.join(timeout=2)

_body = "".join(frames)
chk("run-log-stream tails the ACTIVE ticket's log", "A-LINE-ONE" in _body, _body[:200])
chk("run-log-stream rolls onto the NEXT ticket's log when log_path changes mid-drain (EU-361/1)",
    "B-LINE-ONE" in _body, f"frames={_body[:300]!r}")

st["active"] = False
st["log_path"] = None


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 2) The Stop button works IMMEDIATELY after a run starts (stop_event claimed in the request thread)
# ══════════════════════════════════════════════════════════════════════════════════════════════
_reset()
srv.threading = _stub_threading(_NoStartThread)
srv.intake.from_text = lambda rcfg, app_name, title, keys, description=None: [
    types.SimpleNamespace(key="ALPHA-1", title=title)]
client = srv.create_app(cfg).test_client()

client.post("/api/run", data={"kind": "task", "text": "do a thing", "app": "alpha"})
_st = cockpit_state.get_state("alpha")
chk("POST /api/run claims the run slot", _st.get("active") is True, str(_st.get("active")))
chk("stop_event exists the instant /api/run returns — before the worker is scheduled (EU-361/2)",
    _st.get("stop_event") is not None,
    "stop_event is None: a Stop click in this window silently no-ops")

_r = client.post("/api/stop-run", data={"app": "alpha"})
_ev = _st.get("stop_event")
chk("the Stop button actually signals that fresh run (EU-361/2)",
    _ev is not None and _ev.is_set(), f"stop_event={_ev!r}")

_reset()


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 3) The one-shot ceremony flags are claimed atomically — two clicks start exactly ONE ceremony
# ══════════════════════════════════════════════════════════════════════════════════════════════
CEREMONIES = [
    ("/api/standup", {}, "standuping"),
    ("/api/council", {}, "councilling"),
    ("/api/scribe", {}, "scribing"),
    ("/api/meeting", {"topic": "budget"}, "meeting"),
    ("/api/qa", {"app": "alpha"}, "qa"),
]

for route, data, flag in CEREMONIES:
    _reset()
    srv.threading = _stub_threading(_NoStartThread)
    c = srv.create_app(cfg).test_client()
    c.post(route, data=data)
    c.post(route, data=data)   # the second click lands before the first worker is scheduled
    chk(f"two concurrent {route} clicks start exactly ONE ceremony (EU-361/3: {flag})",
        len(_NoStartThread.made) == 1,
        f"{len(_NoStartThread.made)} workers spawned — the ceremony double-runs")

_reset()


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 4) report_api brackets its run with the EU-175 run_start / run_end audit boundary
# ══════════════════════════════════════════════════════════════════════════════════════════════
_reset()
AUDIT.write_text("", encoding="utf-8")
srv.threading = _stub_threading(_SyncThread)

async def _fake_loop(rcfg, worklist, audit=None, stop_event=None):
    return []
srv.run_loop = _fake_loop
srv.intake.from_text = lambda rcfg, app_name, title, keys, description=None: [
    types.SimpleNamespace(key="ALPHA-9", title=title)]

c = srv.create_app(cfg).test_client()
c.post("/api/report", data={"app": "alpha", "text": "the /leads column gets cut off"})
_kinds = [e.get("event") or e.get("kind") or e.get("type") for e in _audit_events()]
chk("report_api records the EU-175 run_start boundary (EU-361/4)",
    "run_start" in _kinds, str(_kinds))
chk("report_api records the paired run_end boundary (EU-361/4)",
    "run_end" in _kinds, str(_kinds))

_reset()


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 5) No 'ticket created' audit event on the path that creates no ticket
# ══════════════════════════════════════════════════════════════════════════════════════════════
_reset()
AUDIT.write_text("", encoding="utf-8")
c = srv.create_app(cfg).test_client()
c.post("/api/security-reply", data={"ticket_id": "ALPHA-7", "iteration": "1",
                                    "response": "CRITICAL — this is a real RCE, please fix"})
_kinds = [e.get("event") or e.get("kind") or e.get("type") for e in _audit_events()]
chk("the security reply itself is still audited", "security_reply" in _kinds, str(_kinds))
chk("no false 'ticket created' event on the dead security-gate path (EU-361/5)",
    "security_reply_ticket_created" not in _kinds, str(_kinds))


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 6) `general serve --port N` — the CSRF/Host guard must follow the real bind port
# ══════════════════════════════════════════════════════════════════════════════════════════════
_reset()
srv.threading = _stub_threading(_NoStartThread)

_app9 = None
_why9 = ""
try:
    _app9 = srv.create_app(cfg, port=9000)
except TypeError as exc:
    _why9 = str(exc)
chk("create_app accepts the bind port (EU-361/6)", _app9 is not None,
    _why9 or "create_app() rejects port=")

if _app9 is not None:
    c9 = _app9.test_client()
    r9 = c9.post("/api/model", data={"backend": "opus"},
                 headers={"Host": "127.0.0.1:9000", "Origin": "http://127.0.0.1:9000"})
    chk("a legitimate same-origin POST is accepted on --port 9000 (EU-361/6)",
        r9.status_code != 403, f"status={r9.status_code}: every POST 403s off the default port")
    r9x = c9.post("/api/model", data={"backend": "opus"},
                  headers={"Host": "evil.example", "Origin": "http://evil.example"})
    chk("the DNS-rebinding/Host guard still bites on --port 9000",
        r9x.status_code == 403, f"status={r9x.status_code}")

chk("serve() threads its bind port into create_app (EU-361/6)",
    "create_app(cfg, port=port)" in inspect.getsource(srv.serve),
    "serve() still calls create_app(cfg) — --port never reaches the guard")

# The default-port flow must stay byte-identical (the single-operator path).
_app_default = srv.create_app(cfg)
_cd = _app_default.test_client()
_rd = _cd.post("/api/model", data={"backend": "opus"},
               headers={"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787"})
chk("default-port (8787) POSTs are unaffected", _rd.status_code != 403, f"status={_rd.status_code}")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 7) /api/open-logs is state-changing (spawns a subprocess) — the EU-254 guard must cover it
# ══════════════════════════════════════════════════════════════════════════════════════════════
_reset()
c = srv.create_app(cfg).test_client()

# ``path`` is deliberately outside the log root so that if the guard does NOT fire the request
# still dies on the traversal check instead of opening Finder — the two 403s are told apart by
# their body, so this check can never pass for the wrong reason.
r_evil = c.get("/api/open-logs?path=/etc", headers={"Origin": "http://evil.example"})
chk("/api/open-logs rejects a cross-origin GET (EU-361/7)",
    r_evil.status_code == 403 and "cross-origin" in r_evil.get_data(as_text=True),
    f"status={r_evil.status_code} body={r_evil.get_data(as_text=True)[:80]!r}")

r_host = c.get("/api/open-logs?path=/etc", headers={"Host": "evil.example"})
chk("/api/open-logs rejects a DNS-rebound Host (EU-361/7)",
    r_host.status_code == 403 and "Host" in r_host.get_data(as_text=True),
    f"status={r_host.status_code} body={r_host.get_data(as_text=True)[:80]!r}")

# ...and a same-origin click from the cockpit's own '📂 Open logs' link must still reach the handler.
r_ok = c.get("/api/open-logs?path=/etc", headers={"Referer": "http://127.0.0.1:8787/"})
chk("/api/open-logs still reaches the handler for a same-origin request",
    "cross-origin" not in r_ok.get_data(as_text=True),
    r_ok.get_data(as_text=True)[:80])

# A plain read-only GET must stay completely unguarded (the EU-254 contract).
r_board = c.get("/api/health", headers={"Origin": "http://evil.example"})
chk("read-only GETs stay unguarded (EU-254 contract preserved)",
    r_board.status_code == 200, f"status={r_board.status_code}")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# EU-629 — GET /logs/days : day-list view route
# ══════════════════════════════════════════════════════════════════════════════════════════════
_reset()
del srv.threading  # drop any module-level swap; fall back to real threading

from orchestrator import run_logger as _rl  # noqa: E402

_log_root = _rl.log_root(cfg)
_app_dir = _log_root / "alpha"             # safe slug of "alpha" is "alpha"
_app_dir.mkdir(parents=True, exist_ok=True)

_day_a = _app_dir / "2026-07-01"
_day_b = _app_dir / "2026-07-03"
_day_c = _app_dir / "2026-07-02"
_day_a.mkdir(parents=True, exist_ok=True)
_day_b.mkdir(parents=True, exist_ok=True)
_day_c.mkdir(parents=True, exist_ok=True)
# A non-directory MUST NOT appear in the listing.
(_app_dir / "not-a-day.txt").write_text("x", encoding="utf-8")

c = srv.create_app(cfg).test_client()

# --- 1) three day folders → 200 HTML, newest first ---
r = c.get("/logs/days?app=alpha")
chk("GET /logs/days?app=alpha returns 200 HTML", r.status_code == 200, f"status={r.status_code}")
body = r.get_data(as_text=True)
chk("Content-Type contains text/html (EU-629/1)", "text/html" in r.content_type, repr(r.content_type))
# Verify newest-first order by index comparison; .index() raises if absent → fail gracefully.
try:
    _i3 = body.index("2026-07-03")
    _i2 = body.index("2026-07-02")
    _i1 = body.index("2026-07-01")
    _ok_order = _i3 < _i2 < _i1
except ValueError:
    _ok_order = False
chk("dates appear newest-first (EU-629/1)", _ok_order,
    f"order check failed (b={len(body)} chars)")

# --- 2) each day has an href link to /logs/day ---
for d in ("2026-07-01", "2026-07-02", "2026-07-03"):
    chk(f"/logs/day link present for {d} (EU-629/2)",
        f'/logs/day?app=alpha&date={d}' in body,
        f"href missing from body")

# --- 3a) no app param → empty state 200 ---
r_blank = c.get("/logs/days")
chk("no app param → 200 with empty state (EU-629/3a)",
    r_blank.status_code == 200, f"status={r_blank.status_code}")

# --- 3b) unknown app → empty state 200 ---
r_unk = c.get("/logs/days?app=nonexistent")
chk("unknown app → 200 with empty state (EU-629/3b)",
    r_unk.status_code == 200, f"status={r_unk.status_code}")

# --- 4) path traversal → 403 ---
# _safe_slug("..") → ".." (dots survive), resolves outside root → 403.
r_trav = c.get("/logs/days?app=..")
chk("app=.. → 403 (EU-629/4a)", r_trav.status_code == 403, f"status={r_trav.status_code}")

# _safe_slug("../..") → "__..__" (slashes → underscores); no real traversal,
# so we get 200-empty-state — NOT a 403. This matches /api/day-log behaviour.
r_trav2 = c.get("/logs/days?app=../..")
chk("app=../.. → safe slug '__..__', not 403 (slash stripped by _safe_slug)",
    r_trav2.status_code == 200, f"unexpected status={r_trav2.status_code}")

# --- 5) files under app dir are NOT listed (is_dir filter) ---
chk("file entry excluded (is_dir filter, EU-629/5)",
    "not-a-day.txt" not in body,
    f"file leaked into listing")

# --- 6) handler source must NOT contain platform/Darwin guard ---
# Create a fresh app and inspect only the days_list_api function body (exclude docstring).
_fresh = srv.create_app(cfg)
_days_func = _fresh.view_functions.get("days_list_api")
if _days_func is not None:
    _full_src = inspect.getsource(_days_func)
    # Strip docstring (first string-literal block) so false-positives in comments are ignored.
    _after_doc = _full_src.lstrip()
    _dq = _after_doc.find('"""')
    _sq = _after_doc.find("'''")
    if _dq != -1 and (_sq == -1 or _dq < _sq):
        _end = _after_doc.find('"""', _dq + 3)
        _handler_src = _after_doc[_end + 3:] if _end != -1 else ""
    elif _sq != -1:
        _end = _after_doc.find("'''", _sq + 3)
        _handler_src = _after_doc[_end + 3:] if _end != -1 else ""
    else:
        _handler_src = _after_doc
else:
    _handler_src = "<route not yet defined>"

chk("days handler lacks platform.system call (EU-629/6)",
     "platform.system" not in _handler_src,
     repr(_handler_src))
chk("days handler lacks 'Darwin' literal (EU-629/6)",
     "Darwin" not in _handler_src,
     repr(_handler_src))

srv.threading = _stub_threading(_real_thread)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# EU-630 — GET /logs/day : day-detail HTML view (chronological render of read_day_log entries)
# ══════════════════════════════════════════════════════════════════════════════════════════════
_reset()
del srv.threading  # drop any module-level swap; fall back to real threading

from orchestrator import run_logger as _rl  # noqa: E402

_log_root = _rl.log_root(cfg)
_app_dir = _log_root / "alpha"
_app_dir.mkdir(parents=True, exist_ok=True)

# Build a populated day: 3 log files sharing the same time-suffix so they sort by filename.
_day = _app_dir / "2026-08-15"
_day.mkdir(parents=True, exist_ok=True)

# All three files share the same -120000 suffix → same sort-key → filename tiebreak.
_t1 = _day / "EU-630-120000.log"
_t2 = _day / "EU-631-120000.log"
_t3 = _day / "EU-632-120000.log"

_t1.write_text("[Build]\nRunning build steps for EU-630\n", encoding="utf-8")
_t2.write_text("[Gate]\nGate passed for EU-631\n[Review]\nPR approved\n", encoding="utf-8")
_t3.write_text('Script injection attempt: <script>alert("xss")</script>\n', encoding="utf-8")

c = srv.create_app(cfg).test_client()


# --- 1) 200 HTML showing every entry from read_day_log in order ---
r_pop = c.get("/logs/day?app=alpha&date=2026-08-15")
chk("GET /logs/day?app=alpha&date=YYYY-MM-DD returns 200 (EU-630/1)",
    r_pop.status_code == 200, f"status={r_pop.status_code}")
body_pop = r_pop.get_data(as_text=True)
chk("Content-Type contains text/html (EU-630/1)",
    "text/html" in r_pop.content_type, repr(r_pop.content_type))
# Verify chronological order: each known entry must appear AFTER the previous one.
_order_checks = [
    ("Running build steps for EU-630",   "EU-631"),
    ("Gate passed for EU-631",            "PR approved"),
    ('Script injection attempt: &lt;script&gt;', None),
]
try:
    _order_ok = True
    _prev = ""
    for _text, _next_hint in _order_checks:
        _pos = body_pop.index(_text)
        if _prev and _pos < body_pop.index(_prev):
            _order_ok = False
            break
        _prev = _text
except ValueError:
    _order_ok = False
chk("entries appear in chronological order (EU-630/1)", _order_ok,
    f"one of the {len(_order_checks)} known entries not found or out-of-order")

# --- 2) Lines are prefixed [<ticket>] [<stage>] ---
chk("[EU-630] [Build] present (EU-630/2)",
    "[EU-630] [Build]" in body_pop, "ticket+stage prefix missing")
chk("[EU-631] [Gate] present (EU-630/2)",
    "[EU-631] [Gate]" in body_pop, "ticket+stage prefix missing")
chk("[EU-631] [Review] present (EU-630/2)",
    "[EU-631] [Review]" in body_pop, "second stage for EU-631 missing")
chk('[EU-632] [—] present (EU-630/2)',
    '[EU-632] [—]' in body_pop, "missing-stage fallback not rendered")

# An entry with a completely empty stage renders [—]
_empty_stage_day = _app_dir / "2026-08-16"
_empty_stage_day.mkdir(parents=True, exist_ok=True)
_empty_file = _empty_stage_day / "EU-999-130000.log"
_empty_file.write_text("", encoding="utf-8")

c_empty = srv.create_app(cfg).test_client()
r_es = c_empty.get("/logs/day?app=alpha&date=2026-08-16")
chk("empty log file → 200 (EU-630/2-empty)", r_es.status_code == 200,
    f"status={r_es.status_code}")


# --- 3a) malformed date → 400 ---
r_bad = c.get("/logs/day?app=alpha&date=2026-13-99")
chk("malformed date → 400 (EU-630/3a)",
    r_bad.status_code == 400, f"status={r_bad.status_code}")

# --- 3b) missing date param → 400 ---
r_nom = c.get("/logs/day?app=alpha")
chk("missing date param → 400 (EU-630/3b)",
    r_nom.status_code == 400, f"status={r_nom.status_code}")

# --- 3c) path traversal → 403 ---
r_trav = c.get("/logs/day?app=..&date=2026-08-15")
chk("app=.. → 403 (EU-630/3c)",
    r_trav.status_code == 403, f"status={r_trav.status_code}")

# --- 3d) ../.. safe slug → 200 empty-state (NOT 403) ---
r_safe = c.get("/logs/day?app=../..&date=2026-08-15")
chk("app=../.. → safe slug '__..__', 200 empty-state (EU-630/3d)",
    r_safe.status_code == 200, f"unexpected status={r_safe.status_code}")

# --- 4) nonexistent day → 200 with 'no log entries' message ---
r_miss = c.get("/logs/day?app=alpha&date=1999-01-01")
chk("nonexistent day → 200 (EU-630/4)",
    r_miss.status_code == 200, f"status={r_miss.status_code}")
miss_body = r_miss.get_data(as_text=True)
chk("nonexistent day shows 'no log entries' (EU-630/4)",
    "no log entries" in miss_body.lower(),
    repr(miss_body[:300]))

# --- 5) HTML-escaped injection in log text ---
chk("<script> NOT present raw in body (EU-630/5)",
    "<script>" not in body_pop, "raw script tag leaked")
chk("&lt;script&gt; present (escaped) (EU-630/5)",
    "&lt;script&gt;" in body_pop, "HTML entities not escaped")

# --- 6) handler source lacks platform/Darwin guard ---
_fresh = srv.create_app(cfg)
_dv_func = _fresh.view_functions.get("day_view")
if _dv_func is not None:
    _full_src = inspect.getsource(_dv_func)
    _lstripped = _full_src.lstrip()
    _dq = _lstripped.find('"""')
    _sq = _lstripped.find("'''")
    if _dq != -1 and (_sq == -1 or _dq < _sq):
        _end = _lstripped.find('"""', _dq + 3)
        _handler_src = _lstripped[_end + 3:] if _end != -1 else ""
    elif _sq != -1:
        _end = _lstripped.find("'''", _sq + 3)
        _handler_src = _lstripped[_end + 3:] if _end != -1 else ""
    else:
        _handler_src = _lstripped
else:
    _handler_src = "<route not yet defined>"
    chk("day_view route exists (EU-630/6)", False, "view_functions['day_view'] is None")

chk("day_view handler lacks platform.system call (EU-630/6)",
    "platform.system" not in _handler_src,
    repr(_handler_src))
chk("day_view handler lacks 'Darwin' literal (EU-630/6)",
    "Darwin" not in _handler_src,
    repr(_handler_src))

srv.threading = _stub_threading(_real_thread)

print("\n============ EU-361 COCKPIT SERVER CORRECTNESS QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
