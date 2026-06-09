#!/usr/bin/env bash
# archive-transcripts.sh — append-only mirror of ~/.claude/projects/.
#
# rsync without --delete: growing session files are updated in place, and a
# file deleted from the live tree (retention, migration, format change) stays
# in the archive forever. Pair with usage-ledger.py: the ledger keeps the
# numbers, this keeps the raw transcripts.
#
# Usage: archive-transcripts.sh [dest]   (default ~/claude-transcript-archive/projects)
set -euo pipefail

SRC="${CLAUDE_DIR:-$HOME/.claude}/projects/"
DEST="${1:-$HOME/claude-transcript-archive/projects}"
LOG="$(dirname "$DEST")/archive.log"

mkdir -p "$DEST"
rsync -a "$SRC" "$DEST/"

files=$(find "$DEST" -name '*.jsonl' | wc -l | tr -d ' ')
size=$(du -sh "$DEST" | awk '{print $1}')
printf '[%s] archived: %s files, %s\n' "$(date +'%Y-%m-%d %H:%M:%S')" "$files" "$size" >> "$LOG"
