"""EU-234: credential storage (``orchestrator/secrets.py``) + the model registry's
``set_credential``/``get_credential_for`` integration, following the house SDK-stub / tmp-store
convention (see ``tests/model_registry_test.py``).

Pins:
  1. store()/get() round-trip a raw value; get() on an unknown ref returns None; delete() removes
     it (get() then None again) and reports True; delete() on an unknown ref returns False, no
     raise.
  2. list_refs() returns exactly the stored ref keys and NEVER a raw secret value, even though the
     raw value is present on disk.
  3. Every store()/delete() leaves the credential file at permissions exactly 0o600, created fresh
     under a tmp state/ dir with no raise even when the dir/file did not previously exist.
  4. presence_display() returns a constant 8-bullet mask for any input, never containing any
     character of the underlying secret.
  5. model_registry.set_credential()/get_credential_for() round-trip a raw key via a
     credential_ref, without ever persisting the raw key on the registry record; unknown
     backend_id is a clean None.
  6. This harness itself is picked up and stays green under tests/run_all.py.
"""
import stat
import sys
import tempfile
import types
from pathlib import Path

# ── Stub the Agent SDK before any orchestrator import (house convention) ──
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator.secrets import Secrets, presence_display
from orchestrator.model_registry import ModelRegistry

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


def _tmp_secrets() -> tuple[Secrets, Path]:
    d = Path(tempfile.mkdtemp())
    store = d / "state" / "secrets.yaml"
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

# ============ 1) store()/get()/delete() round-trip ============ #
sec, store = _tmp_secrets()
chk("store file does not exist yet", not store.exists())
sec.store("ref-x", "RAW-KEY")
chk("get() returns the stored value", sec.get("ref-x") == "RAW-KEY")
chk("get() on an unknown ref returns None", sec.get("does-not-exist") is None)

deleted = sec.delete("ref-x")
chk("delete() reports True for an existing ref", deleted is True)
chk("get() returns None after delete()", sec.get("ref-x") is None)
chk("delete() on an unknown ref returns False (no raise)", sec.delete("does-not-exist") is False)

# ============ 2) list_refs(): keys only, never raw values ============ #
sec2, store2 = _tmp_secrets()
sec2.store("ref-a", "SECRET-A-VALUE")
sec2.store("ref-b", "SECRET-B-VALUE")
refs = sec2.list_refs()
chk("list_refs() returns exactly the stored ref keys", set(refs) == {"ref-a", "ref-b"}, refs)
chk("list_refs() never contains a raw secret value",
    all("SECRET" not in r for r in refs), refs)
raw_on_disk = store2.read_text(encoding="utf-8")
chk("the raw value IS present on disk (sanity check the store actually holds it)",
    "SECRET-A-VALUE" in raw_on_disk)
chk("presence_display() output never appears alongside a raw value leak in list_refs()",
    presence_display("SECRET-A-VALUE") not in refs)

# ============ 3) file permissions: exactly 0o600 after store()/delete() ============ #
def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)

sec3, store3 = _tmp_secrets()
chk("state dir does not exist yet", not store3.parent.exists())
sec3.store("ref-c", "RAW-C")
chk("store() creates the file", store3.exists())
chk("store() leaves the file at exactly 0o600", _mode(store3) == 0o600, oct(_mode(store3)))

sec3.delete("ref-c")
chk("delete() leaves the file at exactly 0o600", _mode(store3) == 0o600, oct(_mode(store3)))

sec3.delete("still-unknown")
chk("delete() on an unknown ref still leaves the file at exactly 0o600 (no raise)",
    _mode(store3) == 0o600, oct(_mode(store3)))

# ============ 4) presence_display(): constant 8-bullet mask, never the secret ============ #
mask1 = presence_display("some-raw-secret-value")
mask2 = presence_display("a-totally-different-value")
mask3 = presence_display(None)
chk("presence_display() is exactly 8 bullet characters", mask1 == "•" * 8, mask1)
chk("presence_display() is constant regardless of input", mask1 == mask2 == mask3)
chk("presence_display() never contains any character of the underlying secret",
    "some-raw-secret-value" not in mask1 and "s" not in mask1)

# ============ 5) model_registry.set_credential()/get_credential_for() ============ #
reg, reg_store = _tmp_registry()
rec = reg.add(VALID)
reg.set_credential(rec["id"], "RAW-API-KEY-VALUE")

persisted_record = reg.get(rec["id"])
chk("set_credential() does not add a raw api_key field to the record",
    "api_key" not in persisted_record, persisted_record)
chk("set_credential() still leaves credential_ref as the only credential-shaped field",
    persisted_record.get("credential_ref") != "RAW-API-KEY-VALUE", persisted_record)

raw_registry_json = reg_store.read_text(encoding="utf-8")
chk("the raw registry JSON on disk never contains the raw key",
    "RAW-API-KEY-VALUE" not in raw_registry_json)

fetched_key = reg.get_credential_for(rec["id"])
chk("get_credential_for() returns the original raw key", fetched_key == "RAW-API-KEY-VALUE")

chk("get_credential_for() on an unknown backend_id returns None (no raise)",
    reg.get_credential_for("does-not-exist") is None)

print("\n========== SECRETS + CREDENTIAL STORAGE QA (EU-234) ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
