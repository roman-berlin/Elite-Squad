"""EU-40 regression: the officer rename (army -> dev-team names) must stay COMPLETE.

The bug this pins: EU-17's rename was only half-applied — roster.py showed new names but
prompts / labels / notify text across orchestrator/ still carried the OLD army names. The fix
routes every human-facing name through the single source of truth in ``orchestrator.officers``
(``OFFICER_NAMES`` / ``display``), re-exported from ``config``.

This harness guards two things that a unit-level map test (officer_names_test.py) can't:
  1. CODEBASE SCAN — the ticket's own regression grep, run from Python, must find ZERO retired
     display names anywhere in orchestrator/ EXCEPT the one definition module that documents the
     old->new map. If anyone re-hardcodes "Quartermaster"/"the General"/etc. into a prompt or
     label, this goes red.
  2. RUNTIME ROUND-TRIP — council.py resolves a human display name back to its immutable internal
     key via OFFICER_NAMES; the new names must resolve, and stable keys must be preserved.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

from orchestrator.officers import OFFICER_NAMES, display
from orchestrator import config

ROOT = Path(__file__).resolve().parent.parent
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


# --- The ticket's exact regression pattern (EU-40 "Regression" acceptance line). --------------
# Multi-word display names are matched as PHRASES with \s+ between words, so a name that wraps across
# a source line break (e.g. "…officers are the\nGeneral's…", the actual EU-40 leak in adjutant.py) is
# still caught — a plain line-by-line grep silently misses those. Single-word officer names that can't
# collide with an internal key are matched on word boundaries; "the General" carries a lookahead so the
# benign "General-purpose" / "Generals…" tokens never trip it.
OLD_NAME_PATTERN = re.compile(
    r"Provost\s+Marshal|Inspector\s+General|Field\s+Engineer|the\s+General(?![\w-])"
    r"|\bQuartermaster\b|\bDrillmaster\b|\bAdjutant\b|\bSentinel\b|\bScout\b|\bScribe\b"
)
# The ONE place retired names may still appear: the source-of-truth module documents the
# old -> new mapping in its docstring. Everything else must be clean.
ALLOWLIST = {"orchestrator/officers.py"}

# --- Bare-persona leak guard (EU-40 iteration-3 reviewer ask). ---------------------------------
# OLD_NAME_PATTERN above only catches the FULL retired names ("Provost Marshal", "Inspector
# General", "Field Engineer"). It misses a SINGLE retired persona word used as a name in prose —
# "the Provost flagged…", "+ Inspector debate it", "(Engineer / Inspector / a squad role)" — which
# is exactly how the rename leaked back into officer system prompts, format_signals output, and
# cockpit strings. This second pattern closes that gap while leaving the legitimate look-alikes be:
#   • "Provost"/"Inspector" only ever NAMED the old officer when Capitalized; the immutable internal
#     keys and officers/*.md filenames are lowercase ("provost", "inspector.md"), so a case-sensitive
#     \bProvost\b / \bInspector\b never fires on a genuine internal-key / filename usage.
#   • "Engineer" survives in many CURRENT names (QA / Security / Test / Frontend / Software Engineer)
#     and as the roster squad-table column header ("Engineer |" / "Engineer</th>"); those are excluded
#     so ONLY a bare "Engineer" standing in for the retired Field-Engineer officer is flagged.
BARE_PERSONA_PATTERN = re.compile(
    r"\bProvost\b|\bInspector\b"
    r"|(?<!QA )(?<!Security )(?<!Test )(?<!Frontend )(?<!Software )\bEngineer\b(?! \|)(?!</th>)"
)


def scan_codebase() -> list[tuple[str, int, str]]:
    """Scan each orchestrator module as ONE string (not line-by-line) so a retired name that wraps
    across a line break is still found; report an approximate line number from the match offset."""
    leaks: list[tuple[str, int, str]] = []
    for path in sorted((ROOT / "orchestrator").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        for pat in (OLD_NAME_PATTERN, BARE_PERSONA_PATTERN):   # full retired names + bare-persona words
            for m in pat.finditer(text):
                lineno = text.count("\n", 0, m.start()) + 1
                leaks.append((rel, lineno, re.sub(r"\s+", " ", m.group(0))))
    return leaks


# === 1. CODEBASE SCAN — no retired display name survives outside the definition module ========
leaks = scan_codebase()
check(
    "no retired officer display name anywhere in orchestrator/ (excl. officers.py)",
    leaks == [],
    f"{len(leaks)} leak(s): " + "; ".join(f"{f}:{n} {t[:50]}" for f, n, t in leaks[:6]),
)

# Guard the scanner itself: it MUST be able to see a planted old name, else "0 leaks" is a lie.
_planted = "A line mentioning the Quartermaster and the Provost Marshal."
check(
    "scanner actually detects old names (self-check, no false-green)",
    bool(OLD_NAME_PATTERN.search(_planted)),
    "pattern failed to match a planted old name",
)
# ...and it MUST catch a name that WRAPS across a source line break — exactly the adjutant.py leak
# ("…officers are the\nGeneral's…") that a line-by-line scan let slip through.
check(
    "scanner catches a line-WRAPPED old name (the real EU-40 miss)",
    bool(OLD_NAME_PATTERN.search("New MAJOR officers are the\nGeneral's call")),
    "wrapped 'the General' across a line break was not detected",
)
# ...and it must NOT fire on the stable internal keys / repo name that legitimately remain.
for benign in ("provost", "quartermaster", "scout", "General", "[General]", "General-purpose", "generalist"):
    check(
        f"scanner leaves benign internal token alone: {benign!r}",
        OLD_NAME_PATTERN.search(benign) is None,
        "pattern wrongly matched an internal key / repo name",
    )

# Guard the BARE-persona scanner the same way: it MUST fire on a single retired persona word used as
# a name (the iteration-3 leak class)…
for fires in ("the Provost flagged it", "then the Inspector debates", "+ Inspector debate it",
              "(Engineer / Inspector / a squad role)", "Engineer implemented (pass 2)"):
    check(f"bare-persona scanner fires on retired name: {fires!r}",
          bool(BARE_PERSONA_PATTERN.search(fires)), "should have matched a bare old persona")
# …and it MUST leave every legitimate look-alike alone: current compound names, the roster squad
# column header, the lowercase internal keys / .md filenames, and the renamed plural "engineers".
for safe in ("QA Engineer", "Security Engineer", "Test Engineer", "Frontend Engineer", "Software Engineer",
             "Engineering Manager", "Engineering Coach", "| Engineer | Lane |", "<th>Engineer</th>",
             "provost", "inspector", "provost.md", "inspector.md", "engineers"):
    check(f"bare-persona scanner leaves legitimate token alone: {safe!r}",
          BARE_PERSONA_PATTERN.search(safe) is None, "wrongly matched a current name / key / filename")

# === 2. RUNTIME ROUND-TRIP — display name -> immutable internal key (council.py path) =========
name_to_key = {name.lower(): key for key, name in OFFICER_NAMES.items()}
ROUND_TRIP = {
    "Release Manager": "quartermaster",
    "Security Engineer": "provost",
    "QA Engineer": "scout",
    "Code Reviewer": "inspector",
    "Engineering Manager": "adjutant",
    "Engineering Coach": "drillmaster",
    "SRE": "sentinel",
    "CTO": "general",
    "Dev Team Lead": "field_engineer",
    "Technical Writer": "scribe",
}
for disp, key in ROUND_TRIP.items():
    check(f"display->key resolves {disp!r} -> {key!r}", name_to_key.get(disp.lower()) == key,
          f"got {name_to_key.get(disp.lower())!r}")
    check(f"key->display round-trips {key!r}", display(key) == disp, f"got {display(key)!r}")

# Internal keys are STABLE (the rename must never have touched them).
for key in ("provost", "quartermaster", "scout", "inspector", "adjutant",
            "drillmaster", "sentinel", "general", "field_engineer", "scribe"):
    check(f"internal key preserved: {key!r}", key in OFFICER_NAMES, "key missing from map")

# === 3. SINGLE SOURCE OF TRUTH — config re-exports the one map (no drift / no copy) ===========
check("config.OFFICER_NAMES is the same object", config.OFFICER_NAMES is OFFICER_NAMES)
check("config.officer_display is the same function", config.officer_display is display)

# === 4. EDGE — unknown / empty key never crashes, falls back to itself ========================
check("unknown key falls back to itself", display("nope_not_real") == "nope_not_real")
check("empty key falls back to itself", display("") == "")

# === 5. LABEL MAPS RESOLVE TO THE SOT — board / roster / group-room labels == display(key) =====
# Stronger than "no retired name": every programmatic human-facing officer label must RESOLVE to
# display(internal_key). So if a future OFFICER_NAMES rename isn't mirrored in a label, or someone
# re-hard-codes a name, the label DIVERGES from the SOT and this goes red — catching STALE/divergent
# names, not only retired army ones. (Prose system prompts may still spell a name inline; what is
# pinned here are the programmatic label MAPS the cockpit/roster build from.)
from orchestrator import roster, warroom

# roster.py — _OFFICERS (display name, …) is built from _OFFICER_ROWS (internal key, …) in lockstep.
for (key, *_r), (name, *_n) in zip(roster._OFFICER_ROWS, roster._OFFICERS):
    check(f"roster label resolves to SOT: {key!r} -> {name!r}", name == display(key),
          f"{name!r} != display({key!r})={display(key)!r}")
check("roster derives every label (rows count == officers count)",
      len(roster._OFFICER_ROWS) == len(roster._OFFICERS))

# warroom.py — cockpit board roster AND the group-room consult map both resolve via _OFFICER_KEY
# (cockpit key -> internal officers key), so builder/reviewer/drill map to field_engineer/inspector/…
for uikey, name, _role in warroom._OFFICERS:
    ik = warroom._OFFICER_KEY.get(uikey, uikey)
    check(f"cockpit board label resolves to SOT: {uikey!r} -> {name!r}", name == display(ik),
          f"{name!r} != display({ik!r})={display(ik)!r}")
for uikey, gname in warroom._GROUP_NAME.items():
    ik = warroom._OFFICER_KEY.get(uikey, uikey)
    check(f"group-room consult name resolves to SOT: {uikey!r} -> {gname!r}", gname == display(ik),
          f"{gname!r} != display({ik!r})={display(ik)!r}")

# === 6. soldiers -> engineers applied to the human-facing PROMPTS (rename-map item) ===========
# Internal identifiers/tags/keys (tag "soldier·…", _SOLDIER_SYSTEM, soldier_tools, audit "soldier_build")
# intentionally STAY "soldier"; only the agent-facing PROMPT prose is renamed. We assert on the exact
# retired phrases — which only ever lived in prompts — so this can't false-fire on those internals.
PROMPT_RENAME = [
    ("orchestrator/squad.py",       ["You are a SOLDIER"],                          "You are an ENGINEER of the Dev Team Lead"),
    ("orchestrator/council.py",     ["soldiers do not speak", "needs a new soldier", "command soldiers"], "engineers do not speak"),
    ("orchestrator/recon.py",       ["YOU ARE A SOLDIER", "SOLDIER FINDINGS", "Your soldiers"], "YOU ARE AN ENGINEER"),
    ("orchestrator/drillmaster.py", ["officer or soldier"],                         "officer or engineer"),
    ("orchestrator/adjutant.py",    ["own SOLDIERS", "given soldiers", "HIRE a soldier"], "own ENGINEERS"),
]
for rel, absent, present in PROMPT_RENAME:
    src = (ROOT / rel).read_text(encoding="utf-8")
    for phrase in absent:
        check(f"{rel}: retired prompt wording gone: {phrase!r}", phrase not in src, "still present")
    check(f"{rel}: new wording present: {present!r}", present in src, "missing")

# === 7. RUNTIME — format_signals() renders the NEW name, never a retired/bare one ==============
# The reviewer named this surface explicitly: the Engineering Coach's signal digest hard-coded
# "Recurring Inspector issue areas:". It now resolves through display('inspector'), so exercise the
# REAL function (no LLM, pure string-build) and assert the rendered digest is clean + uses the new
# name — a source scan alone wouldn't catch a label rebuilt from a retired token at runtime.
import collections
from orchestrator import drillmaster

_sig = {
    "tasks": 5,
    "outcomes": collections.Counter({"landed": 3, "errored": 2}),
    "issue_areas": collections.Counter({"security": 4, "tests": 2}),
    "avg_passes": 1.4, "retried_tasks": 2, "max_effort_hits": 1, "gate_fails": 1, "needs_human": 0,
}
_fs = drillmaster.format_signals(_sig)
check("format_signals() output carries no retired/bare officer name",
      not OLD_NAME_PATTERN.search(_fs) and not BARE_PERSONA_PATTERN.search(_fs), f"leaked in: {_fs!r}")
check("format_signals() renders the NEW display name (Code Reviewer)",
      "Code Reviewer" in _fs, f"got: {_fs!r}")

# --------------------------------------------------------------------------------------------- #
print("\n=============== EU-40 OFFICER RENAME REGRESSION ===============")
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
passed = sum(1 for _, ok, _ in results if ok)
print("-" * 62)
print(f"  {passed}/{len(results)} passed", "✅" if passed == len(results) else "❌")
sys.exit(0 if passed == len(results) else 1)
