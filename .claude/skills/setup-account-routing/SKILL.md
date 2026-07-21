---
name: setup-account-routing
description: Set up the accounts router (multi-account routing for Claude Code) on top of the base statusline — account labels, credential store, minted launch tokens, zsh wrapper, route daemon.
---

# Setup: account routing (accounts)

Multi-account routing for Claude Code: a headroom board across accounts,
automatic switching when the live account runs low, launch-time routing to the
freshest account, and pin-follows-login. The command is `accounts`. Run
`/setup-statusline` first — the two share `~/.claude/statusline.conf`.

Two independent layers; install both for full coverage:

- **Route daemon** — rewrites `~/.claude/.credentials.json` so the *running*
  session switches accounts mid-flight.
- **Launch injection** — the `claude()` zsh wrapper asks `accounts pick-env`
  for the best account at launch and injects `CLAUDE_CODE_OAUTH_TOKEN`.

## Hard rules

- The router NEVER writes the live keychain slot `Claude Code-credentials`
  (file writes + sanctioned deletion only). Do not "fix" that; it prevents
  the keychain prompt-storm failure class.
- Never display or log token values. `~/.accounts/` contents stay out of
  transcripts and commits.

## Steps

1. **Label the accounts** in `~/.claude/statusline.conf`:

   ```bash
   ACCOUNT_LABELS="alumni:*@alumni.example.edu gmail:me@gmail.com work:me@corp.com|org-uuid"
   LABEL_COLORS="alumni:green gmail:magenta work:cyan"   # optional
   SHOW_ACCOUNT_RESETS=1                                # board row in the statusline
   ```

   Labels must match what you see in the email address — no internal
   codenames. Same email in two orgs → two labels with `|org-uuid` suffixes.

2. **Install the CLI:**

   ```bash
   ln -sf "$REPO/bin/accounts.py" ~/.local/bin/accounts
   ```

3. **Seed each account into the store.** In Claude Code, `/login` as the
   account, then:

   ```bash
   accounts status
   ```

   Any `accounts` command captures the live credential into
   `~/.accounts/blobs.json` under its label. Repeat per account. (That is the
   whole onboarding — there is no enroll step.)

4. **Mint launch tokens** (enables launch-time routing; ~1-year lifetime,
   values are vaulted, never displayed):

   ```bash
   accounts mint <label>     # per account; approves in the browser
   accounts tokens           # inventory + expiry
   ```

5. **Add the launch wrapper** to `~/.zshrc` (canonical copy — the repo is the
   source of truth for this block):

   ```zsh
   # >>> accounts auto-route (env-token injection; zero keychain) >>>
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

   Pin one launch: `ACCOUNTS_PIN=<label> claude`. No minted tokens → the
   wrapper falls through to native auth unchanged.

6. **Install the route daemon:**

   ```bash
   cp "$REPO/macos/launchd/com.claude-accounts-route.plist.example" \
      ~/Library/LaunchAgents/com.claude-accounts-route.plist
   # edit ProgramArguments so the path points at YOUR clone's bin/accounts.py
   launchctl load ~/Library/LaunchAgents/com.claude-accounts-route.plist
   ```

7. **Pick a mode:**

   ```bash
   accounts auto           # route to the freshest account when the live one runs low
   accounts fable          # prefer a Fable-capable account; fall back to normal routing
   accounts set <label>    # pin and hold
   ```

   - A fresh `/login` always moves the pin to that account — the daemon adopts
     your explicit choice instead of fighting it.
   - `fable` keeps you on whichever account has the most Fable (premium weekly)
     headroom and degrades to normal 5h routing when none can do Fable.

## Verify

```bash
accounts ls                         # board: 5h / 7d / fable per account
accounts status                     # mode line + live account
tail -5 ~/.claude/accounts-mirror.log    # every switch is logged with a reason
```

The statusline board row appears once `SHOW_ACCOUNT_RESETS=1` and at least one
poll has run.

## When routing misbehaves

1. `accounts status` — a surprising `SET → <label>` explains "why am I on X".
2. `accounts auto` releases a pin; `accounts set <label>` moves it.
3. Still fighting you: `launchctl unload ~/Library/LaunchAgents/com.claude-accounts-route.plist`,
   then `/login` sticks unconditionally; `load` re-arms.
4. `~/.claude/accounts-mirror.log` names every write (`SET`, `ROUTED`, `ADOPT`).
