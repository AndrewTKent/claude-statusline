#!/usr/bin/env bash
# archive-transcripts.sh — append-only mirror of Claude Code JSONL transcripts.
#
# rsync without --delete: growing session files are updated in place, and a
# file deleted from the live tree stays in the archive forever.
#
# Usage: archive-transcripts.sh [dest]   (default ~/claude-transcript-archive/projects)
set -euo pipefail

SRC="${CLAUDE_DIR:-$HOME/.claude}/projects/"
DEST="${1:-$HOME/claude-transcript-archive/projects}"
LOG="$(dirname "$DEST")/archive.log"

mkdir -p "$DEST"
rsync -a -m \
    --include='*/' \
    --include='*.jsonl' \
    --exclude='*' \
    "$SRC" "$DEST/"

files=$(find "$DEST" -name '*.jsonl' | wc -l | tr -d ' ')
size=$(du -sh "$DEST" | awk '{print $1}')
printf '[%s] archived: %s files, %s\n' "$(date +'%Y-%m-%d %H:%M:%S')" "$files" "$size" >> "$LOG"

# Set ARCHIVE_REMOTE_HOST to enable an append-only remote mirror.
REMOTE_HOST="${ARCHIVE_REMOTE_HOST-}"
SSH_KEY="${ARCHIVE_SSH_KEY:-$HOME/.ssh/id_ed25519}"
if [ -n "$REMOTE_HOST" ]; then
    printf -v RSYNC_SSH "ssh -i '%s' -o BatchMode=yes -o ConnectTimeout=8" "$SSH_KEY"
    if ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=8 "$REMOTE_HOST" \
            'mkdir -p ~/claude-transcript-archive/projects && chmod 700 ~/claude-transcript-archive' 2>>"$LOG" \
       && rsync -a -m \
            --include='*/' \
            --include='*.jsonl' \
            --exclude='*' \
            -e "$RSYNC_SSH" --timeout=300 \
            "$DEST/" "$REMOTE_HOST:claude-transcript-archive/projects/" 2>>"$LOG"; then
        printf '[%s] %s mirror ok\n' "$(date +'%Y-%m-%d %H:%M:%S')" "$REMOTE_HOST" >> "$LOG"
    else
        printf '[%s] %s mirror FAILED — local archive only this run\n' "$(date +'%Y-%m-%d %H:%M:%S')" "$REMOTE_HOST" >> "$LOG"
    fi
fi
