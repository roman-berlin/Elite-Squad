"""EU-154 [auto-split from EU-149] — SSE endpoint that streams live terminal/log output.

``GET /api/terminal/stream`` taps the existing stdout ring buffer (``cockpit_state._LOG``, a
``deque(maxlen=600)`` that already feeds the War Room live-feed panel) instead of a new log
source. This harness drives the raw generator directly (not through a real socket) so it can
control exactly when new lines land relative to the client "connecting", proving:

  * the response declares ``Content-Type: text/event-stream``,
  * lines already in the ring buffer before connect are NOT replayed (no backlog dump),
  * two lines appended after connect arrive as two SEPARATE ``data: <line>\\n\\n`` frames, in
    order,
  * closing the client connection (``generator.close()``) unwinds without an unhandled
    exception (the ``GeneratorExit`` path).
"""
import sys, threading, time, types, tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import server, cockpit_state
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
(tmp / "audit.jsonl").write_text("", encoding="utf-8")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"

app = server.create_app(cfg)

# A clean ring buffer with a KNOWN backlog line already in it, planted BEFORE the client
# "connects" — this is exactly what must never be replayed.
cockpit_state._LOG.clear()
cockpit_state._LOG.append(("BACKLOG line that must never be replayed", None))


# --- 1) content-type ---------------------------------------------------------------------------- #
with app.test_request_context("/api/terminal/stream"):
    resp = app.view_functions["terminal_stream_api"]()
chk("responds with Content-Type: text/event-stream",
    (resp.headers.get("Content-Type") or resp.mimetype or "").startswith("text/event-stream"),
    resp.headers.get("Content-Type"))

gen = resp.response   # the raw generator app_iter — not yet iterated, nothing has "connected" yet


# --- 2) drive the generator from a background thread so the main thread can control timing ------ #
frames = []
close_ok = [None]
close_err = [None]

def consume():
    try:
        for chunk in gen:
            frames.append(chunk)
            if len(frames) >= 2:
                break
    except GeneratorExit:
        pass
    # The generator is now suspended right after its last yield (we just broke out of the
    # `for` loop without exhausting it) — close it from this SAME thread, exactly like a real
    # client disconnect does, and confirm it unwinds without raising.
    try:
        gen.close()
        close_ok[0] = True
    except Exception as e:                      # noqa: BLE001 - captured for the assertion below
        close_ok[0] = False
        close_err[0] = e

t = threading.Thread(target=consume, daemon=True)
t.start()

# Give the poller a full cycle (it polls every 0.5s) with ONLY the backlog present.
time.sleep(0.7)
chk("backlog already in the ring buffer at connect time is NOT replayed", frames == [], frames)

# Now append two NEW lines — these are what the stream must tail.
cockpit_state._LOG.append(("LIVE line one", None))
cockpit_state._LOG.append(("LIVE line two", None))

t.join(timeout=3)
chk("stream is not still stuck (generator produced its two frames)", not t.is_alive())
chk("exactly two frames were emitted for the two new lines", len(frames) == 2, frames)
if len(frames) == 2:
    chk("frame 1 is a single 'data: <line>\\n\\n' event for line one",
        frames[0] == "data: LIVE line one\n\n", repr(frames[0]))
    chk("frame 2 is a single 'data: <line>\\n\\n' event for line two, in order",
        frames[1] == "data: LIVE line two\n\n", repr(frames[1]))
chk("the backlog line never appeared in any frame", not any("BACKLOG" in f for f in frames), frames)


# --- 3) graceful close ---------------------------------------------------------------------------- #
chk("closing the client connection (GeneratorExit) did not raise", close_ok[0] is True, str(close_err[0]))


# === EU-157 — frontend: cockpit page wires the terminal panel to the live SSE stream ============= #
# Static checks against the rendered `/` page HTML (the JS is never executed here — there is no JS
# engine in this harness — so each acceptance criterion is verified as a distinct, precise pattern
# in the emitted source, the same way the rest of this Python test suite verifies generated markup).
import re

client = app.test_client()
page_html = client.get("/?app=automatixy").get_data(as_text=True)

# Isolate the EU-157 IIFE specifically, so these checks can't accidentally match unrelated script
# (e.g. the board stream's own EventSource/onerror/setTimeout usage).
m = re.search(
    r"// EU-157:.*?\n\(function\(\)\{(.*?)\n\}\)\(\);",
    page_html, re.S,
)
chk("cockpit page contains an EU-157 terminal-stream IIFE", m is not None)
term_js = m.group(1) if m else ""

chk("constructs new EventSource(\"/api/terminal/stream\")",
    'new EventSource("/api/terminal/stream")' in term_js)

chk("message handler appends each frame into #termout via termout.appendChild",
    bool(re.search(r"onmessage\s*=\s*function\s*\([^)]*\)\s*\{[^}]*termout\.appendChild", term_js, re.S)))

chk("message handler auto-scrolls: termout.scrollTop=termout.scrollHeight",
    bool(re.search(r"onmessage\s*=\s*function\s*\([^)]*\)\s*\{.*?termout\.scrollTop\s*=\s*termout\.scrollHeight",
                    term_js, re.S)))

chk("streamed line content is HTML-escaped (< and & both handled) before insertion",
    bool(re.search(r"replace\(/&/g,\s*\"&amp;\"\)", term_js))
    and bool(re.search(r"replace\(/</g,\s*\"&lt;\"\)", term_js)))

# Status indicator: element exists with initial 'Connecting…' text, and the script sets
# 'Connected'/'Disconnected' from onopen/onerror.
stat_m = re.search(r'id=termstat class=termstat>([^<]*)<', page_html)
chk("terminal panel has a #termstat status element, initially 'Connecting…'",
    stat_m is not None and stat_m.group(1) == "Connecting…",
    stat_m.group(1) if stat_m else None)

chk("onopen sets the status text to 'Connected'",
    bool(re.search(r'onopen\s*=\s*function\s*\(\)\s*\{[^}]*setTermStat\("Connected"\)', term_js)))

chk("onerror sets the status text to 'Disconnected'",
    bool(re.search(r'onerror\s*=\s*function\s*\(\)\s*\{[^}]*setTermStat\("Disconnected"\)', term_js, re.S)))

# Reconnect: onerror closes the stale EventSource, then schedules a reconnect via setTimeout.
onerror_m = re.search(r"onerror\s*=\s*function\s*\(\)\s*\{(.*?)\};", term_js, re.S)
onerror_body = onerror_m.group(1) if onerror_m else ""
chk("onerror closes the stale EventSource before reconnecting", ".close()" in onerror_body, onerror_body)
chk("onerror schedules a reconnect via setTimeout(startTermStream, ...)",
    bool(re.search(r"setTimeout\(startTermStream\s*,\s*\d+\)", onerror_body)), onerror_body)

# No leak: a beforeunload listener closes the terminal EventSource.
beforeunload_m = re.search(
    r'addEventListener\("beforeunload"\s*,\s*function\s*\(\)\s*\{(.*?)\}\);', term_js, re.S,
)
chk("a beforeunload listener is registered to close the terminal EventSource",
    beforeunload_m is not None and ".close()" in beforeunload_m.group(1),
    beforeunload_m.group(1) if beforeunload_m else None)


passed = sum(1 for _, c, _ in results if c)
for n, c, d in results:
    print(f"  {'✓' if c else '✗'} {n}" + (f"  [{d}]" if (not c and d) else ""))
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
