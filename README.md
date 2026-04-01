<p align="center">
  <strong>claude-statusline</strong><br>
  <em>The cockpit for Claude Code.</em>
</p>

<p align="center">
  <a href="#install">Install</a> &middot;
  <a href="#formats">Formats</a> &middot;
  <a href="#configure">Configure</a> &middot;
  <a href="#macos-native">macOS Native</a> &middot;
  <a href="#how-it-works">How It Works</a>
</p>

---

```
  Opus 4.6 │ work │ voice-agent (EDGE-420*↑1)[PR✓] │ $2.14 ($8.90/d) @$5.30/h │ ⏱ 24:12 ctx:72%
  context ●●●●●●●○○○  72%
  current ●●●●○○○○○○  42% 3:45pm →full ~52m
  weekly  ●●●●●●●○○○  71% apr 4, 10:00am
  extra   ●●●○○○○○○○  32% $63.53/$200.00
  budget  ●●●●○○○○○○  45% $4.50/$10
  tokens  ●●●○○○○○○○  34% 34.1M/100M +120k (+890k/d)
```

Everything you need to not get rate-limited, blow your budget, or lose context mid-task. One bash script, zero dependencies beyond `jq`.

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/AndrewTKent/claude-statusline/main/install.sh | bash
```

Or via npm:
```bash
npx @andrewkent/claude-statusline install
```

Or manually — copy the script, add one key to settings:
```bash
cp bin/statusline.sh ~/.claude/statusline.sh && chmod +x ~/.claude/statusline.sh
```
```json
{ "statusLine": { "type": "command", "command": "~/.claude/statusline.sh" } }
```

Restart Claude Code. Done.

**Requires:** [`jq`](https://jqlang.github.io/jq/) &middot; Claude Code (logged in) &middot; Optional: [`gh`](https://cli.github.com/) for PR badges

---

## What You See

### Line 1 — The headline

```
Opus 4.6 │ work │ voice-agent (EDGE-420*↑1)[PR✓] │ $2.14 ($8.90/d) @$5.30/h │ ⏱ 24:12 ctx:72%
```

| Segment | What it means |
|---------|---------------|
| `Opus 4.6` | Model + effort level (`.high`, `.low`, etc.) |
| `work` | Account label — auto-detected from OAuth, configurable |
| `EDGE-420*↑1` | Git branch, `*` = dirty, `↑1` = 1 commit ahead |
| `[PR✓]` | PR status: `[PR✓]` approved, `[PR✗]` checks failing, `[draft]`, `[PR△]` changes requested, `[PR⋯]` pending |
| `$2.14` | Session cost |
| `($8.90/d)` | Daily aggregate across all sessions |
| `@$5.30/h` | Burn rate |
| `⏱ 24:12` | Session wall-clock |
| `ctx:72%` | Context badge — only appears at 70%+ as a warning |

### Lines 2–7 — The gauges

| Line | What it tracks | Why it matters |
|------|---------------|----------------|
| `context` | Context window fill | At 95% your session is about to die |
| `current` | 5-hour rate limit + reset time | The one that actually blocks you |
| `→full ~52m` | Burn-down projection | Minutes until you hit the wall at current pace |
| `weekly` | 7-day rate limit + reset date | The slow squeeze |
| `extra` | Extra credits spent / limit | Your overflow budget |
| `budget` | Daily spend vs your cap | Only shows if `DAILY_BUDGET` is set |
| `tokens` | Cumulative tokens + session/daily deltas | Track your burn across sessions |

### Background — Notifications

macOS Notification Center alerts fire automatically (once per threshold, deduped):
- **Rate limit** at 80%, 90%, 95%
- **Context** at 80%, 95%
- **Budget** at 90%, 100%

### Automatic account detection

When you `/login` to switch accounts, the status bar detects the credential change and immediately refreshes — rate limits, account label, and extra credits update across all open sessions.

---

## Formats

Five render modes. Set `FORMAT=` in `~/.claude/statusline.conf` or `STATUSLINE_FORMAT=` env var.

### `default` — Multi-line dashboard (shown above)

The full cockpit. Adapts to terminal width (>=100 cols wide, <100 compact).

### `sigil` — Single dense line

```
◈ Opus 4.6 · $2.14 ($8.90/d) · ●●●●●○○○○○ 55% · ⎇ EDGE-420✦↑1[PR✓] · 42%⏱24:12 · 71%w
```

Everything on one line. Width-adaptive — drops segments as the terminal narrows. Good for tmux status bars or small terminals.

### `sparkline` — Default + trend history

```
  ...default output...
  trend   cost▁▂▃▅▃▂▁▄▆█  rate▁▃▅▇█▇▅▃▂▁
```

Appends inline `▁▂▃▄▅▆▇█` mini-charts showing cost and rate limit trends across your last 15 sessions. See if you're burning hotter today than yesterday.

### `rprompt` — Zsh right-prompt

Writes zsh-formatted status to `~/.claude/rprompt.txt`. Add to `.zshrc`:

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

Claude metrics in your shell prompt gutter. Zero vertical space. Auto-hides after 5 minutes of inactivity.

### `iterm2` — Native terminal status bar

Pushes structured data to iTerm2 via `OSC 1337;SetUserVar` or sets Kitty window title via `OSC 2`. Auto-detects your terminal.

**iTerm2 setup:** Preferences → Profiles → Session → Status Bar → add "Interpolated String" components:

`\(user.claude_model)` &middot; `\(user.claude_cost)` &middot; `\(user.claude_ctx)` &middot; `\(user.claude_git)` &middot; `\(user.claude_rate)` &middot; `\(user.claude_timer)`

---

## Configure

Create `~/.claude/statusline.conf`:

```bash
# Cost tracking
HOURLY_RATE=150            # Shows billable amount based on session time
DAILY_BUDGET=20            # Enables budget progress bar + notifications at 90%/100%

# Render format
FORMAT=default             # default | sigil | sparkline | rprompt | iterm2

# Account labels — map OAuth emails to short names
ACCOUNT_LABELS="work:*@company.com personal:me@gmail.com"
```

All settings are optional. The script works with no config file at all.

---

## macOS Native

Three companion apps that read the same data files — no extra API calls.

### Menu Bar App

<img width="24" height="24" alt="green dot" src="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'><circle cx='12' cy='12' r='8' fill='%2327ae60'/></svg>"> Color-coded icon: green = ok, yellow = rate limit 70%+, red = 90%+ or context critical.

Click for a SwiftUI popover with full dashboard.

```bash
cd macos/ClaudeMenuBar
./build.sh      # Compiles with swiftc — no Xcode needed
./install.sh    # Copies to ~/Applications, auto-starts at login
```

### Raycast Extension

Search "Claude Status" for a full metric list, or pin to menu bar for always-visible `$12.34 | 5hr: 45%`.

```
macos/claude-raycast/    # TypeScript — ready when Raycast is installed
```

### Widget Bridge

Consolidates all status data into `~/.claude/widget-snapshot.json` with a 24-hour cost sparkline. Foundation for WidgetKit desktop/lock screen widgets.

```bash
swift macos/claude-widget/Bridge/claude-widget-bridge.swift
```

Run on a 30s launchd timer for auto-refresh. See [`macos/claude-widget/README.md`](macos/claude-widget/README.md) for setup.

---

## How It Works

Claude Code pipes a JSON status blob into the script via stdin on every tool call. The script:

1. **Parses** model, cost, context, tokens, session metadata (single `jq` call)
2. **Updates** daily cost/token ledgers in `~/.claude/`
3. **Fetches** rate limits from Anthropic's OAuth API — background, non-blocking
4. **Detects** account switches (credential file mtime change → cache invalidation)
5. **Checks** notification thresholds (fires once per crossing, deduped)
6. **Renders** in your chosen format

### Architecture

```
Claude Code                    statusline.sh
    │                              │
    ├─ stdin JSON ────────────────►│ parse (jq)
    │                              │
    │                              ├─► update daily-cost.json
    │                              ├─► update token-challenge.json
    │                              ├─► background: fetch /api/oauth/usage (cached 60s)
    │                              ├─► background: fetch /api/oauth/profile (cached 5min)
    │                              ├─► check notification thresholds
    │                              │
    │  stdout ANSI ◄──────────────├─► render (default|sigil|sparkline|rprompt|iterm2)
    │                              │
    ├─ /tmp/claude/*.json ────────►│ macOS apps read these
```

### Performance

| Concern | How it's handled |
|---------|-----------------|
| Network latency | Background subshell, never blocks render |
| Concurrent sessions | Lock file with stale-PID detection (auto-cleanup at 30s) |
| Git dirty check | `git diff-index --quiet HEAD` (faster than `git status`) |
| PR status | `gh pr view` cached 90s, background-refreshed |
| Ledger writes | Atomic (mktemp + mv) |
| Account switch | Credential mtime tracking, instant cache invalidation |

### Files

| File | Purpose | Lifetime |
|------|---------|----------|
| `~/.claude/statusline.sh` | The script (or symlink) | Permanent |
| `~/.claude/statusline.conf` | Config | Permanent |
| `~/.claude/daily-cost.json` | Daily cost ledger | Resets daily |
| `~/.claude/token-challenge.json` | Cumulative token tracker | Persistent |
| `~/.claude/daily-tokens.json` | Daily token tracker | Resets daily |
| `~/.claude/session-history.jsonl` | Sparkline history | Rolling 100 entries |
| `~/.claude/rprompt.txt` | Zsh RPROMPT (rprompt format) | Updated each render |
| `/tmp/claude/statusline-usage-cache.json` | Rate limit API cache | 60s TTL |
| `/tmp/claude/statusline-profile-cache.json` | Profile API cache | 5min TTL |
| `/tmp/claude/statusline-raw.json` | Raw status for macOS apps | Updated each render |
| `/tmp/claude/statusline-notif-state.json` | Notification dedup state | Per-threshold |
| `/tmp/claude/statusline-refresh.lock` | Background refresh lock | Transient |
| `/tmp/claude/statusline-creds-mtime` | Credential change detection | Persistent |

---

## Uninstall

```bash
# curl install
curl -fsSL https://raw.githubusercontent.com/AndrewTKent/claude-statusline/main/uninstall.sh | bash

# npm
npx @andrewkent/claude-statusline uninstall

# Manual
rm ~/.claude/statusline.sh
# Remove "statusLine" key from ~/.claude/settings.json
```

---

## License

MIT
