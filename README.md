<h1 align="center">claude-statusline</h1>

<p align="center">
  <em>Rich, multi-line status bars for Claude Code and Codex —<br>cost, context, rate limits, and burn-down, at a glance.</em>
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
  <a href="#codex">Codex</a> &middot;
  <a href="#macos-native">macOS Native</a> &middot;
  <a href="#how-it-works">How It Works</a>
</p>

---

```
  Opus 4.6 │ work │ my-project (feature-123*↑1)[PR✓] │ $2.14 ($8.90/d) @$5.30/h │ ⏱ 24:12 ctx:72%
  context ●●●●●●●○○○  72%
  current ●●●●○○○○○○  42.3% 3:45pm →full ~52m ✓18m
  weekly  ●●●●●●●○○○  71.4% apr 4, 10:00am →full ~2.1d ✗8h
  extra   ●●●○○○○○○○  32.0% $63.53/$200.00
  budget  ●●●●○○○○○○  45% $4.50/$10
  tokens  ●●●●●●○○○○  60% (11.8+48.7M)/100M +120k +45k sub (+890k/d)
```

> Everything you need to not get rate-limited, blow your budget, or lose context mid-task. **One bash script, zero dependencies beyond `jq`.**

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/AndrewTKent/statusline/main/install.sh | bash
```

<details>
<summary>Other ways — npm, or manual</summary>

```bash
# npm
npx @andrewkent/claude-statusline install

# Manual — copy the script, add one key to settings
cp bin/statusline.sh ~/.claude/statusline.sh && chmod +x ~/.claude/statusline.sh
```
```json
{ "statusLine": { "type": "command", "command": "~/.claude/statusline.sh", "padding": 0 } }
```
</details>

Restart Claude Code. Done.

**Requires:** [`jq`](https://jqlang.github.io/jq/) &middot; Claude Code (logged in) &middot; optional [`gh`](https://cli.github.com/) for PR badges

---

## What You See

### Line 1 — the headline

```
Opus 4.6 │ work │ my-project (feature-123*↑1)[PR✓] │ $2.14 ($8.90/d) @$5.30/h │ ⏱ 24:12 ctx:72%
```

| Segment | Meaning |
|---|---|
| `Opus 4.6` | Model + effort level (`.high`, `.low`, …) |
| `work` | Account label — auto-detected from OAuth email, configurable |
| `feature-123*↑1` | Git branch &middot; `*` dirty &middot; `↑1` commits ahead |
| `[PR✓]` | PR status — `[PR✓]` approved, `[PR✗]` checks failing, `[draft]`, `[PR△]` changes requested, `[PR⋯]` pending |
| `$2.14` | Session cost |
| `($8.90/d)` | Daily aggregate across all sessions |
| `@$5.30/h` | Burn rate |
| `⏱ 24:12` | Session wall-clock |
| `ctx:72%` | Context badge — appears only at 70%+, as a warning |

### Lines 2–7 — the gauges

| Line | Tracks | Why it matters |
|---|---|---|
| `context` | Context window fill | At 95% your session is about to die |
| `current` | 5-hour rate limit + reset | The one that actually blocks you |
| `weekly` | 7-day rate limit + reset | The slow squeeze |
| `extra` | Extra credits spent / limit | Your overflow budget |
| `budget` | Daily spend vs your cap | Only shows if `DAILY_BUDGET` is set |
| `tokens` | Cumulative tokens, in/out split + subagents | See below |

Each gauge carries live projections: `→full ~52m` (minutes to the wall at current pace), `✓18m` (buffer until reset) or `✗1.7h` (downtime before reset), and `~$18 until reset` for projected extra-credit burn.

<details>
<summary>Token tracking, account tagging, tab titles, notifications</summary>

**Tokens** — all-time usage from `stats-cache.json`, colour-split:

```
tokens  ●●●●●●○○○○  60% (11.8+48.7M)/100M +120k +45k sub (+890k/d)
        ╰cyan╯╰magenta╯     ╰cyan╯╰magenta╯
```

- **Cyan** — input tokens (your prompts) &middot; **Magenta** — output tokens (Claude's responses)
- `+120k` this session &middot; `+45k sub` subagents (Agent tool) this session &middot; `(+890k/d)` daily across all sessions
- Subagent tokens come from scanning `~/.claude/projects/<project>/<session>/subagents/*.jsonl` (30s cache)

**Account tagging** — every cost/token ledger entry is tagged with your account label (from your OAuth email), so you can aggregate spend by account after the fact.

**Terminal tab titles** — sets the tab title to `repo-name` (or `repo-name (branch)` off main) so you can tell sessions apart at a glance in Zed, iTerm2, etc.

**Notifications** — macOS Notification Center alerts, once per threshold, deduped: rate limit at 80/90/95%, context at 80/95%, budget at 90/100%.

**Account switches** — `/login` to another account and the bar detects the credential change and refreshes rate limits, label, and credits across all open sessions instantly.
</details>

---

## Formats

Five render modes. Set `FORMAT=` in `~/.claude/statusline.conf` or the `STATUSLINE_FORMAT=` env var.

**`default`** — the full multi-line dashboard shown above. Adapts to terminal width (compact under 100 cols).

**`sigil`** — everything on one width-adaptive line, dropping segments as the terminal narrows. Good for tmux status bars:
```
◈ Opus 4.6 · $2.14 ($8.90/d) · ●●●●●○○○○○ 55% · ⎇ feature-123✦↑1[PR✓] · 42%⏱24:12 · 71%w
```

**`sparkline`** — `default` plus inline `▁▂▃▄▅▆▇█` trend charts for cost and rate limits across your last 15 sessions.

**`rprompt`** — writes zsh-formatted status to `~/.claude/rprompt.txt` for your shell's right-prompt gutter (auto-hides after 5 min idle).

**`iterm2`** — pushes structured data to iTerm2 (`OSC 1337`) or Kitty (`OSC 2`), auto-detecting your terminal.

<details>
<summary>rprompt + iTerm2 setup</summary>

**rprompt** — add to `.zshrc`:
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

**iTerm2** — Preferences → Profiles → Session → Status Bar → add "Interpolated String" components:
`\(user.claude_model)` &middot; `\(user.claude_cost)` &middot; `\(user.claude_ctx)` &middot; `\(user.claude_git)` &middot; `\(user.claude_rate)` &middot; `\(user.claude_timer)`
</details>

---

## Configure

Create `~/.claude/statusline.conf` — all settings optional, works with no config at all:

```bash
HOURLY_RATE=150            # Billable amount based on session time
DAILY_BUDGET=20            # Budget bar + notifications at 90%/100%
FORMAT=default            # default | sigil | sparkline | rprompt | iterm2
ACCOUNT_LABELS="work:*@company.com personal:me@gmail.com"
```

---

## Codex

A matching live dashboard for the Codex CLI — same look, no API calls (reads `~/.codex/state_5.sqlite` and rollout JSONL locally).

**Requires:** Codex CLI &middot; Python 3 &middot; `tmux` &middot; `~/.local/bin` on `PATH`

```bash
./install-codex.sh
codex-statusline                                              # Codex + a fixed status footer
codex-statusline --sandbox read-only --ask-for-approval on-request
codex-top                                                    # live fleet view (parent + subagent sessions)
```

`codex-statusline` launches Codex in tmux with a fixed bottom pane showing model, elapsed time, account, repo, context, 5-hour and weekly limits, tokens, agents, and running tools. Each footer binds to its owning Codex process's rollout file, so concurrent and resumed sessions never swap context. `codex-statusline --json` prints a machine-readable snapshot; `codex-watch --details` expands per-session detail.

<details>
<summary>Behaviour, tuning, and env vars</summary>

- **Refresh** — the footer is live for a single session; multi-session `--top`/`--all` hold at a 30s cadence. Backs off to a 30s poll after 10 min idle, and truncates `state_5.sqlite`'s WAL past 128 MB (long-lived footers otherwise starved SQLite checkpoints).
- **Permissions** — defaults to Codex YOLO (`--dangerously-bypass-approvals-and-sandbox`). An explicit `-a/--ask-for-approval`, `-s/--sandbox`, or bypass flag replaces the default; `-p`/`-c` overrides don't. Set `CODEX_STATUSLINE_MANAGE_APPROVALS=0` to pass no permission default.
- **tmux** — mouse-wheel scrollback with 100k-line history (`CODEX_STATUSLINE_HISTORY_LIMIT`); drag-select copies via OSC 52 (iTerm2, kitty, WezTerm). Launched inside tmux, your pane's options are restored on exit; launched outside, detach (`prefix d`) leaves Codex running (`tmux attach -t codex-statusline-<pid>`).
- **Config** — loads from `${CODEX_HOME:-~/.codex}/statusline.conf`; non-empty env vars override, `CODEX_STATUSLINE_CONFIG` points elsewhere. Tunables: `CODEX_STATUSLINE_INTERVAL`, `_HEIGHT`, `_HISTORY_LIMIT`, `_MANAGE_APPROVALS`, `_THREAD_ID`, `_CODEX_BIN`.
</details>

---

## macOS Native

Three companion apps reading the same data files — no extra API calls.

- **Menu Bar App** — colour-coded icon (green ok, yellow rate-limit 70%+, red 90%+/context critical), click for a SwiftUI popover. `cd macos/ClaudeMenuBar && ./build.sh && ./install.sh` (swiftc, no Xcode).
- **Raycast Extension** — search "Claude Status", or pin `$12.34 | 5hr: 45%` to the menu bar. `macos/claude-raycast/`.
- **Widget Bridge** — consolidates status into `~/.claude/widget-snapshot.json` with a 24h cost sparkline. `swift macos/claude-widget/Bridge/claude-widget-bridge.swift` — see [`macos/claude-widget/README.md`](macos/claude-widget/README.md).

---

## How It Works

Claude Code pipes a JSON status blob into the script on stdin every tool call. The script parses it (one `jq` call), resolves your account label, updates daily cost/token ledgers, scans subagent JSONL, fetches rate limits in the background, interpolates usage between polls for smooth fractional percentages, projects burn-down and survive indicators, detects account switches, sets the tab title, checks notification thresholds, and renders your chosen format.

```
Claude Code                    statusline.sh
    │                              │
    ├─ stdin JSON ────────────────►│ parse (jq)
    │                              ├─► resolve account label (profile cache)
    │                              ├─► update daily-cost / daily-tokens (account-tagged)
    │                              ├─► scan subagent JSONL (cached 30s)
    │                              ├─► background: /api/oauth/usage (60s) + /profile (5min)
    │                              ├─► check notification thresholds
    │                              ├─► set terminal tab title
    │  stdout ANSI ◄──────────────├─► render (default|sigil|sparkline|rprompt|iterm2)
    │                              │
    ├─ /tmp/claude/*.json ────────►│ macOS apps read these
```

<details>
<summary>Performance &amp; caching</summary>

| Concern | How it's handled |
|---|---|
| Network latency | Background subshell, never blocks render (5min poll) |
| Concurrent sessions | Lock file with stale-PID detection (30s auto-cleanup) |
| Git dirty check | `git diff-index --quiet HEAD` (faster than `git status`) |
| PR status | `gh pr view` cached 90s, background-refreshed |
| Ledger writes | Atomic (mktemp + mv) |
| Account switch | Credential mtime tracking, instant cache invalidation |
| Subagent scan | File cache, 30s TTL, scoped to current session |
| Token totals | Single `jq` read from `stats-cache.json` |
</details>

<details>
<summary>Files it reads and writes</summary>

| File | Purpose | Lifetime |
|---|---|---|
| `~/.claude/statusline.sh` | The script (or symlink) | Permanent |
| `~/.claude/statusline.conf` | Config | Permanent |
| `~/.claude/daily-cost.json` | Daily cost ledger (account-tagged) | Resets daily |
| `~/.claude/daily-tokens.json` | Daily token tracker (account-tagged) | Resets daily |
| `~/.claude/stats-cache.json` | Token total source (Claude Code managed) | Persistent |
| `~/.claude/session-history.jsonl` | Sparkline history | Rolling 100 |
| `~/.claude/rprompt.txt` | Zsh RPROMPT (rprompt format) | Each render |
| `/tmp/claude/statusline-usage-cache.json` | Rate-limit API cache | 5min TTL |
| `/tmp/claude/statusline-usage-prev.json` | Previous poll (interpolation) | Each poll |
| `/tmp/claude/statusline-profile-cache.json` | Profile API cache | 5min TTL |
| `/tmp/claude/statusline-subagent-*.txt` | Per-session subagent cache | 30s TTL |
| `/tmp/claude/statusline-raw.json` | Raw status for macOS apps | Each render |
| `/tmp/claude/statusline-notif-state.json` | Notification dedup state | Per-threshold |
| `/tmp/claude/statusline-creds-mtime` | Credential change detection | Persistent |
</details>

---

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/AndrewTKent/statusline/main/uninstall.sh | bash   # or: npx @andrewkent/claude-statusline uninstall

# Manual: rm ~/.claude/statusline.sh and remove the "statusLine" key from ~/.claude/settings.json

# Codex monitor
rm ~/.local/bin/codex-{statusline,top,watch} ~/.local/bin/codex-config.sh ~/.local/bin/codex_statusline.py
# Optionally: rm ~/.codex/statusline.conf
```

---

## License

MIT
