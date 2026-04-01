# claude-statusline

> Your Claude Code cockpit. Cost, rate limits, context, git, tokens — all at a glance.

```
  Opus 4.6 │ personal │ myproject (main*↑2)[PR✓] │ $1.23 ($4.50/d) @$3.20/h │ ⏱ 12:34 ctx:72%
  context ●●●●●●●○○○  72%
  current ●●●○○○○○○○  27% 2:14pm →full ~45m
  weekly  ●●●●●●○○○○  58% apr 4, 10:00am
  extra   ●●●○○○○○○○  32% $63.53/$200.00
  budget  ●●●●○○○○○○  45% $4.50/$10
  tokens  ●●○○○○○○○○  28% 28.3M/100M +8k (+71k/d)
```

---

## Install (pick one)

```bash
# One-liner
curl -fsSL https://raw.githubusercontent.com/AndrewTKent/claude-statusline/main/install.sh | bash

# npm
npx @andrewkent/claude-statusline install

# Manual
cp bin/statusline.sh ~/.claude/statusline.sh && chmod +x ~/.claude/statusline.sh
```

Then add to `~/.claude/settings.json` (the installer does this automatically):

```json
"statusLine": { "type": "command", "command": "~/.claude/statusline.sh" }
```

Restart Claude Code. That's it.

**Requires:** `jq` (`brew install jq`), Claude Code (logged in). Optional: `gh` for PR badges.

---

## What you get

| Signal | Where | What it tells you |
|--------|-------|-------------------|
| Model + effort | Line 1 | Which Claude, what reasoning level |
| Account | Line 1 | Work vs personal (configurable) |
| Git branch | Line 1 | Branch, dirty `*`, ahead `↑` / behind `↓`, PR status `[PR✓]` `[PR✗]` `[draft]` |
| Cost | Line 1 | Session `$`, daily aggregate, burn rate `@$/h` |
| Timer | Line 1 | Session wall-clock |
| Context badge | Line 1 | Appears at 70%+ so you see it before it's a problem |
| Context bar | Line 2 | Full 10-dot bar + percentage |
| 5-hour rate limit | Line 3 | Bar + `%` + reset time + **burn-down projection** (`→full ~38m`) |
| Weekly rate limit | Line 4 | Bar + `%` + reset date |
| Extra credits | Line 5 | Used / limit when enabled |
| Daily budget | Line 6 | Progress toward your configured `$DAILY_BUDGET` |
| Token tracker | Line 7 | Cumulative tokens toward goal + session & daily deltas |
| macOS notifications | Background | Alerts at rate limit 80/90/95%, context 80/95%, budget 90/100% |

---

## 5 Render Formats

Set `FORMAT=` in `~/.claude/statusline.conf` or `STATUSLINE_FORMAT=` env var.

### `default` — Full dashboard

```
  Opus 4.6 │ work │ voice-agent (EDGE-420*↑1)[PR✓] │ $2.14 ($8.90/d) @$5.30/h │ ⏱ 24:12 ctx:55%
  context ●●●●●○○○○○  55%
  current ●●●●○○○○○○  42% 3:45pm →full ~52m
  weekly  ●●●●●●●○○○  71% apr 4, 10:00am
  tokens  ●●●○○○○○○○  34% 34.1M/100M +120k (+890k/d)
```

### `sigil` — Single dense line

```
  ◈ Opus 4.6 · $2.14 ($8.90/d) · ●●●●●○○○○○ 55% · ⎇ EDGE-420✦↑1[PR✓] · 42%⏱24:12 · 71%w
```

Everything on one line. Width-adaptive — drops segments as terminal narrows.

### `sparkline` — Default + trend history

```
  ...same as default...
  trend   cost▁▂▃▅▃▂▁▄▆█  rate▁▃▅▇█▇▅▃▂▁
```

Inline `▁▂▃▄▅▆▇█` mini-charts showing cost and rate limit trends across your last 15 sessions.

### `rprompt` — Zsh right-prompt

Writes to `~/.claude/rprompt.txt` with zsh prompt escapes. Add to `.zshrc`:

```zsh
_claude_rprompt() {
  local f=~/.claude/rprompt.txt
  [[ -f "$f" ]] || return
  local age=$(( $(date +%s) - $(stat -f %m "$f") ))
  (( age > 300 )) && { RPROMPT=""; return }
  RPROMPT="$(cat "$f")"
}
autoload -Uz add-zsh-hook
add-zsh-hook precmd _claude_rprompt
```

Claude metrics in your shell prompt gutter. Zero vertical space.

### `iterm2` — Native terminal status bar

Pushes data to iTerm2 via `OSC 1337;SetUserVar` or sets Kitty window title. Auto-detects your terminal.

iTerm2 setup: Preferences → Profiles → Session → Status Bar → add "Interpolated String" components:
- `\(user.claude_model)` `\(user.claude_cost)` `\(user.claude_ctx)` `\(user.claude_git)` `\(user.claude_rate)` `\(user.claude_timer)`

---

## Configure

`~/.claude/statusline.conf`:

```bash
# Cost tracking
HOURLY_RATE=150            # Enables billable amount display
DAILY_BUDGET=20            # Enables budget progress bar

# Render format
FORMAT=default             # default | sigil | sparkline | rprompt | iterm2

# Account labels (map emails to short names)
ACCOUNT_LABELS="work:*@company.com personal:me@gmail.com"
```

---

## macOS Native Apps

Beyond the terminal, three companion integrations:

### Menu Bar App (SwiftUI)

Color-coded icon (green/yellow/red) in the macOS menu bar. Click for a popover dashboard.

```bash
cd macos/ClaudeMenuBar && ./build.sh     # Builds with swiftc, no Xcode needed
./install.sh                              # Copies to ~/Applications, auto-starts at login
```

### Raycast Extension

Search "Claude Status" or pin to menu bar. TypeScript, reads the same JSON files.

```
macos/claude-raycast/                     # Ready when Raycast is installed
```

### Widget Bridge

Consolidates all status data into `~/.claude/widget-snapshot.json` with 24hr cost sparkline. Foundation for WidgetKit desktop widgets.

```bash
swift macos/claude-widget/Bridge/claude-widget-bridge.swift
```

---

## How it works

Claude Code pipes JSON into the script on every tool call. The script:

1. Parses model, cost, context, tokens, session metadata (single `jq` call)
2. Updates daily cost/token ledgers (`~/.claude/daily-cost.json`)
3. Fetches rate limits from Anthropic's OAuth API — **background, non-blocking** (cached 60s)
4. Checks notification thresholds (fires once per crossing, deduped)
5. Renders in your chosen format

OAuth resolves automatically: `$CLAUDE_CODE_OAUTH_TOKEN` → macOS Keychain → `~/.claude/.credentials.json`.

### Performance

- Network calls never block rendering (fire-and-forget background subshell)
- Lock file prevents concurrent refresh races
- Git dirty check uses `git diff-index` (faster than `git status --porcelain`)
- Rate limit data cached 60s, profile cached 5min
- Ledger updates are atomic (mktemp + mv)

---

## Uninstall

```bash
# curl install
curl -fsSL https://raw.githubusercontent.com/AndrewTKent/claude-statusline/main/uninstall.sh | bash

# npm
npx @andrewkent/claude-statusline uninstall

# Manual: remove ~/.claude/statusline.sh and the "statusLine" key from settings.json
```

---

## License

MIT
