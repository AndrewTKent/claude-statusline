#!/bin/bash
# Claude Code Status Line — Uninstaller
set -euo pipefail

INSTALL_DIR="$HOME/.claude"
SETTINGS_FILE="$INSTALL_DIR/settings.json"

green='\033[32m'; yellow='\033[33m'; reset='\033[0m'
[ -n "${NO_COLOR:-}" ] && { green=''; yellow=''; reset=''; }

info() { printf "${green}[+]${reset} %s\n" "$1"; }
warn() { printf "${yellow}[!]${reset} %s\n" "$1"; }

# Remove script
if [ -f "$INSTALL_DIR/statusline.sh" ]; then
    rm "$INSTALL_DIR/statusline.sh"
    info "Removed ~/.claude/statusline.sh"
else
    warn "~/.claude/statusline.sh not found — skipping"
fi

# Remove statusLine from settings.json
if [ -f "$SETTINGS_FILE" ] && jq -e '.statusLine' "$SETTINGS_FILE" >/dev/null 2>&1; then
    cp "$SETTINGS_FILE" "$SETTINGS_FILE.bak"
    tmp=$(mktemp "$SETTINGS_FILE.XXXXXX")
    jq 'del(.statusLine)' "$SETTINGS_FILE" > "$tmp" && mv "$tmp" "$SETTINGS_FILE"
    info "Removed statusLine from settings.json (backup: settings.json.bak)"
fi

# Clean up runtime files
rm -f /tmp/claude/statusline-*.json
rm -f "$INSTALL_DIR/rprompt.txt"
rm -f "$INSTALL_DIR/session-history.jsonl"
info "Cleaned up runtime files"

# Ask about config
if [ -f "$INSTALL_DIR/statusline.conf" ]; then
    if [ -t 0 ]; then
        printf "Keep ~/.claude/statusline.conf? [Y/n] "
        read -r answer
        case "$answer" in
            [nN]*) rm "$INSTALL_DIR/statusline.conf"; info "Removed config" ;;
            *)     info "Kept config at ~/.claude/statusline.conf" ;;
        esac
    else
        warn "Kept ~/.claude/statusline.conf — remove manually if desired"
    fi
fi

printf "\n${green}Uninstalled.${reset} Restart Claude Code to take effect.\n"
