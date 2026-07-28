#!/usr/bin/env bash
set -euo pipefail

repo_root="${TEST_REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/bin" "$tmp/home/.ssh" "$tmp/source/projects"

cat >"$tmp/bin/tool-mock" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

case "$(basename "$0")" in
    ssh)
        {
            printf 'ssh'
            printf ' <%s>' "$@"
            printf '\n'
        } >>"$BACKUP_CALL_LOG"
        ;;
    restic)
        if [[ ${1:-} == snapshots ]]; then
            echo "mock snapshot"
        fi
        ;;
esac
EOF
chmod +x "$tmp/bin/tool-mock"
ln -s tool-mock "$tmp/bin/ssh"
ln -s tool-mock "$tmp/bin/restic"

export PATH="$tmp/bin:$PATH"
export BACKUP_CALL_LOG="$tmp/calls.log"
export HOME="$tmp/home"
export CLAUDE_DIR="$tmp/source"
unset ARCHIVE_REMOTE_HOST ARCHIVE_SSH_KEY
unset VAULT_REPLICA_HOST VAULT_REPLICA_SSH_KEY

mkdir -p "$CLAUDE_DIR/projects/nested" "$HOME/key dir"
touch "$CLAUDE_DIR/projects/session.jsonl"
touch "$CLAUDE_DIR/projects/nested/session.jsonl"
touch "$CLAUDE_DIR/projects/stats-cache.json"
custom_key="$HOME/key dir/id backup"
touch "$custom_key"

ARCHIVE_REMOTE_HOST="backup-host" ARCHIVE_SSH_KEY="$custom_key" \
    "$repo_root/bin/archive-transcripts.sh" "$HOME/archive/projects"
test -f "$HOME/archive/projects/session.jsonl"
test -f "$HOME/archive/projects/nested/session.jsonl"
test ! -e "$HOME/archive/projects/stats-cache.json"
test "$(grep -Fc -- "<$custom_key>" "$BACKUP_CALL_LOG")" -eq 2
grep -Fq -- "<backup-host>" "$BACKUP_CALL_LOG"
grep -Fq -- "claude-transcript-archive/projects/" "$BACKUP_CALL_LOG"

: >"$BACKUP_CALL_LOG"
unset ARCHIVE_REMOTE_HOST
"$repo_root/bin/archive-transcripts.sh" "$HOME/archive-unset/projects"
test ! -s "$BACKUP_CALL_LOG"

: >"$BACKUP_CALL_LOG"
ARCHIVE_REMOTE_HOST="" \
    "$repo_root/bin/archive-transcripts.sh" "$HOME/archive-disabled/projects"
test ! -s "$BACKUP_CALL_LOG"

mkdir -p "$HOME/claude-vault/restic"
: >"$BACKUP_CALL_LOG"
RESTIC_REPOSITORY="$HOME/claude-vault/restic" \
    VAULT_REPLICA_HOST="backup-host" \
    VAULT_REPLICA_SSH_KEY="$custom_key" \
    "$repo_root/bin/vault-snapshot.sh"
test "$(grep -Fc -- "<$custom_key>" "$BACKUP_CALL_LOG")" -eq 2
grep -Fq -- "<backup-host>" "$BACKUP_CALL_LOG"
grep -Fq -- "claude-vault-replica/restic/" "$BACKUP_CALL_LOG"

: >"$BACKUP_CALL_LOG"
unset VAULT_REPLICA_HOST
RESTIC_REPOSITORY="$HOME/claude-vault/restic" \
    "$repo_root/bin/vault-snapshot.sh"
test ! -s "$BACKUP_CALL_LOG"

: >"$BACKUP_CALL_LOG"
RESTIC_REPOSITORY="$HOME/claude-vault/restic" VAULT_REPLICA_HOST="" \
    "$repo_root/bin/vault-snapshot.sh"
test ! -s "$BACKUP_CALL_LOG"

echo "PASS: transcript backup routing"
