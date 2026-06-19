#!/bin/bash
# Elite Unit — server self-update (auto-deploy `main`).
#
# When origin/main moves, pull it and restart the service — exactly the manual deploy ritual
# (`git reset --hard origin/main` + restart), but automatic, so a `git push origin main` from the Mac
# reaches this 24/7 box on its own. Idempotent: it no-ops unless main actually moved, so running it
# every 15 min from cron costs one `git fetch` and nothing else until you promote.
#
# Needs passwordless sudo for `systemctl restart general.service` (Oracle's `ubuntu` user has it by
# default; if the restart ever prompts for a password, add the sudoers line from VPS_DEPLOYMENT.md).
cd "$HOME/General" || exit 1
mkdir -p council
git fetch origin main --quiet 2>>council/cron.log || exit 0
if [ "$(git rev-parse HEAD 2>/dev/null)" = "$(git rev-parse origin/main 2>/dev/null)" ]; then
  exit 0   # already current — nothing to deploy
fi
echo "----- self-update $(date): $(git rev-parse --short HEAD) -> $(git rev-parse --short origin/main) -----" >> council/cron.log
git checkout main >> council/cron.log 2>&1
git reset --hard origin/main >> council/cron.log 2>&1
sudo systemctl restart general.service >> council/cron.log 2>&1 \
  && echo "self-update: pulled main + restarted general.service" >> council/cron.log
