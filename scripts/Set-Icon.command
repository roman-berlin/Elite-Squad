#!/bin/zsh
# Build the Elite War Room app icon from AppIcon.png using macOS tools, then apply it.
# Run this once after installing the app (double-click it). Optional — the app works
# with the generic icon too; this just gives it the star emblem.

set -e
APP="$HOME/Projects/General/scripts/Elite War Room.app"
[[ -d "/Applications/Elite War Room.app" ]] && APP="/Applications/Elite War Room.app"
RES="$APP/Contents/Resources"
PNG="$RES/AppIcon.png"
[[ -f "$PNG" ]] || { echo "AppIcon.png not found at: $PNG"; exit 1; }

TMP="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$TMP"
for s in 16 32 128 256 512; do
  sips -z $s $s             "$PNG" --out "$TMP/icon_${s}x${s}.png"     >/dev/null
  sips -z $((s*2)) $((s*2)) "$PNG" --out "$TMP/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$TMP" -o "$RES/AppIcon.icns"
touch "$APP"
killall Dock 2>/dev/null || true
echo "✓ Icon applied to: $APP"
