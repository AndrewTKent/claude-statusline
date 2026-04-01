# Claude Code macOS Widget

A WidgetKit-based macOS widget showing Claude Code metrics on your desktop/lock screen.

## Architecture

Two components:

1. **Bridge CLI** (`Bridge/claude-widget-bridge.swift`): Reads the statusline JSON files, writes a consolidated snapshot to `~/.claude/widget-snapshot.json`. Run on a 30s timer via launchd.

2. **Widget Extension** (WidgetKit): Reads the snapshot and renders lock screen / desktop widgets. **Requires an Xcode project** with App Group entitlements and code signing.

## Quick Start (Bridge Only)

The bridge works standalone — any tool can read `widget-snapshot.json`:

```bash
swift Bridge/claude-widget-bridge.swift
cat ~/.claude/widget-snapshot.json
```

### Auto-run with launchd

```bash
cat > ~/Library/LaunchAgents/com.andrewkent.claude-widget-bridge.plist << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.andrewkent.claude-widget-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/swift</string>
        <string>$(pwd)/Bridge/claude-widget-bridge.swift</string>
    </array>
    <key>StartInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>/tmp/claude/widget-bridge.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/claude/widget-bridge.log</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.andrewkent.claude-widget-bridge.plist
```

## Full Widget (Future)

The WidgetKit extension requires:
- Xcode project with App Extension target
- App Group entitlement (`group.com.andrewkent.claude-widget`)
- Code signing (Apple Developer identity)
- The bridge writes to the App Group container instead of `~/.claude/`

This is left as a future enhancement requiring interactive Xcode setup.

## Snapshot Format

`~/.claude/widget-snapshot.json`:

```json
{
  "timestamp": "2026-04-01T15:30:00Z",
  "model": "Opus 4.6",
  "session_cost": 1.23,
  "daily_cost": 4.56,
  "context_pct": 42,
  "five_hour_pct": 27,
  "seven_day_pct": 58,
  "extra_pct": 32,
  "extra_used": 63.53,
  "extra_limit": 200.00,
  "session_count": 3,
  "cost_history": [
    {"hour": "2026-04-01T08:00:00", "cost": 0.50},
    {"hour": "2026-04-01T09:00:00", "cost": 1.20},
    ...
  ]
}
```
