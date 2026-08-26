#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/bin"
cat >"$tmp/bin/python3" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
cat >"$tmp/bin/python3.11" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "-c" ]; then
    exit 0
fi
printf '%s\n' "$*" >"$AGENT_METRICS_WRAPPER_LOG"
EOF
chmod +x "$tmp/bin/python3" "$tmp/bin/python3.11"
for version in python3.14 python3.13 python3.12; do
    ln -s python3 "$tmp/bin/$version"
done

AGENT_METRICS_WRAPPER_LOG="$tmp/args" PATH="$tmp/bin:/usr/bin:/bin" \
    "$repo_root/bin/agent-metrics" status

grep -Fq 'agent_metrics.py status' "$tmp/args"
echo "agent-metrics wrapper tests passed"
