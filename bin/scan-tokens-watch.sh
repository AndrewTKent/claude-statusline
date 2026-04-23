#!/usr/bin/env bash
# scan-tokens-watch.sh — pipe fswatch events into scan-tokens-daemon.py.
#
# This used to invoke scan-tokens.py as a subprocess on every event, which
# meant a fresh Python interpreter + full tree walk + full re-aggregate per
# file change. Under active streaming that pegged a core and paged the
# machine. The daemon holds state in memory and updates incrementally, so
# this script's only job is to shuttle events in.
#
# Env:
#   CLAUDE_DIR        — defaults to $HOME/.claude
#   SCAN_CONFIG       — path to statusline.conf, sourced before exec
#                       (defaults to $CLAUDE_DIR/statusline.conf)
#   SCAN_DEBOUNCE_MS  — fswatch --latency, in ms (default 500)
#   PYTHON_BIN        — python interpreter (default /opt/homebrew/bin/python3)
#   SCAN_VERBOSE      — if set to "1", daemon logs to stderr

set -uo pipefail

CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
SCAN_CONFIG="${SCAN_CONFIG:-$CLAUDE_DIR/statusline.conf}"
WATCH_DIR="$CLAUDE_DIR/projects"
SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON="$SCRIPT_DIR/scan-tokens-daemon.py"
DEBOUNCE_MS="${SCAN_DEBOUNCE_MS:-500}"
PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3}"

if [ ! -d "$WATCH_DIR" ]; then
    echo "watch dir missing: $WATCH_DIR" >&2
    exit 1
fi
if ! command -v fswatch >/dev/null 2>&1; then
    echo "fswatch not installed; brew install fswatch" >&2
    exit 1
fi
if [ ! -x "$DAEMON" ]; then
    echo "daemon not found or not executable: $DAEMON" >&2
    exit 1
fi
if [ ! -x "$PYTHON_BIN" ]; then
    echo "python interpreter not found: $PYTHON_BIN" >&2
    exit 1
fi

# Source statusline.conf so classification env vars flow through to the daemon.
if [ -f "$SCAN_CONFIG" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$SCAN_CONFIG"
    set +a
fi

LATENCY_S=$(awk -v ms="$DEBOUNCE_MS" 'BEGIN { printf "%.3f", ms/1000 }')

daemon_arg=""
if [ "${SCAN_VERBOSE:-}" = "1" ]; then
    daemon_arg="--verbose"
fi

# fswatch options:
#   -0                        NUL-separated paths (safe for spaces/newlines)
#   -r                        recursive
#   --latency                 batch events within N seconds
#   --event Updated/Created   skip uninteresting events (attrs, platform noise)
#   --include '\.jsonl$'      only JSONL files
#   --exclude '.*'            exclude everything else
#
# Piping fswatch directly into the daemon keeps the event path in-kernel +
# one pipe. The daemon reads NUL-delimited paths from stdin and handles
# debounce/flush internally.
if [ -n "$daemon_arg" ]; then
    fswatch -0 -r --latency "$LATENCY_S" \
        --event Updated --event Created \
        --include '\.jsonl$' --exclude '.*' \
        "$WATCH_DIR" \
        | "$PYTHON_BIN" "$DAEMON" "$daemon_arg"
else
    fswatch -0 -r --latency "$LATENCY_S" \
        --event Updated --event Created \
        --include '\.jsonl$' --exclude '.*' \
        "$WATCH_DIR" \
        | "$PYTHON_BIN" "$DAEMON"
fi
