"""EU-105: SecurityArtifact contract and PerTicketArtifactStore.security slot."""
import sys
sys.path.insert(0, ".")

from orchestrator.contracts import (
    SecurityArtifact,
    PerTicketArtifactStore,
    SpecArtifact,
    BuildArtifact,
    ReviewVerdict,
    Verdict,
)

results = []


def chk(name, cond, detail=""):
    results.append((name, bool(cond), detail))


# --------------------------------------------------------------------------- #
# SecurityArtifact.is_signed() — fully-signed artifact
# --------------------------------------------------------------------------- #
good = SecurityArtifact(
    s1_secrets="No hardcoded credentials found in diff.",
    s2_authz="All new routes protected by require_auth middleware.",
    s3_injection="All DB calls use parameterised queries.",
    signed=True,
)
chk("is_signed returns True when all fields filled and signed=True", good.is_signed() is True)

# --------------------------------------------------------------------------- #
# signed=False with real prose → still fails
# --------------------------------------------------------------------------- #
not_signed = SecurityArtifact(
    s1_secrets="No secrets found.",
    s2_authz="Routes guarded.",
    s3_injection="Parameterised.",
    signed=False,
)
chk("is_signed returns False when signed=False even with real prose",
    not_signed.is_signed() is False)

# --------------------------------------------------------------------------- #
# Empty fields fail regardless of signed flag
# --------------------------------------------------------------------------- #
for field_name, kwargs in [
    ("s1_secrets",   dict(s1_secrets="",  s2_authz="ok", s3_injection="ok")),
    ("s2_authz",     dict(s1_secrets="ok", s2_authz="",  s3_injection="ok")),
    ("s3_injection", dict(s1_secrets="ok", s2_authz="ok", s3_injection="")),
]:
    a = SecurityArtifact(**kwargs, signed=True)
    chk(f"is_signed returns False when {field_name} is empty", a.is_signed() is False)

# Whitespace-only
ws = SecurityArtifact(s1_secrets="   ", s2_authz="ok", s3_injection="ok", signed=True)
chk("is_signed returns False when s1_secrets is whitespace-only", ws.is_signed() is False)

# --------------------------------------------------------------------------- #
# Template placeholders (angle-bracket text) fail
# --------------------------------------------------------------------------- #
placeholder_cases = [
    "<describe secrets here>",
    "<§1 fill-in text>",
    "<no credentials found or list them>",
    "  <placeholder>  ",   # leading/trailing whitespace still detected
]
for ph in placeholder_cases:
    a = SecurityArtifact(s1_secrets=ph, s2_authz="ok", s3_injection="ok", signed=True)
    chk(f"is_signed rejects placeholder {ph!r}", a.is_signed() is False)

# A field that happens to CONTAIN angle brackets but is NOT a bare placeholder should pass
multi_sentence = SecurityArtifact(
    s1_secrets="Checked for <token> patterns; none found in changed files.",
    s2_authz="Route /api/foo protected by check_auth() at line 42.",
    s3_injection="cursor.execute(sql, params) used throughout.",
    signed=True,
)
chk("is_signed accepts prose that contains '<…>' substring but is not ONLY a placeholder",
    multi_sentence.is_signed() is True)

# --------------------------------------------------------------------------- #
# PerTicketArtifactStore — security slot and put()/get_security()
# --------------------------------------------------------------------------- #
store = PerTicketArtifactStore()
chk("security slot is None before anything is published", store.security is None)
chk("get_security() returns None before anything is published", store.get_security() is None)

store.put(good)
chk("put(SecurityArtifact) sets the security slot", store.security is good)
chk("get_security() returns the artifact after put()", store.get_security() is good)
chk("get_security() returns a SecurityArtifact", isinstance(store.get_security(), SecurityArtifact))

# Existing slots are unaffected
chk("spec slot still None after security put()", store.spec is None)
chk("build slot still None after security put()", store.build is None)
chk("review slot still None after security put()", store.review is None)

# Overwrite (second put replaces)
another = SecurityArtifact(s1_secrets="updated", s2_authz="updated", s3_injection="updated", signed=True)
store.put(another)
chk("put() overwrites existing security artifact", store.get_security() is another)

# --------------------------------------------------------------------------- #
# put() type dispatch — other artifact types still route correctly
# --------------------------------------------------------------------------- #
store2 = PerTicketArtifactStore()
spec = SpecArtifact(acceptance=["AC1"], scope="narrow", non_goals=["foo"])
build = BuildArtifact(
    files_changed=["a.py"],
    diff_digest="short",
    decisions=["used X"],
    open_questions=[],
)
review = ReviewVerdict(verdict=Verdict.PASS, blocking=[], notes=[])
security = SecurityArtifact(s1_secrets="ok", s2_authz="ok", s3_injection="ok", signed=True)

for artifact in (spec, build, review, security):
    store2.put(artifact)

chk("spec slot filled correctly via put()", store2.spec is spec)
chk("build slot filled correctly via put()", store2.build is build)
chk("review slot filled correctly via put()", store2.review is review)
chk("security slot filled correctly via put()", store2.security is security)

# Unknown type raises TypeError
try:
    store2.put("not an artifact")  # type: ignore[arg-type]
    chk("put() raises TypeError for unknown type", False)
except TypeError:
    chk("put() raises TypeError for unknown type", True)

# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
print("\n============ SECURITY ARTIFACT (EU-105) ============")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))
print("----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
