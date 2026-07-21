#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
CONFIG_FILE="$CODEX_DIR/statusline.conf"

removed=0
for name in codex-config.sh codex-statusline codex-top codex-watch codex_statusline.py; do
    target="$BIN_DIR/$name"
    if [ -L "$target" ] && [ "$(readlink "$target" 2>/dev/null || true)" = "$SRC_DIR/bin/$name" ]; then
        rm "$target"
        removed=$((removed + 1))
    fi
done

if [ "$removed" -gt 0 ]; then
    printf "Removed %d Codex monitor command(s) from %s\n" "$removed" "$BIN_DIR"
else
    printf "No managed Codex monitor commands found in %s\n" "$BIN_DIR"
fi
printf "Leaving %s in place (your config).\n" "$CONFIG_FILE"
