#!/usr/bin/env bash
# scan-tokens-watch.sh — trigger scan-tokens.py within ~1s of any JSONL change.
#
# Uses fswatch to watch ~/.claude/projects/ for writes. Debounces rapid events
# (Claude Code streaming writes many lines per second during a response) so we
# don't re-scan every single append. Calls scan-tokens.py with --incremental,
# which only re-parses files whose mtime changed since the last run.
#
# Designed to run as a long-lived launchd agent (see install.sh).
#
# Env:
#   CLAUDE_DIR              — defaults to $HOME/.claude
#   SCAN_DEBOUNCE_MS        — defaults to 500ms
#   SCAN_CONFIG             — path to statusline.conf (sourced before scan);
#                             defaults to $HOME/.claude/statusline.conf

set -uo pipefail

CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
SCAN_CONFIG="${SCAN_CONFIG:-$CLAUDE_DIR/statusline.conf}"
WATCH_DIR="$CLAUDE_DIR/projects"
SCAN_SCRIPT="${BASH_SOURCE[0]%/*}/scan-tokens.py"
DEBOUNCE_MS="${SCAN_DEBOUNCE_MS:-500}"

if [ ! -d "$WATCH_DIR" ]; then
    echo "watch dir missing: $WATCH_DIR" >&2
    exit 1
fi
if ! command -v fswatch >/dev/null 2>&1; then
    echo "fswatch not installed; brew install fswatch" >&2
    exit 1
fi
if [ ! -x "$SCAN_SCRIPT" ]; then
    echo "scan script not found/executable: $SCAN_SCRIPT" >&2
    exit 1
fi

run_scan() {
    # Source config so WORK_PATHS etc. flow through.
    if [ -f "$SCAN_CONFIG" ]; then
        set -a
        # shellcheck disable=SC1090
        source "$SCAN_CONFIG"
        set +a
    fi
    /opt/homebrew/bin/python3 "$SCAN_SCRIPT" --quiet 2>/dev/null || true
}

# Initial scan on startup so we don't wait for a JSONL change.
run_scan

# fswatch with latency (batch events within that window). fswatch already
# debounces via its --latency flag, so a separate sleep loop isn't needed —
# we convert ms to seconds for fswatch.
LATENCY_S=$(awk -v ms="$DEBOUNCE_MS" 'BEGIN { printf "%.3f", ms/1000 }')

# -r: recursive
# -0: null-separated paths (handles spaces)
# --latency: batch events within N seconds
# --event Updated --event Created: only care about writes
# --include '\.jsonl$': only JSONL files
# --exclude '.*': exclude everything else
fswatch \
    -0 \
    -r \
    --latency "$LATENCY_S" \
    --event Updated \
    --event Created \
    --include '\.jsonl$' \
    --exclude '.*' \
    "$WATCH_DIR" | while IFS= read -r -d '' _; do
    # Drain any remaining queued events in this batch (fswatch already batched,
    # but reading one and sleeping briefly lets a burst fully settle before we
    # re-scan — avoids re-scanning in the middle of a multi-file streaming burst).
    run_scan
done
