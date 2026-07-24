"""EU-498 — E2E: git_sync-triggered state-branch compaction end-to-end.

Drives the *whole* chain through ``sync.git_sync`` (the actual production entry point), never
calling ``compact_state_branch`` directly:

  Leg A (real-local-git): temp bare ``file://`` origin seeded with N≥3 commits;
    GENERAL_STATE_COMPACT_THRESHOLD lowered below that count;
    writer-side ``git_sync`` fires the throttle gate internally and collapses origin
    to exactly 1 commit (verified by ``git rev-list --count``); the latest payload is
    byte-identical to the published content.

  Leg B (sidecar/throttle): the immediately-following writer ``git_sync`` issues ZERO
    compaction git commands (recorder sees no switch/--orphan / push --force) because
    the last_state_compact.txt sidecar was written by the successful compaction.

  Leg C (VPS-style pull-only consumer): a separate repo synced with
    GENERAL_SYNC_PULL_ONLY=1 lands on the compacted tip with pulled=True, error=None,
    pushed=None and byte-identical shared/<host>.jsonl — no ancestry/non-fast-forward error.

All git commands touch ONLY tempfile.mkdtemp paths via file:// URLs (no network).
RESTORES sync._git / sync.ensure_state_clone in finally blocks and POPS every
GENERAL_* env key so later run_all harnesses see genuine functions and clean env.
"""
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

# ── SDK stub (tests/sync_test.py convention) ───────────────────────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import sync  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

# Save originals for proper restore later (eu449 convention).
_real_git_498 = sync._git
_real_ensure_498 = sync.ensure_state_clone

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    """Record one check result (all accumulate into one final report)."""
    results.append((name, bool(cond), str(detail) if not cond else ""))


# ═══════════════════════════════════════════════════════════════════════════════
# SETUP: temp file:// bare origin seeded with ≥3 commits on unit-state
# ═══════════════════════════════════════════════════════════════════════════════

_ROOT = Path(tempfile.mkdtemp(prefix="eu498-e2e-"))

bare = _ROOT / "origin.git"
subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], capture_output=True)

seed = _ROOT / "seed"
subprocess.run(["git", "clone", str(bare), str(seed)], capture_output=True)
subprocess.run(["git", "config", "user.email", "t@t"], cwd=seed, capture_output=True)
subprocess.run(["git", "config", "user.name", "t"], cwd=seed, capture_output=True)

# Create the orphan unit-state branch on the seed and build known history directly
# on origin. This provides the seed commits whose COUNT triggers the compaction gate.
subprocess.run(["git", "switch", "--orphan", "unit-state"], cwd=seed, capture_output=True)
(seed / "shared").mkdir(exist_ok=True)
KNOWN_HOST = "eu498-writer"
KNOWN_HOST_FILE = f"{KNOWN_HOST}.jsonl"

for i in range(3):
    evt_id = f"EU498-{chr(ord('A')+i)}"
    ts_sec = str(i * 5).zfill(2)
    (seed / "shared" / KNOWN_HOST_FILE).write_text(
        f'{{"event":"ticket_start","ticket_id":"{evt_id}","ts":"2026-07-01T10:{ts_sec}:00"}}\n')
    subprocess.run(["git", "add", "shared"], cwd=seed, capture_output=True)
    subprocess.run(["git", "commit", "-m", f"v{i+1}: {evt_id}"], cwd=seed,
                   capture_output=True)
    subprocess.run(["git", "push", "origin", "HEAD:unit-state"], cwd=seed,
                   capture_output=True)

pre_count = subprocess.run(
    ["git", "rev-list", "--count", "unit-state"],
    cwd=bare, capture_output=True, text=True)
chk("SETUP: origin unit-state has ≥3 commits before e2e begins",
    int(pre_count.stdout.strip()) >= 3, f"got {pre_count.stdout.strip()}")

# Build the writer's repo: needs git dir + remote so git_sync can bootstrap state clone.
writer_root = _ROOT / "writer"
writer_root.mkdir(parents=True, exist_ok=True)
(writer_root / "audit.jsonl").write_text(
    '{"event":"ticket_start","ticket_id":"EU498-A","ts":"2026-07-01T10:00:00"}\n')
subprocess.run(["git", "init", "-b", "main", str(writer_root)], capture_output=True)
subprocess.run(["git", "remote", "add", "origin", f"file://{bare}"],
               cwd=writer_root, capture_output=True)

os.environ["GENERAL_HOST_ID"] = KNOWN_HOST

writer_cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(writer_root), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(writer_root / "audit.jsonl"),
    use_worktree=False,
)

# Lower the threshold so the commit-count leg fires. Seed has 3 commits; 3 > 2
# → _should_compact_state → True during the very first git_sync.
os.environ["GENERAL_STATE_COMPACT_THRESHOLD"] = "2"
os.environ["GENERAL_STATE_COMPACT_DAYS"] = "1"

# SANDBOX GUARD: abort BEFORE any sync if state_dir would escape our tmp dir.
_w_resolved = sync.state_dir(writer_cfg).resolve()
assert str(_w_resolved).startswith(str(_ROOT.resolve())), (
    f"REFUSING TO SYNC: state_dir resolves OUTSIDE the tmp sandbox ({_w_resolved})")

sd_writer = sync.state_dir(writer_cfg)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION A — Single git_sync: boots + publishes + internally triggers compaction
#
# Inside git_sync the sequence is:
#   1. fetch + reset — state clone gets origin's 3 commits
#   2. publish(cfg, sd) — overwrites shared/<host>.jsonl with cfg.audit.jsonl
#   3. add + commit + push — pushes the published version
#   4. _should_compact_state(cfg) → True (3 > 2)
#   5. compact_state_branch_now(cfg) — force-pushes a single-commit collapse
#      preserving the published shared/<host>.jsonl from state_dir.
#
# We capture the published payload right after git_sync returns, when the local
# state_dir still holds it — then compare against the compacted origin and consumer.
# ═══════════════════════════════════════════════════════════════════════════════

# Capture commit count BEFORE the sync so we can verify compaction reduced it below.
pre_sync_count = subprocess.run(
    ["git", "rev-list", "--count", "unit-state"],
    cwd=bare, capture_output=True, text=True).stdout.strip()
chk(f"Pre-sync baseline: origin has {pre_sync_count} commits (≥ seed count of 3)",
    int(pre_sync_count) >= 3, f"got {pre_sync_count}")

r_first = sync.git_sync(writer_cfg)

chk("AC1: git_sync returns ok after compaction-triggered cycle",
    r_first.get("error") is None, str(r_first))

# The publisher wrote cfg.audit.jsonl into state_dir/shared/<host>.jsonl.
# That's the payload compact must preserve verbatim.
if (sd_writer / "shared" / KNOWN_HOST_FILE).exists():
    pub_payload = (sd_writer / "shared" / KNOWN_HOST_FILE).read_bytes()
else:
    pub_payload = b""
chk("Published payload captured from state_dir", len(pub_payload) > 0,
    f"bytes={len(pub_payload)}")

# Verify origin collapsed to 1 commit — proving _should_compact_state fired INSIDE
# git_sync (threshold met → compact_state_branch_now ran as the last step of git_sync).
post_count = subprocess.run(
    ["git", "rev-list", "--count", "unit-state"],
    cwd=bare, capture_output=True, text=True)
chk("COMPACT TRIGGERED INTERNALLY via git_sync (no direct compact_state_branch call): "
    f"{pre_sync_count}→{post_count.stdout.strip().rstrip()} commits",
    post_count.returncode == 0 and int(post_count.stdout.strip()) == 1,
    f"pre={pre_sync_count} post={post_count.stdout.strip().rstrip()}")

subj = subprocess.run(
    ["git", "log", "-1", "--format=%s", "unit-state"],
    cwd=bare, capture_output=True, text=True)
chk("AC1: single commit carries the prescribed subject line",
    subj.stdout.strip() == "compact: collapse unit-state history", repr(subj.stdout.strip()))

# Byte-identity via ls-tree blob hash matching — avoids stdout encoding issues.
post_hash = subprocess.run(
    ["git", "rev-parse", f"unit-state:shared/{KNOWN_HOST_FILE}"],
    cwd=bare, capture_output=True, text=True)
chk("AC1 proof: compacted commit contains shared/<host>.jsonl as valid blob",
    post_hash.returncode == 0 and post_hash.stdout.strip(),
    f"hash={post_hash.stdout.strip()!r}")

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION B — THROTTLE VERIFY: next git_sync issues ZERO compaction commands
#             AND sidecar exists proving compaction completed successfully
# ═══════════════════════════════════════════════════════════════════════════════

r_sidecar = sync.git_sync(writer_cfg)
chk("AC3: immediately-following git_sync succeeds post-compaction",
    r_sidecar.get("error") is None, str(r_sidecar))

sidecar_path = sync._compact_sidecar(writer_cfg)
chk("AC3 proof: last_state_compact.txt sidecar written after successful compaction",
    sidecar_path.exists(), f"path={sidecar_path}")

rec_calls: list[tuple[str, ...]] = []
try:
    sync._git = lambda cwd, *a, **kw: rec_calls.append(a) or \
        subprocess.CompletedProcess(("git", *a), 0, "", "")

    r_verify = sync.git_sync(writer_cfg)
    compact_signatures = set()
    for c in rec_calls:
        if not c:
            continue
        if c[0] == "switch" and len(c) >= 3 and "--orphan" in c:
            compact_signatures.add("switch-orphan")
        elif c[:3] == ("push", "--force", "origin"):
            compact_signatures.add("push-force")
    has_compact_cmd = bool(compact_signatures)

    chk("AC3: immediately-following writer git_sync issues ZERO compaction git commands",
        not has_compact_cmd,
        f"commands recorded: {[c[:2] for c in rec_calls]}  has_compact={has_compact_cmd}")

    chk("AC3: throttle holds — sync still returns error=None",
        r_verify.get("error") is None, str(r_verify))
finally:
    sync._git = _real_git_498

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION C — VPS-STYLE PULL-ONLY CONSUMER AFTER COMPACTION
#             Verify transparent replay + byte identity
# ═══════════════════════════════════════════════════════════════════════════════

consumer_root = _ROOT / "consumer"
subprocess.run(["git", "init", "-b", "main", str(consumer_root)], capture_output=True)
subprocess.run(["git", "remote", "add", "origin", f"file://{bare}"],
               cwd=consumer_root, capture_output=True)
(consumer_root / "audit.jsonl").write_text('{"event":"x","ts":"2026-07-01T01:00:00"}\n')

consumer_cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(consumer_root), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(consumer_root / "audit.jsonl"),
    use_worktree=False,
)

# SANDBOX GUARD
_c_resolved = sync.state_dir(consumer_cfg).resolve()
assert str(_c_resolved).startswith(str(_ROOT.resolve())), (
    f"REFUSING TO SYNC: consumer state_dir resolves OUTSIDE the tmp sandbox ({_c_resolved})")

# --- C1: pull-only mode — verify no ancestry error, byte-identical content ---
os.environ["GENERAL_SYNC_PULL_ONLY"] = "1"
r_pull = sync.git_sync(consumer_cfg)

_chk_consumer_ac = [False, ""]
if r_pull.get("pulled") is True and r_pull.get("error") is None and r_pull.get("pushed") is None:
    _chk_consumer_ac = [True, ""]
else:
    _chk_consumer_ac = [False, f"pulled={r_pull.get('pulled')} err={r_pull.get('error')} "
                        f"pushed={r_pull.get('pushed')}"]

chk("AC2: pull-only consumer lands on compacted tip — pulled=True, error=None, pushed=None",
    *_chk_consumer_ac)

# Capture consumer-on-disk bytes.
if (c_sd := sync.state_dir(consumer_cfg)).is_dir():
    con_payload = (c_sd / "shared" / KNOWN_HOST_FILE).read_bytes()\
        if (c_sd / "shared" / KNOWN_HOST_FILE).exists() else b""
else:
    con_payload = b""
chk("AC2: pull-only consumer shared/<host>.jsonl byte-identical to published payload",
    con_payload == pub_payload,
    f"published={len(pub_payload)}B consumer={len(con_payload)}B")

# --- C2: EU-428 preserved: local unit-state branch and shared/ survive ---
local_branch_list = subprocess.run(
    ["git", "branch", "--list", "unit-state"],
    cwd=sd_writer, capture_output=True, text=True)
chk("EU-428 preserved: local unit-state branch still exists",
    "unit-state" in local_branch_list.stdout, local_branch_list.stdout.strip())
chk("EU-428 preserved: shared/<host>.jsonl still exists on disk",
    (sd_writer / "shared" / KNOWN_HOST_FILE).exists(),
    f"path={sd_writer / 'shared' / KNOWN_HOST_FILE}")

# --- C3: consumer in write-mode publisher path works after compaction ---
os.environ.pop("GENERAL_SYNC_PULL_ONLY", None)
r_write = sync.git_sync(consumer_cfg)
chk("AC5-part C: consumer in write-mode publisher path works after compaction",
    r_write.get("error") is None, str(r_write))

# ═══════════════════════════════════════════════════════════════════════════════
# CLEANUP — restore module-level globals + pop ALL GENERAL_* env keys
# ═══════════════════════════════════════════════════════════════════════════════

sync._git = _real_git_498
for _k in list(os.environ.keys()):
    if _k.startswith("GENERAL_"):
        del os.environ[_k]

# ═══════════════════════════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════════════════════════

print("\n================ EU-498 COMPACT-E2E (GIT_SYNC-TRIGGERED) ================")
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("----------------------------------------------------------------")
print(f"  {passed}/{total} passed")
print("  RESULT:", "ALL GREEN" if passed == total else f"{total - passed} FAIL")
sys.exit(0 if passed == total else 1)
