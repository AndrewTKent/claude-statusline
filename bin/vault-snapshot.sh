#!/usr/bin/env bash
# vault-snapshot.sh — encrypted snapshot of the transcript archive into the
# local restic vault, plus a best-effort replica of the (already encrypted)
# repo to remote-host.
#
# Quantum-safety: restic is symmetric-only — AES-256 under a scrypt-derived
# key from the keychain passphrase. No RSA/ECC anywhere at rest, so
# harvest-now-decrypt-later quantum attacks have no public-key material to
# break (Grover leaves AES-256 with ~128-bit quantum security).
#
# Key: macOS Keychain item "restic-claude-vault". Losing it loses the vault.
set -euo pipefail

export RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-$HOME/claude-vault/restic}"
export RESTIC_PASSWORD_COMMAND="security find-generic-password -s restic-claude-vault -w"
RESTIC="$(command -v restic || echo /opt/homebrew/bin/restic)"
REPLICA_HOST="${VAULT_REPLICA_HOST:-remote-host-alt}"
LOG="$HOME/claude-vault/vault.log"

log() { printf '[%s] %s\n' "$(date +'%F %T')" "$*" >> "$LOG"; }

"$RESTIC" backup \
    "$HOME/claude-transcript-archive" \
    "$HOME/.claude/usage-ledger.json" \
    "$HOME/.claude/stats-cache.json" \
    --tag transcripts --quiet
log "snapshot ok ($("$RESTIC" snapshots --tag transcripts | tail -1))"

if rsync -a -e "ssh -o BatchMode=yes -o ConnectTimeout=8" --timeout=30 \
    "$RESTIC_REPOSITORY/" "$REPLICA_HOST:claude-vault-replica/restic/" 2>>"$LOG"; then
    log "replica to $REPLICA_HOST ok"
else
    log "replica to $REPLICA_HOST FAILED — local vault only this run"
fi
