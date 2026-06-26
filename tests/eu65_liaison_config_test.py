"""EU-65 [Ordnance BE] — external/liaison chat config + flag (additive foundation).

Asserts the inter-unit liaison settings on Config: parsed external chat ids, the master flag, the
cheap-model / token-cap settings, and the helper predicates — all kept SEPARATE from the ops
TELEGRAM_CHAT_ID. The key invariant: with nothing configured every value is empty / disabled, so
behaviour is byte-identical to today (the channel is inert until the Commander opts in).
"""
import sys

sys.path.insert(0, ".")
from orchestrator.config import Config, AppConfig, parse_external_chat_ids

_app = AppConfig(name="x", repo_path=".")


def cfg(**kw) -> Config:
    return Config(apps=[_app], **kw)


print("=== parse_external_chat_ids ===")
assert parse_external_chat_ids(None) == [], "None -> empty"
assert parse_external_chat_ids("") == [], "blank -> empty"
assert parse_external_chat_ids("   \n ") == [], "whitespace -> empty"
# comma / space / newline separated, de-duped, order preserved
assert parse_external_chat_ids("-100123, -100456  -100123\n-100789") == \
    ["-100123", "-100456", "-100789"], "split + dedupe + order"
assert parse_external_chat_ids("999") == ["999"], "single id"
assert parse_external_chat_ids(12345) == ["12345"], "non-str coerced"

print("=== empty defaults => inert (byte-identical to today) ===")
c = cfg()
assert c.liaison_enabled is False, "flag OFF by default"
assert c.liaison_external_chat_ids == [], "no external chats by default"
assert c.liaison_active() is False, "inert with nothing configured"
assert c.is_liaison_chat(-100123) is False, "no chat matches when empty"
assert c.is_liaison_chat(None) is False, "None never a liaison chat"

print("=== cheap-model / budget-cap defaults ===")
assert "haiku" in c.liaison_model, "liaison replies use the cheap model, never Opus/Sonnet"
assert c.liaison_model != Config.builder_model, "kept off the Builder's Opus tier"
assert c.liaison_effort == "low", "minimal reasoning depth for an outward reply"
assert isinstance(c.liaison_daily_token_budget, int) and c.liaison_daily_token_budget > 0, "a real per-day cap"
assert c.liaison_max_reply_chars > 0, "a bounded reply size"

print("=== active only when flagged AND configured ===")
assert cfg(liaison_enabled=True).liaison_active() is False, "flag alone is not enough"
assert cfg(liaison_external_chat_ids=["-100123"]).liaison_active() is False, "ids alone is not enough"
on = cfg(liaison_enabled=True, liaison_external_chat_ids=["-100123", "-100456"])
assert on.liaison_active() is True, "flagged + configured -> active"

print("=== is_liaison_chat matches as strings (int update id vs str config) ===")
assert on.is_liaison_chat(-100123) is True, "int id matches str config"
assert on.is_liaison_chat("-100456") is True, "str id matches"
assert on.is_liaison_chat(-100999) is False, "unknown id rejected"

print("ALL GREEN — eu65_liaison_config")
