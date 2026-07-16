"""EU-341 — feed the existing forensics classification into the Builder retry feedback (reflexion).

forensics.classify()/_RULES/_ACTIONS already map a failure signature → a recommended action, and
forensics writes postmortems literally stating "re-running as-is will likely fail the same way" — but
their only consumers were human views (cockpit page, CLI print). The Builder retry got reviewer/gate
feedback with NO forensics, so a recurring crash/gate failure was rediscovered every pass.

This wires the EXISTING machinery (no new memory store — the adversarial review rejected that as
accretion): on a retry, prepend the classified category + recommended action + prior-attempt count to
the Builder's feedback.

Pins (on the pure classify/attempts machinery + the config flag + a source pin for the wiring):
  (1) classify() maps a gate failure to a non-unknown category with an action;
  (2) the config flag retry_forensics_enabled exists (default on);
  (3) source: the loop prepends a "KNOWN FAILURE PATTERN" hint on iteration > 1, gated by the flag,
      and only for a classified (non-unknown) failure;
  (4) source: it reuses forensics.classify + forensics.attempts (not a new store).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", sdk)

sys.path.insert(0, ".")

from orchestrator import forensics  # noqa: E402
from orchestrator.config import AppConfig  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


# (1) classify a gate failure → non-unknown category + an action string
cls = forensics.classify("", "Verification failed; fix these: 3 tests failed in run_all.py")
ok("(1) a gate failure classifies to a known category", cls.get("category") != "unknown", f"got {cls}")
ok("(1b) the classification carries a recommended action", bool(cls.get("action")), f"got {cls}")

# an unknown blob stays unknown (so the loop only injects when there's a real signal)
unk = forensics.classify("", "zxqw nothing recognizable here 12345")
ok("(1c) an unrecognizable failure stays 'unknown' (no spurious hint)",
   unk.get("category") == "unknown", f"got {unk}")

# (2) config flag default-on
app_cfg = AppConfig  # noqa: F841 (import sanity)
from orchestrator.config import Config  # noqa: E402
c = Config(apps=[], audit_path="/tmp/eu341/a.jsonl")
ok("(2) retry_forensics_enabled defaults True", getattr(c, "retry_forensics_enabled", None) is True,
   f"got {getattr(c, 'retry_forensics_enabled', None)}")

# (3)/(4) source pins for the loop wiring
lsrc = Path("orchestrator/loop.py").read_text()
ok("(3) loop injects a KNOWN FAILURE PATTERN hint", "KNOWN FAILURE PATTERN" in lsrc)
ok("(3b) it is gated on a retry (iteration > 1) and the flag",
   "iteration > 1 and getattr(cfg, \"retry_forensics_enabled\"" in lsrc)
ok("(3c) it only injects for a classified (non-unknown) failure",
   '_cls.get("category") != "unknown"' in lsrc)
ok("(4) it reuses forensics.classify + forensics.attempts (no new store)",
   "_forensics.classify(" in lsrc and "_forensics.attempts(" in lsrc)

print(f"\n{checks}/{checks} passed")
