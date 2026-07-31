#!/bin/bash
# The unsupervised tripwire: a router-equipped machine renders UNSUPERVISED for
# sessions the supervisor does not own (no ACCOUNTS_ROUTER_STATE in the env).
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(dirname "$here")"

sandbox="$(mktemp -d)"
trap 'rm -rf "$sandbox"' EXIT
mkdir -p "$sandbox/.claude" "$sandbox/claude" "$sandbox/proj"
printf 'MAX_COLS=120\n' >"$sandbox/.claude/statusline.conf"

# Same hermetic redirection as render-snapshot.sh: the script hardcodes
# /tmp/claude, so point it (and the router-state glob) into the sandbox.
script="$sandbox/statusline.sh"
sed "s#/tmp/claude#$sandbox/claude#g" "$repo/bin/statusline.sh" >"$script"
input="$sandbox/input.json"
sed "s#__CWD__#$sandbox/proj#g" "$here/fixtures/input.json" >"$input"

run_statusline() {
    (cd "$sandbox/proj" && HOME="$sandbox" TZ=UTC STATUSLINE_FORMAT="${2:-default}" \
        ACCOUNTS_ROUTER_STATE="${1-}" bash "$script" <"$input") 2>/dev/null
}

fail() { echo "FAIL: $1" >&2; exit 1; }

out=$(run_statusline "")
case "$out" in *UNSUPERVISED*) fail "badge shown with no router installed" ;; esac

mkdir -p "$sandbox/.accounts/bin"
printf '#!/bin/sh\n' >"$sandbox/.accounts/bin/claude"
chmod +x "$sandbox/.accounts/bin/claude"

out=$(run_statusline "")
case "$out" in *UNSUPERVISED*) : ;; *) fail "badge missing for unsupervised session" ;; esac

out=$(run_statusline "" compact)
case "$out" in *UNSUPERVISED*) : ;; *) fail "badge missing in compact format" ;; esac

out=$(run_statusline "$sandbox/claude/account-router-12345.json")
case "$out" in *UNSUPERVISED*) fail "badge shown for supervised session" ;; esac

echo "OK test_unsupervised_badge"
