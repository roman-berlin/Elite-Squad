"""EU-726 — Chat composer: double-Enter sends are deduped by a shared disable/re-enable helper.

Two layers of coverage over the *served* page (the guard lives in inline JS inside GET /chat):

A. STATIC — the served composer script has one consistent disable/re-enable convention:
  1. A single ``setSending(on)`` helper toggles the ``sending`` flag AND the disabled state of
     BOTH ``chatinput`` and the Send ``<button>`` together.
  2. The submit handler guards ``if(sending)return;`` before anything else (blocks duplicates).
  3. ``setSending(true)`` runs synchronously BEFORE the fetch (the box locks on the first Enter).
  4. ``setSending(false)`` runs on EVERY settle path — empty-text early-return, the ``!ok``
     error branch, and the success fall-through — so the box always re-enables.
  5. No bare ``sending=true/false`` toggles survive outside the declaration/helper init (one
     convention, not a flag plus ad-hoc disables).
  6. EU-307 behaviour intact (preventDefault / focus / chaterr / r.ok guard).

B. BEHAVIOURAL — the served script is eval'd in node (when available) against a stub DOM and
   driven like a real browser session:
  7. Rapid double-Enter sends exactly ONE /api/chat request; input+button are disabled
     synchronously while the request is in flight and re-enabled once it settles.
  8. A held-down Enter (several submits while the first fetch hangs) still sends once.
  9. Error paths (network throw AND HTTP 500) re-enable input+button and keep the typed text.
 10. An empty submit neither sends nor leaves the box disabled.
   (If node is not on PATH the behavioural block prints SKIP and only the static layer counts —
   CI and the dev box both have node.)
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

# ── minimal SDK / requests stubs so the orchestrator can import without real deps ──
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import server
from orchestrator.config import AppConfig, Config

# ── shared config / Flask test client ───────────────────────────────────────────
_TMP = Path(tempfile.mkdtemp())
_CFG = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_TMP), base_branch="DEV",
                     protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(_TMP / "audit.jsonl"),
    use_worktree=False,
)
_CFG.detected_auth = lambda: "test"

_APP = server.create_app(_CFG)
_CLIENT = _APP.test_client()

# ── result accumulator ──────────────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


resp = _CLIENT.get("/chat")
body = resp.get_data(as_text=True)

chk("GET /chat: HTTP 200", resp.status_code == 200, f"status={resp.status_code}")

# =============================================================================
# A. STATIC — extract the composer <script> block for focused assertions
# =============================================================================
composer_match = re.search(r'<div class=composer>.*?</script>', body, re.S)
assert composer_match is not None, "no composer block found"
composer_block = composer_match.group(0)
script_match = re.search(r'<script>(.*)</script>', composer_block, re.S)
assert script_match is not None, "no inline script in the composer block"
composer_js = script_match.group(1)

# (1) one shared helper toggles the flag AND both disabled states together
helper_m = re.search(r'function setSending\(on\)\{([^}]+)\}', composer_js)
helper_body = helper_m.group(1) if helper_m else ""
chk("composer defines a single shared setSending(on) helper",
    helper_m is not None, "function setSending(on){…} not found")
chk("helper toggles the `sending` flag", "sending=on" in helper_body, helper_body)
chk("helper disables/re-enables the chat input", "chatinput.disabled=on" in helper_body, helper_body)
chk("helper disables/re-enables the Send button", "sendbtn.disabled=on" in helper_body, helper_body)
chk("the Send button is captured from the composer form",
    'chatform.querySelector("button")' in composer_js, composer_js[:400])

# (2) re-entry guard: if(sending)return; — and it runs before the box locks
guard_idx = composer_js.find("if(sending)return;")
lock_idx = composer_js.find("setSending(true)")
chk("submit handler guards on `sending` before doing anything (blocks duplicates)",
    guard_idx != -1, composer_js)
chk("the guard precedes the disable call",
    guard_idx != -1 and lock_idx != -1 and guard_idx < lock_idx,
    f"guard@{guard_idx}, setSending(true)@{lock_idx}")

# (3) the box locks synchronously on the first Enter — before any fetch
fetch_idx = composer_js.find('fetch("/api/chat"')
chk("setSending(true) runs BEFORE the fetch call (synchronous lock)",
    lock_idx != -1 and fetch_idx != -1 and lock_idx < fetch_idx,
    f"setSending(true)@{lock_idx}, fetch@{fetch_idx}")

# (4) setSending(false) on every settle path
chk("empty-text submits re-enable via setSending(false) (no permanent lockout)",
    "if(!text.trim()){setSending(false);return;}" in composer_js, composer_js)
err_section_m = re.search(r'if\(!ok\)\{([^}]+)', composer_js)
chk("error branch re-enables via setSending(false) before its return",
    err_section_m is not None and "setSending(false)" in err_section_m.group(1),
    f"error section: {err_section_m.group(1) if err_section_m else 'not found'}")
err_return_idx = composer_js.find("return;}", composer_js.find("if(!ok){"))
success_tail = composer_js[err_return_idx:] if err_return_idx != -1 else ""
chk("success path re-enables via setSending(false) after the error-return",
    "setSending(false)" in success_tail, success_tail[:200])

# (5) one convention: no bare sending= toggles outside the declaration; all flips via the helper
chk("exactly one bare `sending=` (the init) — every flip goes through setSending",
    len(re.findall(r'sending=(?:true|false)', composer_js)) == 1
    and len(re.findall(r'sending=true', composer_js)) == 0,
    f"bare toggles: {re.findall(r'sending=(?:true|false)', composer_js)}")
chk("setSending(false) is called on all three settle branches",
    len(re.findall(r'setSending\(false\)', composer_js)) >= 3,
    f"{len(re.findall(r'setSending.false.', composer_js))} resets found")
chk("setSending(true) is called exactly once (the first Enter)",
    len(re.findall(r'setSending\(true\)', composer_js)) == 1, composer_js)

# (6) EU-307 behaviour still present (no regression)
chk("preventDefault still present", "preventDefault" in composer_js, composer_js)
chk("focus() still called after send", ".focus()" in composer_js, composer_js)
chk("chaterr element still present", 'id=chaterr' in composer_block, composer_block)
chk("r.ok still gates the success branch", "r.ok" in composer_js, composer_js)

# =============================================================================
# B. BEHAVIOURAL — eval the served script in node against a stub DOM
# =============================================================================
_NODE = shutil.which("node")
if _NODE is None:
    print("  [SKIP] behavioural node harness (node not on PATH — static layer only)")
else:
    _HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function makeClassList() {
  const s = new Set();
  return { add: c => s.add(c), remove: c => s.delete(c), contains: c => s.has(c) };
}
function makeEl(id) {
  return {
    id, value: '', disabled: false, textContent: '', innerHTML: '', dataset: {},
    _listeners: {}, classList: makeClassList(),
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    querySelector(sel) { return sel === 'button' ? sendbtn : null; },
    querySelectorAll() { return []; },
    focus() { focused = this.id; },
  };
}
var sendbtn = makeEl('sendbtn');
var chatform = makeEl('chatform');
var chatinput = makeEl('chatinput'); chatinput.value = 'hello cto';
var chaterr = makeEl('chaterr');
var focused = null;
var fetchCalls = [];
var fetchMode = 'ok';           // ok | httperr | throw | slow
var slowResolvers = [];
function fetch(url, opts) {
  fetchCalls.push({ url });
  if (url.indexOf('/api/chat-thread') === 0) return Promise.resolve({ ok: false, status: 404 });
  if (fetchMode === 'throw') return Promise.reject(new Error('network down'));
  if (fetchMode === 'httperr') return Promise.resolve({ ok: false, status: 500 });
  if (fetchMode === 'slow') return new Promise(res => slowResolvers.push(res));
  return Promise.resolve({ ok: true, status: 200 });
}
function FormData(form) { this.form = form; }
var document = {
  getElementById(id) { return { chatform, chatinput, chaterr }[id] || null; },
  createElement() { return makeEl('gen'); },
  addEventListener() {},
  body: { scrollHeight: 1000 },
};
var window = { scrollTo() {}, innerHeight: 800, scrollY: 200 };
function setInterval() {}

eval(src);                       // direct eval: the script's var bindings land in this scope

const out = {};
const ev = { preventDefault() {} };
const sends = () => fetchCalls.filter(c => c.url === '/api/chat').length;
const tick = () => new Promise(r => setImmediate(r));
const handler = chatform._listeners.submit[0];
if (!handler) { console.log(JSON.stringify({ error: 'no submit handler registered' })); process.exit(0); }

(async function main() {
  // 1. rapid double-Enter → one request; locked in flight; re-enabled on success settle
  fetchCalls.length = 0;
  const p1 = handler(ev);
  out.rapidLockedInFlight = chatinput.disabled === true && sendbtn.disabled === true;
  const p2 = handler(ev);        // second Enter while the first is in flight
  await Promise.all([p1, p2]);
  out.rapidSendCount = sends();
  out.rapidReEnabled = chatinput.disabled === false && sendbtn.disabled === false;
  out.successClearedInput = chatinput.value === '';

  // 2. held-down Enter: first fetch hangs; three more Enters must not queue more sends
  chatinput.value = 'held enter message';   // scenario 1's success cleared the box
  fetchMode = 'slow'; fetchCalls.length = 0;
  const slow = handler(ev);
  out.heldLockedInFlight = chatinput.disabled === true && sendbtn.disabled === true;
  handler(ev); handler(ev); handler(ev);
  await tick();
  out.heldSendCountWhilePending = sends();
  slowResolvers.forEach(r => r({ ok: true, status: 200 }));
  await slow;
  out.heldReEnabled = chatinput.disabled === false && sendbtn.disabled === false;

  // 3. failure paths re-enable and keep the typed text
  for (const mode of ['throw', 'httperr']) {
    fetchMode = mode; fetchCalls.length = 0;
    chatinput.value = 'retry me';
    await handler(ev);
    out[mode + 'SendCount'] = sends();
    out[mode + 'ReEnabled'] = chatinput.disabled === false && sendbtn.disabled === false;
    out[mode + 'TextKept'] = chatinput.value === 'retry me';
  }
  out.errorSurfaced = chaterr.classList.contains('on');

  // 4. empty submit: no request, no stuck-disabled box
  fetchMode = 'ok'; fetchCalls.length = 0;
  chatinput.value = '   ';
  await handler(ev);
  out.emptySendCount = sends();
  out.emptyReEnabled = chatinput.disabled === false && sendbtn.disabled === false;

  console.log(JSON.stringify(out));
})().catch(e => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(2); });
"""
    _work = Path(tempfile.mkdtemp(prefix="eu726-"))
    (_work / "composer.js").write_text(composer_js)
    (_work / "harness.js").write_text(_HARNESS)
    _run = subprocess.run([_NODE, str(_work / "harness.js"), str(_work / "composer.js")],
                          capture_output=True, text=True, timeout=60)
    assert _run.returncode == 0, f"node harness crashed: {_run.stdout} {_run.stderr}"
    _beh = json.loads(_run.stdout.strip().splitlines()[-1])
    assert "error" not in _beh, f"node harness error: {_beh['error']}"

    chk("double-Enter: input+button disabled synchronously while the request is in flight",
        _beh["rapidLockedInFlight"], _beh)
    chk("double-Enter: rapid second Enter sends exactly ONE request",
        _beh["rapidSendCount"] == 1, f"sends={_beh['rapidSendCount']}")
    chk("double-Enter: input+button re-enabled once the request settles (success)",
        _beh["rapidReEnabled"], _beh)
    chk("success still clears the input (normal single-Enter behaviour unchanged)",
        _beh["successClearedInput"], _beh)
    chk("held Enter: box locked while the first request hangs", _beh["heldLockedInFlight"], _beh)
    chk("held Enter: 3 extra submits during the in-flight POST add no requests",
        _beh["heldSendCountWhilePending"] == 1, f"sends={_beh['heldSendCountWhilePending']}")
    chk("held Enter: re-enabled after the hanging request settles", _beh["heldReEnabled"], _beh)
    chk("network throw: exactly one request, box re-enabled, typed text kept",
        _beh["throwSendCount"] == 1 and _beh["throwReEnabled"] and _beh["throwTextKept"], _beh)
    chk("HTTP 500: exactly one request, box re-enabled, typed text kept",
        _beh["httperrSendCount"] == 1 and _beh["httperrReEnabled"] and _beh["httperrTextKept"], _beh)
    chk("failure surfaces the inline error element", _beh["errorSurfaced"], _beh)
    chk("empty submit sends nothing and leaves the box enabled",
        _beh["emptySendCount"] == 0 and _beh["emptyReEnabled"], _beh)

# =============================================================================
# Summary
# =============================================================================
passed_n = sum(1 for _, ok, _ in results if ok)
print("\n========= EU-726 /chat double-Enter dedupe tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("---------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
