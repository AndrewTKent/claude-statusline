#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
CONFIG_FILE="$CODEX_DIR/statusline.conf"

if [ ! -d "$CODEX_DIR" ]; then
    printf "Codex home not found: %s\n" "$CODEX_DIR" >&2
    exit 1
fi

command -v python3 >/dev/null 2>&1 || {
    printf "python3 is required\n" >&2
    exit 1
}
command -v tmux >/dev/null 2>&1 || {
    printf "tmux is required\n" >&2
    exit 1
}
if [ -n "${CODEX_STATUSLINE_CODEX_BIN:-}" ]; then
    [ -x "$CODEX_STATUSLINE_CODEX_BIN" ] || {
        printf "Codex CLI is not executable: %s\n" "$CODEX_STATUSLINE_CODEX_BIN" >&2
        exit 1
    }
elif ! command -v codex >/dev/null 2>&1; then
    printf "Codex CLI is required\n" >&2
    exit 1
fi

mkdir -p "$BIN_DIR"
for name in codex-config.sh codex-statusline codex-top codex-watch codex_statusline.py; do
    target="$BIN_DIR/$name"
    if [ -e "$target" ] && [ "$(readlink "$target" 2>/dev/null || true)" != "$SRC_DIR/bin/$name" ]; then
        printf "Skipping %s: existing file is not managed by this repo (remove it and re-run)\n" "$target" >&2
        continue
    fi
    ln -sf "$SRC_DIR/bin/$name" "$target"
done

if [ ! -f "$CONFIG_FILE" ]; then
    cp "$SRC_DIR/config/codex-statusline.conf.example" "$CONFIG_FILE"
fi

printf "Installed Codex monitor commands in %s\n" "$BIN_DIR"
case ":$PATH:" in
    *":$BIN_DIR:"*) printf "Run: codex-statusline\n" ;;
    *) printf "Run: %s/codex-statusline (add %s to PATH for the short command)\n" "$BIN_DIR" "$BIN_DIR" ;;
esac
printf "uninstall: ./uninstall-codex.sh\n"
