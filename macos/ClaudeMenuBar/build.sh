#!/bin/bash
# Build ClaudeMenuBar.app without Xcode
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="ClaudeMenuBar"
BUILD_DIR="$SCRIPT_DIR/.build"
APP_BUNDLE="$BUILD_DIR/$APP_NAME.app"

echo "Building $APP_NAME..."

rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources"

# Compile all Swift files together
swiftc \
    -o "$APP_BUNDLE/Contents/MacOS/$APP_NAME" \
    -framework Cocoa \
    -framework SwiftUI \
    -framework Combine \
    -parse-as-library \
    -swift-version 5 \
    "$SCRIPT_DIR/Sources/"*.swift

# Copy Info.plist
cp "$SCRIPT_DIR/Resources/Info.plist" "$APP_BUNDLE/Contents/"

echo "Built: $APP_BUNDLE"
echo ""
echo "To run:  open $APP_BUNDLE"
echo "To install: cp -r $APP_BUNDLE ~/Applications/"
