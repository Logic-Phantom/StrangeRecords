#!/usr/bin/env bash
# ============================================================
#  기묘한 기록 - macOS launchd 등록 (매일 10:00 run_daily.sh 실행)
#  실행: bash scripts/install_macos_launchd.sh [HH] [MM]
#  해제: launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.strangerecords.daily.plist
# ============================================================
set -euo pipefail
HOUR="${1:-10}"
MINUTE="${2:-0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.strangerecords.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

chmod +x "$ROOT/run_daily.sh"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$ROOT/run_daily.sh</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
  <key>StandardOutPath</key><string>$ROOT/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/launchd.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "등록 완료: 매일 $(printf '%02d:%02d' "$HOUR" "$MINUTE") → $ROOT/run_daily.sh"
echo "지금 테스트: launchctl kickstart gui/$(id -u)/$LABEL"
