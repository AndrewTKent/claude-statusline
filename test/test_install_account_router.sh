#!/bin/bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test_root=$(mktemp -d)
trap 'rm -rf "$test_root"' EXIT

export HOME="$test_root/home"
export ZDOTDIR="$HOME"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/claude/versions"
cat > "$HOME/.local/share/claude/versions/2.0.0" <<'EOF'
#!/bin/sh
printf 'native %s\n' "$*"
EOF
chmod 755 "$HOME/.local/share/claude/versions/2.0.0"
ln -s "$HOME/.local/share/claude/versions/2.0.0" "$HOME/.local/bin/claude"

"$repo_root/install-account-router.sh"
"$repo_root/install-account-router.sh"

[ "$(readlink "$HOME/.local/bin/accounts")" = "$HOME/.local/lib/statusline/accounts.py" ]
[ "$(readlink "$HOME/.local/bin/claude-router")" = "$HOME/.local/lib/statusline/claude-router.py" ]
[ "$(readlink "$HOME/.accounts/bin/claude")" = "$HOME/.local/bin/claude-supervised" ]
[ ! -L "$HOME/.local/bin/claude" ]
grep -q claude-router "$HOME/.local/bin/claude"
[ "$(grep -Fxc 'export PATH="$HOME/.accounts/bin:$PATH"' "$HOME/.zshrc")" -eq 1 ]

output=$(
    CLAUDE_CONFIG_DIR="$HOME/.claude" \
        PATH="$HOME/.accounts/bin:$HOME/.local/bin:/usr/bin:/bin" \
        sh -c 'command -v claude; claude --version'
)
printf '%s\n' "$output" | grep -Fx "$HOME/.accounts/bin/claude"
printf '%s\n' "$output" | grep -Fx 'native --version'

mkdir -p "$test_root/bin"
printf '#!/bin/sh\nexit 0\n' > "$test_root/bin/launchctl"
chmod 755 "$test_root/bin/launchctl"
PATH="$test_root/bin:$PATH" "$repo_root/macos/launchd/install-accounts-poll.sh"

python3 - "$HOME/Library/LaunchAgents/com.claude-accounts-poll.plist" \
    "$HOME/.local/bin/accounts" <<'PY'
import plistlib
import sys

with open(sys.argv[1], "rb") as handle:
    plist = plistlib.load(handle)
assert plist["ProgramArguments"][0] == sys.argv[2]
assert plist["StartInterval"] == 60
PY
