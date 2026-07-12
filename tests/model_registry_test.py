"""EU-233: the model registry data layer (``orchestrator/model_registry.py``) — CRUD + hermetic
persistence, following the house SDK-stub / tmp-store convention (see
``tests/backend_pref_hermetic_test.py``).

Pins:
  1. add() generates a UUID id + created_at/updated_at; get(id) round-trips the same record.
  2. list() returns all added records; delete(id) removes it from both get() and list().
  3. update() changes a field and advances updated_at while preserving id/created_at; update/get/
     delete on an unknown id are a clean no-op (None / False), never a corrupted store.
  4. A missing OR corrupt store file yields an empty registry (list() == []) without raising, and
     the first add() auto-creates the state directory + file.
  5. add() rejects an invalid provider / a missing required field with ValueError and writes
     nothing; only credential_ref is ever persisted — no raw secret field.
  6. This harness itself is picked up and stays green under tests/run_all.py (it lives at
     tests/*_test.py and follows the k/n-passed + sys.exit contract run_all.py verifies).
"""
import sys, types, json, tempfile, uuid, time
from pathlib import Path

# ── Stub the Agent SDK before any orchestrator import (house convention) ──
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator.model_registry import ModelRegistry

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

def _tmp_registry() -> tuple[ModelRegistry, Path]:
    d = Path(tempfile.mkdtemp())
    store = d / "state" / "model_registry.json"
    return ModelRegistry(path=store), store

def _is_uuid(s) -> bool:
    try:
        uuid.UUID(str(s))
        return True
    except (ValueError, TypeError):
        return False

VALID = {
    "display_name": "My Claude",
    "provider": "anthropic",
    "base_url": "https://api.anthropic.com",
    "model_id": "claude-opus-4-6",
    "small_fast_model_id": "claude-haiku-4-6",
    "credential_ref": "vault://anthropic/my-claude",
}

# ============ 1) add() + get(): generated UUID id, timestamps, round-trip ============ #
reg, store = _tmp_registry()
rec = reg.add(VALID)
chk("add() returns an id that is a real UUID", _is_uuid(rec.get("id")), rec.get("id"))
chk("add() stamps created_at", bool(rec.get("created_at")))
chk("add() stamps updated_at", bool(rec.get("updated_at")))
fetched = reg.get(rec["id"])
chk("get(id) returns the same record add() returned", fetched == rec, fetched)

# ============ 2) list() + delete() ============ #
rec2 = reg.add({**VALID, "display_name": "My GPT", "provider": "openai",
                "base_url": "https://api.openai.com", "model_id": "gpt-5",
                "credential_ref": "vault://openai/my-gpt"})
listed_ids = {r["id"] for r in reg.list()}
chk("list() returns every added record", listed_ids == {rec["id"], rec2["id"]}, listed_ids)

deleted = reg.delete(rec["id"])
chk("delete() reports success for an existing id", deleted is True)
chk("get() returns None after delete()", reg.get(rec["id"]) is None)
chk("list() no longer includes the deleted id", rec["id"] not in {r["id"] for r in reg.list()})

# ============ 3) update(): changes a field, advances updated_at, preserves id/created_at ===== #
before = reg.get(rec2["id"])
time.sleep(0.01)
updated = reg.update(rec2["id"], {"display_name": "Renamed GPT"})
chk("update() changes the targeted field", updated["display_name"] == "Renamed GPT", updated)
chk("update() preserves the original id", updated["id"] == before["id"])
chk("update() preserves the original created_at", updated["created_at"] == before["created_at"])
chk("update() advances updated_at", updated["updated_at"] != before["updated_at"],
    (before["updated_at"], updated["updated_at"]))

chk("update() on an unknown id returns None (no raise, no corruption)",
    reg.update("does-not-exist", {"display_name": "x"}) is None)
chk("get() on an unknown id returns None", reg.get("does-not-exist") is None)
chk("delete() on an unknown id returns False", reg.delete("does-not-exist") is False)
chk("store still holds exactly the one surviving record after the unknown-id ops",
    {r["id"] for r in reg.list()} == {rec2["id"]}, reg.list())

# ============ 4) missing / corrupt store => empty registry, no raise; first add() creates it === #
reg_missing, store_missing = _tmp_registry()
chk("store file does not exist yet", not store_missing.exists())
chk("list() on a missing store is empty, no raise", reg_missing.list() == [])
chk("get() on a missing store returns None, no raise", reg_missing.get("anything") is None)

reg_corrupt, store_corrupt = _tmp_registry()
store_corrupt.parent.mkdir(parents=True, exist_ok=True)
store_corrupt.write_text("{not valid json", encoding="utf-8")
chk("list() on a corrupt store is empty, no raise", reg_corrupt.list() == [])

first = reg_missing.add(VALID)
chk("first add() auto-creates the state directory", store_missing.parent.is_dir())
chk("first add() auto-creates the store file", store_missing.exists())
chk("the auto-created store round-trips the new record",
    reg_missing.get(first["id"]) == first)

# ============ 5) validation: bad provider / missing field rejected; no secret ever stored ===== #
reg_bad, store_bad = _tmp_registry()
try:
    reg_bad.add({**VALID, "provider": "azure"})
    bad_provider_raised = False
except ValueError:
    bad_provider_raised = True
chk("add() with an invalid provider raises ValueError", bad_provider_raised)
chk("a rejected add() writes nothing (store still missing)", not store_bad.exists())

missing_field = dict(VALID)
missing_field.pop("base_url")
try:
    reg_bad.add(missing_field)
    missing_field_raised = False
except ValueError:
    missing_field_raised = True
chk("add() with a missing required field raises ValueError", missing_field_raised)
chk("a rejected add() (missing field) writes nothing (store still missing)", not store_bad.exists())

# A stray raw-secret field must be dropped by the schema whitelist and NEVER reach the persisted
# store — that is the security contract. The value below is a harmless placeholder, not a real key.
secretive = reg_bad.add({**VALID, "api_key": "RAW-SECRET-PLACEHOLDER"})
chk("credential_ref is persisted as given",
    secretive["credential_ref"] == VALID["credential_ref"])
chk("an unrecognised secret-shaped field (e.g. api_key) is never persisted",
    "api_key" not in secretive, secretive)
raw_on_disk = json.loads(store_bad.read_text(encoding="utf-8"))
chk("the raw store file itself never contains an api_key field either",
    all("api_key" not in r for r in raw_on_disk.get("models", {}).values()), raw_on_disk)

print("\n========== MODEL REGISTRY CRUD QA (EU-233) ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
