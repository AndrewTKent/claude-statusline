<h1 align="center">statusline</h1>

<p align="center">
  <em>Terminal status bars and account tooling for AI coding CLIs —<br>cost, context, rate limits, burn-down, and multi-account routing, at a glance.</em>
</p>

<p align="center">
  <a href="https://www.npmjs.com/package/@andrewkent/claude-statusline"><img alt="npm" src="https://img.shields.io/npm/v/@andrewkent/claude-statusline?color=cb3837&logo=npm&label=npm"></a>
  <a href="https://github.com/AndrewTKent/statusline/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/AndrewTKent/statusline/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="platform" src="https://img.shields.io/badge/platform-macOS%20%C2%B7%20Linux-blue">
  <img alt="dependencies" src="https://img.shields.io/badge/deps-jq-lightgrey">
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

<p align="center">
  <a href="#install">Install</a> &middot;
  <a href="#what-you-see">What You See</a> &middot;
  <a href="#formats">Formats</a> &middot;
  <a href="#configure">Configure</a> &middot;
  <a href="#accounts-multi-account-routing">Accounts</a> &middot;
  <a href="#token-scanning-and-redaction">Token Scanning</a> &middot;
  <a href="#macos-native">macOS Native</a> &middot;
  <a href="#how-it-works">How It Works</a>
</p>

---

Four tools, one repo, shared data files:

- **Claude Code statusline** (`bin/statusline.sh`) — the multi-line dashboard below
- **Codex statusline** (`bin/codex-statusline`, `codex-top`) — the same idea for the Codex CLI
- **`accounts`** (`bin/accounts.py`) — multi-account router: headroom board, auto-switching, pin-follows-login
- **Token scanning & redaction** (`bin/scan-tokens*`) — attribute every token, redact before sharing

```
model   Fable 5.max
time    ⏱ 2:29:20
account you@example.com
repo    my-project main (v1.2.0*)
context ●●●●●●●○○○○○○○○ 49%
session ●●●●●●●●●○○○○○○ 60.2%   resets 10:00pm PDT
weekly  ●●●●○○○○○○○○○○○ 31.07%  resets jul 27, 12:00pm PDT
fable   ●●●●○○○○○○○○○○○ 33%
usage   today 5.57M · session 1.16M · lifetime 593.31M
  acct        5h   reset   week   fable   reset
· Work       84%   2h15m    51%     80%      2d
· Work-Max   25%   2h25m    68%    100%      2d
* Uni        60%   3h45m    31%     33%      6d
· Mail        0%       —   100%     87%      2d
· Side        0%       —   100%     16%      2d
· Personal    0%       —   100%      8%     23h
```

Everything you need to not get rate-limited, blow your budget, or lose context mid-task. The Claude statusline is one bash script, zero dependencies beyond `jq`.

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/AndrewTKent/statusline/main/install.sh | bash
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
{ "statusLine": { "type": "command", "command": "~/.claude/statusline.sh", "padding": 0 } }
```

Restart Claude Code. Done.

**Requires:** [`jq`](https://jqlang.github.io/jq/) &middot; Claude Code (logged in) &middot; Optional: [`gh`](https://cli.github.com/) for PR badges

### Codex

**Requires:** Codex CLI &middot; Python 3 &middot; `tmux` &middot; `~/.local/bin` on `PATH`

```bash
./install-codex.sh
codex-statusline
codex-statusline --sandbox read-only --ask-for-approval on-request
```

`codex-statusline` launches Codex in tmux with a fixed bottom pane matching the
multi-line Claude Code status view. It shows the current model, elapsed time,
account, repository, context use, 5-hour and weekly limits, tokens, agents, and
running tools. It binds each footer to the rollout file opened by its owning Codex
process, so concurrent and resumed sessions do not exchange context values. Mouse
wheel scrollback is enabled with a 100,000-line history; tune it with
`CODEX_STATUSLINE_HISTORY_LIMIT`. Mouse-dragging output selects it in tmux and
copies it to the system clipboard on release in OSC 52-capable terminals
(iTerm2, kitty, WezTerm — not stock macOS Terminal.app). When launched inside an existing
tmux pane, that pane keeps the history depth it was created with; the session
`mouse` and window `history-limit` options are restored when the launcher exits.
When launched outside tmux, detaching (prefix d) leaves Codex running — reattach with
`tmux attach -t codex-statusline-<pid>`; the session ends when Codex exits.

The footer refreshes every 3s (`CODEX_STATUSLINE_INTERVAL`) and backs off to a
30s poll once its session has been idle for 10 minutes, exits when the owning
process is gone, and opportunistically truncates the state DB's WAL when it
grows past 128 MB — long-lived footers previously starved SQLite checkpoints
until every Codex query slowed to a crawl.

The launcher defaults to Codex YOLO mode by passing
`--dangerously-bypass-approvals-and-sandbox`. An explicit `-a/--ask-for-approval`,
`-s/--sandbox`, or dangerous-bypass flag replaces that default; profile (`-p`) or
`-c` approval overrides do not. Set
`CODEX_STATUSLINE_MANAGE_APPROVALS=0` to pass no permission default. The launcher also uses
`tui.status_line=[]` so Codex keeps only its compact built-in prompt footer while
the detailed dashboard stays in the fixed pane.
Settings load from `${CODEX_HOME:-~/.codex}/statusline.conf`; non-empty environment
variables override file values, and `CODEX_STATUSLINE_CONFIG` points at a
different file.

`codex-top` is the live fleet view for parent and subagent sessions. Both views
read the newest `~/.codex/state_N.sqlite` and rollout JSONL files locally; neither
calls an API. Use `codex-watch --details` for expanded session details or
`codex-statusline --json` for a machine-readable snapshot (renderer-only first flags
dispatch to the renderer; anything else launches Codex). `codex-top` monitors existing sessions.

---

## What You See

`default` renders one labeled row per fact — the block at the top of this README. Every row below `repo` is conditional on data actually being available:

| Row | Shown when | What it shows |
|-----|-----------|----------------|
| `model` | always | Model + effort suffix (`.low`/`.medium`/`.high`/`.xhigh`/`.max`) + `⚡fast` when Settings' fast mode is on |
| `time` | session duration available | Wall-clock (`⏱ 24:12`); adds `idle Nm` after 30s with no user turn |
| `account` | account resolved | Tag from `ACCOUNT_LABELS`, colored per `LABEL_COLORS` |
| `repo` | always | Dir name, `worktree`/`primary` tag, branch (dirty `*`, `↑`/`↓` ahead/behind), PR badge |
| `context` | always | Context-window fill — 15-dot sweet-spot bar (blue <30%, green 30–70%, yellow 70–85%, red 85%+) |
| `session` | 5h rate-limit data available | 5h window used, 15-dot bar + `resets <time>` |
| `weekly` | 7-day rate-limit data available | 7-day window used, 15-dot bar + `resets <date>` |
| `fable` | account has a per-model weekly cap | That cap's usage, 15-dot bar (label = the scoped model; opt-out `SHOW_FABLE_ROW=0`) |
| `budget` | `DAILY_BUDGET` set | Spend vs. cap, 10-dot bar |
| `tokens` | scan data available | All-time work/personal token ratio, 10-dot bar (opt-out `SHOW_TOKENS_ROW=0`) |
| goal row | `CHALLENGE_GOAL_M` set (see script header comment) | Progress toward a token goal, labeled `CHALLENGE_LABEL` (opt-out `SHOW_CHALLENGE_ROW=0`) |
| `bounty` | bounty config set and uncleared | ETA to a work-token floor (opt-out `SHOW_BOUNTY_ROW=0`) |
| `usage` | scan data available | Today / this session / lifetime totals, human-formatted |
| `stack` | `SHOW_BACKENDS_ROW=1` | Live snapshot across Claude/Codex/hound agents (`bin/live-state.py`) |
| per-account rows | `SHOW_ACCOUNT_RESETS=1` | One row per tracked account: 5h%, reset, week%, fable%, reset, work-unit cap |

PR badge states: `[draft]`, `[PR✗]` checks failing, `[PR△]` changes requested, `[PR✓]` approved, `[PR⋯]` checks pending, `[PR]` open with no strong signal either way.

### Token tracking

`tokens` and `usage` are both fed by `bin/scan-tokens.py`'s background scan of every session JSONL, cached to `~/.claude/token-scan-summary.json` (small, preferred) or `~/.claude/token-scan-cache.json` (full, fallback) — rescanned in the background whenever that cache is older than 180s.

- **`tokens`** — all-time work/personal ratio (cyan = work, magenta = personal), classified per-request by the `WORK_PATHS`/`WORK_KEYWORDS` vs `PERSONAL_PATHS`/`PERSONAL_KEYWORDS` rules in `statusline.conf`
- **`usage`** — today / this session / lifetime, human-formatted (k/M/B)
- Subagent (Agent tool) tokens are scanned separately (30s cache) and only break out in the optional token-goal row

### Account tagging

All cost and token ledgers are tagged with your account label (e.g., `work` or `personal`), derived from your OAuth email via `ACCOUNT_LABELS`. This lets you aggregate spend by account after the fact. Two related but distinct dimensions live inside the token scanner itself: `EMAIL_PAYER_MAP` (which plan paid) and the work/personal path/keyword classifier (what the work was) — see Configure.

### Terminal tab titles

The script sets the terminal tab title (via ANSI escape) to `repo-name` on main/master, or `repo-name (branch)` on feature branches. Useful in Zed, iTerm2, and other terminals to tell sessions apart at a glance.

### Background — Notifications

macOS Notification Center alerts fire automatically (once per threshold, deduped):
- **Rate limit** at 80%, 90%, 95%
- **Context** at 80%, 95%
- **Budget** at 90%, 100%

### Automatic account detection

When you `/login` to switch accounts, the status bar detects the change (OAuth token hash, with a credential-file mtime fallback) and immediately refreshes — rate limits, account label, and profile data update across all open sessions.

---

## Formats

Seven render modes. Set `FORMAT=` in `~/.claude/statusline.conf` or `STATUSLINE_FORMAT=` env var.

### `default` — Multi-line dashboard (shown above)

The full cockpit, one labeled row per fact. Auto-falls-through to `narrow` when the detected terminal width is below `NARROW_THRESHOLD` (default 60 cols).

### `compact` — Context + session only

Just the `context` and `session` rows — the two numbers that actually gate you.

### `narrow` — Trimmed fallback for tight panels

Same facts as `default` (model+effort, dir+branch, context, 5h, 7d+cost), trimmed hard: short labels, 5–8 char bars scaled to `COLS`, no reset timestamps or breakdowns. Auto-selected under `default` when the panel is narrow; can also be set explicitly.

### `sigil` — Single dense line

```
◈ Opus 4.6 · $2.14 ($8.90/d) · ●●●○○ 60% · ⎇ feature-123✦↑1[PR✓] · 42%⏱24:12 · 71%w
```

Width-adaptive: full detail (cost, daily aggregate, context, git, 5h rate, weekly) at ≥120 cols; drops the daily aggregate and weekly at ≥80; drops git detail to a bare branch name and rate to a bare percentage below 80. Good for tmux status bars or small terminals.

### `sparkline` — Default + trend history

```
  ...default output...
  trend   cost▁▂▃▅▃▂▁▄▆█  rate▁▃▅▇█▇▅▃▂▁
```

Appends inline `▁▂▃▄▅▆▇█` mini-charts (cost and 5h-rate trend, last 15 sessions) read from `~/.claude/session-history.jsonl`. See if you're burning hotter today than yesterday.

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

Claude metrics in your shell prompt gutter. Zero vertical space. Auto-hides after 5 minutes of inactivity. Also emits `sigil` to stdout for Claude Code's own status area.

### `iterm2` — Native terminal status bar

Pushes structured data to iTerm2 via `OSC 1337;SetUserVar` or sets the Kitty window title via `OSC 2`. Auto-detects your terminal; also emits `sigil` to stdout as a fallback.

**iTerm2 setup:** Preferences → Profiles → Session → Status Bar → add "Interpolated String" components:

`\(user.claude_model)` &middot; `\(user.claude_cost)` &middot; `\(user.claude_ctx)` &middot; `\(user.claude_git)` &middot; `\(user.claude_rate)` &middot; `\(user.claude_timer)`

---

## Configure

Create `~/.claude/statusline.conf` (bash, sourced directly). Full annotated version with every knob: [`config/statusline.conf.example`](config/statusline.conf.example). All settings are optional — the script works with no config file at all.

**Cost & format**
- `DAILY_BUDGET=20` — daily cost ceiling; enables the `budget` row + 90%/100% notifications
- `FORMAT=default` — `default | compact | narrow | sigil | sparkline | rprompt | iterm2`

**Branch display**
- `BRANCH_PREFIX_STRIP="andrew/"` — strip a literal prefix off the displayed branch name
- `MAX_BRANCH=24` — max visible branch chars before an ellipsis

**Account labels**
- `ACCOUNT_LABELS="work:*@company.com personal:me@gmail.com"` — email pattern → short tag, first match wins
- `LABEL_COLORS="work:cyan personal:magenta"` — tag → color for the `account` row (unmapped tags default to orange)
- `EMAIL_PAYER_MAP="work:andrew@company.com personal:me@gmail.com"` — which plan paid, for the token scanner's `payer` dimension (independent of the work/personal classifier below)
- `SHOW_ACCOUNT_RESETS=1` — adds a per-account board (5h%, reset, week%, fable%, reset, work-unit cap) below the main rows

**Token classifier** (feeds the `tokens` row's work/personal split — see `bin/scan-tokens.py`)
- `WORK_PATHS` / `PERSONAL_PATHS` — comma-separated cwd/file-path substrings
- `WORK_KEYWORDS` / `PERSONAL_KEYWORDS` — comma-separated prompt keywords (weighted 3× a path hit)

**Bounty / challenge tracker** (opt-in token-goal ETA)
- `CHALLENGE_START`, `BOUNTY_TARGET_TOKENS`, `BOUNTY_LOOKBACK_DAYS`, `BOUNTY_SESSION_GAP_MIN`

**Row visibility** (each defaults on when its data exists; `0` hides it)
- `SHOW_FABLE_ROW`, `SHOW_TOKENS_ROW`, `SHOW_CHALLENGE_ROW`, `SHOW_BOUNTY_ROW`

**Live state stack row** (opt-in)
- `SHOW_BACKENDS_ROW=1` — adds a `stack` row from `bin/live-state.py`: a snapshot across Claude (`account-resets.json`), Codex (newest `state_N.sqlite`), and hound autobuild agents (`sessions.jsonl`)

---

## Accounts: Multi-Account Routing

`accounts` (`bin/accounts.py`) is a router +
headroom board for accounts logged in via `/login`. Onboarding is automatic: log
into an account, then run a routing command (`route`, `set`, `auto`, `fable`, `status`) —
it folds the live credential into `~/.accounts/blobs.json` on its own. No separate
enrollment step.

| Command | What it does |
|---------|---------------|
| `accounts route` | Router daemon: polls all accounts, auto-switches the live one when it runs low (`--interval`, `--at`, `--once`) |
| `accounts set <label>` | Pin the live account to `<label>` (manual mode) |
| `accounts auto` | Hand routing back to the daemon |
| `accounts fable` | Prefer a Fable-capable account; fall back to normal 5h routing when none can |
| `accounts status` | Mode + per-account 5h headroom + ⚠login flags |
| `accounts poll` | Refresh the usage board for all stored accounts now |
| `accounts refresh [label]` | Re-auth stale blobs via their own refresh token, no browser (default: every stale-but-refreshable account) |
| `accounts mint <label>` | Mint + vault a 1-year token via `claude setup-token` |
| `accounts tokens` | List minted tokens and expiry |
| `accounts sync` | Converge the token vault with a second machine |
| `accounts pick-env` | Emit env exports for the best routable account (wrapper hook) |

A fresh `/login` always wins: the daemon adopts the account you just logged into
instead of switching away from it.

Routing never adds to or modifies the Keychain's live `Claude Code-credentials` slot — every switch is a plain file write to `~/.claude/.credentials.json`, followed by a delete of the keychain slot so Claude Code's own ~30s re-read lands on the file. Minted long-lived tokens live outside `~/.claude` (`~/.accounts/vault.json`), since the nightly transcript-archival chain mirrors `~/.claude` in plaintext.

---

## Token Scanning and Redaction

Two independent tools, both built on the same session JSONLs.

**Token scanning** (`bin/scan_tokens_core.py` + the `bin/scan-tokens*.py`/`.sh` CLIs) attributes every request to work/personal and to a payer, incrementally, and feeds the `tokens`/`usage`/goal/`bounty` rows above plus the work-unit cap columns on the account board. `bin/derive-cap.py` fits those per-account caps from utilization history — it's a manual, unscheduled tool you re-run occasionally, not something cron or launchd calls. Full design, cache schema, and failure modes: [`bin/ARCHITECTURE.md`](bin/ARCHITECTURE.md).

**Durable ledger & archival** (`bin/usage-ledger.py`, `bin/archive-transcripts.sh`, `bin/vault-snapshot.sh`) keep a permanent per-day/per-model token ledger at `~/.claude/usage-ledger.json` and mirror transcripts nightly — rows never pruned, survives transcript cleanup.

**Redaction** (`bin/scan-tokens-export.py`, `-autoflag.py`, `-coworker-scrub.py`, `-merge.py`) strips sensitive content from a session JSONL into a separate export copy before you hand it to a third party — source files are never modified. Pipeline, re-run steps, and file layout: [`bin/REDACTION.md`](bin/REDACTION.md).

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

1. **Parses** model, cost, context, session metadata (single `jq` call)
2. **Resolves** account label from the OAuth profile cache (tags all ledger entries) and updates the daily cost/token ledgers in `~/.claude/`
3. **Scans** subagent JSONL files for the current session (cached 30s) and reads `token-scan-summary.json` (fallback: `token-scan-cache.json`) for the work/personal token split — kicks off a background `scan-tokens.py` rescan when that cache is stale (>180s)
4. **Builds** the git/PR segment (branch, dirty, ahead/behind, `gh pr view` cached 90s) and the effort/fast-mode/focus badges
5. **Fetches** rate limits and profile from Anthropic's OAuth API in the background, never blocking render (usage cached 60s, profile cached 5min)
6. **Interpolates** usage between polls — tracks velocity across consecutive API responses for smooth fractional percentages
7. **Builds** the budget row (if `DAILY_BUDGET` is set) and the optional multi-account reset board (if `SHOW_ACCOUNT_RESETS=1`)
8. **Detects** account switches (OAuth token hash change, with a credential-mtime fallback → cache invalidation)
9. **Sets** terminal tab title to repo + branch
10. **Checks** notification thresholds (fires once per crossing, deduped)
11. **Renders** in your chosen format, falling back to `narrow` under `NARROW_THRESHOLD` columns

### Architecture

```
Claude Code                    statusline.sh
    │                              │
    ├─ stdin JSON ────────────────►│ parse (jq)
    │                              │
    │                              ├─► resolve account label (profile cache)
    │                              ├─► update daily-cost.json    (tagged w/ account)
    │                              ├─► update daily-tokens.json  (tagged w/ account)
    │                              ├─► scan subagent JSONL files (cached 30s)
    │                              ├─► read token-scan-summary.json (fallback: token-scan-cache.json)
    │                              ├─► background: fetch /api/oauth/usage (cached 60s)
    │                              ├─► background: fetch /api/oauth/profile (cached 5min)
    │                              ├─► check notification thresholds
    │                              ├─► set terminal tab title (\033]0;repo (branch)\007)
    │                              │
    │  stdout ANSI ◄──────────────├─► render (default|compact|narrow|sigil|sparkline|rprompt|iterm2)
    │                              │
    ├─ /tmp/claude/*.json ────────►│ macOS apps read these
```

### Performance

| Concern | How it's handled |
|---------|-----------------|
| Network latency | Background subshell, never blocks render (usage poll 60s, profile poll 5min) |
| Concurrent sessions | Lock file with stale-PID detection (auto-cleanup at 30s) |
| Git dirty check | `git diff-index --quiet HEAD` (faster than `git status`) |
| PR status | `gh pr view` cached 90s, background-refreshed |
| Ledger writes | Atomic (mktemp + mv) |
| Account switch | OAuth token hash + credential mtime tracking, instant cache invalidation |
| Subagent scan | File-based cache with 30s TTL, scoped to current session |
| Token bar | `jq` read from `token-scan-summary.json` (fallback: `token-scan-cache.json`); the actual JSONL rescan runs in the background via `scan-tokens.py`, never inline |

### Files

| File | Purpose | Lifetime |
|------|---------|----------|
| `~/.claude/statusline.sh` | The script (or symlink) | Permanent |
| `~/.claude/statusline.conf` | Config | Permanent |
| `~/.claude/daily-cost.json` | Daily cost ledger (account-tagged) | Resets daily |
| `~/.claude/daily-tokens.json` | Daily token tracker (account-tagged) | Resets daily |
| `~/.claude/token-scan-summary.json` | Small token-scan summary (preferred read) | Persistent |
| `~/.claude/token-scan-cache.json` | Full token-scan cache (fallback read) | Persistent |
| `~/.claude/account-resets.json` | Multi-account reset ledger (`SHOW_ACCOUNT_RESETS`) | Persistent |
| `~/.claude/account-caps.json` | Per-account work-unit caps, written by `bin/derive-cap.py` | Persistent |
| `~/.claude/utilization-history.jsonl` | Raw utilization samples backing the account board | Rolling |
| `~/.claude/session-history.jsonl` | Sparkline history (account + subagent fields) | Rolling 100 entries |
| `~/.claude/rprompt.txt` | Zsh RPROMPT (`rprompt` format) | Updated each render |
| `~/.claude/usage-ledger.json` | Durable per-day/per-model token ledger (`bin/usage-ledger.py`) | Permanent |
| `~/.claude/statusline-tz` | Optional timezone override for reset-time display | Permanent |
| `~/.claude/.credentials.json` | Claude Code's own OAuth credential — read-only, mtime-tracked | Claude-Code-managed |
| `/tmp/claude/statusline-usage-cache.json` | Rate-limit API cache | 60s TTL |
| `/tmp/claude/statusline-profile-cache.json` | Profile API cache | 5min TTL |
| `/tmp/claude/statusline-usage-prev.json` | Previous poll, for interpolation | Updated each poll |
| `/tmp/claude/statusline-subagent-<sid>.txt` | Subagent token cache per session | 30s TTL |
| `/tmp/claude/ctx-history-<sid>.txt` | Context-fill samples, for the fill-ETA calc | Rolling |
| `/tmp/claude/statusline-pr-<branch>.json` | PR status cache | 90s TTL |
| `/tmp/claude/statusline-raw.json` | Raw status blob, for macOS apps | Updated each render |
| `/tmp/claude/statusline-notif-state.json` | Notification dedup state | Per-threshold |
| `/tmp/claude/statusline-refresh.lock` | Background refresh lock | Transient |
| `/tmp/claude/statusline-creds-mtime` | Credential mtime, account-switch fallback detector | Persistent |
| `/tmp/claude/statusline-token-hash` | OAuth token hash, primary account-switch detector | Persistent |

---

## Uninstall

```bash
# curl install
curl -fsSL https://raw.githubusercontent.com/AndrewTKent/statusline/main/uninstall.sh | bash

# npm
npx @andrewkent/claude-statusline uninstall

# Manual
rm ~/.claude/statusline.sh
# Remove "statusLine" key from ~/.claude/settings.json

# Codex monitor
./uninstall-codex.sh
# Optionally: rm ~/.codex/statusline.conf
```

---

## License

MIT
