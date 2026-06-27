"""EU-69 [Software Engineer] — HR ephemeral specialist synthesis (orchestrator/hr.py).

Checks, all without a real model (the SDK + run_agent are stubbed):
  1. parse_specialists (pure): parses a valid JSON array, slugifies + de-dupes lane_key, drops entries
     missing a mandatory field (name / lane_key / identity / domain_gate), and tolerates a skills list
     OR a numbered-string, and tolerates prose around the JSON.
  2. Every returned charter carries the template shape PLUS lane_key + domain_gate, and is flagged
     ephemeral (NEVER written to officers/).
  3. automode  → auto-proceeds (no approval call) and returns the charters.
  4. non-automode + Commander 'y' → returns the charters; anything else → returns [] (fail-safe).
  5. A synthesis/SDK failure degrades to [] instead of crashing.
"""
import sys, types, asyncio, tempfile
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import hr
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


def _cfg(**kw):
    fd, path = tempfile.mkstemp(suffix=".jsonl"); import os; os.close(fd)
    return Config(apps=[AppConfig(name="x", repo_path=".", backlog_backend="none")],
                  audit_path=path, use_worktree=False, auto_model=False, **kw)


class _Run:
    def __init__(self, text): self.final = text; self.text = text


def _stub_runner(text):
    async def _fake(prompt, opts, tag=""):
        return _Run(text)
    return _fake


# ── 1. parse_specialists (pure) ──────────────────────────────────────────────
GOOD = """sure, here you go:
[
  {"name": "MQL5 Algo Engineer", "lane_key": "MQL5 Algo!", "identity": "owns the EA",
   "knowledge": "mql5 docs", "skills": ["1. write", "2. self-check"],
   "constraints": "no other files", "domain_gate": "mql5 compile + Strategy Tester run"},
  {"name": "Risk Sizer", "lane_key": "risk-sizing", "identity": "position sizing",
   "knowledge": "k", "skills": "1. size\\n2. verify", "constraints": "c",
   "domain_gate": "backtest drawdown < 20%"}
]
trailing prose"""
got = hr.parse_specialists(GOOD)
chk("parses two charters", len(got) == 2, str(len(got)))
chk("lane_key slugified", got[0]["lane_key"] == "mql5-algo", got[0]["lane_key"])
chk("skills string coerced to list", got[1]["skills"] == ["size", "verify"], str(got[1]["skills"]))
chk("skills list preserved", got[0]["skills"] == ["1. write", "2. self-check"], str(got[0]["skills"]))
chk("ephemeral flag set", all(c.get("ephemeral") is True for c in got))
chk("template shape present", all(
    {"name", "lane_key", "identity", "knowledge", "skills", "constraints", "domain_gate"} <= set(c)
    for c in got))
chk("domain_gate non-empty on every charter", all(c["domain_gate"].strip() for c in got))

# de-dupe lanes + drop missing-mandatory-field entries
DUPE = """[
  {"name": "A", "lane_key": "rust-cli", "identity": "i", "domain_gate": "cargo test"},
  {"name": "B", "lane_key": "rust-cli", "identity": "i2", "domain_gate": "cargo build"},
  {"name": "NoGate", "lane_key": "go-svc", "identity": "i3", "domain_gate": ""},
  {"name": "", "lane_key": "py-job", "identity": "i4", "domain_gate": "pytest"}
]"""
dd = hr.parse_specialists(DUPE)
chk("duplicate lane collapsed to one", [c["lane_key"] for c in dd] == ["rust-cli"], str([c["lane_key"] for c in dd]))
chk("missing domain_gate dropped", all(c["lane_key"] != "go-svc" for c in dd))
chk("missing name dropped", all(c["lane_key"] != "py-job" for c in dd))

chk("no JSON -> []", hr.parse_specialists("SOLO, nothing here") == [])
chk("None -> []", hr.parse_specialists(None) == [])
chk("malformed JSON -> []", hr.parse_specialists("[{bad json}]") == [])
chk("max_n caps the count", len(hr.parse_specialists(GOOD, max_n=1)) == 1)


# ── 3. automode auto-proceeds (no approver invoked) ──────────────────────────
hr.run_agent = _stub_runner(GOOD)
def _boom(_p):  # approver must NOT be called in automode
    raise AssertionError("approver called in automode")
auto = asyncio.run(hr.synthesize_specialists("mql5", "build an EA", _cfg(auto_mode=True), approver=_boom))
chk("automode returns charters without approval", len(auto) == 2, str(len(auto)))
chk("automode result is ephemeral", all(c.get("ephemeral") for c in auto))


# ── 4. non-automode approval gate ────────────────────────────────────────────
hr.run_agent = _stub_runner(GOOD)
yes = asyncio.run(hr.synthesize_specialists("mql5", "build an EA", _cfg(auto_mode=False),
                                            approver=lambda _p: "y"))
chk("non-automode + 'y' returns charters", len(yes) == 2, str(len(yes)))

hr.run_agent = _stub_runner(GOOD)
no = asyncio.run(hr.synthesize_specialists("mql5", "build an EA", _cfg(auto_mode=False),
                                           approver=lambda _p: "n"))
chk("non-automode + 'n' returns [] (discarded)", no == [], str(no))

hr.run_agent = _stub_runner(GOOD)
def _eof(_p): raise EOFError
eof = asyncio.run(hr.synthesize_specialists("mql5", "x", _cfg(auto_mode=False), approver=_eof))
chk("non-automode + no TTY (EOF) returns [] (fail-safe)", eof == [])


# ── 5. synthesis failure degrades to [] ──────────────────────────────────────
async def _explode(prompt, opts, tag=""):
    raise RuntimeError("model down")
hr.run_agent = _explode
safe = asyncio.run(hr.synthesize_specialists("mql5", "x", _cfg(auto_mode=True), approver=_boom))
chk("SDK failure degrades to [] (no crash)", safe == [])

hr.run_agent = _stub_runner("no array here, sorry")
empty = asyncio.run(hr.synthesize_specialists("mql5", "x", _cfg(auto_mode=True), approver=_boom))
chk("unparseable reply -> [] in automode", empty == [])


# ── 6. record_domain_use + check_promote ─────────────────────────────────────
import json, os, tempfile
from pathlib import Path

def _cfg_tmp(**kw):
    """Config wired to a fresh temp directory (audit.jsonl + officers/ live inside it)."""
    d = Path(tempfile.mkdtemp())
    audit = d / "audit.jsonl"
    return Config(apps=[AppConfig(name="x", repo_path=".", backlog_backend="none")],
                  audit_path=str(audit), use_worktree=False, auto_model=False, **kw)


# record_domain_use: appends a domain_use event to audit.jsonl.
_cfg_rec = _cfg_tmp()
hr.record_domain_use("mql5", _cfg_rec)
hr.record_domain_use("mql5", _cfg_rec)
hr.record_domain_use("rust-cli", _cfg_rec)
_lines = [json.loads(l) for l in Path(_cfg_rec.audit_path).read_text().splitlines() if l.strip()]
chk("record_domain_use: appends event=domain_use rows",
    all(r["event"] == "domain_use" for r in _lines), str(_lines))
chk("record_domain_use: domain field preserved",
    [r["domain"] for r in _lines] == ["mql5", "mql5", "rust-cli"],
    str([r["domain"] for r in _lines]))


# check_promote: below threshold -> False, no officer file written.
_cfg_below = _cfg_tmp()
hr.record_domain_use("mql5", _cfg_below)
hr.record_domain_use("mql5", _cfg_below)          # count = 2, threshold = 3
_promoted = hr.check_promote("mql5", _cfg_below)
_officers_below = Path(_cfg_below.audit_path).parent / "officers"
chk("check_promote: below threshold -> False", _promoted is False, str(_promoted))
chk("check_promote: below threshold -> no charter file", not _officers_below.exists() or not (_officers_below / "mql5-specialist.md").exists())


# check_promote: at threshold in automode -> True, charter written.
_cfg_auto = _cfg_tmp(auto_mode=True)
for _ in range(3):
    hr.record_domain_use("mql5", _cfg_auto)        # count = 3 >= 3
def _boom_approver(_p):
    raise AssertionError("approver must NOT be called in automode")
_promoted_auto = hr.check_promote("mql5", _cfg_auto, approver=_boom_approver)
_charter_path = Path(_cfg_auto.audit_path).parent / "officers" / "mql5-specialist.md"
chk("check_promote automode: returns True at threshold", _promoted_auto is True, str(_promoted_auto))
chk("check_promote automode: charter file written", _charter_path.exists())
_charter_text = _charter_path.read_text() if _charter_path.exists() else ""
chk("check_promote automode: charter contains domain name", "mql5" in _charter_text.lower(), _charter_text[:80])
chk("check_promote automode: charter has Identity section", "## Identity" in _charter_text)


# check_promote: already in post -> idempotent (FileExistsError swallowed), returns True.
_promoted_again = hr.check_promote("mql5", _cfg_auto, approver=_boom_approver)
chk("check_promote automode: already exists -> True (idempotent)", _promoted_again is True, str(_promoted_again))


# check_promote: at threshold in non-automode + 'y' -> True, charter written.
_cfg_yes = _cfg_tmp(auto_mode=False)
for _ in range(3):
    hr.record_domain_use("rust-cli", _cfg_yes)
_promoted_yes = hr.check_promote("rust-cli", _cfg_yes, approver=lambda _p: "y")
_rust_path = Path(_cfg_yes.audit_path).parent / "officers" / "rust-cli-specialist.md"
chk("check_promote non-automode + 'y': returns True", _promoted_yes is True, str(_promoted_yes))
chk("check_promote non-automode + 'y': charter written", _rust_path.exists())


# check_promote: non-automode + 'n' -> False, no charter written.
_cfg_no = _cfg_tmp(auto_mode=False)
for _ in range(3):
    hr.record_domain_use("solidity", _cfg_no)
_promoted_no = hr.check_promote("solidity", _cfg_no, approver=lambda _p: "n")
_sol_path = Path(_cfg_no.audit_path).parent / "officers" / "solidity-specialist.md"
chk("check_promote non-automode + 'n': returns False", _promoted_no is False, str(_promoted_no))
chk("check_promote non-automode + 'n': no charter written", not _sol_path.exists())


# check_promote: EOF from approver (no TTY) -> False (fail-safe).
_cfg_eof = _cfg_tmp(auto_mode=False)
for _ in range(3):
    hr.record_domain_use("wasm", _cfg_eof)
def _eof_approver(_p): raise EOFError
_promoted_eof = hr.check_promote("wasm", _cfg_eof, approver=_eof_approver)
chk("check_promote non-automode EOF -> False (fail-safe)", _promoted_eof is False, str(_promoted_eof))


# check_promote: missing audit.jsonl -> False (not an error).
_cfg_nofile = _cfg_tmp()
# audit_path file is never written — so it doesn't exist
_promoted_nofile = hr.check_promote("go", _cfg_nofile)
chk("check_promote: missing audit file -> False (no crash)", _promoted_nofile is False, str(_promoted_nofile))


# ── 7. DEFAULT approver is TTY-guarded — the unattended unit NEVER blocks on input() ──────────
# This is the EU-69 iteration-1 regression: synthesize_specialists / check_promote defaulted to the
# bare builtin input(), so a non-automode call from the autonomous build loop (no Commander at a TTY)
# blocked on stdin and wedged tests/run_all.py for the full 1800 s timeout. The default is now
# `_tty_input`, which raises EOFError when no terminal is attached → callers take their fail-safe
# DECLINE path instead of blocking. We force "no TTY" so this is deterministic even when a developer
# runs the harness from a real terminal.
import types as _types
_real_stdin = hr.sys.stdin
hr.sys.stdin = _types.SimpleNamespace(isatty=lambda: False)   # simulate the unattended unit (no TTY)
try:
    _eof_raised = False
    try:
        hr._tty_input("approve? ")           # must NOT call builtin input() (which would block)
    except EOFError:
        _eof_raised = True
    chk("_tty_input raises EOFError when no TTY (never blocks)", _eof_raised)

    # synthesize_specialists with the DEFAULT approver (none injected), non-automode + no TTY -> [].
    hr.run_agent = _stub_runner(GOOD)
    _def_syn = asyncio.run(hr.synthesize_specialists("mql5", "x", _cfg(auto_mode=False)))
    chk("synthesize default approver: non-automode + no TTY -> [] (fail-safe, no block)",
        _def_syn == [], str(_def_syn))

    # check_promote at threshold with the DEFAULT approver, non-automode + no TTY -> False.
    _cfg_def = _cfg_tmp(auto_mode=False)
    for _ in range(3):
        hr.record_domain_use("cuda", _cfg_def)
    _pc_def = hr.check_promote("cuda", _cfg_def)   # no approver -> _tty_input -> EOFError -> False
    chk("check_promote default approver: threshold + no TTY -> False (no block)",
        _pc_def is False, str(_pc_def))
finally:
    hr.sys.stdin = _real_stdin


print("\n================ HR SPECIALIST SYNTHESIS QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
