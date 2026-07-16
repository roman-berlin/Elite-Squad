"""EU-342 — tee the full failing gate output to a file; feed the Builder sliced evidence + the path.

gate.py discarded all but the last 4000 chars of failing output — the full output was never
persisted anywhere — and the loop fed that raw truncated tail to the Builder on retry. When the
tail missed the real failure, the Builder re-ran the suite inside its own turns to rediscover it,
burning turns (the EU-248 exhaustion class) and tokens (retry feedback is the single biggest input-
token contributor, EU-38). Pattern borrowed from rtk's tee-on-failure (reimplemented in Python — the
rtk binary is disqualified by an open security review, rtk#640).

Pins:
  (1) GateResult carries a full_report distinct from the truncated report;
  (2) run_commands populates full_report with the COMPLETE output (not the -4000 tail) while report
      stays truncated;
  (3) _gate_retry_feedback writes the full output to a run-log file and returns sliced evidence + a
      'Full ... output saved to: <path>' reference;
  (4) it falls back gracefully (no crash) when the full output is empty.
"""
from __future__ import annotations

import sys
import types
import tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", sdk)

sys.path.insert(0, ".")

from orchestrator import gate as gate_mod, loop  # noqa: E402
from orchestrator.config import AppConfig  # noqa: E402
from orchestrator.contracts import GateResult  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


# (1) GateResult has full_report, defaulting empty (positional back-compat preserved)
gr = GateResult(passed=False, report="tail")
ok("(1) GateResult.full_report exists, defaults empty", hasattr(gr, "full_report") and gr.full_report == "")

# (2) run_commands: report truncated to ~4000, full_report complete
_TMP = Path(tempfile.mkdtemp())
app = AppConfig(name="t", repo_path=str(_TMP), base_branch="dev", protected_branch="main",
                backlog_backend="none")
# emit 9000 chars of output (generated, NOT embedded in the command) then exit non-zero, so the
# tail truncation of the OUTPUT is observable without a giant command string in the report header.
res = gate_mod.run_commands(
    app, ["python3 -c \"import sys; sys.stdout.write('X'*9000)\"; exit 1"], cwd=str(_TMP))
ok("(2) failing gate → not passed", res.passed is False)
ok("(2b) report output tail is truncated (fewer than the 9000 X's emitted)",
   res.report.count("X") <= 4100, f"report X count={res.report.count('X')}")
ok("(2c) full_report keeps the COMPLETE output", res.full_report.count("X") >= 9000,
   f"full X count={res.full_report.count('X')}")
ok("(2d) full_report is strictly longer than the truncated report",
   len(res.full_report) > len(res.report), f"{len(res.full_report)} vs {len(res.report)}")


class _Cfg:
    dry_run = False
    audit_path = str(_TMP / "state" / "audit.jsonl")


# (3) _gate_retry_feedback tees full output to a file + returns evidence + a path reference
fb = loop._gate_retry_feedback(_Cfg(), app, types.SimpleNamespace(id="EU-342", key="EU-342",
                                                                  ephemeral=False), "Verification", res)
ok("(3) feedback references a saved full-output file",
   "Full verification output saved to:" in fb, f"fb head={fb[:120]!r}")
ok("(3b) feedback carries sliced evidence, not the raw 9000-char dump",
   len(fb) < 9000, f"len={len(fb)}")

# (4) empty full output → graceful (no crash, still returns a fix instruction)
empty = GateResult(passed=False, report="", full_report="")
fb2 = loop._gate_retry_feedback(_Cfg(), app, types.SimpleNamespace(id="EU-342", key="EU-342",
                                                                   ephemeral=False), "Verification", empty)
ok("(4) empty output → graceful feedback string", isinstance(fb2, str) and "failed" in fb2, f"fb2={fb2!r}")

# source pin: both Builder-retry sites (Verification + Deterministic) use the tee helper. The
# terminal "stuck-fingerprint" sites break immediately (no rebuild), so they keep the plain report.
lsrc = Path("orchestrator/loop.py").read_text()
ok("(5) both retry sites use _gate_retry_feedback (not the raw report tail)",
   lsrc.count("_gate_retry_feedback(cfg, app, ticket") >= 2)
ok("(5b) the retry Verification feedback no longer inlines the raw gate.report",
   'Verification failed; fix these:\\n{gate.report}' not in lsrc)

print(f"\n{checks}/{checks} passed")
