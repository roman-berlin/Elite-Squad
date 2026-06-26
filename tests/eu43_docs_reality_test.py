"""EU-43: CLAUDE.md must not describe skills/docs that don't exist (the "docs lie" guard).

Greps the repo-root CLAUDE.md for every concrete path it claims — each repo-root `*.md` doc, each
`Documentation/*.md` doc, and each `.claude/skills/*` skill path — and asserts each one resolves to
a real file/dir on disk. Also checks the reverse: every file actually under Documentation/ is named
in CLAUDE.md's index, so a newly added doc can't silently drift out of the index. This is the safety
mechanism the ticket calls for, so CLAUDE.md, its repo-root doc list, and the Documentation/ index
can't fall out of sync with reality again.

No imports of the orchestrator, no network — pure filesystem assertions."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # the General repo root
CLAUDE = ROOT / "CLAUDE.md"

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# --- 0) CLAUDE.md exists at all (the doc the whole guard is about) ---
chk("CLAUDE.md exists at the repo root", CLAUDE.exists())
text = CLAUDE.read_text(encoding="utf-8") if CLAUDE.exists() else ""

# --- 1) every Documentation/*.md path CLAUDE.md names resolves to a real file ---
doc_refs = sorted(set(re.findall(r"Documentation/[A-Za-z0-9_\-./]+\.md", text)))
chk("CLAUDE.md references at least one Documentation/*.md (the index is present)", len(doc_refs) >= 1,
    str(doc_refs))
for ref in doc_refs:
    chk(f"referenced doc exists: {ref}", (ROOT / ref).is_file())

# --- 2) every .claude/skills/* path CLAUDE.md claims resolves (none expected, but guard future drift) ---
skill_refs = sorted(set(re.findall(r"\.claude/skills/[A-Za-z0-9_\-./]+", text)))
for ref in skill_refs:
    chk(f"claimed skill path exists: {ref}", (ROOT / ref).exists(), ref)
# The legacy /skill-name commands must not be presented as if invocable from a real skills dir.
chk("no bare '/skill-name' command claims without a backing .claude/skills file",
    not re.search(r"(?<![\w/])/(?:update-development-status|post-dev-checklist|run-elite-protocol|"
                  r"verify-tenant-isolation)\b", text),
    "found a /skill-name claim with no skills/ file")

# --- 3) reverse: every real Documentation/*.md is named in the index (no drift the other way) ---
real_docs = sorted(p.name for p in (ROOT / "Documentation").glob("*.md"))
chk("Documentation/ has docs to index", len(real_docs) >= 1, str(real_docs))
for name in real_docs:
    chk(f"Documentation/{name} is listed in CLAUDE.md's index", f"Documentation/{name}" in text, name)

# --- 4) every bare repo-root *.md CLAUDE.md names resolves (covers the 'Repo-root docs' line) ---
# Match `Foo.md` filenames that start at a real boundary (backtick/space/line start), NOT inside a
# longer token: the lookbehind rejects path separators (so `Documentation/*.md` refs, handled by
# section 1, are skipped) and filename chars `-`/`.` (so we don't latch onto a mid-name fragment
# like `06-25.md` inside `…2026-06-25.md`). The `*.md` glob has no alnum before its `.md`, so it
# never matches. This is the line that used to carry the phantom `ROSTER.md` reference, so the
# guard must cover it too.
root_md_refs = sorted(set(re.findall(r"(?<![\w/\-.])([A-Za-z0-9][A-Za-z0-9_\-]*\.md)", text)))
chk("CLAUDE.md names at least one repo-root *.md doc", len(root_md_refs) >= 1, str(root_md_refs))
for name in root_md_refs:
    chk(f"referenced repo-root doc exists: {name}", (ROOT / name).is_file(), name)

print("\n=============== EU-43 DOCS-REALITY QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
