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

Five tools, one repo, shared data files:

- **Claude Code statusline** (`bin/statusline.sh`) — the multi-line dashboard below
- **Codex statusline** (`bin/codex-statusline`, `codex-top`) — the same idea for the Codex CLI
- **`accounts`** (`bin/accounts.py`) — native-profile account routing and headroom board
- **Token scanning & redaction** (`bin/scan-tokens*`) — attribute every token, redact before sharing
- **Agent Metrics** (`bin/agent-metrics`) — opt-in local telemetry and dashboard

```
model   Fable 5.ultracode
time    ⏱ 2:29:20
account you@example.com
repo    my-project feature/fix-the-thing (v1.2.0*)
pr      #N Fix The Thing The Session Is Working On
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
{ "statusLine": { "type": "command", "command": "~/.claude/statusline.sh", "padding": 0, "refreshInterval": 60 } }
```

Restart Claude Code. Done.

**Requires:** [`jq`](https://jqlang.github.io/jq/) &middot; Claude Code (logged in) &middot; Optional: [`gh`](https://cli.github.com/) for PR badges

### Codex

**Requires:** Codex CLI &middot; Python 3 &middot; `~/.local/bin` on `PATH` &middot; Legacy dashboard: `tmux` &middot; Optional: [`gh`](https://cli.github.com/) for PR linkage

```bash
./install-codex.sh
codex-statusline
codex-statusline --sandbox read-only --ask-for-approval on-request
```

Set `CODEX_STATUSLINE_NATIVE=1` in `~/.codex/statusline.conf` to launch Codex
with its native themed status line and `--no-alt-screen`. The native line shows
model, project, branch, context, usage limits, tokens, permissions, approval
mode, and task progress while preserving normal terminal selection and
scrollback. Set it to `0` for the legacy tmux dashboard described below.

The legacy mode launches Codex in tmux with a fixed bottom pane matching the
multi-line Claude Code status view. It shows the current model, elapsed time,
account, repository, linked pull request, context use, 5-hour and weekly limits,
remaining purchased credits, tokens, agents, and running tools. It binds each
footer to the rollout file opened by its owning Codex process, so concurrent and
resumed sessions do not exchange context values. Mouse
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
`CODEX_STATUSLINE_MANAGE_APPROVALS=0` to pass no permission default. In legacy
mode, `tui.status_line=[]` keeps only Codex's compact built-in prompt footer while
the detailed dashboard stays in the fixed pane.
Settings load from `${CODEX_HOME:-~/.codex}/statusline.conf`; non-empty environment
variables override file values, and `CODEX_STATUSLINE_CONFIG` points at a
different file.

`codex-top` is the live fleet view for parent and subagent sessions. Both views
read the newest `~/.codex/state_N.sqlite` and rollout JSONL files locally; neither
calls an API. Use `codex-watch --details` for expanded session details or
`codex-statusline --json` for a machine-readable snapshot (renderer-only first flags
dispatch to the renderer; anything else launches Codex). `codex-top` monitors existing sessions.

### Agent Metrics (optional)

Agent Metrics is an opt-in, local-first history and dashboard add-on for Claude
Code and Codex. It is not installed or started by either default installer.
Nothing is collected until one of its explicit commands is run. It requires
Python 3.11 or newer; set `AGENT_METRICS_PYTHON` to a compatible interpreter
when the system `python3` is older.

```bash
bin/agent-metrics init
bin/agent-metrics sync --max-lines 5000
bin/agent-metrics watch --interval 60 --max-lines 5000
bin/agent-metrics serve
# In another terminal, only when you want a browser window:
bin/agent-metrics open
```

`init` creates private runtime storage and a configuration file. On macOS the
default is `~/Library/Application Support/statusline/agent-metrics/`; on Linux
it is `${XDG_DATA_HOME:-~/.local/share}/statusline/agent-metrics/`. Override it
with `--data-dir` or `AGENT_METRICS_DATA_DIR`. Runtime data is never written to
this repository.

`sync` incrementally scans local Claude Code and Codex JSONL files into raw,
event-level SQLite rows and rebuilds derived one-minute metrics. Repeated scans
are idempotent. `--max-lines` bounds one invocation; omit it for an unlimited
manual backfill. Bounded scans reserve capacity for appended live files and for
both providers while rotating through older sources by salted source ID.
`watch` is an explicit foreground loop that defaults to 5,000 lines every 60
seconds, measured after each completed cycle. It prints live/backfill progress
and remaining file/byte counts; Ctrl-C stops it cleanly. There is no daemon,
autostart, or launch-at-login integration. The local dashboard polls that database for a stacked token
timeline with selectable token series, one-minute raw or trailing moving-average views, a trailing-day hourly/cumulative view, provider/account/model/effort/session/agent filters, account and model
totals, parent/child agent drilldown, compactions, tool outcomes and durations,
turn latency, quota snapshots, and explicitly exposed cost. `serve` does not
scan automatically and binds only to a loopback address; non-loopback binds are
rejected. Its HTML, CSS, and JavaScript have no network dependencies or
analytics.
`open` passes a private local capability to the browser; dashboard API reads
without that capability are rejected, including requests from other local processes.

The database stores numerical metadata plus provider, model, effort, opaque
session/request/call IDs, and tool names/statuses. It never stores prompts,
transcript text, tool arguments or output, source text, source paths,
credentials, token values, emails, or account-holder names. Account and source
identities use a local salt. Claude attribution matches each event timestamp to
`session-accounts.json` using half-open `[from,to)` spans; the organization ID
participates in the account hash. Codex reads only the explicit current
`account_id` field from `auth.json`; it never decodes or stores access, refresh,
or identity tokens.

Configuration lives in the runtime directory's `config.toml`; the generic
template is [`config/agent-metrics.toml.example`](config/agent-metrics.toml.example).
Source paths, account aliases, pricing metadata, retention, bind address, and
port are configurable. Agent Metrics can reuse declared short Claude account
labels from `ACCOUNT_LABELS` in a configurable `statusline.conf`; explicit
`[account_aliases]` entries win, and the feature can be disabled. Patterns,
emails, and organization IDs are matched only in memory and are never stored.
Pricing is not applied to infer event cost.

Optional `[account_tiers]` entries map declared account labels to `5x` or
`20x`. Agent Metrics records minute quota observations from the shared account
snapshot and incrementally backfills the existing Claude utilization history
when a declared label matches in memory. It excludes stale, pending-reset,
reset-crossing, and zero/negative-utilization intervals, then compares tracked
token deltas with positive five-hour utilization deltas by plan cohort, model,
and reasoning effort. The dashboard reports samples, dispersion, and observed
token ranges as a **tracked-token equivalent**. This is empirical local data,
not an Anthropic-published fixed quota; other clients and untracked usage can
bias it. For accounts without safe declared-label history, inference starts
with new shared snapshots.

Current capture limits: Codex local history does not expose historical account
handoffs, so newly ingested Codex rows receive the account active at their first
sync. Some Claude records omit reasoning effort, context limits, compaction
details, quota, or cost; those fields remain empty rather than being inferred.
Tool duration is available only when matching start/end records are present.

---

## What You See

`default` renders one labeled row per fact — the block at the top of this README. Every row below `repo` is conditional on data actually being available:

| Row | Shown when | What it shows |
|-----|-----------|----------------|
| `model` | always | Model + effort (`· low`/`· medium`/`· high`/`· xhigh`/`· max`/`· ultracode`) + `⚡fast` when Settings' fast mode is on |
| `time` | session duration available | Wall-clock (`⏱ 24:12`); adds `idle Nm` after 30s with no user turn |
| `account` | account resolved | Tag from `ACCOUNT_LABELS`, colored per `LABEL_COLORS` |
| `repo` | always | Primary repository name |
| `tree` | the checkout is a linked worktree | Worktree name |
| `branch` | the checkout is in Git | Branch, dirty `*`, and `↑`/`↓` divergence |
| `pr` | the checkout maps to an open PR | PR number and title for the checked-out branch or detached PR head |
| `context` | always | Context-window fill — 15-dot sweet-spot bar (blue <30%, green 30–70%, yellow 70–85%, red 85%+) |
| `session` | 5h rate-limit data available | 5h window used, 15-dot bar + `resets <time>` |
| `weekly` | 7-day rate-limit data available | 7-day window used, 15-dot bar + `resets <date>` |
| `fable` | account has a per-model weekly cap | That cap's usage, 15-dot bar (label = the scoped model; opt-out `SHOW_FABLE_ROW=0`) |
| `budget` | `DAILY_BUDGET` set | Spend vs. cap, 10-dot bar |
| `tokens` | scan data available | All-time work/personal token ratio, 10-dot bar (opt-out `SHOW_TOKENS_ROW=0`) |
| goal row | `CHALLENGE_GOAL_M` set (see script header comment) | Progress toward a token goal, labeled `CHALLENGE_LABEL` (opt-out `SHOW_CHALLENGE_ROW=0`) |
| `bounty` | bounty config set and uncleared | ETA to a work-token floor (opt-out `SHOW_BOUNTY_ROW=0`) |
| `usage` | scan data available | Today / this session / lifetime totals, human-formatted |
| `stack` | `SHOW_BACKENDS_ROW=1` | Live snapshot across Claude/Codex/remote agents (`bin/live-state.py`) |
| per-account rows | `SHOW_ACCOUNT_RESETS=1` | One row per tracked account: 5h%, reset, week%, fable%, reset, work-unit cap |

PR badge states: `[draft]`, `[PR✗]` checks failing, `[PR△]` changes requested, `[PR✓]` approved, `[PR⋯]` checks pending, `[PR]` open with no strong signal either way.

### Token tracking

`tokens` and `usage` are both fed by `bin/scan-tokens.py`'s background scan of every session JSONL, cached to `~/.claude/token-scan-summary.json` (small, preferred) or `~/.claude/token-scan-cache.json` (full, fallback) — rescanned in the background whenever that cache is older than 180s.

- **`tokens`** — all-time work/personal ratio (cyan = work, magenta = personal), classified per-request by the `WORK_PATHS`/`WORK_KEYWORDS` vs `PERSONAL_PATHS`/`PERSONAL_KEYWORDS` rules in `statusline.conf`
- **`usage`** — today / this session / lifetime, human-formatted (k/M/B)
- Subagent (Agent tool) tokens are scanned separately (30s cache) and only break out in the optional token-goal row

### Account tagging

All cost and token ledgers are tagged with your account label (e.g., `work` or `personal`), derived from your OAuth email via `ACCOUNT_LABELS`. This lets you aggregate spend by account after the fact. Two related but distinct dimensions live inside the token scanner itself: `EMAIL_PAYER_MAP` (which plan paid) and the work/personal path/keyword classifier (what the work was) — see Configure.

Set `SHARED_ACCOUNT_SNAPSHOT=1` to make account and quota rendering read-only and snapshot-only. Run `accounts poll` for one refresh or `accounts watch --interval 60` as an explicit foreground loop. The renderer reads `~/.accounts/statusline-snapshot.json` once, maps the current account only through `ACCOUNTS_ROUTED_LABEL`, and displays only declared short labels. It does not inspect credentials, call the profile or usage APIs, write shared ledgers, or start the full token scanner. Missing, stale, pending-reset, and error data remain unknown or visibly stale; they are never rendered as zero. `SHARED_ACCOUNT_SNAPSHOT_FILE` and `SHARED_ACCOUNT_SNAPSHOT_MAX_AGE` are configurable.

Shared mode uses its own lightweight presentation: the default layout keeps the
account board, while compact terminal formats use one line. It still refreshes
the terminal title and router state, but skips legacy notifications and history writes.

Claude Code's `statusLine.refreshInterval` controls renderer cadence. A 60-second interval matches the foreground account watcher and avoids repeated work for minute-resolution quota data.

### Terminal tab titles

The script sets the terminal tab title (via ANSI escape) to `repo-name` on main/master, or `repo-name (branch)` on feature branches. Useful in Zed, iTerm2, and other terminals to tell sessions apart at a glance.

### Background — Notifications

macOS Notification Center alerts are off by default; opt in with
`STATUSLINE_NOTIFY=1` (exported, or set in `~/.claude/statusline.conf`).
When enabled they fire once per threshold, deduped:
- **Rate limit** at 80%, 90%, 95%
- **Context** at 80%, 95%
- **Budget** at 90%, 100%

### Automatic account detection

When you `/login` inside a routed profile, the status bar detects the credential change before writing its ledgers, refreshes the profile, and updates the rate limits and account label on the next render. Sessions using that same native profile see the refreshed login.

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
- `DAILY_BUDGET=20` — daily cost ceiling; enables the `budget` row + 90%/100% notifications (when opted in)
- `STATUSLINE_NOTIFY=1` — opt in to macOS Notification Center threshold alerts (default off)
- `FORMAT=default` — `default | compact | narrow | sigil | sparkline | rprompt | iterm2`

**Branch display**
- `BRANCH_PREFIX_STRIP="andrew/"` — strip a literal prefix off the displayed branch name
- `MAX_BRANCH=24` — max visible branch chars before an ellipsis

**Account labels**
- `ACCOUNT_LABELS="work:*@company.com personal:me@gmail.com"` — email pattern → short tag, first match wins
- `LABEL_COLORS="work:cyan personal:magenta"` — tag → color for the `account` row (unmapped tags default to orange)
- `EMAIL_PAYER_MAP="work:you@company.com personal:me@gmail.com"` — which plan paid, for the token scanner's `payer` dimension (independent of the work/personal classifier below)
- `SHOW_ACCOUNT_RESETS=1` — adds a per-account board (5h%, reset, week%, fable%, reset, work-unit cap) below the main rows
- `SHARED_ACCOUNT_SNAPSHOT=1` — read account/routing/quota rows only from the private accounts snapshot; use `accounts watch --interval 60` to refresh it explicitly
- `SHARED_ACCOUNT_SNAPSHOT_FILE` / `SHARED_ACCOUNT_SNAPSHOT_MAX_AGE` — override the snapshot path or stale threshold
- `ACCOUNTS_HARD_SESSION_LIMIT=1` — opt in to stopping routed Claude sessions at 100% five-hour utilization; account pins are bypassed only at that boundary

**Token classifier** (feeds the `tokens` row's work/personal split — see `bin/scan-tokens.py`)
- `WORK_PATHS` / `PERSONAL_PATHS` — comma-separated cwd/file-path substrings
- `WORK_KEYWORDS` / `PERSONAL_KEYWORDS` — comma-separated prompt keywords (weighted 3× a path hit)

**Bounty / challenge tracker** (opt-in token-goal ETA)
- `CHALLENGE_START`, `BOUNTY_TARGET_TOKENS`, `BOUNTY_LOOKBACK_DAYS`, `BOUNTY_SESSION_GAP_MIN`

**Row visibility** (each defaults on when its data exists; `0` hides it)
- `SHOW_FABLE_ROW`, `SHOW_TOKENS_ROW`, `SHOW_CHALLENGE_ROW`, `SHOW_BOUNTY_ROW`

**Live state stack row** (opt-in)
- `SHOW_BACKENDS_ROW=1` — adds a `stack` row from `bin/live-state.py`: a snapshot across Claude (`account-resets.json`), Codex (newest `state_N.sqlite`), and remote autobuild agents (`$AGENT_SESSIONS_PATH`)

---

## Accounts: Multi-Account Routing

`accounts` (`bin/accounts.py`) is a per-session router and headroom board. Each
account gets a native Claude config under `~/.accounts/profiles/<label>`.
Credentials and entitlement caches are isolated; projects, transcripts, settings,
skills, and plugins are shared. Interactive sessions remain first-party
`claude.ai` subscription sessions instead of API/setup-token sessions.

For shared statusline rendering, enable `SHARED_ACCOUNT_SNAPSHOT=1` in
`statusline.conf`. The router installer registers a launch agent that runs
`accounts poll` every minute; `accounts watch --interval 60` is the foreground
alternative.

Install the router from a local checkout:

```bash
./install-account-router.sh
```

The installer puts the router wrapper at `~/.local/bin/claude`, keeps native
Claude binaries under `~/.local/share/claude/versions`, installs the router
tools under `~/.local/bin`, and prepends a supervised launcher from
`~/.accounts/bin` in new zsh sessions.

`ACCOUNTS_HARD_SESSION_LIMIT=1` is an opt-in overage guard. A supervised
session resumes on another safe account on the next supervisor check after
100% five-hour utilization is observed, or terminates when none is available.

| Command | What it does |
|---------|---------------|
| `accounts set <label>` | Force every supervised session onto `<label>` |
| `accounts pane set <label>` | Pin only the current terminal pane to `<label>` |
| `accounts pane clear` | Return the current pane to the global policy |
| `accounts auto` | Route supervised sessions to the freshest account |
| `accounts fable` | Switch live supervised sessions to Fable while headroom is available |
| `accounts status` | Mode + per-account 5h/7d/Fable headroom + ⚠login flags |
| `accounts poll` | Refresh dormant stored/native profiles, then poll every routable account |
| `accounts refresh [label]` | Refresh stale file-backed credentials without a browser |
| `accounts mint <label>` | Mint + vault a 1-year token for headless jobs |
| `accounts tokens` | List minted tokens and expiry |
| `accounts sync` | Converge the token vault with a second machine |
| `accounts pick-env` | Emit `CLAUDE_CONFIG_DIR` and account metadata |

Inside Claude Code, prefix these with `!` (for example,
`!accounts set acme-max`). Set `"respondToBashCommands": false` in
`~/.claude/settings.json` so the switch does not trigger an LLM response.

`claude-router.py` supervises interactive sessions. It reserves the selected
account, watches the active model's quota windows, and resumes the exact session
under another isolated profile before a window is exhausted. The shell never
regains control during a handoff. Changing to Fable mode also moves running
supervised sessions to Fable in place — except a session you explicitly put on
another model (a `--model` launch flag or a live `/model` switch), which stays
there until you switch back to `/model fable` or re-run `accounts fable`. If
every Fable-capable account is gated, the same session resumes on Opus using the
safest general-model account.
Minted long-lived tokens remain outside `~/.claude`
(`~/.accounts/vault.json`); archival copies only session JSONLs from
`~/.claude/projects`.

---

## Token Scanning and Redaction

Two independent tools, both built on the same session JSONLs.

**Token scanning** (`bin/scan_tokens_core.py` + the `bin/scan-tokens*.py`/`.sh` CLIs) attributes every request to work/personal and to a payer, incrementally, and feeds the `tokens`/`usage`/goal/`bounty` rows above plus the work-unit cap columns on the account board. `bin/derive-cap.py` fits those per-account caps from utilization history — it's a manual, unscheduled tool you re-run occasionally, not something cron or launchd calls. Full design, cache schema, and failure modes: [`bin/ARCHITECTURE.md`](bin/ARCHITECTURE.md).

**Durable ledger & archival** (`bin/usage-ledger.py`, `bin/archive-transcripts.sh`, `bin/vault-snapshot.sh`) keep a permanent per-day/per-model token ledger at `~/.claude/usage-ledger.json` and mirror Claude Code session JSONLs nightly — rows never pruned, survives transcript cleanup.

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
2. **Detects** credential changes and validates changed profile identity before any account-tagged ledger write
3. **Resolves** the account label from the OAuth profile cache and updates the daily cost/token ledgers in `~/.claude/`
4. **Scans** subagent JSONL files for the current session (cached 30s) and reads `token-scan-summary.json` (fallback: `token-scan-cache.json`) for the work/personal token split — kicks off a background `scan-tokens.py` rescan when that cache is stale (>180s)
5. **Builds** the git/PR segment (branch, dirty, ahead/behind, `gh pr view` cached 90s) and the effort/fast-mode/focus badges
6. **Refreshes** rate limits and profile from Anthropic's OAuth API in the background (usage cached 60s, profile cached 5min)
7. **Interpolates** usage between polls — tracks velocity across consecutive API responses for smooth fractional percentages
8. **Builds** the budget row (if `DAILY_BUDGET` is set) and the optional multi-account reset board (if `SHOW_ACCOUNT_RESETS=1`)
9. **Sets** terminal tab title to repo + branch
10. **Checks** notification thresholds when `STATUSLINE_NOTIFY=1` (fires once per crossing, deduped)
11. **Renders** in your chosen format, falling back to `narrow` under `NARROW_THRESHOLD` columns

### Architecture

```
Claude Code                    statusline.sh
    │                              │
    ├─ stdin JSON ────────────────►│ parse (jq)
    │                              │
    │                              ├─► changed credential: fetch profile (≤2s)
    │                              ├─► resolve account label (profile cache)
    │                              ├─► update daily-cost.json    (tagged w/ account)
    │                              ├─► update daily-tokens.json  (tagged w/ account)
    │                              ├─► scan subagent JSONL files (cached 30s)
    │                              ├─► read token-scan-summary.json (fallback: token-scan-cache.json)
    │                              ├─► background: fetch /api/oauth/usage (cached 60s)
    │                              ├─► background: refresh /api/oauth/profile (cached 5min)
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
| Network latency | Background refreshes; a changed credential can block up to 2s for identity validation |
| Concurrent sessions | Lock file with stale-PID detection (auto-cleanup at 30s) |
| Git dirty check | `git diff-index --quiet HEAD` (faster than `git status`) |
| PR status | Repository-scoped `gh` lookup cached 90s, background-refreshed |
| Ledger writes | Atomic (mktemp + mv) |
| Account switch | OAuth token hash + credential mtime tracking, synchronous identity validation before ledger writes |
| Subagent scan | File-based cache with 30s TTL, scoped to current session |
| Token bar | `jq` read from `token-scan-summary.json` (fallback: `token-scan-cache.json`); the actual JSONL rescan runs in the background via `scan-tokens.py`, never inline |
| Shared account snapshot | One stable inode+mtime read; no credential/profile/usage calls or shared-ledger writes |

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
| `~/.accounts/statusline-snapshot.json` | Private declared-label routing and quota snapshot (`SHARED_ACCOUNT_SNAPSHOT=1`) | Written only by explicit `accounts poll`/`accounts watch` |
| `~/.claude/.credentials.json` | Claude Code's own OAuth credential — read-only, mtime-tracked | Claude-Code-managed |
| `/tmp/claude/statusline-usage-cache-<profile>.json` | Account-keyed rate-limit API cache | 60s TTL |
| `/tmp/claude/statusline-profile-cache-<profile>.json` | Account-keyed profile API cache | 5min TTL |
| `/tmp/claude/statusline-usage-prev-<profile>.json` | Account-keyed previous poll, for interpolation | Updated each poll |
| `/tmp/claude/statusline-{usage,profile}-cache.json` | Current-profile aliases for companion apps | Updated each render |
| `/tmp/claude/statusline-subagent-<sid>.txt` | Subagent token cache per session | 30s TTL |
| `/tmp/claude/ctx-history-<sid>.txt` | Context-fill samples, for the fill-ETA calc | Rolling |
| `/tmp/claude/statusline-pr-<repo-ref-key>.json` | PR status cache | 90s TTL |
| `/tmp/claude/statusline-pr-<repo-ref-key>.json.lock` | PR refresh lock | Persistent file, transient lock |
| `/tmp/claude/statusline-raw.json` | Raw status blob, for macOS apps | Updated each legacy render; not used in shared snapshot mode |
| `/tmp/claude/statusline-notif-state.json` | Notification dedup state | Per-threshold |
| `/tmp/claude/statusline-refresh-<profile>.lock` | Account-keyed background refresh lock | Transient |
| `/tmp/claude/statusline-creds-mtime-<profile>` | Account-keyed credential mtime detector | Persistent |
| `/tmp/claude/statusline-token-hash-<profile>` | Account-keyed OAuth token hash detector | Persistent |

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
