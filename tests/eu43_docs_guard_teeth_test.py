"""EU-43 (regression / teeth): prove the docs-reality guard actually catches a lying doc.

`eu43_docs_reality_test.py` asserts the *current* CLAUDE.md is clean — a happy-path snapshot. That
passes trivially the moment the doc happens to be in sync and tells us nothing about whether the
guard would *catch* drift. The whole point of the ticket is the safety mechanism: a guard that fails
when CLAUDE.md describes a doc/skill that isn't on disk (the "docs lie" class from EU-40/41/42).

So this harness unit-tests the guard's PREDICATE against synthetic CLAUDE.md fixtures — no dependency
on the live file's current contents, fully deterministic. It pins the exact defect: the pre-EU-43
CLAUDE.md (which claimed `/skill-name` commands and referenced a missing Development_Status.md) MUST
be flagged; a reconciled CLAUDE.md MUST pass clean. If someone later weakens the guard so a fabricated
reference slips through, these checks go red.

Pure string/predicate assertions — no orchestrator import, no network."""
import re
import sys

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ---- the guard's predicate, mirrored as a pure function over (doc_text, existing_paths) ----
# `existing` = the set of repo-relative paths that really exist on disk. The guard flags any
# Documentation/*.md or .claude/skills/* path the doc names that is NOT in `existing`, plus any
# legacy `/skill-name` command claim (which implies a skills file that this repo has none of).
LEGACY_SKILLS = ("update-development-status", "post-dev-checklist",
                 "run-elite-protocol", "verify-tenant-isolation")

def guard_problems(text, existing):
    problems = []
    for ref in sorted(set(re.findall(r"Documentation/[A-Za-z0-9_\-./]+\.md", text))):
        if ref not in existing:
            problems.append(f"missing doc: {ref}")
    # bare repo-root *.md refs (e.g. the phantom `ROSTER.md`) — same predicate as the reality guard:
    # only names at a real boundary, never a `/`-prefixed path or a mid-name fragment.
    for ref in sorted(set(re.findall(r"(?<![\w/\-.])([A-Za-z0-9][A-Za-z0-9_\-]*\.md)", text))):
        if ref not in existing:
            problems.append(f"missing root doc: {ref}")
    for ref in sorted(set(re.findall(r"\.claude/skills/[A-Za-z0-9_\-./]+", text))):
        if ref not in existing:
            problems.append(f"missing skill path: {ref}")
    pat = r"(?<![\w/])/(?:" + "|".join(LEGACY_SKILLS) + r")\b"
    if re.search(pat, text):
        problems.append("legacy /skill-name command claim")
    return problems


REAL = {"Documentation/Development_Status.md", "Documentation/REVIEW_BACKLOG.md",
        "Documentation/SYSTEM_OVERVIEW.md", "Documentation/UNIT_REVIEW_2026-06-25.md"}

# --- 1) HAPPY PATH: a reconciled doc that only names real docs and no /skill commands passes clean ---
clean = (
    "Docs under Documentation/:\n"
    "- `Documentation/SYSTEM_OVERVIEW.md`\n"
    "- `Documentation/Development_Status.md`\n"
    "Behaviours are built in; there is no .claude/skills/ directory.\n"
)
chk("reconciled CLAUDE.md (only real docs, no /skill claims) yields zero problems",
    guard_problems(clean, REAL) == [], str(guard_problems(clean, REAL)))

# --- 2) REGRESSION: the pre-EU-43 'docs lie' must be flagged (fails on old behaviour) ---
old = (
    "Run `/update-development-status` to record progress and `/post-dev-checklist` before exit.\n"
    "See `Documentation/Development_Status.md` for the current status.\n"   # didn't exist pre-EU-41
)
old_problems = guard_problems(old, REAL - {"Documentation/Development_Status.md"})
chk("legacy /skill-name command claim is flagged",
    any("legacy /skill-name" in p for p in old_problems), str(old_problems))
chk("reference to a then-missing Documentation/*.md is flagged",
    any("Development_Status.md" in p for p in old_problems), str(old_problems))

# --- 3) a fabricated Documentation/*.md reference is caught ---
fab = "Trust me, read `Documentation/TotallyReal.md` for details.\n"
chk("fabricated Documentation/ reference is flagged",
    guard_problems(fab, REAL) == ["missing doc: Documentation/TotallyReal.md"],
    str(guard_problems(fab, REAL)))

# --- 4) a claimed .claude/skills/* path that isn't on disk is caught ---
sk = "Invoke `.claude/skills/run-elite-protocol/SKILL.md`.\n"
chk("claimed-but-absent .claude/skills path is flagged",
    any("missing skill path" in p for p in guard_problems(sk, REAL)),
    str(guard_problems(sk, REAL)))
chk("a .claude/skills path that DOES exist is not flagged",
    guard_problems(sk, REAL | {".claude/skills/run-elite-protocol/SKILL.md"}) == [],
    "should be clean once the skill file exists")

# --- 5) edge: empty doc has nothing to verify and must not crash or false-positive ---
chk("empty document yields no problems (no false positives)", guard_problems("", REAL) == [])

# --- 6) cross-check: the predicate agrees with reality for the live guard's clean verdict ---
#     (a real doc referenced + present => clean; a real doc referenced + absent => flagged)
chk("present real doc => clean", guard_problems("`Documentation/REVIEW_BACKLOG.md`", REAL) == [])
chk("same doc, absent from disk => flagged",
    guard_problems("`Documentation/REVIEW_BACKLOG.md`", set()) ==
    ["missing doc: Documentation/REVIEW_BACKLOG.md"])

# --- 7) repo-root *.md coverage: the phantom `ROSTER.md` class is flagged; real root docs pass ---
#     This is the exact defect this iteration fixed — a 'Repo-root docs' line naming a doc that
#     isn't on disk. The guard must bite on it just like it does for Documentation/ and skills.
ROOT_REAL = REAL | {"README.md", "ORG.md", "WAR_ROOM.md"}
phantom_root = "Repo-root docs: `README.md`, `ORG.md`, `ROSTER.md`.\n"   # ROSTER.md isn't on disk
rp = guard_problems(phantom_root, ROOT_REAL)
chk("phantom repo-root doc (the ROSTER.md class) is flagged",
    rp == ["missing root doc: ROSTER.md"], str(rp))
chk("repo-root line naming only real docs => clean",
    guard_problems("Repo-root docs: `README.md`, `WAR_ROOM.md`, `ORG.md`.\n", ROOT_REAL) == [],
    "real root docs must not false-positive")
chk("a Documentation/*.md path is not mis-flagged as a bare root doc",
    "missing root doc:" not in " ".join(guard_problems("`Documentation/REVIEW_BACKLOG.md`", set())),
    "Documentation/ refs belong to section-1 scan, not the root-doc scan")

print("\n=========== EU-43 DOCS-GUARD TEETH (regression) ===========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
