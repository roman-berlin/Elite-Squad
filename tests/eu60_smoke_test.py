"""EU-60 post-merge smoke runner QA: gating (armed + per-app command), and the flag-only post-merge
behaviour — pass keeps quiet (audit smoke_pass, no alert), red FLAGS (audit smoke_fail + Telegram)
WITHOUT ever reverting, and a runner that throws is itself treated as a red flag. The contrast with the
SRE (sentinel) is the whole point: smoke surfaces a broken DEV, it does not roll the merge back."""
import sys, types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import smoke
from orchestrator.config import Config
from orchestrator.contracts import GateResult

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace

# Capture Telegram so we can assert the flag fired (and never let a real send happen).
sent: list[str] = []
smoke.notify.send = lambda text, *a, **k: (sent.append(text), True)[1]

class Audit:
    def __init__(s): s.events = []
    def record(s, kind, **kw): s.events.append((kind, kw))

def app(cmd):
    return ns(name="automatixy", base_branch="DEV", smoke_command=cmd,
              workdir="/tmp", repo_path="/tmp", gate_timeout_sec=30, gate_env={})

on = Config(apps=[], audit_path="/tmp/x.jsonl", smoke_enabled=True)
off = Config(apps=[], audit_path="/tmp/x.jsonl", smoke_enabled=False)

# --- gating -------------------------------------------------------------------
chk("should_run OFF when smoke disabled", not smoke.should_run(off, app("npx playwright test")))
chk("should_run OFF when no smoke command", not smoke.should_run(on, app(None)))
chk("should_run ON when enabled + smoke command", smoke.should_run(on, app("npx playwright test")))

# smoke takes NO git handle — it structurally cannot revert (the SRE owns rollback). (EU-60)
import inspect
chk("smoke.run has no `git` parameter (cannot revert)", "git" not in inspect.signature(smoke.run).parameters)

# --- run PASS -> quiet, audit smoke_pass, NO alert ---------------------------
sent.clear()
smoke.gate.run_commands = lambda a, c: GateResult(passed=True, report="green")
au = Audit()
ok, note = smoke.run(on, app("npx playwright test"), ns(id="AUTO-1", ephemeral=False), au)
chk("run pass -> True", ok)
chk("run pass -> audit smoke_pass", any(k == "smoke_pass" for k, _ in au.events))
chk("run pass -> NO Telegram alert", not sent)

# --- run RED -> FLAG (audit smoke_fail + Telegram), no revert ----------------
sent.clear()
smoke.gate.run_commands = lambda a, c: GateResult(passed=False, report="auth-redirect: expected /login, got 500")
au2 = Audit()
ok2, note2 = smoke.run(on, app("npx playwright test"), ns(id="AUTO-2", ephemeral=False), au2)
chk("run red -> False", not ok2)
chk("run red -> audit smoke_fail", any(k == "smoke_fail" for k, _ in au2.events))
chk("run red -> Telegram alert sent", len(sent) == 1)
chk("run red -> alert names the ticket + branch", "AUTO-2" in sent[0] and "DEV" in sent[0])
chk("run red -> failure tail surfaced in note", "auth-redirect" in note2)

# --- a runner that throws is treated as red (flagged, still no revert) -------
sent.clear()
def _boom(a, c): raise RuntimeError("playwright binary missing")
smoke.gate.run_commands = _boom
au3 = Audit()
ok3, note3 = smoke.run(on, app("npx playwright test"), ns(id="AUTO-3", ephemeral=False), au3)
chk("runner exception -> treated as red (False)", not ok3)
chk("runner exception -> audit smoke_fail", any(k == "smoke_fail" for k, _ in au3.events))
chk("runner exception -> Telegram alert sent", len(sent) == 1)

# --- no audit handle -> never raises (best-effort logging) -------------------
smoke.gate.run_commands = lambda a, c: GateResult(passed=True, report="green")
ok4, _ = smoke.run(on, app("npx playwright test"), ns(id="AUTO-4", ephemeral=False), None)
chk("run with audit=None -> still returns cleanly", ok4)

print("\n================= EU-60 SMOKE QA =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
