# claude-statusline

A rich, multi-line status bar for [Claude Code](https://claude.ai/code) that shows everything you need at a glance.

```
  Opus 4.6 │ personal │ myproject (main*↑2)[PR✓] │ $1.23 ($4.50/d) @$3.20/h │ ⏱ 12:34
  context ●●●●●●●○○○  72%
  current ●●●○○○○○○○  27% 2:14pm →full ~45m
  weekly  ●●●●●●○○○○  58% apr 4, 10:00am
  extra   ●●●○○○○○○○  32% $63.53/$200.00
  budget  ●●●●○○○○○○  45% $4.50/$10
  tokens  ●●○○○○○○○○  28% 28.3M/100M +8k (+71k/d)
```

## What it shows

- **Model + effort level** — which Claude model and reasoning effort
- **Account** — work vs personal (configurable email mapping)
- **Git** — branch, dirty state, ahead/behind, PR status (`[PR✓]`/`[PR✗]`/`[draft]`)
- **Cost** — session cost, daily aggregate, burn rate ($/hr)
- **Timer** — session wall-clock time
- **Context window** — bar + percentage, badge on line 1 at 70%+
- **Rate limits** — 5-hour and weekly bars with reset times
- **Burn-down projection** — "→full ~38m" when approaching rate limit
- **Extra credits** — usage and monthly limit
- **Daily budget** — configurable spending ceiling with progress bar
- **Token tracking** — cumulative tokens toward a goal with daily deltas
- **macOS notifications** — alerts at 80%/90%/95% rate limit, 80%/95% context, budget thresholds

## Install

### One-liner

```bash
curl -fsSL https://raw.githubusercontent.com/AndrewTKent/claude-statusline/main/install.sh | bash
```

### Homebrew

```bash
brew install andrewtkent/tap/claude-statusline
```

### npm

```bash
npx claude-statusline install
```

### Manual

```bash
cp bin/statusline.sh ~/.claude/statusline.sh
chmod +x ~/.claude/statusline.sh
```

Then add to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "~/.claude/statusline.sh"
  }
}
```

## Configure

Edit `~/.claude/statusline.conf`:

```bash
HOURLY_RATE=150        # Billing rate (enables billable amount)
DAILY_BUDGET=20        # Daily cost ceiling (enables budget bar)
FORMAT=default         # Render format (see below)
ACCOUNT_LABELS="work:*@company.com personal:me@gmail.com"
```

## Formats

Set `FORMAT=` in `statusline.conf` or `STATUSLINE_FORMAT=` env var.

| Format | Description |
|--------|-------------|
| `default` | Multi-line with full detail (shown above) |
| `sigil` | Single dense line: `◈ opus · $0.14 · ●●●●●○○ 42% · ⎇ main✦ · 27%⏱3:02` |
| `sparkline` | Default + inline mini-charts of cost/rate history |
| `rprompt` | Writes to `~/.claude/rprompt.txt` for zsh RPROMPT integration |
| `iterm2` | Pushes data to iTerm2/Kitty native status bar via escape sequences |

### RPROMPT setup

Add to `~/.zshrc`:

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

### iTerm2 setup

Set `FORMAT=iterm2`. The script pushes user variables (`claude_model`, `claude_cost`, `claude_ctx`, `claude_git`, `claude_rate`, `claude_timer`).

In iTerm2: Preferences → Profiles → Session → Status Bar → add "Interpolated String" components referencing `\(user.claude_model)`, `\(user.claude_cost)`, etc.

## Requirements

- **jq** — `brew install jq` or `sudo apt install jq`
- **bash** — ships with macOS/Linux
- **Claude Code** — logged in
- **gh** (optional) — for PR status badges

## Uninstall

```bash
# If installed via curl:
curl -fsSL https://raw.githubusercontent.com/AndrewTKent/claude-statusline/main/uninstall.sh | bash

# If installed via Homebrew:
brew uninstall claude-statusline

# If installed via npm:
npx claude-statusline uninstall
```

## How it works

Claude Code pipes JSON status data into the script via stdin on every tool call. The script:

1. Parses model, cost, context, tokens, and session metadata
2. Updates daily cost/token ledgers in `~/.claude/`
3. Fetches rate limits from Anthropic's OAuth API (cached 60s, background non-blocking)
4. Renders the status bar in your chosen format

OAuth tokens are resolved automatically from macOS Keychain or `~/.claude/.credentials.json`.

## License

MIT
