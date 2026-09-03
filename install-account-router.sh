#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
native_claude="$HOME/.local/bin/claude"
local_bin="$HOME/.local/bin"
install_root="$HOME/.local/lib/statusline"
router_bin="$HOME/.accounts/bin"
codex_router_bin="$HOME/.codex-accounts/bin"
zshrc="${ZDOTDIR:-$HOME}/.zshrc"
path_line='export PATH="$HOME/.accounts/bin:$PATH"'
codex_path_line='export PATH="$HOME/.codex-accounts/bin:$PATH"'

versions_dir="$HOME/.local/share/claude/versions"
if ! ls "$versions_dir"/* >/dev/null 2>&1; then
    printf 'No Claude Code native binary under %s\n' "$versions_dir" >&2
    exit 1
fi

mkdir -p "$local_bin" "$install_root" "$router_bin" "$codex_router_bin" "$(dirname -- "$zshrc")"
for script in accounts.py claude-router.py codex_accounts.py codex-router.py codex-account-session.py; do
    temp="$install_root/.$script.$$"
    cp "$repo_root/bin/$script" "$temp"
    chmod 755 "$temp"
    mv -f "$temp" "$install_root/$script"
done
ln -sfn "$install_root/accounts.py" "$local_bin/accounts"
ln -sfn "$install_root/claude-router.py" "$local_bin/claude-router"
ln -sfn "$install_root/codex_accounts.py" "$local_bin/codex-accounts"
ln -sfn "$install_root/codex-router.py" "$local_bin/codex-router"
ln -sfn "$install_root/codex-account-session.py" "$local_bin/codex-account-session"
cp "$repo_root/shell/claude-supervised" "$local_bin/claude-supervised"
chmod 755 "$local_bin/claude-supervised"
ln -sfn "$local_bin/claude-supervised" "$router_bin/claude"
cp "$repo_root/shell/codex-supervised" "$local_bin/codex-supervised"
chmod 755 "$local_bin/codex-supervised"
ln -sfn "$local_bin/codex-supervised" "$codex_router_bin/codex"

# Own the chokepoint: every launcher ultimately execs ~/.local/bin/claude,
# so the wrapper there catches stale shells and scripts that bypass zsh.
temp="$local_bin/.claude.$$"
cp "$repo_root/shell/claude-supervised" "$temp"
chmod 755 "$temp"
mv -f "$temp" "$native_claude"

touch "$zshrc"
if ! grep -Fqx "$path_line" "$zshrc"; then
    printf '\n%s\n' "$path_line" >> "$zshrc"
fi
if ! grep -Fqx "$codex_path_line" "$zshrc"; then
    printf '\n%s\n' "$codex_path_line" >> "$zshrc"
fi

printf 'Installed supervised Claude launcher at %s and %s\n' "$native_claude" "$router_bin/claude"
printf 'Installed supervised Codex launcher at %s\n' "$codex_router_bin/codex"
printf 'Open a new shell or run: %s\n' "$path_line"
printf 'Then run: %s\n' "$codex_path_line"

# Routing quality depends on a fresh board, so the pollers ship with the router
# rather than as a step someone can skip.
"$repo_root/macos/launchd/install-agents.sh" || \
    printf 'Background agents did not fully install — run macos/launchd/install-agents.sh\n' >&2
