---
name: setup-account-routing
description: Set up native-profile multi-account routing for Claude Code — account labels, isolated config profiles, headroom selection, and the zsh wrapper.
---

# Setup: account routing (accounts)

Multi-account routing for Claude Code. Each account gets an isolated native
`CLAUDE_CONFIG_DIR`; new sessions choose a profile by headroom while running
sessions keep their selected account. Run `/setup-statusline` first — both tools
share `~/.claude/statusline.conf`.

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
   SHOW_ACCOUNT_RESETS=1
   ```

   Labels must match the email address shown by Claude. Same email in two
   organizations requires `|org-uuid` suffixes.

2. **Install the CLI:**

   ```bash
   ln -sf "$REPO/bin/accounts.py" ~/.local/bin/accounts
   ```

3. **Seed each account into the store.** Bypass the wrapper with `command claude`,
   `/login` as the account, exit Claude, then run:

   ```bash
   accounts status
   ```

   Repeat for each account. `accounts status` captures Claude's default-profile
   login into `~/.accounts/blobs.json`; routed launches subsequently keep refreshed
   credentials synchronized from their native profiles.

4. **Add the launch wrapper** to `~/.zshrc`:

   ```zsh
   # >>> accounts auto-route (native profiles) >>>
   claude() {
     local _bin _env _mode _k
     _bin="$(whence -p claude)" || { print -u2 "claude binary not found"; return 127; }
     case " $* " in
       *" -p "*|*" --print "*|*" --version "*|*" --help "*|*" setup-token "*|*" -c "*|*" --continue "*|*" --resume "*)
         _env="$("$HOME/.local/bin/accounts" pick-env 2>/dev/null)" || _env=""
         ( [ -n "$_env" ] && eval "$_env"; exec "$_bin" "$@" )
         return $? ;;
     esac
     _mode="start"
     while true; do
       _env="$("$HOME/.local/bin/accounts" pick-env 2>/dev/null)" || _env=""
       ( [ -n "$_env" ] && eval "$_env"
         [ -n "${ACCOUNTS_ROUTED_LABEL:-}" ] && print -u2 "accounts → $ACCOUNTS_ROUTED_LABEL"
         if [ "$_mode" = "start" ]; then exec "$_bin" "$@"; else exec "$_bin" --continue; fi )
       printf 'accounts: [Enter] resume this conversation on the freshest account · anything else quits  '
       _k=""
       read -t 5 -k 1 _k 2>/dev/null || _k="q"
       printf '\n'
       case "$_k" in
         $'\n'|$'') _mode="continue" ;;
         *) break ;;
       esac
     done
   }
   # <<< accounts auto-route <<<
   ```

   Pin one launch with `ACCOUNTS_PIN=<label> claude`. If no account is eligible,
   the wrapper clears inherited routing variables and Claude uses its default
   native login.

5. **Pick a mode:**

   ```bash
   accounts auto           # each new session uses the freshest account
   accounts fable          # prefer a Fable-capable account
   accounts set <label>    # pin new sessions
   ```

   Mode changes affect new and restarted sessions. Running sessions keep their
   current profile; exit and resume to reroute the conversation.

6. **Optional headless-job tokens:**

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
4. Exit and resume the Claude session after changing modes.
