"""send() folds long messages, and the Planner sizes by TIME as well as file count (2026-07-22).

Complements the EU-62 harness (tests/phone_brevity_test.py), which pins `bulletize`/`report_brief`
themselves. This one pins the two NEW contracts:

  · "the automode message is too long and cut in TG". Nothing was lost — EU-358 chunks anything over
    Telegram's 4096 ceiling — but a report arriving as three phone notifications is still unreadable.
    Rather than hunt each verbose call site (and re-hunt every new one), the fold now happens at
    ``notify.send``: the ONE seam every officer message passes through. Deliberately the
    DETERMINISTIC bulletizer, never the model — this sits on the notification path of every event,
    so it must add no latency, no cost and no failure mode of its own.

  · "why didn't they split it BEFORE the implementation?" EU-438 timed out at 3600s, was re-planned
    as BUILD unchanged, and timed out again. The Planner's SPLIT rule was purely FILE SPAN ("more
    than 6-8 independent files") — which an investigation ticket can never trigger: bounded in files,
    unbounded in time. The prior-attempts digest DID reach the Planner (243 chars, carrying the
    failure); it simply had no rule saying a previous wall-clock timeout is proof of oversize.

Pins:
  1. an over-long message folds, and the header line (emoji + ticket id) survives verbatim;
  2. short messages are returned byte-identical — most traffic must be untouched;
  3. the fold can never lose a message: hostile input returns something, never raises;
  4. send() actually applies it, and does so WITHOUT a model call;
  5. the Planner treats a prior wall-clock timeout as proof of oversize;
  6. open-ended investigation is named as its own size class, with a stopping condition.
"""
import pathlib
import sys
import types

_req = types.ModuleType("requests")
_req.Session = lambda *a, **k: None
_req.RequestException = Exception
_req.post = lambda *a, **k: None
sys.modules["requests"] = _req
_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda _n: _D
sys.modules["claude_agent_sdk"] = _sdk
sys.path.insert(0, ".")

from orchestrator import notify

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

head = "\U0001f528 EU-438 started — investigate the flaky harness"
long_msg = head + "\n" + ("Some detailed prose about what the builder is doing. " * 60)
out = notify.brief_for_phone(long_msg)

chk("(1a) an over-long message is folded",
    len(out) < len(long_msg) // 2, f"{len(long_msg)}->{len(out)}")
chk("(1b) it lands within a phone screenful",
    len(out) <= notify._PHONE_MAX_CHARS, len(out))
chk("(1c) the header line survives verbatim",
    out.splitlines()[0] == head, out.splitlines()[0][:60])
chk("(1d) the fold produced bullets, not a prefix cut",
    "• " in out and "full report in the cockpit" not in out.lower())

for short in ("ok", head, "❌ EU-1 — error: boom", "line one\nline two"):
    chk(f"(2) short message untouched: {short[:22]!r}",
        notify.brief_for_phone(short) == short)

for bad in (None, "", "x" * 5000, "\n\n\n", "header only\n"):
    try:
        r = notify.brief_for_phone(bad)
        ok = (r == bad) or (isinstance(r, str) and r.strip() != "")
    except Exception:  # noqa: BLE001
        ok = False
    chk(f"(3) hostile input survives: {str(bad)[:16]!r}", ok)

nsrc = pathlib.Path("orchestrator/notify.py").read_text(encoding="utf-8")
chk("(4a) send() applies the fold", "text = brief_for_phone(text)" in nsrc)
_body = nsrc.split("def brief_for_phone")[1].split("def send")[0]
chk("(4b) the fold is deterministic — no model call on the notify path",
    "report_brief" not in _body and "await" not in _body)

psrc = pathlib.Path("orchestrator/planner.py").read_text(encoding="utf-8")
chk("(5a) a prior wall-clock timeout is proof of oversize",
    "PRIOR WALL-CLOCK TIMEOUT" in psrc and "Prior attempts" in psrc)
chk("(5b) it names the case that taught it", "EU-438" in psrc and "3600s" in psrc)
chk("(6a) open-ended investigation is its own size class",
    "OPEN-ENDED INVESTIGATION" in psrc and "unbounded in TIME" in psrc)
chk("(6b) it demands a definite stopping condition", "stopping condition" in psrc)

print("\n========== SEND BREVITY + TIME-AWARE SIZING ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
