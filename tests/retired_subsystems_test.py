"""EU-295: the retired-subsystem reality guard — a deleted subsystem must not stay referenced live.

Every cockpit dead-vestige bug of the 2026-07-12 audit had the same root cause: a subsystem is
deleted but rendering code / the roster keeps referencing it, and nothing fails. Real cases:
security_block counted as a live KPI after its gate died (EU-290, fixed by EU-313's freshness
window), the Test Engineer phantom re-emitted into the daily roster after c276155 deleted its
module (EU-260, fixed), the adjutant/drillmaster half-collapse (EU-294). eu43_docs_reality_test
guards doc *paths*; eu260_org_reality_test guards the two 2026-07 retirements it names. Nothing
guarded the *class* — this harness does, via an explicit registry: when a future subsystem is
deleted, adding one registry entry makes the suite fail until every live reference is gone, so a
deletion can't ship half-done again.

Three guards over the registry below:
  A — no module under orchestrator/ (recursively) imports a retired module (catches a
      re-introduced ``from . import reaper``).
  B — the cockpit/roster rendering sources (server.py, cockpit_views.py, warroom.py, roster.py,
      officers.py, dashboard.py — forensics.py is deliberately OUT of scope: reading retired
      events as history is its whole job) must not reference a retired token in live CODE.
      Comment-only mentions are fine (comments render nothing; the house style cites retirements
      as history constantly). A live-code reference passes only via an inline ``# retired:``
      comment on that line, or an entry in ALLOW/PENDING here — and a stale ALLOW/PENDING entry
      (no matching reference left) FAILS, so the registry can never drift from the tree
      (same tombstone-honesty pattern as eu260_org_reality_test).
  C — roster._OFFICER_ROWS carries no retired officer key, and every registered module really is
      absent from disk (registering a still-live module goes RED with a clear message).

Deviations from the ticket text, forced by the tree as it stands 2026-07-18:
  * The ticket predates EU-260's land. officers.OFFICER_NAMES is now a DELIBERATE superset —
    retired keys stay so display("test_engineer") still renders historical audit records
    (officers.py:28-31; eu260_org_reality_test asserts display(key) != key for retired keys).
    So guard C covers roster._OFFICER_ROWS only, never OFFICER_NAMES — asserting the opposite
    would fight a landed doctrine and an existing green assertion.
  * The registry lives HERE, not in a new orchestrator/retired.py: this change is scoped to
    tests/ only. Promoting it to a shared module later is a one-file move.
  * 2026-07-19 stabilization: drillmaster (EU-327), squad, governor, and doctrine are now
    registered — all four modules are deleted from disk. squad/governor/doctrine fell in the
    stabilization sweep (zero production imports / write-only / caller died with drillmaster);
    their entries are token-less because their obvious tokens ("squad", "governor", "doctrine")
    are generic English that live code uses in unrelated senses (recon squads, cost governor
    comments, memory doctrine).
  * The liaison roster row (the KNOWN LIVE VESTIGE this guard originally reported as PENDING)
    was removed in the same 2026-07-19 sweep — roster.py no longer ships the row or the mermaid
    node, guard C now asserts the key's absence, and officers.py keeps only the display label
    per the EU-260 label-superset doctrine (the ALLOW entry).

Guard predicates are pure functions, self-tested against synthetic fixtures in section 5 (the
eu43_docs_guard_teeth pattern), so the mutation-proof of the ticket's AC — fake retired token
referenced in live code goes RED, comment/allowlisted stays GREEN — re-runs on every suite.

Pure filesystem + AST + one stubbed-SDK roster import — no network, no models."""
import ast
import re
import sys
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

ROOT = Path(__file__).resolve().parent.parent
ORCH = ROOT / "orchestrator"

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# =====================================================================================
# The registry. subsystem -> (retired module name or None, guarded tokens, tombstone).
# A module entry means orchestrator/<module>.py was deleted and must stay deleted +
# unimported. Tokens are word-boundary-matched in the rendering sources' live code.
# Token choice is deliberate per entry: hr/events/adjutant are module-only because their
# obvious tokens are either generic English ("hr", "events") or belong to a still-active
# officer (adjutant keeps its council seat per EU-325 — module retired, post is not).
# =====================================================================================
RETIRED = {
    "test_engineer": ("test_engineer", ("test_engineer",), "c276155 / Phase-2 §2 (EU-260)"),
    "senior_pm":     ("senior_pm",     ("senior_pm",),     "31d4349 / Phase-2 §2"),
    "security_gate": (None,            ("security_block",), "c24f459 gate deleted; KPI fixed by EU-313"),
    "reaper":        ("reaper",        ("reaper",),        "e7dd174 (EU-358/360/366)"),
    "events_layer":  ("events",        (),                 "33da61d / Phase-2 §2"),
    "hr_synthesis":  ("hr",            (),                 "4fdb13a / Phase-2 §2 slice B"),
    "adjutant_cli":  ("adjutant",      (),                 "4bf4fe2 (EU-325) — officer stays in post"),
    "liaison":       ("liaison",       ("liaison",),       "40da120 / Phase-2 §2"),
    "drillmaster":   ("drillmaster",   (),                 "12ff475 (EU-327)"),
    "squad_lanes":   ("squad",         (),                 "2026-07-19 stabilization — zero imports since Phase-2 §2"),
    "usage_governor": ("governor",     (),                 "2026-07-19 stabilization — write-only, budget never read"),
    "doctrine_backups": ("doctrine",   (),                 "2026-07-19 stabilization — last caller died with EU-327"),
}

# Rendering sources guard B scans — the surfaces that make live claims to a human.
RENDER_SOURCES = ("server.py", "cockpit_views.py", "warroom.py", "roster.py",
                  "officers.py", "dashboard.py")

# (file, token) -> why this live-code reference is deliberate and stays. A stale entry fails.
ALLOW = {
    ("officers.py", "test_engineer"):
        "EU-260 label-superset doctrine: display() must render historical audit records",
    ("officers.py", "senior_pm"):
        "EU-260 label-superset doctrine (same as test_engineer)",
    ("officers.py", "liaison"):
        "EU-260 label-superset doctrine (same as test_engineer)",
    ("warroom.py", "security_block"):
        "deliberate historical reads + EU-313 freshness-window KPI (no live producer exists)",
}

# (file, token) -> a KNOWN vestige awaiting its own cleanup, outside this change's file scope.
# WARNs every run instead of failing; self-destructs (fails) once the reference is gone so the
# entry cannot outlive the vestige. Do NOT add entries here to silence a new failure — new
# references to retired tokens are the bug this guard exists to catch.
# (Empty since 2026-07-19: the liaison roster-row vestige was cleaned in the stabilization sweep.)
PENDING: dict = {}


# =====================================================================================
# Guard predicates — pure functions so section 5 can prove their teeth on fixtures.
# =====================================================================================
def _split_code_comment(line: str) -> tuple[str, str]:
    """Split a source line at the first ``#`` that starts a comment (quote-aware, single line).

    Docstring text counts as code on purpose: a token inside rendered/inspectable strings is
    exactly what guard B is after, and multi-line string state isn't tracked — the cost is only
    that a docstring mention needs the same allowlist a code mention would."""
    q, i = None, 0
    while i < len(line):
        c = line[i]
        if q:
            if c == "\\":
                i += 2
                continue
            if c == q:
                q = None
        elif c in ("'", '"'):
            q = c
        elif c == "#":
            return line[:i], line[i:]
        i += 1
    return line, ""


def token_code_hits(text: str, token: str) -> list[tuple[int, bool]]:
    """(lineno, has_retired_marker) for every LIVE-CODE occurrence of ``token`` in ``text``.

    Comment-only mentions are skipped; a line whose trailing comment contains ``retired:`` is
    the ticket's explicit in-source opt-out and comes back flagged as such."""
    pat = re.compile(r"\b%s\b" % re.escape(token))
    hits = []
    for no, line in enumerate(text.splitlines(), 1):
        code, comment = _split_code_comment(line)
        if pat.search(code):
            hits.append((no, "retired:" in comment))
    return hits


def import_violations(src: str, retired_modules: set[str]) -> list[str]:
    """Names of retired orchestrator modules ``src`` imports (AST — no false hits on words).

    Catches: ``import orchestrator.X``, ``from orchestrator import X``, ``from orchestrator.X
    import …``, ``from .X import …`` and ``from . import X`` (any relative level, so the
    backlog/ subpackage's ``from .. import X`` is covered). ``from .other import X`` is NOT an
    import of module X and must not be flagged — that's an attribute of ``other``."""
    out = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if parts[0] == "orchestrator" and len(parts) > 1 and parts[1] in retired_modules:
                    out.append(parts[1])
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            parts = mod.split(".") if mod else []
            if mod == "orchestrator" or (node.level >= 1 and not mod):
                out.extend(a.name for a in node.names if a.name in retired_modules)
            elif parts and parts[0] == "orchestrator" and len(parts) > 1 and parts[1] in retired_modules:
                out.append(parts[1])
            elif node.level >= 1 and parts and parts[0] in retired_modules:
                out.append(parts[0])
    return out


_RETIRED_MODULES = {mod for mod, _t, _c in RETIRED.values() if mod}

# --- 1) registry tombstones: every registered module really is gone (and stays gone) -----------
# The self-check that keeps the registry honest: if a module ever comes back (or someone registers
# a still-live one, e.g. drillmaster before EU-327 lands), this fails and says what to do.
for name, (mod, _tokens, commit) in sorted(RETIRED.items()):
    if mod:
        chk(f"{name}: orchestrator/{mod}.py really is deleted ({commit})",
            not (ORCH / f"{mod}.py").is_file(),
            f"module is live — remove {name!r} from RETIRED instead of asserting a lie")

# --- 2) guard A: no live orchestrator module imports a retired module --------------------------
for py in sorted(ORCH.rglob("*.py")):
    rel = py.relative_to(ROOT)
    try:
        bad = import_violations(py.read_text(encoding="utf-8"), _RETIRED_MODULES)
    except SyntaxError as e:  # a rendering source that can't parse is its own defect
        chk(f"guard A: {rel} parses", False, str(e))
        continue
    chk(f"guard A: {rel} imports no retired module", not bad, f"imports {sorted(set(bad))}")

# --- 3) guard B: rendering sources carry no retired token in live code -------------------------
seen_allow, seen_pending = set(), set()
for fname in RENDER_SOURCES:
    path = ORCH / fname
    chk(f"guard B: rendering source exists: orchestrator/{fname}", path.is_file())
    if not path.is_file():
        continue
    text = path.read_text(encoding="utf-8")
    for name, (_mod, tokens, commit) in sorted(RETIRED.items()):
        for token in tokens:
            hits = token_code_hits(text, token)
            live = [no for no, marked in hits if not marked]
            if not live:
                chk(f"guard B: {fname} has no live-code '{token}' ({name})", True)
                continue
            if (fname, token) in ALLOW:
                seen_allow.add((fname, token))
                chk(f"guard B: {fname} '{token}' allowlisted — {ALLOW[(fname, token)]}", True)
            elif (fname, token) in PENDING:
                seen_pending.add((fname, token))
                print(f"  [WARN] {fname}:{live} still references retired '{token}' — "
                      f"{PENDING[(fname, token)]}")
                chk(f"guard B: {fname} '{token}' is a tracked PENDING vestige (WARN above)", True)
            else:
                chk(f"guard B: {fname} references retired token '{token}' in live code", False,
                    f"lines {live} ({name}, retired {commit}) — delete the reference, or add an "
                    f"inline '# retired:' comment / an ALLOW entry with a reason")

# stale-entry self-destruct: an ALLOW/PENDING pair with no live reference left must be removed,
# otherwise it would silently bless a future re-introduction.
for pair in sorted(set(ALLOW) - seen_allow):
    chk(f"ALLOW entry is stale — remove it: {pair}", False, ALLOW[pair])
for pair in sorted(set(PENDING) - seen_pending):
    chk(f"PENDING entry is stale — the vestige is gone, remove it: {pair}", False, PENDING[pair])

# --- 4) guard C: the runtime roster rosters no retired officer key -----------------------------
# Keys asserted absent are only those whose POST is retired. The adjutant row is governed above
# (stays by doctrine — council-backed, EU-325 + eu260 test §4). liaison joined the absent set on
# 2026-07-19 when its phantom row was cleaned. OFFICER_NAMES is deliberately untouched (label
# superset — see docstring).
from orchestrator import roster  # noqa: E402  (needs the SDK stub above)

row_keys = [key for key, *_ in roster._OFFICER_ROWS]
for key in ("test_engineer", "senior_pm", "liaison"):
    chk(f"guard C: '{key}' absent from roster._OFFICER_ROWS", key not in row_keys)

# --- 5) teeth: the predicates catch the defect class on synthetic fixtures ---------------------
# The ticket's mutation AC, made durable: a fake retired token in live rendering code is RED; a
# comment mention, an inline '# retired:' opt-out, and a non-import name reference stay GREEN.
FAKE = 'card = build_card(fakeofficer_stats)\n'
chk("teeth: fake retired token in live code is caught",
    [no for no, m in token_code_hits(FAKE, "fakeofficer_stats") if not m] == [1])
chk("teeth: comment-only mention is NOT flagged",
    token_code_hits("# fakeofficer_stats died with c000000\nx = 1\n", "fakeofficer_stats") == [])
chk("teeth: inline '# retired:' opt-out is honoured",
    token_code_hits("y = fakeofficer_stats  # retired: historical read\n",
                    "fakeofficer_stats") == [(1, True)])
chk("teeth: token inside a string IS live code (renders to a human)",
    token_code_hits('href = "/forensics?cat=fake_evt"  # link\n', "fake_evt") == [(1, False)])
chk("teeth: word-boundary — 'test_engineers'/'retest' do not false-positive",
    token_code_hits("xs = test_engineers + latest\n", "test_engineer") == [])
chk("teeth: quote-aware split — '#' inside a string is not a comment",
    token_code_hits('s = "a # fake_evt"\n', "fake_evt") == [(1, False)])

chk("teeth: 'from . import X' of a retired module is caught",
    import_violations("from . import fakemod\n", {"fakemod"}) == ["fakemod"])
chk("teeth: 'from orchestrator.X import y' is caught",
    import_violations("from orchestrator.fakemod import y\n", {"fakemod"}) == ["fakemod"])
chk("teeth: 'import orchestrator.X' is caught",
    import_violations("import orchestrator.fakemod\n", {"fakemod"}) == ["fakemod"])
chk("teeth: 'from orchestrator import X' is caught",
    import_violations("from orchestrator import fakemod\n", {"fakemod"}) == ["fakemod"])
chk("teeth: 'from .. import X' (backlog/ depth) is caught",
    import_violations("from .. import fakemod\n", {"fakemod"}) == ["fakemod"])
chk("teeth: 'from .other import X' is a name, not a module import — NOT flagged",
    import_violations("from .signals import fakemod\n", {"fakemod"}) == [])
chk("teeth: unrelated imports stay clean",
    import_violations("import os\nfrom . import gate\n", {"fakemod"}) == [])

print("\n========== EU-295 RETIRED-SUBSYSTEM REALITY GUARD ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
