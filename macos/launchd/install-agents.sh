#!/usr/bin/env bash
# install-agents.sh — install every background agent the statusline depends on.
#
# The rows these feed look merely empty when nothing is polling, so a machine
# that skipped this step reads as working: the account board silently goes
# stale and routing then decides on old data, and the lifetime figure sits at
# zero forever. Setup installs them rather than leaving them optional.
#
# Usage:
#     macos/launchd/install-agents.sh           # install/reload all
#     macos/launchd/install-agents.sh --remove  # unload and delete all
set -uo pipefail

LAUNCHD_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACTION="${1:-install}"

# launchctl registers against the live login session, not $HOME, so a run with
# a sandboxed HOME would unload the real agents and replace them with plists
# pointing into a temp dir.
if [ -n "${STATUSLINE_SKIP_AGENTS:-}" ]; then
    echo "STATUSLINE_SKIP_AGENTS set — skipping launchd agents"
    exit 0
fi
if [ "$(uname)" != "Darwin" ]; then
    echo "launchd agents are macOS-only — skipping"
    exit 0
fi
if ! command -v launchctl >/dev/null 2>&1; then
    echo "launchctl unavailable — skipping"
    exit 0
fi

failed=0

run() {
    local name="$1" script="$2"
    if ! "$LAUNCHD_DIR/$script" "$ACTION"; then
        echo "[!] $name failed — continuing" >&2
        failed=1
    fi
}

# fswatch is the watcher's only external dependency; without it launchd would
# crash-loop the agent every 10s instead of reporting one clear failure.
if [ "$ACTION" = "install" ] && ! command -v fswatch >/dev/null 2>&1; then
    echo "[!] fswatch not installed — skipping token-scan watcher"
    echo "    brew install fswatch, then re-run this script"
else
    run "token-scan watcher" install-daemon.sh
fi

run "account-board poller" install-accounts-poll.sh
run "usage-ledger fold" install-usage-ledger.sh

exit "$failed"
