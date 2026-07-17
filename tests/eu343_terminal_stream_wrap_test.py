"""EU-343 — /api/terminal/stream must not silently swallow lines when the ring buffer wraps.

``terminal_stream_api()``'s generator bookmarks its position in ``cockpit_state._LOG``
(a ``deque(maxlen=600)``) by object identity. When more than 600 lines land between two 0.5s
polls the bookmark falls off the back of the deque and the ``idx is None`` branch resyncs.

The ticket's stated failure mode — DUPLICATE replay — cannot actually happen and this harness
does not assert it: ``bookmark`` is only ever assigned ``new_items[-1]``, so it is always the
deque's tail, and the only production mutation is a single tail append. Everything in a resync
snapshot is therefore strictly newer than the bookmark and was never sent. (A spurious ``is``
match is impossible too — the bookmark holds a strong reference, so the tuple's address cannot
be reused.)

The REAL defect is the inverse, and it is the first option the ticket's own acceptance criteria
allow: the resync silently DROPS the (appends - 600) lines in the gap. The operator watching the
terminal during an incident — exactly when a burst like a large stack-trace dump happens — gets a
clean-looking stream with a hole in it and no way to know. This pins:

  * a wrap emits a visible gap marker, exactly once, before the resync;
  * no line the client already received is replayed after the gap;
  * the un-wrapped common case is byte-identical to before (no marker, no behaviour change).
"""
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

from orchestrator import cockpit_state, server  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

results: list[tuple[str, bool, str]] = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
(tmp / "audit.jsonl").write_text("", encoding="utf-8")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
app = server.create_app(cfg)

MAXLEN = cockpit_state._LOG.maxlen
chk("precondition: the ring buffer is the deque(maxlen=600) the ticket describes",
    MAXLEN == 600, f"maxlen={MAXLEN}")


def _open_stream():
    """Connect a client to the SSE generator and start draining it on a background thread."""
    with app.test_request_context("/api/terminal/stream"):
        resp = app.view_functions["terminal_stream_api"]()
    gen = resp.response
    frames: list[str] = []

    def _consume():
        try:
            for chunk in gen:
                frames.append(chunk if isinstance(chunk, str)
                              else chunk.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 — closed from the main thread below
            pass

    t = threading.Thread(target=_consume, daemon=True)
    t.start()
    return gen, frames, t


def _close(gen, t):
    try:
        gen.close()
    except Exception:  # noqa: BLE001
        pass
    t.join(timeout=2)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 1) WRAP: >600 lines land between two polls -> one gap marker, no duplicates, no silent hole
# ══════════════════════════════════════════════════════════════════════════════════════════════
cockpit_state._LOG.clear()
gen, frames, t = _open_stream()
time.sleep(0.7)          # let the stream connect and bookmark an empty buffer

cockpit_state._LOG.append(("EARLY-LINE-0", None))
time.sleep(0.7)          # ...and receive that first line, so it holds a real bookmark
chk("precondition: the stream delivered the pre-burst line", "EARLY-LINE-0" in "".join(frames),
    "".join(frames)[:120])
_before = len(frames)

# The burst: 900 lines in one poll window — the bookmark is pushed off the back of the deque.
for i in range(900):
    cockpit_state._LOG.append((f"BURST-{i}", None))
time.sleep(1.2)
_close(gen, t)

body = "".join(frames)
chk("a wrap emits a visible gap marker (EU-343)",
    "lines dropped" in body,
    "the stream resynced silently — the operator cannot tell lines are missing")
chk("the gap marker is emitted exactly once per wrap (EU-343)",
    body.count("lines dropped") == 1, f"count={body.count('lines dropped')}")
chk("the marker is a well-formed SSE data frame",
    any(f.startswith("data: ") and "lines dropped" in f for f in frames),
    str([f for f in frames if "lines dropped" in f])[:160])
chk("the surviving tail of the burst is still delivered after the gap (EU-343)",
    "BURST-899" in body, body[-200:])
chk("no line already sent before the wrap is replayed after it (EU-343)",
    body.count("EARLY-LINE-0") == 1, f"count={body.count('EARLY-LINE-0')}")
# The gap marker must come BEFORE the resynced lines, or it labels the wrong side of the hole.
_gap_at = next((i for i, f in enumerate(frames) if "lines dropped" in f), None)
_first_burst_at = next((i for i, f in enumerate(frames) if "BURST-" in f), len(frames))
chk("the gap marker precedes the resynced lines (EU-343)",
    _gap_at is not None and _gap_at < _first_burst_at,
    f"marker at {_gap_at}, first resynced line at {_first_burst_at}")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 2) COMMON CASE: an un-wrapped buffer behaves exactly as before — no marker, no change
# ══════════════════════════════════════════════════════════════════════════════════════════════
cockpit_state._LOG.clear()
cockpit_state._LOG.append(("BACKLOG that must never be replayed", None))
gen2, frames2, t2 = _open_stream()
time.sleep(0.7)
chk("backlog present at connect time is still never replayed (EU-154 contract)",
    frames2 == [], str(frames2))

cockpit_state._LOG.append(("LIVE one", None))
cockpit_state._LOG.append(("LIVE two", None))
time.sleep(0.9)
_close(gen2, t2)

body2 = "".join(frames2)
chk("the un-wrapped path emits no gap marker (EU-343: no behaviour change)",
    "lines dropped" not in body2, body2[:160])
chk("the un-wrapped path still emits one plain data frame per new line (EU-154 contract)",
    [f for f in frames2 if f.startswith("data: ")] == ["data: LIVE one\n\n", "data: LIVE two\n\n"],
    str(frames2))

print("\n============ EU-343 TERMINAL-STREAM WRAP QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
