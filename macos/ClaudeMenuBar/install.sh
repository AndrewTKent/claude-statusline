#!/bin/bash
# Install ClaudeMenuBar: build, copy to ~/Applications, set up auto-start
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="ClaudeMenuBar"
APP_BUNDLE="$SCRIPT_DIR/.build/$APP_NAME.app"
INSTALL_DIR="$HOME/Applications"
PLIST_NAME="com.andrewkent.claude-menubar"
PLIST_DIR="$HOME/Library/LaunchAgents"

# Build
bash "$SCRIPT_DIR/build.sh"

# Install
mkdir -p "$INSTALL_DIR"
rm -rf "$INSTALL_DIR/$APP_NAME.app"
cp -r "$APP_BUNDLE" "$INSTALL_DIR/"
echo "Installed to $INSTALL_DIR/$APP_NAME.app"

# Create LaunchAgent for auto-start at login
mkdir -p "$PLIST_DIR"
cat > "$PLIST_DIR/$PLIST_NAME.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>open</string>
        <string>-a</string>
        <string>$INSTALL_DIR/$APP_NAME.app</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF

launchctl load "$PLIST_DIR/$PLIST_NAME.plist" 2>/dev/null || true
echo "Auto-start configured"

# Launch now
open "$INSTALL_DIR/$APP_NAME.app"
echo "Launched $APP_NAME"
