"""EU-580 -- QA progress strip on cockpit home page.

iter-2 (review fix): adds poll-script integrity checks (AC6) — iter-1 shipped an
UNPARSEABLE inline <script> (the 'Phase 1/2…' JS string was split across two Python
literals and a stray second `)();` trailed the IIFE) yet every substring check stayed
green. AC6 extracts the qa-strip <script> body and verifies it is executable JS:
complete string literals, single IIFE close, balanced delimiters outside strings, and
— when node is on PATH — a real `node --check` parse. Also guards the label against
double-escaping ('&amp;amp;').

Exits non-zero on any failed check.
"""
import shutil, subprocess, sys, tempfile, threading, time, types, re
from pathlib import Path
sys.path.insert(0, ".")

# Stub dependencies BEFORE any orchestrator import
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req

class _SyncThread:
    """Runs target INLINE on start() -- deterministic."""
    def __init__(self, target=None, daemon=False, **kw): self.t = target
    def start(self):
        if self.t: self.t()

import orchestrator.cockpit_state as cockpit_state
import orchestrator.server as srv
from orchestrator import council as _council_mod
from orchestrator import patrol as _patrol_mod
from orchestrator.config import AppConfig, Config

srv.threading = types.SimpleNamespace(Thread=_SyncThread, Event=threading.Event, Lock=threading.Lock)

results: list[tuple[str, bool, str]] = []

def chk(name, condition, detail=""):
    results.append((name, bool(condition), detail))

tmp = Path(tempfile.mkdtemp())
af = tmp / "test_ajl.jsonl"
af.write_text("", encoding="utf-8")

cfg = Config(apps=[AppConfig(name="alpha", repo_path=str(tmp), base_branch="DEV", protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(af), use_worktree=False)
cfg.detected_auth = lambda: "test"
srv.health.summary = lambda c: {"healthy": True, "checks": []}

_old_patrol = _patrol_mod.patrol
_old_ship = _council_mod.ship_review

def _reset():
    cockpit_state.reset_run_state()

async def _fp_ok(c, app_name, do_file=True, audit=None):
    return _patrol_mod.PatrolSummary("ok", [])

async def _sr_ok(c, app_name, audit=None):
    return ""

_patrol_mod.patrol = _fp_ok
_council_mod.ship_review = _sr_ok

def _has_elapsed(html_body):
    return bool(re.search(r"\d{1,2}:\d{2}", html_body))

def _qa_form_block(html_body):
    """The <form action=/api/qa>…</form> chunk — scopes 'disabled' assertions to the Run QA button."""
    fs = html_body.find("action=/api/qa")
    fe = html_body.find("</form>", fs) if fs >= 0 else -1
    return html_body[fs:fe] if fs >= 0 and fe >= 0 else ""

def _qa_script(html_body):
    """Body of the qa-strip's inline <script> (the first <script> after <div class="qa-strip">)."""
    ds = html_body.find('<div class="qa-strip">')
    if ds < 0:
        return None
    s0 = html_body.find("<script>", ds)
    s1 = html_body.find("</script>", s0) if s0 >= 0 else -1
    return html_body[s0 + len("<script>"):s1] if s0 >= 0 and s1 >= 0 else None

def _js_balance(js):
    """Stack-check (), {}, [] OUTSIDE single-quoted string literals (with \\ escapes).
    Returns (ok, detail). This is the pure-Python guard that catches a script that can't
    possibly execute — independent of node being installed."""
    stack = []
    pairs = {")": "(", "}": "{", "]": "["}
    in_str = esc = False
    for ch in js:
        if in_str:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == "'": in_str = False
            continue
        if ch == "'":
            in_str = True
        elif ch in "({[":
            stack.append(ch)
        elif ch in ")}]":
            if not stack or stack.pop() != pairs[ch]:
                return False, f"unbalanced '{ch}'"
    if in_str:
        return False, "unterminated string literal"
    if stack:
        return False, f"unclosed {stack[-1]!r}"
    return True, ""

# AC1: Phase-1 strip visible immediately; elapsed ticking; button disabled
_reset()
st = cockpit_state.get_state()
st.update({"qa": True, "qa_phase": "patrol", "qa_started": time.time() - 42})
client = srv.create_app(cfg).test_client()
body = client.get("/").get_data(as_text=True)

chk("AC1: phase-1 label Phase 1/2 present", "Phase 1/2" in body, "missing Phase 1/2")
chk("AC1: phase-1 desc inspecting dev + filing findings", "inspecting dev" in body and "filing findings" in body)
chk("AC1: elapsed-time span mm:ss", _has_elapsed(body), "no mm:ss span found")
_qfb = _qa_form_block(body)
chk("AC1: Run QA button disabled", bool(_qfb) and "disabled" in _qfb, "no 'disabled' inside the /api/qa form block")

# AC2: Phase-2 label without reload
_reset()
st = cockpit_state.get_state()
st.update({"qa": True, "qa_phase": "ship_review", "qa_started": time.time() - 120})
client = srv.create_app(cfg).test_client()
body = client.get("/").get_data(as_text=True)

chk("AC2: phase-2 label Phase 2/2 present", "Phase 2/2" in body, "missing Phase 2/2")
chk("AC2: phase-2 desc ship verdict present", "ship verdict" in body, "missing ship verdict")

# AC3: qa_phase=None while active still renders phase-1 (claim-time race)
_reset()
st = cockpit_state.get_state()
st.update({"qa": True, "qa_phase": None, "qa_started": time.time() - 5})
client = srv.create_app(cfg).test_client()
body = client.get("/").get_data(as_text=True)

chk("AC3: phase-1 strip when qa=True & phase=None", "Phase 1/2" in body, "missing Phase 1/2 with qa_phase=None")
chk("AC3: elapsed when qa=True & phase=None", _has_elapsed(body), "no elapsed with qa_phase=None")

# AC4: No strip when QA not running; button NOT disabled
_reset()
client = srv.create_app(cfg).test_client()
body = client.get("/").get_data(as_text=True)

chk("AC4: no Phase 1/2 when qa falsy", "Phase 1/2" not in body, "Phase 1/2 leaked into idle page")
chk("AC4: no Phase 2/2 when qa falsy", "Phase 2/2" not in body, "Phase 2/2 leaked into idle page")
chk("AC4: no qa-strip div when qa falsy", '<div class="qa-strip">' not in body, "qa-strip leaked into idle page")
chk("AC4: button NOT disabled when qa falsy", "disabled" not in _qa_form_block(body), "disabled in qa form block")

# AC5: Inline poll script fetches /api/qa-status, updates elements by id
_reset()
st = cockpit_state.get_state()
st.update({"qa": True, "qa_phase": "patrol", "qa_started": time.time() - 10})
client = srv.create_app(cfg).test_client()
body = client.get("/").get_data(as_text=True)

chk("AC5: fetch /api/qa-status in inline script", "fetch('/api/qa-status')" in body, "poll URL not in body")
chk("AC5: qaphase element id present", "id=qaphase" in body or '"qaphase"' in body, "no #qaphase span id")
chk("AC5: qaelapsed element id present", "id=qaelapsed" in body or '"qaelapsed"' in body, "no #qaelapsed span id")

# AC6: Poll-script integrity — iter-1's script could not execute at all, but substring
# checks stayed green. These verify the extracted <script> body is real JS.
js = _qa_script(body)
chk("AC6: qa-strip <script> block extracted", js is not None and "fetch('/api/qa-status')" in js,
    "could not locate the qa-strip inline script")
if js is not None:
    # iter-1 bug #1: the phase-1 string was split across two Python literals, ending the JS
    # string at 'Phase 1/2' — assert BOTH labels appear as COMPLETE single-quoted JS literals.
    chk("AC6: phase-1 label is one complete JS string literal",
        "'Phase 1/2 — inspecting dev & filing findings'" in js,
        "phase-1 label missing or split inside the poll script")
    chk("AC6: phase-2 label is one complete JS string literal",
        "'Phase 2/2 — ship verdict'" in js,
        "phase-2 label missing or split inside the poll script")
    # iter-1 bug #2: a stray second ')();' trailed the IIFE. Exactly one IIFE close, at the end.
    chk("AC6: single IIFE close at script end",
        js.rstrip().endswith("})();") and js.count("})();") == 1,
        f"script tail {js.rstrip()[-12:]!r}, '}})();' count={js.count('})();')}")
    # Structural balance outside string literals — the language-agnostic catch-all.
    _ok, _det = _js_balance(js)
    chk("AC6: balanced () {} [] and quotes in poll script", _ok, _det)
    # <script> content is raw text — HTML entities are NOT decoded there; a '&amp;' inside a
    # JS string would render literally. (Also catches the ternary's split remnant.)
    chk("AC6: no '&amp;' entity inside the poll script", "&amp;" not in js,
        "entity inside <script> renders literally — use a plain '&'")
    # iter-1 bug #3: qlabel pre-escaped '&amp;' + html.escape() again → '&amp;amp;' in browser.
    chk("AC6: server label single-escaped (&amp; not &amp;amp;)",
        "inspecting dev &amp; filing findings" in body and "&amp;amp;" not in body,
        "phase-1 label double-escaped or missing in server HTML")
    # Gold standard: a real JS parser. Degrades to the structural checks when node is absent.
    if shutil.which("node"):
        jf = tmp / "qa_strip.js"
        jf.write_text(js + "\n", encoding="utf-8")
        _r = subprocess.run(["node", "--check", str(jf)], capture_output=True, text=True)
        _err = next((ln for ln in _r.stderr.splitlines() if "Error" in ln), _r.stderr.strip().splitlines()[0] if _r.stderr.strip() else "?")
        chk("AC6: poll script parses (node --check)", _r.returncode == 0,
            "node --check failed: " + _err)
    else:
        print("  (note: node not on PATH — poll script checked structurally only)")

_patrol_mod.patrol = _old_patrol
_council_mod.ship_review = _old_ship

print("\n============ EU-580 QA PROGRESS STRIP TEST ===========================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-" * 72)
print(f"  {passed}/{len(results)} passed")
if passed == len(results):
    print("  RESULT: ALL GREEN")
else:
    print(f"  RESULT: {len(results) - passed} FAIL(s)")
sys.exit(0 if passed == len(results) else 1)
