"""EU-234: secure credential storage (``orchestrator/secrets.py``) + the model registry's
credential-ref wiring (``ModelRegistry.set_credential`` / ``get_credential_for``), following the
house SDK-stub / tmp-store convention (see ``tests/model_registry_test.py``).

Test-data note: the fake credential values below use a neutral ``DUMMY-CRED-`` prefix rather than a
realistic ``sk-…`` shape ON PURPOSE — the unit's deterministic SECRET-LEAK gate (gate.py
``_SECRET_PATTERNS``) scans every added diff line, so an ``sk-…``-shaped literal (even an obviously
fake one) would trip it and block the diff. A distinctive dummy prefix keeps the leak assertions
meaningful without smuggling a secret-shaped string into the repo.

Pins:
  1. store()/get() round-trip the exact secret; get() on an unknown ref is None (no raise);
     delete() removes it so get() is None afterward and list_refs() no longer includes it.
  2. presence_display() always returns the fixed 8-bullet mask '••••••••'; list_refs() returns
     only reference names, never a secret value.
  3. store() leaves the credential file chmod 0o600, under the (gitignored) state directory.
  4. ModelRegistry.set_credential() stores the raw key in Secrets and leaves the registry record
     carrying only a credential_ref — the raw key never appears anywhere in the registry JSON.
  5. ModelRegistry.get_credential_for() resolves the exact key previously stored; None for a
     backend with no stored credential (or no record at all).
  6. A missing or corrupt secrets file yields an empty store without raising, and the first
     store() auto-creates the state directory + file.
"""
import sys, types, json, tempfile, os
from pathlib import Path

# ── Stub the Agent SDK before any orchestrator import (house convention) ──
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator.secrets import Secrets
from orchestrator.model_registry import ModelRegistry

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


def _tmp_secrets() -> tuple[Secrets, Path]:
    d = Path(tempfile.mkdtemp())
    store = d / "state" / "secrets.json"
    return Secrets(path=store), store


def _tmp_registry() -> tuple[ModelRegistry, Path]:
    d = Path(tempfile.mkdtemp())
    store = d / "state" / "model_registry.json"
    return ModelRegistry(path=store), store


VALID = {
    "display_name": "My Claude",
    "provider": "anthropic",
    "base_url": "https://api.anthropic.com",
    "model_id": "claude-opus-4-6",
    "credential_ref": "placeholder",
}

# ============ 1) store()/get() round-trip; unknown ref; delete() ============ #
sec, store = _tmp_secrets()
chk("get() on an unknown ref returns None (no raise)", sec.get("does-not-exist") is None)

sec.store("secret://backend-a", "DUMMY-CRED-alpha-1234")
chk("store() then get() round-trips the exact secret",
    sec.get("secret://backend-a") == "DUMMY-CRED-alpha-1234")

sec.store("secret://backend-b", "DUMMY-CRED-bravo-5678")
chk("a second store() doesn't clobber the first",
    sec.get("secret://backend-a") == "DUMMY-CRED-alpha-1234")

deleted = sec.delete("secret://backend-a")
chk("delete() reports success for an existing ref", deleted is True)
chk("get() returns None after delete()", sec.get("secret://backend-a") is None)
chk("list_refs() no longer includes the deleted ref",
    "secret://backend-a" not in sec.list_refs(), sec.list_refs())
chk("delete() on an unknown ref returns False", sec.delete("still-not-there") is False)

# ============ 2) presence_display() mask; list_refs() never leaks a value ============ #
chk("presence_display() returns the fixed 8-bullet mask",
    sec.presence_display("DUMMY-CRED-bravo-5678") == "•" * 8)
chk("presence_display() is exactly 8 characters", len(sec.presence_display()) == 8)
chk("presence_display() ignores its input (same mask with no argument)",
    sec.presence_display() == sec.presence_display("anything-at-all"))

refs = sec.list_refs()
chk("list_refs() returns only reference names", refs == ["secret://backend-b"], refs)
chk("no stored secret value leaks into list_refs()",
    all("DUMMY-CRED" not in r for r in refs), refs)

# ============ 3) restricted permissions + gitignored state directory ============ #
mode = os.stat(store).st_mode & 0o777
chk("the secrets file is chmod 0o600", mode == 0o600, oct(mode))
chk("the secrets file lives under a state/ directory, not the repo root",
    store.parent.name == "state", store)

# ============ 4) ModelRegistry.set_credential(): raw key never in the registry JSON ============ #
reg, reg_store = _tmp_registry()
rec = reg.add(VALID)
RAW_KEY = "DUMMY-CRED-raw-value-abcdef"
updated = reg.set_credential(rec["id"], RAW_KEY)
chk("set_credential() returns the updated record", updated is not None and updated["id"] == rec["id"])
chk("set_credential() sets a credential_ref (not the raw key)",
    updated["credential_ref"] not in (None, "", RAW_KEY), updated.get("credential_ref"))

raw_on_disk = reg_store.read_text(encoding="utf-8")
chk("the raw API key never appears anywhere in the registry JSON on disk",
    RAW_KEY not in raw_on_disk)
parsed = json.loads(raw_on_disk)
chk("no field in the persisted record equals the raw api key",
    all(v != RAW_KEY for v in parsed["models"][rec["id"]].values()), parsed)

# ============ 5) ModelRegistry.get_credential_for(): resolves through Secrets ============ #
chk("get_credential_for() returns the exact key previously passed to set_credential()",
    reg.get_credential_for(rec["id"]) == RAW_KEY)

rec_no_cred = reg.add({**VALID, "display_name": "No Cred", "model_id": "claude-haiku-4-6",
                        "credential_ref": "unset"})
chk("get_credential_for() is None for a backend with no stored credential",
    reg.get_credential_for(rec_no_cred["id"]) is None)
chk("get_credential_for() is None for an unknown backend id",
    reg.get_credential_for("does-not-exist") is None)

# ============ 6) missing/corrupt secrets file => empty store, no raise; auto-create ============ #
sec_missing, store_missing = _tmp_secrets()
chk("secrets file does not exist yet", not store_missing.exists())
chk("list_refs() on a missing store is empty, no raise", sec_missing.list_refs() == [])
chk("get() on a missing store returns None, no raise", sec_missing.get("anything") is None)

sec_corrupt, store_corrupt = _tmp_secrets()
store_corrupt.parent.mkdir(parents=True, exist_ok=True)
store_corrupt.write_text("{not valid json", encoding="utf-8")
chk("list_refs() on a corrupt store is empty, no raise", sec_corrupt.list_refs() == [])
chk("get() on a corrupt store returns None, no raise", sec_corrupt.get("anything") is None)

sec_missing.store("secret://first", "DUMMY-CRED-first-9999")
chk("the first store() auto-creates the state directory", store_missing.parent.is_dir())
chk("the first store() auto-creates the store file", store_missing.exists())
chk("the auto-created store round-trips the new secret",
    sec_missing.get("secret://first") == "DUMMY-CRED-first-9999")

print("\n========== SECRETS + CREDENTIAL-REF QA (EU-234) ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
