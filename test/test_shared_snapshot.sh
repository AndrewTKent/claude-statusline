#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
SANDBOX=$(mktemp -d)
trap 'rm -rf "$SANDBOX"' EXIT
TEST_HOME="$SANDBOX/home"
TEST_TMP="$SANDBOX/tmp/claude"
STUBS="$SANDBOX/stubs"
WORKTREE="$SANDBOX/worktrees/metrics-pane"
mkdir -p "$TEST_HOME/.claude" "$TEST_HOME/.accounts" "$TEST_TMP" "$STUBS"

sed "s#/tmp/claude#$TEST_TMP#g" "$ROOT/bin/statusline.sh" > "$SANDBOX/statusline.sh"
chmod +x "$SANDBOX/statusline.sh"

VIOLATIONS="$SANDBOX/violations"
for command_name in curl claude python3; do
    printf '#!/bin/sh\nprintf "%%s\\n" "$0 $*" >> "$SHARED_TEST_VIOLATIONS"\nexit 97\n' > "$STUBS/$command_name"
    chmod +x "$STUBS/$command_name"
done
printf '#!/bin/sh\nprintf "scanner\\n" >> "$SHARED_TEST_VIOLATIONS"\nexit 97\n' > "$STUBS/scanner.py"
chmod +x "$STUBS/scanner.py"

cat > "$SANDBOX/input.json" <<JSON
{
  "model": {"display_name": "Claude Test"},
  "cost": {"total_cost_usd": 1.25, "total_duration_ms": 42000},
  "context_window": {
    "used_percentage": 37.5,
    "total_input_tokens": 12000,
    "total_output_tokens": 3000,
    "current_usage": {"cache_read_input_tokens": 500, "cache_creation_input_tokens": 100},
    "context_window_size": 200000
  },
  "workspace": {"current_dir": "$SANDBOX/project/nested"},
  "session_id": "session-test",
  "effort": {"level": "high"}
}
JSON
mkdir -p "$SANDBOX/project"
git -C "$SANDBOX/project" init -q
printf 'tracked\n' > "$SANDBOX/project/tracked.txt"
git -C "$SANDBOX/project" add tracked.txt
env GIT_AUTHOR_NAME=Test GIT_AUTHOR_EMAIL=test@example.invalid \
    GIT_COMMITTER_NAME=Test GIT_COMMITTER_EMAIL=test@example.invalid \
git -C "$SANDBOX/project" commit -qm initial
git -C "$SANDBOX/project" branch -M main
git -C "$SANDBOX/project" worktree add -q -b feature/agent-metrics-worktree-identity "$WORKTREE"
mkdir -p "$SANDBOX/project/nested"
mkdir -p "$WORKTREE/nested"
project_dir=$(printf '%s' "$SANDBOX/project/nested" | tr '/' '-')
mkdir -p "$TEST_HOME/.claude/projects/$project_dir"
cat > "$TEST_HOME/.claude/projects/$project_dir/session-test.jsonl" <<JSON
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"git -C $WORKTREE/nested status"}}]}}
JSON
sqlite3 "$TEST_HOME/metrics.sqlite3" >/dev/null <<SQL
pragma journal_mode=wal;
create table minute_metrics(provider text, minute integer, input_tokens integer, output_tokens integer);
insert into minute_metrics values('claude', strftime('%s','now','localtime','start of day','utc') * 1000, 1200, 34);
SQL
sqlite3 "$TEST_HOME/metrics.sqlite3" "pragma wal_checkpoint(truncate);" >/dev/null
rm -f "$TEST_HOME/metrics.sqlite3-wal" "$TEST_HOME/metrics.sqlite3-shm"
branch=$(git -C "$WORKTREE" branch --show-current)
worktree_root=$(git -C "$WORKTREE" rev-parse --show-toplevel)
pr_cache_key=$(printf '%s\0%s' "$worktree_root" "$branch" | cksum)
pr_cache_key="${pr_cache_key%% *}"
printf '{"state":"OPEN","number":9,"title":"Cached local PR"}\n' > "$TEST_TMP/statusline-pr-${pr_cache_key}.json"

NOW=$(date +%s)
cat > "$TEST_HOME/.accounts/statusline-snapshot.json" <<JSON
{
  "version": 1,
  "generated_at": ${NOW}.5,
  "health": {"last_success_at": ${NOW}.5, "error": null},
  "mode": {"mode": "auto", "label": null, "global_generation": 7},
  "accounts": {
    "work": {
      "five_hour": {"used_pct": 42.5, "resets_at": "2099-01-01T12:00:00Z", "observed_at": $NOW, "stale": false, "pending_reset": false},
      "seven_day": {"used_pct": 66, "resets_at": "2099-01-07T12:00:00Z", "observed_at": $NOW, "stale": false, "pending_reset": false},
      "scoped": [{"kind": "fable", "label": "Fable", "used_pct": 17, "resets_at": "2099-01-07T12:00:00Z", "observed_at": $NOW, "stale": false, "pending_reset": false}],
      "expired": false,
      "live_leases": 2
    },
    "general": {
      "five_hour": {"used_pct": 12, "resets_at": "2099-01-01T12:00:00Z", "observed_at": $NOW, "stale": false, "pending_reset": false},
      "seven_day": {"used_pct": 24, "resets_at": "2099-01-07T12:00:00Z", "observed_at": $NOW, "stale": false, "pending_reset": false},
      "scoped": [],
      "expired": false,
      "live_leases": 0
    },
    "personal": {
      "five_hour": {"used_pct": 91, "resets_at": null, "observed_at": $NOW, "stale": true, "pending_reset": true},
      "seven_day": {"used_pct": null, "resets_at": null, "observed_at": null, "stale": true, "pending_reset": false},
      "scoped": [],
      "expired": true,
      "live_leases": 0
    }
  }
}
JSON

cat > "$TEST_HOME/.claude/statusline.conf" <<EOF
SHARED_ACCOUNT_SNAPSHOT=1
SHARED_ACCOUNT_SNAPSHOT_FILE="$TEST_HOME/.accounts/statusline-snapshot.json"
SHARED_ACCOUNT_SNAPSHOT_MAX_AGE=180
AGENT_METRICS_RECORDER=1
AGENT_METRICS_DB="$TEST_HOME/metrics.sqlite3"
SHOW_ACCOUNT_RESETS=1
MAX_COLS="\${MAX_COLS:-}"
SCAN_SCRIPT="$STUBS/scanner.py"
EOF

run_statusline() {
    HOME="$TEST_HOME" PATH="$STUBS:$PATH" SHARED_TEST_VIOLATIONS="$VIOLATIONS" \
        ACCOUNTS_ROUTED_LABEL="${1:-work}" ACCOUNTS_POLICY_SCOPE="${2:-global}" \
        "$SANDBOX/statusline.sh" < "$SANDBOX/input.json"
}

snapshot_home_files() {
    find "$TEST_HOME/.claude" "$TEST_HOME/.accounts" -type f -print0 |
        while IFS= read -r -d '' item_file; do
            stat -c '%n:%i:%Y:%s' "$item_file" 2>/dev/null ||
                stat -f '%N:%i:%m:%z' "$item_file"
        done | sort
}

output=$(run_statusline work)
for expected in "Claude Test" "work" "42.5%" "66%" "17%" "fable" "Personal" "pending" "needs reauth" "15.00k" "1.23k" "⌥ metrics-pane" "#9" "Cached local PR"; do
    [[ "$output" == *"$expected"* ]] || { printf 'missing shared value: %s\n' "$expected" >&2; exit 1; }
done
plain_output=$(printf '%s' "$output" | sed $'s/\033\\[[0-9;]*m//g')
[[ "$plain_output" == *$'model   Claude Test · high\n'* ]] || { printf 'model and effort are jammed together\n' >&2; exit 1; }
[[ "$plain_output" == *$'repo    project\ntree    ⌥ metrics-pane\nbranch  feature/agent-metrics-w…\npr      #9 Cached local PR\n'* ]] || {
    printf 'checkout identity is not split into readable rows\n' >&2
    exit 1
}
[[ "$plain_output" != *'project ›'* ]] || { printf 'default renderer collapsed checkout identity\n' >&2; exit 1; }
[[ "$output" == *$'\033]0;metrics-pane'* ]] || { printf 'shared renderer did not refresh terminal title\n' >&2; exit 1; }
[[ "$output" != *"2099-01-01 12:00:00 · stale"* ]] || { printf 'fresh current window rendered stale\n' >&2; exit 1; }
[[ "$output" != *"2099-01-01"* ]] || { printf 'raw reset timestamp leaked into renderer\n' >&2; exit 1; }
[[ "$output" != *$'cost   '* ]] || { printf 'shared renderer added cost noise\n' >&2; exit 1; }
general_line=""
while IFS= read -r output_line; do
    [[ "$output_line" == *General* ]] && general_line="$output_line"
done <<< "$plain_output"
[[ -n "$general_line" && "$general_line" != *"~ stale"* ]] || { printf 'fresh account without a scoped limit rendered stale\n' >&2; exit 1; }
wide_output=$(MAX_COLS=100 COLUMNS=80 run_statusline work)
wide_plain=$(printf '%s' "$wide_output" | sed $'s/\033\\[[0-9;]*m//g')
wide_first_line="${wide_plain%%$'\n'*}"
[[ "${#wide_first_line}" -eq 97 ]] || { printf 'shared renderer did not claim the usable panel width\n' >&2; exit 1; }
wide_prefix="${wide_first_line%%model*}"
[[ -n "$wide_prefix" ]] || { printf 'shared renderer left the status block against the margin\n' >&2; exit 1; }
[[ "$wide_plain" == *$'\n'"${wide_prefix}time"* ]] || { printf 'shared renderer did not center rows as one block\n' >&2; exit 1; }
narrow_output=$(MAX_COLS=20 run_statusline work)
narrow_plain=$(printf '%s' "$narrow_output" | sed $'s/\033\\[[0-9;]*m//g')
narrow_first_line="${narrow_plain%%$'\n'*}"
[[ "$narrow_first_line" == "model   Claude Test · high" ]] || { printf 'narrow shared renderer padded the first row\n' >&2; exit 1; }
compact_output=$(STATUSLINE_FORMAT=sigil run_statusline work)
[[ "$compact_output" == *"project › ⌥ metrics-pane"*"#9"* ]] || {
    printf 'compact renderer hid worktree or PR identity\n' >&2
    exit 1
}
pane_output=$(run_statusline work pane)
[[ "$pane_output" == *"pane pinned"* ]] || { printf 'pane policy scope not shown\n' >&2; exit 1; }
router_state="$TEST_TMP/account-router-parent-$$/state.json"
rm -rf "$(dirname "$router_state")"
jq '.session_id = "session-test"' "$SANDBOX/input.json" > "$SANDBOX/router-input.json"
HOME="$TEST_HOME" PATH="$STUBS:$PATH" SHARED_TEST_VIOLATIONS="$VIOLATIONS" \
    ACCOUNTS_ROUTED_LABEL=work ACCOUNTS_POLICY_SCOPE=global \
    ACCOUNTS_ROUTER_STATE="$router_state" \
    "$SANDBOX/statusline.sh" < "$SANDBOX/router-input.json" >/dev/null
jq -e '.session_id == "session-test" and .label == "work"' "$router_state" >/dev/null
rm -rf "$(dirname "$router_state")"
[ ! -s "$VIOLATIONS" ] || { printf 'forbidden command invoked:\n' >&2; tee /dev/stderr < "$VIOLATIONS"; exit 1; }
[ ! -e "$TEST_TMP/statusline-raw.json" ]
[ ! -e "$TEST_TMP/statusline-usage-cache.json" ]
[ ! -e "$TEST_TMP/statusline-profile-cache.json" ]

before=$(snapshot_home_files)
pids=()
for _ in 1 2 3 4 5 6; do
    run_statusline work > "$SANDBOX/concurrent-${_}.out" &
    pids+=("$!")
done
for pid in "${pids[@]}"; do wait "$pid"; done
after=$(snapshot_home_files)
[ "$before" = "$after" ] || { printf 'shared render wrote under HOME\n' >&2; exit 1; }
[ ! -s "$VIOLATIONS" ]

snapshot_file="$TEST_HOME/.accounts/statusline-snapshot.json"
jq '.health.error = "upstream unavailable" | .accounts.work.five_hour.stale = true' "$snapshot_file" > "$SANDBOX/snapshot.next"
mv "$SANDBOX/snapshot.next" "$snapshot_file"
output=$(run_statusline work)
[[ "$output" == *"42.5%"* ]]
[[ "$output" == *"stale"* ]]
[[ "$output" == *"snapshot error"* ]]
[[ "$output" != *"upstream unavailable"* ]]

output=$(run_statusline absent)
[[ "$output" == *"unknown"* ]]
[[ "$output" == *"stale"* ]]
[[ "$output" != *"session"*" 0%"* ]]

rm "$TEST_HOME/metrics.sqlite3"
printf '{"today":{"total_tokens":9876}}\n' > "$TEST_HOME/.claude/token-scan-summary.json"
output=$(run_statusline work)
[[ "$output" == *"9.88k"* ]] || { printf 'legacy usage fallback was not rendered\n' >&2; exit 1; }

printf '{"version":1,"accounts":{"work":{"five_hour":{},"seven_day":{},"scoped":[]}}}' > "$snapshot_file"
output=$(run_statusline work)
[[ "$output" == *"work"* ]]
[[ "$output" == *"stale"* ]]
[[ "$output" != *"session"*" 0%"* ]]

printf '{not-json' > "$snapshot_file"
output=$(run_statusline work)
[[ "$output" == *"unknown"* ]]
[[ "$output" == *"stale"* ]]

cat > "$TEST_HOME/.claude/statusline.conf" <<EOF
SHOW_ACCOUNT_RESETS=0
MAX_COLS=120
SCAN_SCRIPT="$STUBS/scanner.py"
EOF
unset_output=$(HOME="$TEST_HOME" PATH="$STUBS:$PATH" SHARED_TEST_VIOLATIONS="$VIOLATIONS" \
    "$SANDBOX/statusline.sh" < "$SANDBOX/input.json")
printf '\nSHARED_ACCOUNT_SNAPSHOT=0\n' >> "$TEST_HOME/.claude/statusline.conf"
disabled_output=$(HOME="$TEST_HOME" PATH="$STUBS:$PATH" SHARED_TEST_VIOLATIONS="$VIOLATIONS" \
    "$SANDBOX/statusline.sh" < "$SANDBOX/input.json")
[ "$unset_output" = "$disabled_output" ] || { printf 'disabled mode changed legacy output\n' >&2; exit 1; }

printf 'shared snapshot tests passed\n'
