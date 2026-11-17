#!/bin/bash
# Installs a macOS LaunchAgent that records a snapshot every weekday at 17:30 and 21:00
# (the second run catches late upstream refreshes; same-session runs just overwrite).
# If the Mac is asleep at that time, launchd runs the job when it wakes.
# Uninstall: launchctl unload ~/Library/LaunchAgents/com.dailymarket.snapshot.plist && rm that file
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.dailymarket.snapshot.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$DIR/data"
intervals=""
for wd in 1 2 3 4 5; do
  for t in "17 30" "21 0"; do
    set -- $t
    intervals+="<dict><key>Weekday</key><integer>$wd</integer><key>Hour</key><integer>$1</integer><key>Minute</key><integer>$2</integer></dict>"
  done
done
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.dailymarket.snapshot</string>
  <key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$DIR/market.py</string><string>snapshot</string></array>
  <key>StartCalendarInterval</key><array>$intervals</array>
  <key>StandardOutPath</key><string>$DIR/data/snapshot.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/snapshot.log</string>
</dict></plist>
PL
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed: weekdays 17:30 & 21:00 → $DIR/data/snapshot.log"
