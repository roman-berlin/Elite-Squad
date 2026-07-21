#!/bin/bash
# EU-408 — nightly private backup of .env + config.yaml (the two unversioned single points of loss).
#
# WHY: .env (Jira/Telegram/GLM tokens) and config.yaml are gitignored by design, so they have NO
# version-history safety net. Disk failure, an errant `rm -rf`, or a bad agent edit destroys them,
# and the unit is down until every credential is re-issued and the ~150-line config is rebuilt from
# memory (a stale .bak may not match the current schema). This script copies BOTH files to a PRIVATE
# location OUTSIDE the repo (so a repo wipe does not take the backup with it), keeps a bounded
# history of every change, and is safe to run from a timer. Production audit 2026-07-21, ops P2.
#
# WHAT IT DOES, each run:
#   - For each of .env and config.yaml present in $GENERAL_DIR: if it differs from the most recent
#     backup, write a new timestamped copy (so the retained history is a real changelog of edits,
#     not 14 identical nightly snapshots); otherwise no-op for that file.
#   - Prune to the newest BACKUP_KEEP copies per file.
#   - <name>.latest always points at the newest copy, for a one-line restore.
#   - Every copy is chmod 600 and the backup dir chmod 700 — these files ARE the secrets.
#
# This script NEVER exits non-zero: it is timer-driven (launchd StartInterval / cron), and a
# non-zero exit under a periodic timer only spams the system log. A missing source file is a
# note, not an abort (a brand-new checkout has no .env yet). errexit is intentionally NOT set.
#
# Install (Mac launchd, nightly):  bash scripts/install-mac-config-backup-daemon.sh
# VPS cron (3:17am daily):         17 3 * * *  /path/to/repo/scripts/backup-config.sh >> ~/general-config-backup.log 2>&1
# One-off (run now):               bash scripts/backup-config.sh
#
# Tunables (env, all optional): GENERAL_DIR GENERAL_CONFIG_BACKUP_DIR BACKUP_KEEP
set -uo pipefail

GENERAL_DIR="${GENERAL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# Default OUTSIDE the repo: a repo wipe (`rm -rf`, a bad re-clone) must not destroy the backup too.
GENERAL_CONFIG_BACKUP_DIR="${GENERAL_CONFIG_BACKUP_DIR:-$HOME/.general-config-backups}"
BACKUP_KEEP="${BACKUP_KEEP:-14}"     # newest N timestamped copies retained per file

mkdir -p "$GENERAL_CONFIG_BACKUP_DIR" 2>/dev/null || true
chmod 700 "$GENERAL_CONFIG_BACKUP_DIR" 2>/dev/null || true

_ts="$(date +%Y%m%d-%H%M%S)"
_copied=0
_skipped=0
_missing=0

for name in .env config.yaml; do
  src="$GENERAL_DIR/$name"
  if [[ ! -f "$src" ]]; then
    echo "backup-config: $name absent in $GENERAL_DIR — nothing to back up (skipped)"
    _missing=$((_missing + 1))
    continue
  fi
  latest="$GENERAL_CONFIG_BACKUP_DIR/$name.latest"
  # Only snapshot when the content actually changed since the last backup — keeps the retained
  # history a faithful changelog of edits and bounds disk instead of piling up identical copies.
  if [[ -L "$latest" || -f "$latest" ]] && cmp -s "$src" "$latest" 2>/dev/null; then
    echo "backup-config: $name unchanged since last backup — no new snapshot"
    _skipped=$((_skipped + 1))
    continue
  fi
  dst="$GENERAL_CONFIG_BACKUP_DIR/$name.$_ts"
  # Same-second re-runs (a manual run overlapping the timer, or a burst of edits) would land on the
  # same timestamp and overwrite — collapsing the history. Disambiguate so every retained snapshot
  # is a distinct file the rotation below can age out correctly.
  _n=0
  while [[ -e "$dst" ]]; do
    _n=$((_n + 1))
    dst="$GENERAL_CONFIG_BACKUP_DIR/$name.${_ts}-${_n}"
  done
  if cp -p "$src" "$dst" 2>/dev/null; then
    chmod 600 "$dst" 2>/dev/null || true
    # Point <name>.latest at the newest copy. -f overwrites a stale symlink; ln -s -f leaves a
    # link even across renames, and falls back to a real copy if symlinks are unavailable.
    ln -s -f "$dst" "$latest" 2>/dev/null || cp -p "$src" "$latest" 2>/dev/null || true
    chmod 600 "$latest" 2>/dev/null || true
    echo "backup-config: snapshotted $name -> $(basename "$dst")"
    _copied=$((_copied + 1))
  else
    echo "backup-config: FAILED to copy $name (permissions? disk full?) — skipped" >&2
    continue
  fi
  # Rotate: keep the newest BACKUP_KEEP timestamped copies of THIS file (exclude the .latest link).
  _kept=0
  # shellcheck disable=SC2012  # ls is fine here: filenames are date-stamped, no spaces/globs
  for old in $(ls -1t "$GENERAL_CONFIG_BACKUP_DIR/$name".* 2>/dev/null | grep -v '\.latest$'); do
    _kept=$((_kept + 1))
    if [[ $_kept -gt $BACKUP_KEEP ]]; then
      rm -f "$old" 2>/dev/null && echo "backup-config: pruned old snapshot $(basename "$old")"
    fi
  done
done

echo "backup-config: done — copied=$_copied unchanged=$_skipped missing=$_missing " \
     "retention=newest $BACKUP_KEEP per file -> $GENERAL_CONFIG_BACKUP_DIR"
exit 0
