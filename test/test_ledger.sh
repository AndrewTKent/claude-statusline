#!/usr/bin/env bash
# Regression tests for update_ledger's cost mode. Pins the seed-baseline
# branch: a session first seen mid-day must get a baseline so deltas count
# from now, not from the session's lifetime total.
set -uo pipefail
# No -e: update_ledger's trailing [ -z ] && guard returns 1 by design;
# the explicit assertions below are the oracle.
cd "$(dirname "$0")/.."

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# statusline.sh reads stdin at top level, so extract just the functions.
sed -n '/^jq_update()/,/^}/p' bin/statusline.sh > "$tmp/fns.sh"
sed -n '/^update_ledger()/,/^}/p' bin/statusline.sh >> "$tmp/fns.sh"
grep -q 'update_ledger()' "$tmp/fns.sh" || { echo "FAIL: extraction broke"; exit 1; }

LEDGER_RESULT=0; LEDGER_SESSION_DELTA=0
TOKEN_LEDGER_RESULT=0; TOKEN_LEDGER_SESSION=0
# shellcheck disable=SC1091
source "$tmp/fns.sh"

f="$tmp/daily-cost.json"
day="2026-01-02"
fail() { echo "FAIL: $1"; exit 1; }

# First write of the day: baseline == value, delta 0.
update_ledger cost "$f" s1 5.25 "$day" work
[ "$LEDGER_SESSION_DELTA" = "0" ] || fail "fresh-day delta, got $LEDGER_SESSION_DELTA"

# Second session, same day, existing file (the regression): baseline must be
# seeded so this session's prior lifetime cost does not count into today.
update_ledger cost "$f" s2 3.5 "$day" work
jq -e '.sessions.s2.baseline == 3.5' "$f" >/dev/null || fail "s2 baseline not seeded"
[ "$LEDGER_SESSION_DELTA" = "0" ] || fail "s2 first-seen delta, got $LEDGER_SESSION_DELTA"

# Growth counts from the seeded baseline; daily total is the sum of deltas.
update_ledger cost "$f" s2 4.25 "$day" work
[ "$LEDGER_SESSION_DELTA" = "0.75" ] || fail "s2 growth delta, got $LEDGER_SESSION_DELTA"
[ "$LEDGER_RESULT" = "0.75" ] || fail "daily total, got $LEDGER_RESULT"

# Known session updating in place keeps its baseline.
update_ledger cost "$f" s1 6.25 "$day" work
jq -e '.sessions.s1.baseline == 5.25' "$f" >/dev/null || fail "s1 baseline clobbered"
[ "$LEDGER_SESSION_DELTA" = "1" ] || fail "s1 delta, got $LEDGER_SESSION_DELTA"

echo "PASS: ledger baseline seeding"
