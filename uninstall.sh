#!/bin/bash
# Claude Code Status Line — Uninstaller
set -euo pipefail

INSTALL_DIR="$HOME/.claude"
SETTINGS_FILE="$INSTALL_DIR/settings.json"
PURGE_FLAG="${1:-}"

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
rm -f /tmp/claude/statusline-*
rm -f /tmp/claude/ctx-history-*.txt
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

# Handle --purge flag
if [ "$PURGE_FLAG" = "--purge" ]; then
    printf "${yellow}[*]${reset} Purging state files and launchd agents...\n"
    rm -f "$INSTALL_DIR/daily-cost.json"
    rm -f "$INSTALL_DIR/daily-tokens.json"
    rm -f "$INSTALL_DIR/token-scan-cache.json"
    rm -f "$INSTALL_DIR/token-scan-summary.json"
    rm -f "$INSTALL_DIR/account-resets.json"
    rm -f "$INSTALL_DIR/utilization-history.jsonl"
    rm -f "$INSTALL_DIR/account-caps.json"
    rm -f "$INSTALL_DIR/statusline-tz"
    rm -f "$INSTALL_DIR/focus"
    info "Removed state files"

    for agent in "com.claude-scan-tokens-watch" "com.claude-scan-tokens" "com.claude-accounts-route"; do
        if launchctl list "$agent" >/dev/null 2>&1; then
            launchctl unload "$HOME/Library/LaunchAgents/${agent}.plist" 2>/dev/null || true
            rm -f "$HOME/Library/LaunchAgents/${agent}.plist"
            info "Unloaded and removed $agent"
        fi
    done

    printf "\n${green}Purged.${reset} All state files and launchd agents removed.\n"
else
    printf "\n${yellow}State files remain:${reset}\n"
    printf "  ~/.claude/daily-cost.json\n"
    printf "  ~/.claude/daily-tokens.json\n"
    printf "  ~/.claude/token-scan-{cache,summary}.json\n"
    printf "  ~/.claude/account-resets.json\n"
    printf "  ~/.claude/utilization-history.jsonl\n"
    printf "  ~/.claude/account-caps.json\n"
    printf "  ~/.claude/statusline-tz\n"
    printf "  ~/.claude/focus\n"
    printf "  ~/Library/LaunchAgents/com.claude-scan-tokens-watch.plist\n"
    printf "  ~/Library/LaunchAgents/com.claude-scan-tokens.plist\n"
    printf "  ~/Library/LaunchAgents/com.claude-accounts-route.plist\n"
    if [ -f "$0" ]; then
        purge_hint="$0 --purge"
    else
        purge_hint="curl -fsSL https://raw.githubusercontent.com/AndrewTKent/statusline/main/uninstall.sh | bash -s -- --purge"
    fi
    printf "\n${green}Uninstalled.${reset} Run ${green}%s${reset} to remove state files.\n" "$purge_hint"
    printf "Restart Claude Code to take effect.\n"
fi
