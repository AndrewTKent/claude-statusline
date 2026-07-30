#!/bin/bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test_root=$(mktemp -d)
trap 'rm -rf "$test_root"' EXIT

export HOME="$test_root/home"
export ZDOTDIR="$HOME"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/claude" <<'EOF'
#!/bin/sh
printf 'native %s\n' "$*"
EOF
chmod 755 "$HOME/.local/bin/claude"

"$repo_root/install-account-router.sh"
"$repo_root/install-account-router.sh"

[ "$(readlink "$HOME/.local/bin/accounts")" = "$repo_root/bin/accounts.py" ]
[ "$(readlink "$HOME/.local/bin/claude-router")" = "$repo_root/bin/claude-router.py" ]
[ "$(readlink "$HOME/.accounts/bin/claude")" = "$HOME/.local/bin/claude-supervised" ]
[ "$(grep -Fxc 'export PATH="$HOME/.accounts/bin:$PATH"' "$HOME/.zshrc")" -eq 1 ]

output=$(
    CLAUDE_CONFIG_DIR="$HOME/.claude" \
        PATH="$HOME/.accounts/bin:$HOME/.local/bin:/usr/bin:/bin" \
        sh -c 'command -v claude; claude --version'
)
printf '%s\n' "$output" | grep -Fx "$HOME/.accounts/bin/claude"
printf '%s\n' "$output" | grep -Fx 'native --version'
