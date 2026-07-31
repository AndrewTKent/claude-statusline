#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
native_claude="$HOME/.local/bin/claude"
local_bin="$HOME/.local/bin"
install_root="$HOME/.local/lib/statusline"
router_bin="$HOME/.accounts/bin"
zshrc="${ZDOTDIR:-$HOME}/.zshrc"
path_line='export PATH="$HOME/.accounts/bin:$PATH"'

versions_dir="$HOME/.local/share/claude/versions"
if ! ls "$versions_dir"/* >/dev/null 2>&1; then
    printf 'No Claude Code native binary under %s\n' "$versions_dir" >&2
    exit 1
fi

mkdir -p "$local_bin" "$install_root" "$router_bin" "$(dirname -- "$zshrc")"
for script in accounts.py claude-router.py; do
    temp="$install_root/.$script.$$"
    cp "$repo_root/bin/$script" "$temp"
    chmod 755 "$temp"
    mv -f "$temp" "$install_root/$script"
done
ln -sfn "$install_root/accounts.py" "$local_bin/accounts"
ln -sfn "$install_root/claude-router.py" "$local_bin/claude-router"
cp "$repo_root/shell/claude-supervised" "$local_bin/claude-supervised"
chmod 755 "$local_bin/claude-supervised"
ln -sfn "$local_bin/claude-supervised" "$router_bin/claude"

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

printf 'Installed supervised Claude launcher at %s and %s\n' "$native_claude" "$router_bin/claude"
printf 'Open a new shell or run: %s\n' "$path_line"
