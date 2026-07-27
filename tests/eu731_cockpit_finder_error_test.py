"""EU-731 — "Open logs" Finder fetch now surfaces errors instead of failing silently.

The ``_control_bar`` function emits a macOS-only "Finder" link that fires
``fetch('/api/open-logs')`` behind the scenes.  Before this fix the handler was:

    onclick="fetch(this.href);return false"       ← fire-and-forget, no error handling

After the fix every non-ok response or rejected promise shows a visible inline error span.

iter-2 (review fix): iter-1 shipped the handler as
``fetch(this.href).then(function(r){…}.catch(function(e){…}))`` — the ``.catch`` was
attached to the FUNCTION EXPRESSION (and a ``)`` short), so the rendered onclick did not
even PARSE (``missing ) after argument list``): clicking never fetched, never surfaced an
error, and — since ``return false`` never ran — navigated away from the page. Yet every
iter-1 substring check stayed green. iter-2 fixes the placement
(``.then(function(r){…}).catch(function(e){…})``) and strengthens this harness the way
eu539's AC6 did: the extracted onclick is bracket-balanced in pure Python AND — when node
is on PATH — actually EXECUTED with stubbed fetch/document against three scenarios:

  1. HTTP 200           → fetch fires, returns false, NO visible change (spans stay hidden)
  2. non-ok (500)       → error span visible with the REAL status code ("…:500")
  3. rejected (network) → error span visible with the rejection reason; rejection handled

Exits non-zero on any failed check.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

# Stub the Agent SDK so the orchestrator imports cleanly with no network / models.
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


_sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = _sdk
sys.path.insert(0, ".")

from orchestrator import cockpit_views as V  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ── Fixture: minimal Config + temp working dir ──────────────────────────────────────
tmp = Path(tempfile.mkdtemp())

app = AppConfig(name="alpha", repo_path=str(tmp), base_branch="DEV",
                protected_branch="MAIN", backlog_backend="none")
cfg = Config(apps=[app], audit_path=str(tmp / "test_ajl.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"

# ── 1) Mac: error-display markup present in rendered output ─────────────────────────
mac_bar = V._control_bar(cfg, current_app="alpha", is_mac=True)

chk("mac bar contains the error span (EU-731/1)",
    'id=finder-err-span' in mac_bar, "error span not rendered")
chk("error span has aria-live=polite (EU-731/1)",
    'aria-live="polite"' in mac_bar, "screen-reader live region missing")
chk("spans start hidden (EU-731/1)",
    'display:none' in mac_bar, "spans should be display:none initially")

# ── 2) Non-ok path: r.ok check present in JS (EU-731 AC1) ─────────────────────────
chk("r.ok guard is present (EU-731/2a)",
    "!r.ok" in mac_bar,
    "no r.ok check — non-ok responses will fail silently")
chk("non-ok path throws an Error with status code (EU-731/2b)",
    "throw Error(String(r.status))" in mac_bar,
    "non-ok response must surface the HTTP status code")

# ── 3) Network-error path: .catch() present in JS (EU-731 AC2) ─────────────────────
chk(".catch() handler present (EU-731/3a)",
    ".catch(function(e)" in mac_bar,
    "no .catch() — network errors will fail silently")
chk("error span text set with 'Failed to open log:' prefix (EU-731/3b)",
    "Failed to open log:" in mac_bar,
    "error message must have a user-readable prefix")
chk("error span displayed via style.display='block' (EU-731/3c)",
    "style.display='block'" in mac_bar,
    "error span must become visible on error")
chk("catch reads e.message || e for the reason (EU-731/3d)",
    "(e.message||e)" in mac_bar,
    "error reason should include the thrown Error's message or fallback string")

# ── 4) Success path unchanged — fetch(this.href) still fires normally (EU-731 AC3) ──
chk("fetch(this.href) still fires (EU-731/4a)",
    "fetch(this.href)" in mac_bar,
    "success path must still call fetch")
chk("onclick still returns false (EU-731/4b)",
    "return false" in mac_bar,
    "click must not navigate away")

# ── 5) iter-1 regression guard: .catch attaches to the PROMISE CHAIN, not the
#       function expression. iter-1 rendered `…'Opened.';}.catch(function(e)…` —
#       `function(r){…}.catch(…)` — which (with the missing `)`) did not even parse.
#       The corrected close is `'Opened.';}).catch(` — `)` closes `.then(` FIRST.
chk(".catch chains on .then() — ')' closes then before catch (EU-731/5)",
    "'Opened.';}).catch(function(e)" in mac_bar,
    "'.catch' must follow '}).', not '}.', or it attaches to the function expression")

# ── 6) Non-Mac: no Finder link or error spans rendered (regression guard) ──────────
nomac_bar = V._control_bar(cfg, current_app="alpha", is_mac=False)

chk("non-mac bar has NO error span (EU-731/6a)",
    'finder-err-span' not in nomac_bar,
    "error span leaked into non-mac output")
chk("non-mac bar has NO Finder link (EU-731/6b)",
    '/api/open-logs' not in nomac_bar,
    "Finder link leaked into non-mac output")


# ── 7) Execute the extracted onclick JS (the iter-1 hole: substring checks stayed
#       green while the handler was unparseable). Two layers: pure-Python bracket
#       balance (always runs), then real execution under node when available. ────────
def _js_balance(js):
    """Stack-check (), {}, [] OUTSIDE single-quoted string literals (with \\ escapes).
    Returns (ok, detail) — the node-independent guard against a handler that cannot
    possibly execute (same helper as eu539's AC6)."""
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


_m = re.search(r'href="/api/open-logs"\s+onclick="([^"]+)"', mac_bar)
onclick_js = _m.group(1) if _m else None

chk("Finder onclick handler extracted from rendered HTML (EU-731/7a)",
    onclick_js is not None and onclick_js.startswith("try{fetch(this.href)"),
    "could not locate/extract the Finder link onclick JS")

if onclick_js is not None:
    _ok, _det = _js_balance(onclick_js)
    chk("onclick JS brackets balanced outside strings (EU-731/7b)", _ok,
        _det or "iter-1 rendered an unbalanced handler — this is the class guard")

    if shutil.which("node"):
        # Run the handler for real with stubbed fetch/document and inspect span state.
        _DRIVER = r"""
const ONCLICK = __ONCLICK_JSON__;
function mkWorld() {
  const spans = new Map();
  const doc = { getElementById(id) {
    if (!spans.has(id)) spans.set(id, { textContent: "", style: { display: "none" } });
    return spans.get(id);
  } };
  return { spans, doc };
}
async function run(fetchImpl) {
  const { spans, doc } = mkWorld();
  global.document = doc;
  const unhandled = [];
  const onU = (r) => unhandled.push(String((r && r.message) || r));
  process.on("unhandledRejection", onU);
  let called = null, ret = null, syncThrow = null;
  global.fetch = (u) => { called = u; return fetchImpl(); };
  try {
    ret = new Function(ONCLICK).call({ href: "http://t.local/api/open-logs" });
  } catch (e) {
    syncThrow = String((e && e.message) || e);
  }
  await new Promise((r) => setTimeout(r, 10));   // let then/catch microtasks settle
  process.off("unhandledRejection", onU);
  const g = (id) => { const s = spans.get(id);
    return { text: s ? s.textContent : "", display: s ? s.style.display : "never-created" }; };
  return { called, ret, syncThrow, unhandled, ok: g("finder-ok-span"), err: g("finder-err-span") };
}
const out = {};
out.s200 = await run(() => Promise.resolve({ ok: true, status: 200 }));
out.s500 = await run(() => Promise.resolve({ ok: false, status: 500 }));
out.snet = await run(() => Promise.reject(new TypeError("Failed to fetch (ECONNREFUSED)")));
console.log("RESULT_JSON " + JSON.stringify(out));
""".replace("__ONCLICK_JSON__", json.dumps(onclick_js))
        _jf = tmp / "eu731_onclick_driver.mjs"
        _jf.write_text(_DRIVER, encoding="utf-8")
        try:
            _r = subprocess.run(["node", str(_jf)], capture_output=True, text=True, timeout=30)
            _line = next((ln for ln in _r.stdout.splitlines() if ln.startswith("RESULT_JSON ")), None)
            _res = json.loads(_line[len("RESULT_JSON "):]) if _line else None
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            _res = None
            _r = types.SimpleNamespace(returncode=1, stderr=f"harness driver error: {exc!r}", stdout="")
        chk("onclick driver executed under node (EU-731/7c)",
            _res is not None and _r.returncode == 0,
            "node run failed: " + (_r.stderr.strip().splitlines() or ["?"])[-1])

        if _res is not None:
            s = _res["s200"]
            chk("exec 200: fetch dispatched with link href (EU-731/AC3)",
                s["called"] == "http://t.local/api/open-logs", str(s["called"]))
            chk("exec 200: returns false — click never navigates (EU-731/AC3)",
                s["ret"] is False, f"ret={s['ret']!r} syncThrow={s['syncThrow']!r}")
            chk("exec 200: no sync throw / no unhandled rejection (EU-731/AC3)",
                s["syncThrow"] is None and not s["unhandled"],
                f"syncThrow={s['syncThrow']!r} unhandled={s['unhandled']}")
            chk("exec 200: visible state UNCHANGED — err span never shown (EU-731/AC3)",
                s["err"]["text"] == "" and s["err"]["display"] != "block",
                f"err={s['err']}")

            s = _res["s500"]
            chk("exec non-ok 500: err span made VISIBLE (EU-731/AC1)",
                s["err"]["display"] == "block", f"err={s['err']} syncThrow={s['syncThrow']!r}")
            chk("exec non-ok 500: message shows the REAL status code (EU-731/AC1)",
                s["err"]["text"].startswith("Failed to open log:") and "500" in s["err"]["text"],
                f"text={s['err']['text']!r}")
            chk("exec non-ok 500: returns false, rejection not left unhandled (EU-731/AC1)",
                s["ret"] is False and not s["unhandled"],
                f"ret={s['ret']!r} unhandled={s['unhandled']}")

            s = _res["snet"]
            chk("exec network error: err span made VISIBLE (EU-731/AC2)",
                s["err"]["display"] == "block", f"err={s['err']} syncThrow={s['syncThrow']!r}")
            chk("exec network error: message shows the rejection reason (EU-731/AC2)",
                s["err"]["text"].startswith("Failed to open log:") and "ECONNREFUSED" in s["err"]["text"],
                f"text={s['err']['text']!r}")
            chk("exec network error: rejection HANDLED by .catch (EU-731/AC2)",
                not s["unhandled"] and s["syncThrow"] is None,
                f"unhandled={s['unhandled']} syncThrow={s['syncThrow']!r}")
    else:
        print("  (note: node not on PATH — onclick JS checked by balance + substrings only)")

print("\n============ EU-731 COCKPIT FINDER ERROR SURFACING QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
