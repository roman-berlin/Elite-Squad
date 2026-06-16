#!/bin/zsh
# The General — desktop launcher.
# Opens a Terminal in the repo, runs a preflight health check (doctor), then
# drops you into an interactive shell ready to drive the unit.
#
# Canonical copy lives here in the repo. Install/refresh the Desktop shortcut with:
#   cp ~/Projects/General/scripts/General.command ~/Desktop/General.command
#   chmod +x ~/Desktop/General.command

cd "$HOME/Projects/General" || { echo "General repo not found at ~/Projects/General"; exec zsh -i; }

# Match what ./general does: activate venv, load secrets from .env
[[ -f .venv/bin/activate ]] && source .venv/bin/activate
[[ -f .env ]] && { set -a; source .env; set +a; }

echo "★ The General — preflight"
echo
./general doctor
echo
echo "────────────────────────────────────────────────────────────"
echo "Ready.   general task <app> --spec-file <f> --title <t>"
echo "         general drain   |   general serve   |   general standup"
echo "         add --live to actually push/merge (default is dry-run)"
echo "────────────────────────────────────────────────────────────"

# Stay in an interactive shell so your global `general` function is available
exec zsh -i
