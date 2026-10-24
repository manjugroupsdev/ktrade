#!/bin/sh
# Run ktrade as an always-on macOS login service (launchd). It restarts if it crashes and keeps the Mac awake
# while it runs. The Mac still sleeps on lid close; keep it plugged in with the lid open for 24/7 operation.
#   sh scripts/install_macos.sh            install / reinstall and start
#   sh scripts/install_macos.sh uninstall  stop and remove
set -e
cd "$(dirname "$0")/.."
REPO="$(pwd)"
LABEL="io.ktrade.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "$1" = "uninstall" ]; then rm -f "$PLIST"; echo "ktrade service removed"; exit 0; fi

[ -x "$REPO/.venv/bin/python" ] || { echo "Create the virtualenv first (see README: Quick start)"; exit 1; }
CLAUDE="$(command -v claude || true)"
[ -n "$CLAUDE" ] || echo "warning: 'claude' not found on PATH; the AI book will stay flat until KTRADE_CLAUDE_BIN is set"
mkdir -p "$REPO/logs" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-is</string>
    <string>$REPO/.venv/bin/python</string><string>-m</string><string>ktrade</string><string>run</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$(dirname "${CLAUDE:-/usr/bin/true}"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$REPO/logs/ktrade.log</string>
  <key>StandardErrorPath</key><string>$REPO/logs/ktrade.log</string>
</dict></plist>
EOF

launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "ktrade is running. Dashboard: http://127.0.0.1:${KTRADE_PORT:-8060}  Logs: $REPO/logs/ktrade.log"
