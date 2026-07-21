---
name: setup-statusline
description: Set up the base Claude Code statusline from this repo on a new machine — symlink, settings.json, config, optional token-scan daemon.
---

# Setup: base statusline

Install the Claude Code statusline from a clone of this repo. For the
multi-account routing layer on top, run `/setup-account-routing` after this.

## Prerequisites

- `jq` (`brew install jq`)
- This repo cloned; `$REPO` below means the clone root.

## Install (from a clone — preferred; `git pull` updates the live script)

1. Symlink the script:

   ```bash
   ln -sf "$REPO/bin/statusline.sh" ~/.claude/statusline.sh
   ```

2. Point Claude Code at it — add to `~/.claude/settings.json` (or run
   `$REPO/install.sh`, which patches it for you but copies instead of
   symlinking):

   ```json
   "statusLine": { "type": "command", "command": "~/.claude/statusline.sh", "padding": 0 }
   ```

3. Config:

   ```bash
   cp "$REPO/config/statusline.conf.example" ~/.claude/statusline.conf
   ```

   Knobs worth setting on day one (the example documents the rest):
   - `ACCOUNT_LABELS` — `label:email` pairs; disambiguate same-email orgs with
     `label:email|org-uuid`. Make each label match what you see in the email
     (`alumni:*@alumni.example.edu`, not an internal codename).
   - `WORK_PATHS` / `PERSONAL_PATHS` — repo path prefixes for work/personal
     token attribution.
   - `EMAIL_PAYER_MAP` — who pays for which account, for the cost rows.
   - `FORMAT` — `default | compact | narrow | sigil | rprompt | sparkline | iterm2`.

Alternative installs (copy, not symlink): `curl` per the README, Homebrew
(`homebrew/claude-statusline.rb`), or npm (`npm/`).

## Optional: token-scan daemon

The token/challenge rows read `~/.claude/token-scan-summary.json`, produced by
a background scanner. Without it those rows stay empty.

```bash
brew install fswatch
"$REPO/macos/launchd/install-daemon.sh"
```

## Optional extras

- `macos/ClaudeMenuBar/` — menu-bar app (`build.sh` then `install.sh`)
- `macos/claude-raycast/` — Raycast extension
- `macos/claude-widget/` — widget bridge

All read the statusline's JSON sidecars; no extra config.

## Verify

```bash
echo '' | bash ~/.claude/statusline.sh    # must print exactly: Claude
```

Then start a Claude Code session and confirm the statusline renders. If
`test/render-snapshot.sh` exists in your checkout, `test/render-snapshot.sh
snap /tmp/sl-check` renders every format against a fixture.

## Troubleshooting

- Nothing renders: `settings.json` statusLine block missing or wrong path.
- Token rows empty: the scan daemon isn't installed/running
  (`launchctl list | grep scan-tokens`).
- Board row missing: that's the routing layer — see `/setup-account-routing`.
