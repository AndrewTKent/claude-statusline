#!/bin/bash
# Claude Code Status Line — One-liner Installer
# Usage: curl -fsSL https://raw.githubusercontent.com/AndrewTKent/claude-statusline/main/install.sh | bash
set -euo pipefail

REPO="AndrewTKent/claude-statusline"
BRANCH="main"
INSTALL_DIR="$HOME/.claude"
SCRIPT_NAME="statusline.sh"
CONF_NAME="statusline.conf"
SETTINGS_FILE="$INSTALL_DIR/settings.json"

# Colors (respects NO_COLOR)
if [ -z "${NO_COLOR:-}" ] && [ -t 1 ]; then
    green='\033[32m'; red='\033[31m'; yellow='\033[33m'; dim='\033[2m'; reset='\033[0m'
else
    green=''; red=''; yellow=''; dim=''; reset=''
fi

info()  { printf "${green}[+]${reset} %s\n" "$1"; }
warn()  { printf "${yellow}[!]${reset} %s\n" "$1"; }
error() { printf "${red}[x]${reset} %s\n" "$1" >&2; exit 1; }

# ── Preflight ──────────────────────────────────────────
command -v curl >/dev/null 2>&1 || error "curl is required but not installed"
command -v jq >/dev/null 2>&1 || error "jq is required. Install with: brew install jq (macOS) or sudo apt install jq (Linux)"
[ -d "$INSTALL_DIR" ] || error "~/.claude/ directory not found. Install Claude Code first: https://claude.ai/code"

# ── Download script ────────────────────────────────────
info "Downloading statusline.sh..."
SCRIPT_URL="https://raw.githubusercontent.com/$REPO/$BRANCH/bin/statusline.sh"
curl -fsSL "$SCRIPT_URL" -o "$INSTALL_DIR/$SCRIPT_NAME"
chmod +x "$INSTALL_DIR/$SCRIPT_NAME"
info "Installed $INSTALL_DIR/$SCRIPT_NAME"

# ── Create default config ──────────────────────────────
if [ ! -f "$INSTALL_DIR/$CONF_NAME" ]; then
    CONF_URL="https://raw.githubusercontent.com/$REPO/$BRANCH/config/statusline.conf.example"
    curl -fsSL "$CONF_URL" -o "$INSTALL_DIR/$CONF_NAME"
    info "Created default config at $INSTALL_DIR/$CONF_NAME"
else
    warn "Config already exists at $INSTALL_DIR/$CONF_NAME — skipping"
fi

# ── Patch settings.json ────────────────────────────────
STATUSLINE_CONFIG='{"type":"command","command":"~/.claude/statusline.sh"}'

if [ ! -f "$SETTINGS_FILE" ]; then
    echo "{\"statusLine\":$STATUSLINE_CONFIG}" | jq . > "$SETTINGS_FILE"
    info "Created $SETTINGS_FILE with statusLine config"
elif jq -e '.statusLine' "$SETTINGS_FILE" >/dev/null 2>&1; then
    CURRENT_CMD=$(jq -r '.statusLine.command // ""' "$SETTINGS_FILE")
    if [ "$CURRENT_CMD" = "~/.claude/statusline.sh" ]; then
        info "settings.json already configured — skipping"
    else
        warn "settings.json has a different statusLine command: $CURRENT_CMD"
        warn "Not overwriting. To use claude-statusline, manually set:"
        printf "${dim}  \"statusLine\": {\"type\": \"command\", \"command\": \"~/.claude/statusline.sh\"}${reset}\n"
    fi
else
    # Backup, then merge
    cp "$SETTINGS_FILE" "$SETTINGS_FILE.bak"
    local_tmp=$(mktemp "$SETTINGS_FILE.XXXXXX")
    jq --argjson sl "$STATUSLINE_CONFIG" '. + {statusLine: $sl}' "$SETTINGS_FILE" > "$local_tmp"
    # Validate
    if jq . "$local_tmp" >/dev/null 2>&1; then
        mv "$local_tmp" "$SETTINGS_FILE"
        info "Updated settings.json (backup: settings.json.bak)"
    else
        rm -f "$local_tmp"
        error "Failed to patch settings.json — backup preserved at settings.json.bak"
    fi
fi

# ── Verify ─────────────────────────────────────────────
OUTPUT=$(echo '' | "$INSTALL_DIR/$SCRIPT_NAME" 2>/dev/null)
if [ "$OUTPUT" = "Claude" ]; then
    info "Verified: script runs correctly"
else
    warn "Script ran but output was unexpected: $OUTPUT"
fi

# ── Done ───────────────────────────────────────────────
printf "\n${green}Done!${reset} Restart Claude Code to see the status line.\n"
printf "\nConfigure: edit ${dim}~/.claude/statusline.conf${reset}\n"
printf "  ${dim}HOURLY_RATE=150${reset}    # enables cost tracking\n"
printf "  ${dim}DAILY_BUDGET=20${reset}    # enables budget bar\n"
printf "  ${dim}FORMAT=sigil${reset}       # single-line compact mode\n"
