#!/usr/bin/env bash
# install-agent-metrics.sh — install (or reinstall) the Agent Metrics recorder
# as a launchd agent. Idempotent: unloads any existing agent first.
#
# Usage:
#     macos/launchd/install-agent-metrics.sh           # install/reload
#     macos/launchd/install-agent-metrics.sh --remove  # unload and delete plist
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LABEL="com.claude-agent-metrics-watch"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
TEMPLATE="$REPO_ROOT/macos/launchd/${LABEL}.plist.template"

unload_if_loaded() {
    if launchctl list | awk '{print $3}' | grep -qx "$LABEL"; then
        launchctl unload "$PLIST" 2>/dev/null || true
    fi
}

remove() {
    unload_if_loaded
    rm -f "$PLIST"
    echo "Removed $LABEL"
}

install() {
    if [ ! -f "$TEMPLATE" ]; then
        echo "template missing: $TEMPLATE" >&2
        exit 1
    fi
    if [ ! -x "$REPO_ROOT/bin/agent-metrics" ]; then
        echo "agent-metrics missing or not executable" >&2
        exit 1
    fi

    mkdir -p "$HOME/Library/LaunchAgents"
    unload_if_loaded

    # Render template with escaped substitutions — avoids issues if HOME
    # contains characters special to sed.
    tmp=$(mktemp "$PLIST.XXXXXX")
    python3 - "$TEMPLATE" "$tmp" "$REPO_ROOT" "$HOME" <<'PY'
import sys, pathlib
src, dst, repo_root, home = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
content = pathlib.Path(src).read_text()
content = content.replace("__REPO_ROOT__", repo_root).replace("__HOME__", home)
pathlib.Path(dst).write_text(content)
PY
    mv "$tmp" "$PLIST"
    launchctl load "$PLIST"
    echo "Installed and loaded $LABEL"
    echo "  plist:    $PLIST"
    echo "  recorder: $REPO_ROOT/bin/agent-metrics watch --interval 60 --max-lines 5000"
    echo "  logs:     $HOME/.claude/agent-metrics-watch-std{out,err}.log"
}

case "${1:-install}" in
    --remove|remove) remove ;;
    install|*) install ;;
esac
