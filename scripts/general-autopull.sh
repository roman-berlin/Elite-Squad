#!/bin/bash
# EU-335 autopull — keeps the Mac checkout fast-forwarded to origin/dev so the launchd cockpit
# (KeepAlive) always respawns onto current code. Installed copy: ~/bin/general-autopull.sh,
# driven by ~/Library/LaunchAgents/com.roman.general-autopull.plist (StartInterval). This
# versioned copy is the source of truth (2026-07-19 stabilization — it was previously an
# unmanaged single point of failure); after editing, `cp scripts/general-autopull.sh ~/bin/`.
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
cd "$HOME/Projects/General" || exit 0
git fetch -q origin || exit 0
# 2026-07-15 (EU-335): the old guard [ -z "$(git status --porcelain)" ] blocked the ff forever,
# because the unit's own changelog writer + untracked artifacts keep this checkout dirty.
# --autostash stashes/restores local modifications around the fast-forward; untracked files
# never block an ff-merge in the first place.
git merge --ff-only --autostash -q origin/dev
