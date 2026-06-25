"""EU-40: pin the ACTUAL runtime resolver council._officer_key() to the single source of truth.

council parses an officer's human display name out of a 'MEETING:' line and resolves it back to the
immutable internal key via ``council._officer_key`` (which reads ``OFFICER_NAMES`` through its
``_NAME_TO_KEY`` index). The officer_rename_regression harness rebuilds its OWN name->key map and asserts
on that copy; this harness instead drives the real function, so a future drift between council's runtime
index and the SOT — or a re-hardcoded old name — is caught at the exact call site a human name flows through.

Covered:
  * happy path  — each NEW display name resolves to its stable internal key (case-insensitive).
  * round-trip  — display(key) feeds straight back into _officer_key(...) and lands on the same key.
  * regression  — RETIRED army display names ('Provost Marshal', 'Field Engineer', 'the General' …)
                  no longer resolve to the internal key (the rename actually moved the mapping).
  * edge        — unknown / empty / ad-hoc rank never crashes; falls back to a slug.
"""
import sys
import types

# Stub the Agent SDK exactly like council_test.py so importing council is network-free.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import council
from orchestrator.officers import OFFICER_NAMES, display

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# === 1. HAPPY PATH — new display names resolve to stable internal keys (via the real function) ====
HAPPY = {
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
for disp, key in HAPPY.items():
    check(f"_officer_key({disp!r}) -> {key!r}", council._officer_key(disp) == key,
          f"got {council._officer_key(disp)!r}")
    # case-insensitive: council lower-cases the parsed rank before lookup.
    check(f"_officer_key is case-insensitive for {disp!r}",
          council._officer_key(disp.upper()) == key and council._officer_key(disp.lower()) == key,
          f"upper->{council._officer_key(disp.upper())!r} lower->{council._officer_key(disp.lower())!r}")

# === 2. ROUND-TRIP — display(key) parsed back by the runtime resolver lands on the same key ========
for key in HAPPY.values():
    check(f"round-trip display({key!r}) -> _officer_key -> {key!r}",
          council._officer_key(display(key)) == key,
          f"display={display(key)!r} resolved={council._officer_key(display(key))!r}")

# === 3. REGRESSION — retired army display names no longer resolve to the internal key =============
# This is the EU-40 defect, pinned at the resolver: before the rename "Provost Marshal" et al. were the
# live display names and resolved straight to the key. After it, they must NOT — they're gone from the map,
# so the slug fallback yields something OTHER than the internal key (proving the mapping really moved).
RETIRED = {
    "Provost Marshal": "provost",
    "Field Engineer": "field_engineer",
    "Inspector General": "inspector",
    "the General": "general",
    "Release-master": "quartermaster",
}
for old, key in RETIRED.items():
    check(f"retired name {old!r} no longer resolves to {key!r}",
          council._officer_key(old) != key,
          f"old name still resolved to the internal key {key!r}")
# ...and none of the retired display names is even present as a value in the SOT map.
for old in RETIRED:
    check(f"retired name {old!r} absent from OFFICER_NAMES values",
          old not in OFFICER_NAMES.values())

# === 4. EDGE — unknown / empty / ad-hoc rank never crashes; slug fallback is returned =============
check("unknown rank -> slug fallback, no crash",
      council._officer_key("Some Ad-Hoc Person") == "some-ad-hoc-person",
      council._officer_key("Some Ad-Hoc Person"))
check("empty rank -> '' (no crash)", council._officer_key("") == "")
check("whitespace/case-only rank slugged, no crash",
      council._officer_key("Two Words") == "two-words", council._officer_key("Two Words"))
# every resolution returns a non-None str — the resolver must never blow up a lookup.
for probe in ("", "CTO", "Nope", "the General", "QA  Engineer"):
    check(f"_officer_key({probe!r}) returns a str", isinstance(council._officer_key(probe), str))

# --------------------------------------------------------------------------------------------- #
print("\n=============== EU-40 council._officer_key RESOLVER ===============")
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
passed = sum(1 for _, ok, _ in results if ok)
print("-" * 62)
print(f"  {passed}/{len(results)} passed", "✅" if passed == len(results) else "❌")
sys.exit(0 if passed == len(results) else 1)
