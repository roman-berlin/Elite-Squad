"""EU-322 — tighten infra_classify's 5xx heuristic + probe env-parity, so ticket failures are
never mis-classified as infra.

Two live defects (triage-verified 2026-07-17 against dev, infra_classify.py unchanged since
EU-228 / 44004f7):

  1. ``_5XX_RE = r"\\b5\\d\\d\\b"`` matched ANY standalone 500-599 integer — e.g.
     "AssertionError: expected 500 items, got 499" or a traceback's 'line 512, in _run'. The text
     classify() judges is raw exception text (loop.py builds TicketReport.notes from str(exc)), so
     a deterministic ticket failure got tagged infra: it accrued NO error strike (blind-retried
     every cycle, never parks at _MAX_TICKET_ERRORS) AND armed a spurious GLOBAL offline-hold
     pausing all new work. Fix: a 5xx number now needs HTTP context (a status/HTTP word before it,
     or a 5xx reason phrase next to/instead of it).
  2. ``missing_toolchain`` probed binaries with shutil.which against the RAW os.environ PATH, but
     the gate actually runs under ``gate._subprocess_env(app)`` which layers ``app.gate_env`` on
     top — an app whose toolchain resolves only via gate_env['PATH'] was spuriously HELD (all its
     tickets dropped every cycle). Fix: probe under the same effective env the gate will use.
     Plus: only ``app.worktree_setup_cmd`` was probed, never the unit-wide
     ``Config.worktree_setup_cmd`` fallback loop._worktree_setup_command actually runs — that
     precedence (per-app value, even "", overrides unit-wide) is now mirrored exactly.

No network, no real models — stub the Agent SDK + requests before importing the orchestrator.
"""
import os
import sys
import tempfile
import types
from pathlib import Path

# Stub the Agent SDK + requests so importing the orchestrator never reaches the network.
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

from orchestrator import infra_classify
from orchestrator.config import AppConfig, Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))
    print(("PASS: " if c else "FAIL: ") + n + (f" - {d}" if d and not c else ""))


tmp = Path(tempfile.mkdtemp())


# ================================================================================================ #
# AC1: classify() — an incidental 5xx-looking integer in a genuine ticket failure is NOT infra
# ================================================================================================ #
print("\n=== AC1: 5xx needs HTTP context — incidental 500-599 integers are NOT infra ===")
# The triage's live repro texts: both matched the old bare \b5\d\d\b and got tagged "5xx".
for msg, label in [
    ("AssertionError: expected 500 items, got 499", "assertion count of 500"),
    ('File "runner.py", line 512, in _run', "traceback line number 512"),
    ("test summary: 503 passed, 2 failed", "test tally of 503"),
    ("ValueError: batch size 550 exceeds limit", "plain 550 in a ValueError"),
]:
    got = infra_classify.classify(msg)
    chk(f"classify() does NOT tag {label} as infra", got == "", f"got {got!r}")

# Genuine HTTP 5xx texts MUST still classify as infra ("5xx") — no regression on real outages.
for msg, label in [
    ("502 Bad Gateway from upstream", "502 Bad Gateway (eu228's pinned case)"),
    ("requests.exceptions.HTTPError: 503 Service Unavailable", "HTTPError 503 Service Unavailable"),
    ("HTTPError: 500 Server Error: Internal Server Error for url: https://x", "requests 500 Server Error"),
    ("upstream returned HTTP 502", "HTTP 502"),
    ("server responded with status 503", "status 503"),
    ("response.status_code == 500", "status_code == 500"),
    ("Service Unavailable — retry later", "bare reason phrase, no number"),
]:
    got = infra_classify.classify(msg)
    chk(f"classify() still tags {label} as '5xx'", got == "5xx", f"got {got!r}")

# Precedence pins: turn-limit ALWAYS wins (EU-248 routing), and non-5xx statuses stay non-infra.
got = infra_classify.classify("max_turns_reached after HTTP 503 retries")
chk("turn-limit marker still beats a real HTTP 503 (classify returns falsy)", got == "", repr(got))
got = infra_classify.classify("HTTPError: 404 Not Found for url: https://x")
chk("a 4xx status is still NOT infra", got == "", repr(got))


# ================================================================================================ #
# AC2: missing_toolchain — probe under the gate's EFFECTIVE env (gate_env PATH overlay), not raw
# os.environ. An app whose toolchain resolves only via gate_env['PATH'] must NOT be held.
# ================================================================================================ #
print("\n=== AC2: toolchain probe uses gate._subprocess_env(app), honouring gate_env['PATH'] ===")
tmpbin = tmp / "bin"
tmpbin.mkdir()
tool = tmpbin / "eu322-fake-tool"
tool.write_text("#!/bin/sh\nexit 0\n")
tool.chmod(0o755)

app_env = AppConfig(name="envpath", repo_path=str(tmp), base_branch="dev", protected_branch="main",
                    backlog_backend="none", gate_commands=["eu322-fake-tool test"],
                    gate_env={"PATH": f"{tmpbin}:{os.environ.get('PATH', '')}"})
missing = infra_classify.missing_toolchain(app_env)
chk("a binary resolvable ONLY via gate_env['PATH'] is NOT reported missing (app not held)",
    missing == [], f"got {missing!r}")

# Control: a binary absent from BOTH the daemon PATH and gate_env's PATH is still caught — the
# env-parity fix must not blind the hold to genuinely missing toolchains.
app_absent = AppConfig(name="absent", repo_path=str(tmp), base_branch="dev", protected_branch="main",
                       backlog_backend="none", gate_commands=["eu322-absent-tool build"],
                       gate_env={"PATH": f"{tmpbin}:{os.environ.get('PATH', '')}"})
missing = infra_classify.missing_toolchain(app_absent)
chk("a binary missing from BOTH PATHs is still reported (hold still works)",
    missing == ["eu322-absent-tool"], f"got {missing!r}")

# Crash-safety: a malformed app (no gate_env attr -> gate._subprocess_env raises) must fall back
# to the daemon environ instead of crashing the drain cycle.
ns_app = types.SimpleNamespace(name="ns", repo_path=str(tmp), gate_commands=["git status"])
try:
    missing = infra_classify.missing_toolchain(ns_app)
    chk("a malformed app (no gate_env) falls back to os.environ without crashing",
        missing == [], f"got {missing!r}")
except Exception as e:  # noqa: BLE001
    chk("a malformed app (no gate_env) falls back to os.environ without crashing", False, repr(e))


# ================================================================================================ #
# AC3: missing_toolchain(app, cfg) — the unit-wide Config.worktree_setup_cmd fallback is probed,
# with EXACTLY loop._worktree_setup_command's precedence (per-app value, even "", overrides it).
# ================================================================================================ #
print("\n=== AC3: unit-wide worktree_setup_cmd is probed, per-app override precedence mirrored ===")
def _app(wsc):
    return AppConfig(name="a", repo_path=str(tmp), base_branch="dev", protected_branch="main",
                     backlog_backend="none", gate_commands=[], worktree_setup_cmd=wsc)

cfg = Config(apps=[], audit_path=str(tmp / "audit.jsonl"), use_worktree=False,
             worktree_setup_cmd="eu322-unit-tool install")

try:
    missing = infra_classify.missing_toolchain(_app(None), cfg)
    chk("app wsc=None + cfg wsc='eu322-unit-tool install' -> the unit-wide binary IS probed",
        missing == ["eu322-unit-tool"], f"got {missing!r}")
except TypeError as e:
    chk("app wsc=None + cfg wsc='eu322-unit-tool install' -> the unit-wide binary IS probed",
        False, f"missing_toolchain does not accept cfg yet: {e!r}")

try:
    missing = infra_classify.missing_toolchain(_app("eu322-app-tool install"), cfg)
    chk("a per-app wsc OVERRIDES the unit-wide one (only the app's binary probed, loop.py parity)",
        missing == ["eu322-app-tool"], f"got {missing!r}")
except TypeError as e:
    chk("a per-app wsc OVERRIDES the unit-wide one (only the app's binary probed, loop.py parity)",
        False, repr(e))

try:
    missing = infra_classify.missing_toolchain(_app(""), cfg)
    chk("app wsc='' (explicit no-op) suppresses the unit-wide cmd — nothing probed (loop.py parity)",
        missing == [], f"got {missing!r}")
except TypeError as e:
    chk("app wsc='' (explicit no-op) suppresses the unit-wide cmd — nothing probed (loop.py parity)",
        False, repr(e))

# Backward compat: the one-arg call shape used by older call sites/tests keeps working.
missing = infra_classify.missing_toolchain(_app(None))
chk("missing_toolchain(app) with no cfg still works (cfg defaults to None)",
    missing == [], f"got {missing!r}")


print("\n================ EU-322 INFRA CLASSIFY PRECISION QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
