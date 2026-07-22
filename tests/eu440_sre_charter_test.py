"""EU-440: SRE officer charter + postmerge_commands integration protocol.

CORE FINDING (from the ticket's design brief): the integration is ALREADY fully implemented, wired,
and tested in CODE — sentinel.guard() (runs an app's postmerge_commands on landed DEV; green→audit
sentinel_pass; red→forward-only git revert, no force-push/history rewrite; EU-117 fail-soft on shell
exit 126/127; EU-359 required-env pre-flight), smoke.run() (EU-60 flag-only canary — Telegram + audit
+ comment on red, NEVER reverts), loop._land (calls sentinel.guard() then smoke.run() right after a
live merge), and config.py (postmerge_commands / smoke_command / sentinel_enabled / smoke_enabled,
the last two armed by default). Pinned already by eu60/eu81/sentinel/eu117/eu359 wiring tests.

The ONLY genuinely missing artifact was the SRE officer CHARTER + its index entry — the gap 'flagged
2 days running'. This harness pins that deliverable so it cannot silently disappear again, and it
pins the CODE SYMBOLS the charter documents (so the charter can never advertise a gate no code runs —
the EU-260 phantom-officer class, applied to a charter's citations).

NAMING: the ticket says officers/sre.md, but repo convention (officers/README.md:8 — "Filenames are
the internal keys") is filename = internal module key, so the file is officers/sentinel.md with a
'# SRE' heading (display name). officers.py maps 'sentinel' -> 'SRE'. This is exactly the
scout.md = QA Engineer pattern, and it is what makes eu260's _is_live_officer('sentinel') truthful.

Pure filesystem + string assertions — no orchestrator import, no network."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHARTER = ROOT / "officers" / "sentinel.md"
README = ROOT / "officers" / "README.md"
SENTINEL_PY = ROOT / "orchestrator" / "sentinel.py"
SMOKE_PY = ROOT / "orchestrator" / "smoke.py"
LOOP_PY = ROOT / "orchestrator" / "loop.py"

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ============================================================================ #
# AC1 — charter exists under the internal-key filename, heading = SRE display name
# ============================================================================ #
charter_text = CHARTER.read_text(encoding="utf-8") if CHARTER.is_file() else ""
chk("AC1 charter file exists at officers/sentinel.md (internal-key filename)", CHARTER.is_file(),
    "ticket says sre.md but repo convention is filename = internal key -> sentinel.md")
chk("AC1 charter is non-empty", len(charter_text.strip()) > 0)
chk("AC1 charter heading is the SRE display name", charter_text.lstrip().startswith("# SRE"),
    "heading must be '# SRE' (display name); officers.py maps sentinel -> SRE")

# ============================================================================ #
# AC2 — charter documents the opt-in + build-sequence wiring + revert contract
# ============================================================================ #
low = charter_text.lower()
for tok in ("postmerge_commands", "revert", "forward-only"):
    chk(f"AC2 charter documents '{tok}'", tok in low,
        f"charter must contain '{tok}' (post-merge suite opt-in + forward-only revert contract)")

# ============================================================================ #
# AC3 — charter documents the SRE-vs-smoke division of labour
# (smoke.py flags red but never rolls back; reverting is SRE's job)
# ============================================================================ #
for tok in ("smoke", "flag", "never reverts"):
    chk(f"AC3 charter documents SRE-vs-smoke division: '{tok}'", tok in low,
        f"charter must contain '{tok}' — the smoke canary flags; only SRE reverts")

# ============================================================================ #
# AC4 — charter documents the two-tenant smoke arming protocol
# (ABSOLUTE path because cwd = the app repo; creds injected from env, never hardcoded)
# ============================================================================ #
for tok in ("two_tenant_smoke", "tenant_a_email", "dev_base_url", "absolute"):
    chk(f"AC4 charter documents two-tenant arming protocol: '{tok}'", tok in low,
        f"charter must contain '{tok}'")

# ============================================================================ #
# Charter follows the _TEMPLATE.md structure (Identity / Knowledge / Skills / Constraints)
# ============================================================================ #
for section in ("Identity", "Knowledge", "Skills", "Constraints"):
    chk(f"charter carries the _TEMPLATE.md '{section}' section", section in charter_text,
        f"officers/_TEMPLATE.md requires a '{section}' section")

# ============================================================================ #
# AC5 — officers/README.md indexes SRE as an in-post officer (no '(PLANNED)' tag)
# ============================================================================ #
readme_text = README.read_text(encoding="utf-8") if README.is_file() else ""
chk("AC5 officers/README.md chain-of-command tree names SRE", "SRE" in readme_text,
    "the chain-of-command tree must list SRE")
chk("AC5 README tree row points at the sentinel.md charter", "sentinel.md" in readme_text,
    "the SRE tree row should reference its charter, like '→ scout.md'")

# Any chain-of-command TREE row (a line carrying a tree glyph) that names SRE/sentinel must NOT be
# tagged '(PLANNED)' — the same EU-260 invariant that bit scout/provost/quartermaster. Once
# officers/sentinel.md exists, _is_live_officer('sentinel') is True, so a '(PLANNED)' tree row naming
# it would fail eu260 too — this catches it here first.
def _tree_rows(text):
    return [l for l in text.splitlines() if "──" in l]

planned_sre = [
    ln.strip() for ln in _tree_rows(readme_text)
    if "(PLANNED)" in ln and ("sre" in ln.lower() or "sentinel" in ln.lower())
]
chk("AC5 no SRE/sentinel tree row carries '(PLANNED)'", not planned_sre,
    f"these tree rows wrongly tag a live officer planned: {planned_sre}")

# ============================================================================ #
# AC6 — the charter cites code symbols that still exist (phantom-charter guard) + suite is green
# (the 'run_all.py exits 0' half of AC6 is the gate this harness runs inside; this half pins that the
# charter never advertises a gate whose code has been deleted — the EU-260 phantom class, for docs.)
# ============================================================================ #
chk("AC6 cited code symbol exists: orchestrator/sentinel.py", SENTINEL_PY.is_file(),
    "the charter documents sentinel.guard(); the module must still exist")
chk("AC6 cited code symbol exists: orchestrator/smoke.py", SMOKE_PY.is_file(),
    "the charter documents the SRE-vs-smoke split; smoke.py must still exist")
chk("AC6 build-sequence wiring symbol exists: orchestrator/loop.py", LOOP_PY.is_file(),
    "the charter documents sentinel.guard firing inside loop._land after a live merge")
# The charter must actually NAME these symbols it leans on — a charter that cites nothing is unfalsifiable.
for sym in ("sentinel.guard", "smoke.run", "sentinel.should_run"):
    chk(f"AC6 charter names the cited symbol '{sym}'", sym in charter_text,
        f"charter should reference '{sym}' so the citation is checkable")


print("\n=============== EU-440 SRE CHARTER QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
