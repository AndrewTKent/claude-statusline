#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
native_claude="$HOME/.local/bin/claude"
local_bin="$HOME/.local/bin"
router_bin="$HOME/.accounts/bin"
zshrc="${ZDOTDIR:-$HOME}/.zshrc"
path_line='export PATH="$HOME/.accounts/bin:$PATH"'

if [ ! -x "$native_claude" ]; then
    printf 'Claude Code native binary not found at %s\n' "$native_claude" >&2
    exit 1
fi

mkdir -p "$local_bin" "$router_bin" "$(dirname -- "$zshrc")"
ln -sfn "$repo_root/bin/accounts.py" "$local_bin/accounts"
ln -sfn "$repo_root/bin/claude-router.py" "$local_bin/claude-router"
cp "$repo_root/shell/claude-supervised" "$local_bin/claude-supervised"
chmod 755 "$local_bin/claude-supervised"
ln -sfn "$local_bin/claude-supervised" "$router_bin/claude"

touch "$zshrc"
if ! grep -Fqx "$path_line" "$zshrc"; then
    printf '\n%s\n' "$path_line" >> "$zshrc"
fi

printf 'Installed supervised Claude launcher at %s\n' "$router_bin/claude"
printf 'Open a new shell or run: %s\n' "$path_line"
