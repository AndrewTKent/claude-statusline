#!/bin/bash
# Golden render snapshots of bin/statusline.sh, one file per FORMAT, for
# regression-checking edits to the renderer.
#
#   test/render-snapshot.sh snap <outdir>
#
# Hermetic by construction: a throwaway HOME with no credentials, a non-git
# scratch cwd, TZ=UTC and a pinned MAX_COLS. The script hardcodes its
# /tmp/claude cache dir with no env override, so we snapshot a copy with that
# path redirected into the sandbox — otherwise the live machine's usage/profile
# caches leak in and the time-interpolated usage bars drift between runs.
set -u

FORMATS="default compact narrow sigil rprompt sparkline iterm2"

if [ "${1:-}" != "snap" ] || [ -z "${2:-}" ]; then
    echo "usage: $0 snap <outdir>" >&2
    exit 2
fi
outdir="$2"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(dirname "$here")"
src="$repo/bin/statusline.sh"
fixture="$here/fixtures/input.json"

mkdir -p "$outdir"

sandbox="$(mktemp -d)"
trap 'rm -rf "$sandbox"' EXIT
mkdir -p "$sandbox/.claude" "$sandbox/claude"
printf 'MAX_COLS=80\n' >"$sandbox/.claude/statusline.conf"

proj="$sandbox/proj"
mkdir -p "$proj"

# Redirect the hardcoded cache dir so live machine state can't leak in.
script="$sandbox/statusline.sh"
sed "s#/tmp/claude#$sandbox/claude#g" "$src" >"$script"

# workspace.current_dir points at the non-git scratch dir: stable basename,
# no git repo to detect.
input="$sandbox/input.json"
sed "s#__CWD__#$proj#g" "$fixture" >"$input"

for fmt in $FORMATS; do
    (cd "$proj" && HOME="$sandbox" TZ=UTC STATUSLINE_FORMAT="$fmt" bash "$script" <"$input") \
        >"$outdir/$fmt.txt" 2>/dev/null
done

# Empty stdin must print exactly "Claude".
(cd "$proj" && HOME="$sandbox" TZ=UTC bash "$script" </dev/null) >"$outdir/empty.txt" 2>/dev/null
