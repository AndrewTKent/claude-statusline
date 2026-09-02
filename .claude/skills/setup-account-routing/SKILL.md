---
name: setup-account-routing
description: Set up native-profile multi-account routing for Claude Code — account labels, isolated config profiles, quota-aware live handoffs, and the supervised launcher.
---

# Setup: account routing (accounts)

Multi-account routing for Claude Code. Each account gets an isolated native
`CLAUDE_CONFIG_DIR`. The supervisor chooses by quota headroom and resumes the
same conversation under another profile when the active account is blocked.
Run `/setup-statusline` first — both tools share `~/.claude/statusline.conf`.

## Hard rules

- Interactive routing uses Claude's native OAuth credentials, not setup-token
  injection or global credential replacement.
- Never display or log credential values. `~/.accounts/` stays out of transcripts
  and commits.
- `accounts mint` is only for headless jobs.

## Steps

1. **Label the accounts** in `~/.claude/statusline.conf`:

   ```bash
   ACCOUNT_LABELS="gmail:me@gmail.com work:me@corp.com|org-uuid"
   LABEL_COLORS="gmail:magenta work:cyan"   # optional
   ACCOUNTS_EXCLUDE=""                      # labels excluded from automatic routing
   ACCOUNTS_HIDE=""                         # labels hidden from the statusline board, still routed
   SHOW_ACCOUNT_RESETS=1
   ```

   To prevent routed sessions from consuming paid overage after the five-hour
   window reaches 100%, opt in locally:

   ```bash
   ACCOUNTS_HARD_SESSION_LIMIT=1
   ```

   Labels must match the email address shown by Claude. Same email in two
   organizations requires `|org-uuid` suffixes.

2. **Install the supervised launcher:**

   ```bash
   cd "$REPO"
   ./install-account-router.sh
   export PATH="$HOME/.accounts/bin:$PATH"
   ```

   The installer keeps the native Claude binary at `~/.local/bin/claude`, links
   the router tools under `~/.local/bin`, and places the supervised `claude`
   launcher first on `PATH`.

3. **Seed each account into the store.** Run the native binary directly, `/login`
   as the account, exit Claude, then run:

   ```bash
   ~/.local/bin/claude
   accounts status
   ```

   Repeat for each account. `accounts status` captures Claude's default-profile
   login into `~/.accounts/blobs.json`; routed launches subsequently keep refreshed
   credentials synchronized from their native profiles.

4. **Pick a mode:**

   ```bash
   accounts auto           # route supervised sessions by general quota headroom
   accounts fable          # switch live supervised sessions by Fable, weekly, and 5h headroom
   accounts set <label>    # force every supervised session to this account
   ```

   Mode changes are detected by running supervisors. Handoffs preserve the exact
   session ID and do not return control to the shell. Source changes to either
   router module reload the supervisor in place. New launches default to
   high effort and an invisible Claude session name unless explicitly set.

   Pin one launch with `ACCOUNTS_PIN=<label> claude`.

5. **Optional headless-job tokens:**

   ```bash
   accounts mint <label>
   accounts tokens
   accounts sync
   ```

## Verify

```bash
accounts poll
accounts ls
accounts status
ACCOUNTS_PIN=<label> accounts pick-env
```

`pick-env` should emit `CLAUDE_CONFIG_DIR=~/.accounts/profiles/<label>` and no
`CLAUDE_CODE_OAUTH_TOKEN` export.

## When routing misbehaves

1. `accounts status` shows the mode and next selected account.
2. `accounts auto` releases a persistent pin.
3. `accounts poll` refreshes the headroom board; `accounts refresh <label>`
   refreshes an expired access token.
4. `accounts status` reports a forced `set` target even when its cached quota is
   exhausted; the runtime follows the same rule.
