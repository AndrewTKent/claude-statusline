#!/bin/bash
# shellcheck disable=SC2059,SC2034,SC2154,SC2153,SC1090,SC2329,SC2016
set -f

input=$(cat)

# Config: ~/.claude/statusline.conf (sourced as bash)
#   HOURLY_RATE=150    # Billing rate in $/hour (enables cost tracking)
#   DAILY_BUDGET=20    # Daily cost ceiling in $ (enables budget bar)

if [ -z "$input" ]; then
    printf "Claude"
    exit 0
fi

# ── Config ──────────────────────────────────────────────
CONFIG_FILE="$HOME/.claude/statusline.conf"
HOURLY_RATE=0
DAILY_BUDGET=0
[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"

FOCUS_FILE="$HOME/.claude/focus"

# ── True Colors (24-bit RGB) ───────────────────────────
blue='\033[38;2;0;153;255m'
orange='\033[38;2;255;176;85m'
green='\033[38;2;0;175;80m'
cyan='\033[38;2;86;182;194m'
red='\033[38;2;255;85;85m'
yellow='\033[38;2;230;200;0m'
white='\033[38;2;220;220;220m'
magenta='\033[38;2;180;140;255m'
dim='\033[2m'
reset='\033[0m'

sep=" ${dim}│${reset} "

# ── Helpers ─────────────────────────────────────────────
format_tokens() {
    local num=$1
    if [ "$num" -ge 1000000 ] 2>/dev/null; then
        awk "BEGIN {printf \"%.1fm\", $num / 1000000}"
    elif [ "$num" -ge 1000 ] 2>/dev/null; then
        awk "BEGIN {printf \"%.0fk\", $num / 1000}"
    else
        printf "%d" "$num"
    fi
}

color_for_pct() {
    local pct=$1
    if [ "$pct" -ge 90 ] 2>/dev/null; then printf "$red"
    elif [ "$pct" -ge 70 ] 2>/dev/null; then printf "$yellow"
    elif [ "$pct" -ge 50 ] 2>/dev/null; then printf "$orange"
    else printf "$green"
    fi
}

build_bar() {
    local pct=$1
    local width=$2
    [ "$pct" -lt 0 ] 2>/dev/null && pct=0
    [ "$pct" -gt 100 ] 2>/dev/null && pct=100

    local filled=$(( pct * width / 100 ))
    local empty=$(( width - filled ))
    local bar_color
    bar_color=$(color_for_pct "$pct")

    local filled_str="" empty_str=""
    for ((i=0; i<filled; i++)); do filled_str+="●"; done
    for ((i=0; i<empty; i++)); do empty_str+="○"; done

    printf "${bar_color}${filled_str}${dim}${empty_str}${reset}"
}

# Atomic jq update: reads file, applies jq filter, writes back atomically.
jq_update() {
    local file="$1"; shift
    local tmpfile
    tmpfile=$(mktemp "${file}.XXXXXX") || return 1
    if jq "$@" "$file" > "$tmpfile" 2>/dev/null; then
        mv "$tmpfile" "$file"
    else
        rm -f "$tmpfile"
        return 1
    fi
}

# ── Account label resolution ─────────────────────────────
# Resolves email to account label (e.g., "work", "personal")
# Checks ACCOUNT_LABELS config first, then hardcoded fallbacks
resolve_account_label() {
    local email="$1"
    [ -z "$email" ] && return

    # Check ACCOUNT_LABELS config: "work:*@company.com personal:me@gmail.com"
    if [ -n "$ACCOUNT_LABELS" ]; then
        local pair label pattern
        for pair in $ACCOUNT_LABELS; do
            label="${pair%%:*}"
            pattern="${pair#*:}"
            # shellcheck disable=SC2254
            case "$email" in $pattern) echo "$label"; return ;; esac
        done
    fi

    # Hardcoded fallbacks
    case "$email" in
        *@acme.ai)                echo "work" ;;
        user@example.com)    echo "personal" ;;
        ?*)                        echo "$email" ;;
    esac
}

# ── Reusable ledger function ─────────────────────────────
# Usage: update_ledger <file> <session_id> <value> <today> [acct]
# Returns the daily delta (sum of all session deltas) via LEDGER_RESULT
LEDGER_RESULT=0
update_ledger() {
    local file="$1" sid="$2" value="$3" today="$4" acct="${5:-}"

    if [ -f "$file" ]; then
        # Single jq call to get date + baseline existence
        local info
        info=$(jq -r --arg sid "$sid" '[.date // "", (.sessions[$sid].baseline // empty | tostring)] | join("|")' "$file" 2>/dev/null)
        local ledger_date="${info%%|*}"
        local has_baseline="${info#*|}"

        if [ "$ledger_date" = "$today" ]; then
            if [ -z "$has_baseline" ]; then
                if [ -n "$acct" ]; then
                    jq_update "$file" --arg sid "$sid" --argjson val "$value" --arg acct "$acct" \
                        '.sessions[$sid] = {"baseline": $val, "current": $val, "acct": $acct}'
                else
                    jq_update "$file" --arg sid "$sid" --argjson val "$value" \
                        '.sessions[$sid] = {"baseline": $val, "current": $val}'
                fi
            else
                if [ -n "$acct" ]; then
                    jq_update "$file" --arg sid "$sid" --argjson val "$value" --arg acct "$acct" \
                        '.sessions[$sid].current = $val | .sessions[$sid].acct = $acct'
                else
                    jq_update "$file" --arg sid "$sid" --argjson val "$value" \
                        '.sessions[$sid].current = $val'
                fi
            fi
            LEDGER_RESULT=$(jq '[.sessions[] | .current - .baseline] | add // 0' "$file" 2>/dev/null)
            [ -z "$LEDGER_RESULT" ] && LEDGER_RESULT=0
        else
            if [ -n "$acct" ]; then
                printf '{"date":"%s","sessions":{"%s":{"baseline":%s,"current":%s,"acct":"%s"}}}' \
                    "$today" "$sid" "$value" "$value" "$acct" > "$file"
            else
                printf '{"date":"%s","sessions":{"%s":{"baseline":%s,"current":%s}}}' \
                    "$today" "$sid" "$value" "$value" > "$file"
            fi
            LEDGER_RESULT=0
        fi
    else
        if [ -n "$acct" ]; then
            printf '{"date":"%s","sessions":{"%s":{"baseline":%s,"current":%s,"acct":"%s"}}}' \
                "$today" "$sid" "$value" "$value" "$acct" > "$file"
        else
            printf '{"date":"%s","sessions":{"%s":{"baseline":%s,"current":%s}}}' \
                "$today" "$sid" "$value" "$value" > "$file"
        fi
        LEDGER_RESULT=0
    fi
}

# ── Subagent token tracking ──────────────────────────────
# Sums tokens from subagent JSONL files for the current session.
# Caches result for 30s to avoid scanning on every render.
SUBAGENT_TOKENS=0
get_subagent_tokens() {
    local sid="$1" cwd="$2"
    SUBAGENT_TOKENS=0
    [ -z "$sid" ] || [ -z "$cwd" ] && return

    local cache_file="/tmp/claude/statusline-subagent-${sid}.txt"
    mkdir -p /tmp/claude

    # Check cache (30s TTL)
    if [ -f "$cache_file" ]; then
        local cache_age
        local cache_mtime
        cache_mtime=$(stat -f %m "$cache_file" 2>/dev/null || stat -c %Y "$cache_file" 2>/dev/null)
        cache_age=$(( $(date +%s) - cache_mtime ))
        if [ "$cache_age" -lt 30 ]; then
            SUBAGENT_TOKENS=$(cat "$cache_file" 2>/dev/null)
            [ -z "$SUBAGENT_TOKENS" ] && SUBAGENT_TOKENS=0
            return
        fi
    fi

    # Map CWD to project dir name (Claude's convention: slashes become dashes)
    local project_dir
    project_dir=$(echo "$cwd" | tr '/' '-')
    local subagent_path="$HOME/.claude/projects/${project_dir}/${sid}/subagents"

    if [ -d "$subagent_path" ]; then
        # Sum input + output tokens across subagent files
        SUBAGENT_TOKENS=$(jq -s '[.[].message.usage | select(.) | (.input_tokens // 0) + (.output_tokens // 0)] | add // 0' "$subagent_path"/agent-*.jsonl 2>/dev/null)
        [ -z "$SUBAGENT_TOKENS" ] && SUBAGENT_TOKENS=0
    fi

    echo "$SUBAGENT_TOKENS" > "$cache_file"
}

iso_to_epoch() {
    local iso_str="$1"

    # GNU date
    local epoch
    epoch=$(date -d "${iso_str}" +%s 2>/dev/null)
    if [ -n "$epoch" ]; then
        echo "$epoch"
        return 0
    fi

    # macOS date
    local stripped="${iso_str%%.*}"
    stripped="${stripped%%Z}"
    stripped="${stripped%%+*}"
    stripped="${stripped%%-[0-9][0-9]:[0-9][0-9]}"

    if [[ "$iso_str" == *"Z"* ]] || [[ "$iso_str" == *"+00:00"* ]] || [[ "$iso_str" == *"-00:00"* ]]; then
        epoch=$(env TZ=UTC date -j -f "%Y-%m-%dT%H:%M:%S" "$stripped" +%s 2>/dev/null)
    else
        epoch=$(date -j -f "%Y-%m-%dT%H:%M:%S" "$stripped" +%s 2>/dev/null)
    fi

    if [ -n "$epoch" ]; then
        echo "$epoch"
        return 0
    fi

    return 1
}

format_reset_time() {
    local iso_str="$1"
    local style="$2"
    [ -z "$iso_str" ] || [ "$iso_str" = "null" ] && return

    local epoch
    epoch=$(iso_to_epoch "$iso_str")
    [ -z "$epoch" ] && return

    # If the reset time is in the past, project forward in 5-hour increments
    local now
    now=$(date +%s)
    while [ "$epoch" -le "$now" ]; do
        epoch=$((epoch + 18000))
    done

    case "$style" in
        time)
            date -j -r "$epoch" +"%l:%M%p" 2>/dev/null | sed 's/^ //; s/\.//g' | tr '[:upper:]' '[:lower:]' || \
            date -d "@$epoch" +"%l:%M%P" 2>/dev/null | sed 's/^ //; s/\.//g'
            ;;
        datetime)
            date -j -r "$epoch" +"%b %-d, %l:%M%p" 2>/dev/null | sed 's/  / /g; s/^ //; s/\.//g' | tr '[:upper:]' '[:lower:]' || \
            date -d "@$epoch" +"%b %-d, %l:%M%P" 2>/dev/null | sed 's/  / /g; s/^ //; s/\.//g'
            ;;
    esac
}

# ── Parse JSON (single jq call for performance) ────────
eval "$(echo "$input" | jq -r '
    "MODEL=" + (.model.display_name // "unknown" | @sh),
    "COST=" + (.cost.total_cost_usd // 0 | tostring | @sh),
    "DURATION_MS=" + (.cost.total_duration_ms // 0 | tostring | @sh),
    "CONTEXT_PCT=" + (.context_window.used_percentage // 0 | tostring | @sh),
    "LINES_ADDED=" + (.cost.total_lines_added // 0 | tostring | @sh),
    "LINES_REMOVED=" + (.cost.total_lines_removed // 0 | tostring | @sh),
    "CWD=" + (.workspace.current_dir // "" | @sh),
    "INPUT_TOKENS=" + (.context_window.total_input_tokens // 0 | tostring | @sh),
    "OUTPUT_TOKENS=" + (.context_window.total_output_tokens // 0 | tostring | @sh),
    "CACHE_READ=" + (.context_window.current_usage.cache_read_input_tokens // 0 | tostring | @sh),
    "CACHE_CREATE=" + (.context_window.current_usage.cache_creation_input_tokens // 0 | tostring | @sh),
    "SESSION_ID=" + (.session_id // "" | @sh),
    "CTX_SIZE=" + (.context_window.context_window_size // 200000 | tostring | @sh)
' 2>/dev/null)"

# ── Early account resolution (needed before ledger writes) ──
ACCT_TAG=""
ACCT_EMAIL=""
profile_cache_file="/tmp/claude/statusline-profile-cache.json"
if [ -f "$profile_cache_file" ]; then
    ACCT_EMAIL=$(jq -r '.account.email // empty' "$profile_cache_file" 2>/dev/null)
    ACCT_TAG=$(resolve_account_label "$ACCT_EMAIL")
fi

# ── Daily cost ledger ──────────────────────────────────
DAILY_LEDGER="$HOME/.claude/daily-cost.json"
TODAY=$(date +%Y-%m-%d)
DAILY_COST="$COST"
if [ -n "$SESSION_ID" ] && [ "$(awk "BEGIN {print ($COST > 0)}")" = "1" ]; then
    update_ledger "$DAILY_LEDGER" "$SESSION_ID" "$COST" "$TODAY" "$ACCT_TAG"
    DAILY_COST="${LEDGER_RESULT:-0}"
fi
DAILY_FMT=$(printf "%.2f" "$DAILY_COST")

# ── Token challenge tracker (incremental checkpoint) ─────
TOKEN_CHECKPOINT="$HOME/.claude/token-checkpoint.json"
TOKEN_DISPLAY=""

if [ -n "$SESSION_ID" ]; then
    SESSION_TOKENS=$((INPUT_TOKENS + OUTPUT_TOKENS))
    get_subagent_tokens "$SESSION_ID" "$CWD"

    PROJECTS_DIR="$HOME/.claude/projects"
    CHALLENGE_TOKENS=0

    # Sum input_tokens + output_tokens from JSONL usage blocks (batched for large file counts)
    sum_jsonl_tokens() {
        xargs -0 -n 200 jq -s '[.[].message.usage | select(.) | (.input_tokens // 0) + (.output_tokens // 0)] | add // 0' 2>/dev/null \
            | awk '{s+=$1} END {print s+0}'
    }

    # Checkpoint stores: {"total": N, "files": {"path": tokens, ...}}
    # On incremental render: re-scan changed files, replace their entry, recompute total.
    TOKEN_WATERMARK="/tmp/claude/statusline-token-watermark"
    mkdir -p /tmp/claude

    if [ -f "$TOKEN_CHECKPOINT" ] && [ -f "$TOKEN_WATERMARK" ]; then
        CHALLENGE_TOKENS=$(jq '.total // 0' "$TOKEN_CHECKPOINT" 2>/dev/null)
        [ -z "$CHALLENGE_TOKENS" ] && CHALLENGE_TOKENS=0

        if [ -d "$PROJECTS_DIR" ]; then
            # Find files changed since last scan
            mapfile -d '' changed < <(find "$PROJECTS_DIR" -name "*.jsonl" -newer "$TOKEN_WATERMARK" -print0 2>/dev/null)

            if [ "${#changed[@]}" -gt 0 ]; then
                # Subtract old values for changed files, add new values
                for f in "${changed[@]}"; do
                    old_val=$(jq -r --arg f "$f" '.files[$f] // 0' "$TOKEN_CHECKPOINT" 2>/dev/null)
                    [ -z "$old_val" ] && old_val=0
                    new_val=$(jq -s '[.[].message.usage | select(.) | (.input_tokens // 0) + (.output_tokens // 0)] | add // 0' "$f" 2>/dev/null)
                    [ -z "$new_val" ] && new_val=0
                    CHALLENGE_TOKENS=$((CHALLENGE_TOKENS - old_val + new_val))
                    # Update file entry in checkpoint
                    jq_update "$TOKEN_CHECKPOINT" --arg f "$f" --argjson v "$new_val" \
                        '.files[$f] = $v'
                done
                # Update total
                jq_update "$TOKEN_CHECKPOINT" --argjson t "$CHALLENGE_TOKENS" '.total = $t'
                touch "$TOKEN_WATERMARK"
            fi
        fi
    else
        # First run: use stats-cache as instant estimate
        CHALLENGE_TOKENS=0
        if [ -f "$HOME/.claude/stats-cache.json" ]; then
            CHALLENGE_TOKENS=$(jq '[.modelUsage[] | .inputTokens + .outputTokens] | add // 0' "$HOME/.claude/stats-cache.json" 2>/dev/null)
            [ -z "$CHALLENGE_TOKENS" ] && CHALLENGE_TOKENS=0
        fi

        printf '{"total":%s,"files":{}}' "$CHALLENGE_TOKENS" > "$TOKEN_CHECKPOINT"
        touch "$TOKEN_WATERMARK"

        # Background full scan builds accurate per-file index
        (
            if [ -d "$PROJECTS_DIR" ]; then
                # Process files in batches of 50, output "file\ttokens" per file
                result=$(find "$PROJECTS_DIR" -name "*.jsonl" -print0 2>/dev/null \
                    | xargs -0 -n 50 sh -c '
                        for f do
                            t=$(jq -s "[.[].message.usage | select(.) | (.input_tokens // 0) + (.output_tokens // 0)] | add // 0" "$f" 2>/dev/null)
                            [ -z "$t" ] && t=0
                            printf "%s\t%s\n" "$f" "$t"
                        done
                    ' _ 2>/dev/null \
                    | awk -F'\t' '
                        BEGIN { printf "{\"files\":{" }
                        NR>1 { printf "," }
                        {
                            s += $2
                            # Escape backslashes and quotes in path
                            gsub(/\\/, "\\\\", $1)
                            gsub(/"/, "\\\"", $1)
                            printf "\"%s\":%s", $1, $2
                        }
                        END { printf "},\"total\":%s}", s }
                    ')
                if [ -n "$result" ] && echo "$result" | jq -e '.total > 0' >/dev/null 2>&1; then
                    echo "$result" > "$TOKEN_CHECKPOINT"
                    touch "$TOKEN_WATERMARK"
                fi
            fi
        ) &
    fi

    if [ "$CHALLENGE_TOKENS" -gt 0 ] 2>/dev/null; then
        TOKEN_M=$(awk "BEGIN {printf \"%.1f\", $CHALLENGE_TOKENS / 1000000}")
        GOAL_M="100"
        TOKEN_PCT=$(awk "BEGIN {printf \"%.0f\", $CHALLENGE_TOKENS / (${GOAL_M} * 10000)}")
        [ "$TOKEN_PCT" -gt 100 ] 2>/dev/null && TOKEN_PCT=100
        TOKEN_BAR=$(build_bar "$TOKEN_PCT" 10)

        # Daily token tracking
        DAILY_TOKEN_LEDGER="$HOME/.claude/daily-tokens.json"
        update_ledger "$DAILY_TOKEN_LEDGER" "$SESSION_ID" "$SESSION_TOKENS" "$TODAY" "$ACCT_TAG"
        DAILY_TOKENS="${LEDGER_RESULT:-0}"

        # Session token delta
        SESSION_DELTA=0
        if [ -f "$DAILY_TOKEN_LEDGER" ]; then
            SESSION_DELTA=$(jq --arg sid "$SESSION_ID" '(.sessions[$sid].current // 0) - (.sessions[$sid].baseline // 0)' "$DAILY_TOKEN_LEDGER" 2>/dev/null)
            [ -z "$SESSION_DELTA" ] && SESSION_DELTA=0
        fi

        SESSION_TOKEN_FMT=$(awk "BEGIN {
            t=$SESSION_DELTA;
            if (t >= 1000000) printf \"%.1fM\", t/1000000;
            else if (t >= 1000) printf \"%.0fk\", t/1000;
            else printf \"%d\", t
        }")

        DAILY_TOKEN_FMT=$(awk "BEGIN {
            t=$DAILY_TOKENS;
            if (t >= 1000000) printf \"%.1fM\", t/1000000;
            else if (t >= 1000) printf \"%.0fk\", t/1000;
            else printf \"%d\", t
        }")

        TOKEN_SUFFIX=" ${magenta}+${SESSION_TOKEN_FMT}${reset}"
        if [ "$SUBAGENT_TOKENS" -gt 0 ] 2>/dev/null; then
            sub_fmt=$(format_tokens "$SUBAGENT_TOKENS")
            TOKEN_SUFFIX+=" ${dim}+${sub_fmt} sub${reset}"
        fi
        if [ "$DAILY_TOKENS" -gt "$SESSION_DELTA" ] 2>/dev/null; then
            TOKEN_SUFFIX+=" ${dim}(+${DAILY_TOKEN_FMT}/d)${reset}"
        fi

        TOKEN_PCT_COLOR=$(color_for_pct "$TOKEN_PCT")
        TOKEN_DISPLAY="${TOKEN_BAR} ${TOKEN_PCT_COLOR}$(printf "%3d" "$TOKEN_PCT")%${reset} ${magenta}${TOKEN_M}M${reset}${dim}/${GOAL_M}M${reset}${TOKEN_SUFFIX}"
    fi
fi

# ── Cost & burn rate ────────────────────────────────────
COST_FMT=$(printf "%.2f" "$COST")

BURN_RATE=""
if [ "$DURATION_MS" -gt 0 ] 2>/dev/null; then
    RATE=$(awk "BEGIN {h=$DURATION_MS/3600000; if(h>0.001) printf \"%.2f\", $COST/h}")
    [ -n "$RATE" ] && BURN_RATE=" ${magenta}@\$${RATE}/h${reset}"
fi

# ── Session timer ───────────────────────────────────────
SESSION_TIME=""
if [ "$DURATION_MS" -gt 0 ] 2>/dev/null; then
    TOTAL_SECS=$((DURATION_MS / 1000))
    H=$((TOTAL_SECS / 3600))
    M=$(((TOTAL_SECS % 3600) / 60))
    S=$((TOTAL_SECS % 60))
    if [ "$H" -gt 0 ]; then
        SESSION_TIME=$(printf "%d:%02d:%02d" $H $M $S)
    else
        SESSION_TIME=$(printf "%d:%02d" $M $S)
    fi
fi

# ── Billable amount ────────────────────────────────────
BILLABLE=""
if [ "$HOURLY_RATE" -gt 0 ] 2>/dev/null && [ "$DURATION_MS" -gt 0 ] 2>/dev/null; then
    BILL=$(awk "BEGIN {printf \"%.2f\", ($DURATION_MS/3600000) * $HOURLY_RATE}")
    BILLABLE=" ${orange}\$${BILL}b${reset}"
fi

# ── Context % with visual bar ──────────────────────────
CONTEXT_INT=$(printf "%.0f" "$CONTEXT_PCT")
CTX_BAR=$(build_bar "$CONTEXT_INT" 10)
CTX_COLOR=$(color_for_pct "$CONTEXT_INT")

# ── Context badge for line 1 (surfaces at 70%+) ───────
CTX_BADGE=""
if [ "$CONTEXT_INT" -ge 70 ] 2>/dev/null; then
    CTX_BADGE=" ${CTX_COLOR}ctx:${CONTEXT_INT}%${reset}"
fi

# ── Effort level ───────────────────────────────────────
EFFORT=""
effort_val=$(echo "$input" | jq -r '.effort_level // empty' 2>/dev/null)
if [ -z "$effort_val" ]; then
    settings_path="$HOME/.claude/settings.json"
    [ -f "$settings_path" ] && effort_val=$(jq -r '.effortLevel // empty' "$settings_path" 2>/dev/null)
fi
case "$effort_val" in
    low)    EFFORT="${dim}.low${reset}" ;;
    medium) EFFORT="${orange}.medium${reset}" ;;
    high)   EFFORT="${red}.high${reset}" ;;
    max)    EFFORT="${red}.max${reset}" ;;
esac

# ── Focus mode ──────────────────────────────────────────
FOCUS=""
[ -f "$FOCUS_FILE" ] && FOCUS=" ${red}[FOCUS]${reset}"

# ── Cache hit ratio ─────────────────────────────────────
CACHE_TOTAL=$((CACHE_READ + CACHE_CREATE))
if [ "$CACHE_TOTAL" -gt 0 ] 2>/dev/null; then
    CACHE_PCT=$((CACHE_READ * 100 / CACHE_TOTAL))
    CACHE_COLOR=$(color_for_pct $((100 - CACHE_PCT)))  # invert: high cache = good
    CACHE_STR="${CACHE_COLOR}cache:${CACHE_PCT}%${reset}"
else
    CACHE_STR="${dim}cache:--${reset}"
fi

# ── Git info ────────────────────────────────────────────
GIT_INFO=""
IS_DIRTY=false
BRANCH=""
if [ -d "$CWD" ] && git -C "$CWD" rev-parse --git-dir > /dev/null 2>&1; then
    BRANCH=$(git -C "$CWD" branch --show-current 2>/dev/null)
    if [ -n "$BRANCH" ]; then
        # Use git diff-index for fast dirty check (single call, no untracked scan)
        if ! git -C "$CWD" diff-index --quiet HEAD -- 2>/dev/null; then
            IS_DIRTY=true
        fi

        if $IS_DIRTY; then
            GIT_INFO=" ${orange}(${BRANCH}${red}*${orange})${reset}"
        else
            GIT_INFO=" ${green}(${BRANCH})${reset}"
        fi
        # Ahead/behind
        UPSTREAM=$(git -C "$CWD" rev-parse --abbrev-ref "${BRANCH}@{upstream}" 2>/dev/null)
        if [ -n "$UPSTREAM" ]; then
            COUNTS=$(git -C "$CWD" rev-list --left-right --count HEAD..."${UPSTREAM}" 2>/dev/null)
            AHEAD=$(echo "$COUNTS" | cut -f1)
            BEHIND=$(echo "$COUNTS" | cut -f2)
            AB=""
            [ "$AHEAD" -gt 0 ] 2>/dev/null && AB="↑${AHEAD}"
            [ "$BEHIND" -gt 0 ] 2>/dev/null && AB="${AB}↓${BEHIND}"
            [ -n "$AB" ] && GIT_INFO="${GIT_INFO}${cyan}${AB}${reset}"
        fi
    fi
fi

# ── PR state indicator (cached 90s) ────────────────────
PR_BADGE=""
if [ -n "$BRANCH" ] && [ "$BRANCH" != "main" ] && [ "$BRANCH" != "master" ] && command -v gh >/dev/null 2>&1; then
    pr_cache_file="/tmp/claude/statusline-pr-${BRANCH//\//_}.json"
    pr_cache_max_age=90
    pr_needs_refresh=true

    if [ -f "$pr_cache_file" ]; then
        pr_mtime=$(stat -f %m "$pr_cache_file" 2>/dev/null || stat -c %Y "$pr_cache_file" 2>/dev/null)
        pr_now=$(date +%s)
        pr_age=$(( pr_now - pr_mtime ))
        [ "$pr_age" -lt "$pr_cache_max_age" ] && pr_needs_refresh=false
    fi

    if $pr_needs_refresh; then
        # Fire-and-forget background refresh
        (
            pr_data=$(gh pr view "$BRANCH" --json state,isDraft,reviewDecision,statusCheckRollup 2>/dev/null)
            if [ -n "$pr_data" ] && echo "$pr_data" | jq -e '.state' >/dev/null 2>&1; then
                echo "$pr_data" > "$pr_cache_file"
            else
                echo '{"state":"NONE"}' > "$pr_cache_file"
            fi
        ) &
    fi

    # Always read from cache
    if [ -f "$pr_cache_file" ]; then
        pr_info=$(jq -r '[.state // "NONE", .isDraft // false | tostring, .reviewDecision // "NONE", ([.statusCheckRollup[]? | .status] | if any(. == "FAILURE") then "FAIL" elif any(. == "PENDING") then "PENDING" else "PASS" end)] | join("|")' "$pr_cache_file" 2>/dev/null)
        pr_state="${pr_info%%|*}"
        pr_rest="${pr_info#*|}"
        pr_draft="${pr_rest%%|*}"
        pr_rest2="${pr_rest#*|}"
        pr_review="${pr_rest2%%|*}"
        pr_checks="${pr_rest2#*|}"

        if [ "$pr_state" = "OPEN" ]; then
            if [ "$pr_draft" = "true" ]; then
                PR_BADGE="${dim}[draft]${reset}"
            elif [ "$pr_checks" = "FAIL" ]; then
                PR_BADGE="${red}[PR✗]${reset}"
            elif [ "$pr_review" = "CHANGES_REQUESTED" ]; then
                PR_BADGE="${orange}[PR△]${reset}"
            elif [ "$pr_review" = "APPROVED" ] && [ "$pr_checks" != "FAIL" ]; then
                PR_BADGE="${green}[PR✓]${reset}"
            elif [ "$pr_checks" = "PENDING" ]; then
                PR_BADGE="${yellow}[PR⋯]${reset}"
            else
                PR_BADGE="${cyan}[PR]${reset}"
            fi
        fi
    fi
fi

# ── Session ID ──────────────────────────────────────────
SID=""
[ -n "$SESSION_ID" ] && SID=" ${dim}${SESSION_ID:0:8}${reset}"

# ── Directory name ──────────────────────────────────────
DIR_NAME="${CWD##*/}"

# ── OAuth token resolution ──────────────────────────────
get_oauth_token() {
    if [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
        echo "$CLAUDE_CODE_OAUTH_TOKEN"
        return 0
    fi

    if command -v security >/dev/null 2>&1; then
        local blob
        # Timeout guard: cap keychain call at 2 seconds
        blob=$(timeout 2 security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null || \
               security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)
        if [ -n "$blob" ]; then
            local token
            token=$(echo "$blob" | jq -r '.claudeAiOauth.accessToken // empty' 2>/dev/null)
            if [ -n "$token" ] && [ "$token" != "null" ]; then
                echo "$token"
                return 0
            fi
        fi
    fi

    local creds_file="${HOME}/.claude/.credentials.json"
    if [ -f "$creds_file" ]; then
        local token
        token=$(jq -r '.claudeAiOauth.accessToken // empty' "$creds_file" 2>/dev/null)
        if [ -n "$token" ] && [ "$token" != "null" ]; then
            echo "$token"
            return 0
        fi
    fi

    echo ""
}

# ── Fetch rate limits + profile (background, never blocking) ──
cache_file="/tmp/claude/statusline-usage-cache.json"
profile_cache_file="/tmp/claude/statusline-profile-cache.json"
cache_max_age=60
profile_cache_max_age=300
lock_file="/tmp/claude/statusline-refresh.lock"
mkdir -p /tmp/claude

# Check if caches need refresh
needs_refresh=false
needs_profile_refresh=false
now=$(date +%s)

# Detect account switch: if credentials file changed, invalidate caches
creds_mtime_file="/tmp/claude/statusline-creds-mtime"
creds_file="$HOME/.claude/.credentials.json"
if [ -f "$creds_file" ]; then
    creds_mtime=$(stat -f %m "$creds_file" 2>/dev/null || stat -c %Y "$creds_file" 2>/dev/null)
    old_creds_mtime=$(cat "$creds_mtime_file" 2>/dev/null)
    if [ "$old_creds_mtime" != "$creds_mtime" ]; then
        rm -f "$cache_file" "$profile_cache_file" "$lock_file"
        echo "$creds_mtime" > "$creds_mtime_file"
        needs_refresh=true
        # Synchronous profile fetch on account switch — avoids stale label
        token=$(get_oauth_token)
        if [ -n "$token" ] && [ "$token" != "null" ]; then
            p_response=$(curl -s --max-time 2 \
                -H "Accept: application/json" \
                -H "Content-Type: application/json" \
                -H "Authorization: Bearer $token" \
                -H "anthropic-beta: oauth-2025-04-20" \
                -H "User-Agent: claude-code/2.1.34" \
                "https://api.anthropic.com/api/oauth/profile" 2>/dev/null)
            if [ -n "$p_response" ] && echo "$p_response" | jq -e '.account' >/dev/null 2>&1; then
                echo "$p_response" > "$profile_cache_file"
            fi
        fi
        needs_profile_refresh=false
    fi
fi

if [ -f "$cache_file" ]; then
    cache_mtime=$(stat -f %m "$cache_file" 2>/dev/null || stat -c %Y "$cache_file" 2>/dev/null)
    cache_age=$(( now - cache_mtime ))
    [ "$cache_age" -ge "$cache_max_age" ] && needs_refresh=true
else
    needs_refresh=true
fi

if [ -f "$profile_cache_file" ]; then
    p_mtime=$(stat -f %m "$profile_cache_file" 2>/dev/null || stat -c %Y "$profile_cache_file" 2>/dev/null)
    p_age=$(( now - p_mtime ))
    [ "$p_age" -ge "$profile_cache_max_age" ] && needs_profile_refresh=true
else
    needs_profile_refresh=true
fi

# Fire-and-forget background refresh (never blocks the status line)
if $needs_refresh || $needs_profile_refresh; then
    # Clean up stale lock files (PID dead or lock older than 30s)
    if [ -f "$lock_file" ]; then
        lock_pid=$(cat "$lock_file" 2>/dev/null)
        lock_age=$(( now - $(stat -f %m "$lock_file" 2>/dev/null || stat -c %Y "$lock_file" 2>/dev/null || echo "$now") ))
        if [ "$lock_age" -gt 30 ] || ! kill -0 "$lock_pid" 2>/dev/null; then
            rm -f "$lock_file"
        fi
    fi
    # Use a lock file to prevent concurrent refreshes from racing
    if (set -o noclobber; echo $$ > "$lock_file") 2>/dev/null; then
        (
            trap 'rm -f "$lock_file"' EXIT
            token=$(get_oauth_token)
            if [ -n "$token" ] && [ "$token" != "null" ]; then
                if $needs_refresh; then
                    response=$(curl -s --max-time 5 \
                        -H "Accept: application/json" \
                        -H "Content-Type: application/json" \
                        -H "Authorization: Bearer $token" \
                        -H "anthropic-beta: oauth-2025-04-20" \
                        -H "User-Agent: claude-code/2.1.34" \
                        "https://api.anthropic.com/api/oauth/usage" 2>/dev/null)
                    if [ -n "$response" ] && echo "$response" | jq -e '.five_hour' >/dev/null 2>&1; then
                        echo "$response" > "$cache_file"
                    fi
                fi
                if $needs_profile_refresh; then
                    p_response=$(curl -s --max-time 5 \
                        -H "Accept: application/json" \
                        -H "Content-Type: application/json" \
                        -H "Authorization: Bearer $token" \
                        -H "anthropic-beta: oauth-2025-04-20" \
                        -H "User-Agent: claude-code/2.1.34" \
                        "https://api.anthropic.com/api/oauth/profile" 2>/dev/null)
                    if [ -n "$p_response" ] && echo "$p_response" | jq -e '.account' >/dev/null 2>&1; then
                        echo "$p_response" > "$profile_cache_file"
                    fi
                fi
            fi
        ) &
    fi
fi

# Always read from cache (may be stale by one cycle — imperceptible)
usage_data=""
[ -f "$cache_file" ] && usage_data=$(cat "$cache_file" 2>/dev/null)
profile_data=""
[ -f "$profile_cache_file" ] && profile_data=$(cat "$profile_cache_file" 2>/dev/null)

# ── Account label (colorize ACCT_TAG resolved earlier) ──
ACCOUNT_LABEL=""
if [ -n "$ACCT_TAG" ]; then
    case "$ACCT_TAG" in
        work)     ACCOUNT_LABEL="${cyan}work${reset}" ;;
        personal) ACCOUNT_LABEL="${magenta}personal${reset}" ;;
        *)        ACCOUNT_LABEL="${dim}${ACCT_TAG}${reset}" ;;
    esac
fi

# ── Build rate limit lines ─────────────────────────────
rate_lines=""

if [ -n "$usage_data" ] && echo "$usage_data" | jq -e . >/dev/null 2>&1; then
    bar_width=10

    # Parse all usage data in a single jq call
    eval "$(echo "$usage_data" | jq -r '
        "five_hour_pct=" + (.five_hour.utilization // 0 | tostring | @sh),
        "five_hour_reset_iso=" + (.five_hour.resets_at // "" | @sh),
        "five_hour_prev_pct=" + (.five_hour.previous_utilization // 0 | tostring | @sh),
        "seven_day_pct_raw=" + (.seven_day.utilization // 0 | tostring | @sh),
        "seven_day_reset_iso=" + (.seven_day.resets_at // "" | @sh),
        "extra_enabled=" + (.extra_usage.is_enabled // false | tostring | @sh),
        "extra_pct_raw=" + (.extra_usage.utilization // 0 | tostring | @sh),
        "extra_used_raw=" + (.extra_usage.used_credits // 0 | tostring | @sh),
        "extra_limit_raw=" + (.extra_usage.monthly_limit // 0 | tostring | @sh)
    ' 2>/dev/null)"

    five_hour_pct=$(printf "%.0f" "$five_hour_pct" 2>/dev/null || echo 0)
    five_hour_reset=$(format_reset_time "$five_hour_reset_iso" "time")
    five_hour_bar=$(build_bar "$five_hour_pct" "$bar_width")
    five_hour_pct_color=$(color_for_pct "$five_hour_pct")

    rate_lines+="${white}$(printf "%-7s" "current")${reset} ${five_hour_bar} ${five_hour_pct_color}$(printf "%3d" "$five_hour_pct")%${reset}"
    [ -n "$five_hour_reset" ] && rate_lines+=" ${white}${five_hour_reset}${reset}"

    # ── Burn-down projection ──────────────────────────
    # Estimate minutes until 100% based on utilization velocity
    if [ "$five_hour_pct" -gt 5 ] 2>/dev/null && [ -n "$five_hour_reset_iso" ] && [ "$five_hour_reset_iso" != "" ]; then
        reset_epoch=$(iso_to_epoch "$five_hour_reset_iso")
        if [ -n "$reset_epoch" ]; then
            now_bd=$(date +%s)
            # The 5-hour window: reset is 5h from window start
            # Time elapsed in window = 18000 - (reset - now)
            secs_to_reset=$(( reset_epoch - now_bd ))
            [ "$secs_to_reset" -lt 0 ] && secs_to_reset=0
            secs_elapsed=$(( 18000 - secs_to_reset ))
            [ "$secs_elapsed" -lt 60 ] && secs_elapsed=60

            # Rate: pct per second
            if [ "$secs_elapsed" -gt 0 ] && [ "$five_hour_pct" -gt 0 ] 2>/dev/null; then
                remaining_pct=$(( 100 - five_hour_pct ))
                if [ "$remaining_pct" -gt 0 ]; then
                    # Minutes to full = remaining_pct / (pct / secs_elapsed) / 60
                    mins_to_full=$(awk "BEGIN {
                        rate = $five_hour_pct / $secs_elapsed;
                        if (rate > 0) printf \"%.0f\", $remaining_pct / rate / 60;
                        else print 999
                    }")
                    if [ "$mins_to_full" -le 120 ] 2>/dev/null && [ "$mins_to_full" -gt 0 ] 2>/dev/null; then
                        bd_color="$green"
                        [ "$mins_to_full" -le 60 ] && bd_color="$orange"
                        [ "$mins_to_full" -le 30 ] && bd_color="$yellow"
                        [ "$mins_to_full" -le 15 ] && bd_color="$red"
                        rate_lines+=" ${bd_color}→full ~${mins_to_full}m${reset}"
                    fi
                fi
            fi
        fi
    fi

    seven_day_pct=$(printf "%.0f" "$seven_day_pct_raw" 2>/dev/null || echo 0)
    seven_day_reset=$(format_reset_time "$seven_day_reset_iso" "datetime")
    seven_day_bar=$(build_bar "$seven_day_pct" "$bar_width")
    seven_day_pct_color=$(color_for_pct "$seven_day_pct")

    rate_lines+="\n${white}$(printf "%-7s" "weekly")${reset} ${seven_day_bar} ${seven_day_pct_color}$(printf "%3d" "$seven_day_pct")%${reset}"
    [ -n "$seven_day_reset" ] && rate_lines+=" ${white}${seven_day_reset}${reset}"

    # Extra usage credits
    if [ "$extra_enabled" = "true" ]; then
        extra_pct=$(printf "%.0f" "$extra_pct_raw" 2>/dev/null || echo 0)
        extra_used=$(awk "BEGIN {printf \"%.2f\", $extra_used_raw / 100}" 2>/dev/null)
        extra_limit=$(awk "BEGIN {printf \"%.2f\", $extra_limit_raw / 100}" 2>/dev/null)
        extra_bar=$(build_bar "$extra_pct" "$bar_width")
        extra_pct_color=$(color_for_pct "$extra_pct")

        rate_lines+="\n${white}$(printf "%-7s" "extra")${reset} ${extra_bar} ${extra_pct_color}$(printf "%3d" "$extra_pct")%${reset} ${white}\$${extra_used}${dim}/${reset}${white}\$${extra_limit}${reset}"
    fi
fi

# ── Daily budget line ──────────────────────────────────
BUDGET_DISPLAY=""
if [ "$DAILY_BUDGET" -gt 0 ] 2>/dev/null; then
    budget_pct=$(awk "BEGIN {p=$DAILY_COST * 100 / $DAILY_BUDGET; printf \"%.0f\", (p > 100 ? 100 : p)}")
    budget_bar=$(build_bar "$budget_pct" 10)
    budget_color=$(color_for_pct "$budget_pct")
    BUDGET_DISPLAY="${white}$(printf "%-7s" "budget")${reset} ${budget_bar} ${budget_color}$(printf "%3d" "$budget_pct")%${reset} ${white}\$${DAILY_FMT}${dim}/${reset}${white}\$${DAILY_BUDGET}${reset}"
fi

# ── Terminal width detection ──────────────────────────────
COLS=$(tput cols 2>/dev/null || echo 120)

# ── Branch name compression ──────────────────────────────
SHORT_GIT_INFO="$GIT_INFO"
TINY_GIT_INFO=""
if [ -n "$BRANCH" ]; then
    SHORT_BRANCH="$BRANCH"
    [[ "$BRANCH" == */* ]] && SHORT_BRANCH="${BRANCH##*/}"

    MAX_BRANCH=20
    CAPPED_BRANCH="$SHORT_BRANCH"
    if [ "${#SHORT_BRANCH}" -gt "$MAX_BRANCH" ]; then
        CAPPED_BRANCH="${SHORT_BRANCH:0:$((MAX_BRANCH-1))}…"
    fi

    DIRTY=""
    $IS_DIRTY && DIRTY="${red}*"

    AB_SUFFIX=""
    if [ -n "$UPSTREAM" ]; then
        AB=""
        [ "$AHEAD" -gt 0 ] 2>/dev/null && AB="↑${AHEAD}"
        [ "$BEHIND" -gt 0 ] 2>/dev/null && AB="${AB}↓${BEHIND}"
        [ -n "$AB" ] && AB_SUFFIX="${cyan}${AB}${reset}"
    fi

    # PR badge appended to branch info
    local_pr_badge=""
    [ -n "$PR_BADGE" ] && local_pr_badge="${PR_BADGE}"

    if [ -n "$DIRTY" ]; then
        SHORT_GIT_INFO=" ${orange}(${SHORT_BRANCH}${DIRTY}${orange})${reset}${AB_SUFFIX}${local_pr_badge}"
        TINY_GIT_INFO=" ${orange}(${CAPPED_BRANCH}${DIRTY}${orange})${reset}${local_pr_badge}"
    else
        SHORT_GIT_INFO=" ${green}(${SHORT_BRANCH})${reset}${AB_SUFFIX}${local_pr_badge}"
        TINY_GIT_INFO=" ${green}(${CAPPED_BRANCH})${reset}${local_pr_badge}"
    fi
fi

# ── Shared pre-render ──────────────────────────────────────
DAILY_SUFFIX=""
if [ "$(awk "BEGIN {print ($DAILY_COST > $COST + 0.01)}")" = "1" ]; then
    DAILY_SUFFIX=" ${dim}(\$${DAILY_FMT}/d)${reset}"
fi

# Write raw JSON sidecar for external consumers (menu bar app, widgets)
[ -n "$input" ] && echo "$input" > /tmp/claude/statusline-raw.json

# ── Render: default (multi-line) ──────────────────────────
render_default() {
    if [ "$COLS" -ge 100 ]; then
        line1="${blue}${MODEL}${reset}${EFFORT}"
        [ -n "$ACCOUNT_LABEL" ] && line1+="${sep}${ACCOUNT_LABEL}"
        line1+="${sep}${cyan}${DIR_NAME}${reset}${SHORT_GIT_INFO}"
        line1+="${sep}${magenta}\$${COST_FMT}${reset}${DAILY_SUFFIX}${BURN_RATE}"
        [ -n "$SESSION_TIME" ] && line1+="${sep}${dim}⏱${reset} ${white}${SESSION_TIME}${reset}"
        line1+="${CTX_BADGE}${FOCUS}"
    else
        line1="${blue}${MODEL}${reset}${EFFORT}"
        [ -n "$ACCOUNT_LABEL" ] && line1+="${sep}${ACCOUNT_LABEL}"
        [ -n "$TINY_GIT_INFO" ] && line1+="${sep}${TINY_GIT_INFO}"
        line1+="${sep}${magenta}\$${COST_FMT}${reset}${DAILY_SUFFIX}"
        line1+="${CTX_BADGE}${FOCUS}"
    fi

    printf "%b\n" "$line1"

    # Detail lines (dimmer for visual hierarchy)
    ctx_line="${dim}${white}$(printf "%-7s" "context")${reset} ${CTX_BAR} ${CTX_COLOR}$(printf "%3d" "$CONTEXT_INT")%${reset}"
    printf "\n%b" "$ctx_line"
    [ -n "$rate_lines" ] && printf "\n%b" "$rate_lines"
    [ -n "$BUDGET_DISPLAY" ] && printf "\n%b" "$BUDGET_DISPLAY"
    [ -n "$TOKEN_DISPLAY" ] && printf "\n${white}$(printf "%-7s" "tokens")${reset} %b" "$TOKEN_DISPLAY"
}

# ── Render: sigil (single dense line) ─────────────────────
render_sigil() {
    local s=" "  # separator (space)
    local dot=" ${dim}·${reset} "

    # Git segment
    local git_seg=""
    if [ -n "$BRANCH" ]; then
        git_seg="${cyan}⎇${reset} "
        $IS_DIRTY && git_seg+="${orange}${BRANCH}${red}✦${reset}" || git_seg+="${green}${BRANCH}${reset}"
        [ -n "$UPSTREAM" ] && {
            [ "$AHEAD" -gt 0 ] 2>/dev/null && git_seg+="${cyan}↑${AHEAD}${reset}"
            [ "$BEHIND" -gt 0 ] 2>/dev/null && git_seg+="${cyan}↓${BEHIND}${reset}"
        }
        [ -n "$PR_BADGE" ] && git_seg+="${PR_BADGE}"
    fi

    # Rate limit segment
    local rate_seg=""
    if [ -n "$five_hour_pct" ] && [ "$five_hour_pct" -gt 0 ] 2>/dev/null; then
        local fh_color
        fh_color=$(color_for_pct "$five_hour_pct")
        rate_seg="${fh_color}${five_hour_pct}%${reset}"
        [ -n "$SESSION_TIME" ] && rate_seg+="${dim}⏱${reset}${white}${SESSION_TIME}${reset}"
    fi

    # Weekly segment
    local weekly_seg=""
    if [ -n "$seven_day_pct" ] && [ "$seven_day_pct" -gt 0 ] 2>/dev/null; then
        local sd_color
        sd_color=$(color_for_pct "$seven_day_pct")
        weekly_seg="${sd_color}${seven_day_pct}%${reset}${dim}w${reset}"
    fi

    # Context bar (compact: 5 chars)
    local ctx_bar_sm
    ctx_bar_sm=$(build_bar "$CONTEXT_INT" 5)
    local ctx_seg="${ctx_bar_sm} ${CTX_COLOR}${CONTEXT_INT}%${reset}"

    # Assemble based on width
    local out="${blue}◈${reset} ${blue}${MODEL}${reset}${EFFORT}"

    if [ "$COLS" -ge 120 ]; then
        out+="${dot}${magenta}\$${COST_FMT}${reset}${DAILY_SUFFIX}"
        out+="${dot}${ctx_seg}"
        [ -n "$git_seg" ] && out+="${dot}${git_seg}"
        [ -n "$rate_seg" ] && out+="${dot}${rate_seg}"
        [ -n "$weekly_seg" ] && out+="${dot}${weekly_seg}"
    elif [ "$COLS" -ge 80 ]; then
        out+="${dot}${magenta}\$${COST_FMT}${reset}"
        out+="${dot}${ctx_seg}"
        [ -n "$git_seg" ] && out+="${dot}${git_seg}"
        [ -n "$rate_seg" ] && out+="${dot}${rate_seg}"
    else
        out+="${dot}${magenta}\$${COST_FMT}${reset}"
        out+="${dot}${CTX_COLOR}${CONTEXT_INT}%${reset}"
        [ -n "$BRANCH" ] && out+="${dot}${BRANCH}"
        [ -n "$five_hour_pct" ] && out+="${dot}${five_hour_pct}%"
    fi

    printf "%b" "$out"
}

# ── Render: rprompt (zsh right-prompt compatible) ──────────
render_rprompt() {
    # Zsh prompt color escapes (256-color approximations)
    local zb='%F{39}'     # blue
    local zm='%F{141}'    # magenta
    local zg='%F{35}'     # green
    local zo='%F{215}'    # orange
    local zr='%F{203}'    # red
    local zy='%F{220}'    # yellow
    local zc='%F{73}'     # cyan
    local zd='%F{240}'    # dim
    local zf='%f'         # reset

    # Context color for zsh
    local zctx_color="$zg"
    [ "$CONTEXT_INT" -ge 90 ] 2>/dev/null && zctx_color="$zr"
    [ "$CONTEXT_INT" -ge 70 ] 2>/dev/null && [ "$CONTEXT_INT" -lt 90 ] && zctx_color="$zy"
    [ "$CONTEXT_INT" -ge 50 ] 2>/dev/null && [ "$CONTEXT_INT" -lt 70 ] && zctx_color="$zo"

    # Rate limit color for zsh
    local zrl_color="$zg"
    if [ -n "$five_hour_pct" ]; then
        [ "$five_hour_pct" -ge 90 ] 2>/dev/null && zrl_color="$zr"
        [ "$five_hour_pct" -ge 70 ] 2>/dev/null && [ "$five_hour_pct" -lt 90 ] && zrl_color="$zy"
        [ "$five_hour_pct" -ge 50 ] 2>/dev/null && [ "$five_hour_pct" -lt 70 ] && zrl_color="$zo"
    fi

    # Git segment
    local zgit=""
    if [ -n "$BRANCH" ]; then
        if $IS_DIRTY; then
            zgit="${zo}⎇${BRANCH}${zr}✦${zf}"
        else
            zgit="${zg}⎇${BRANCH}${zf}"
        fi
    fi

    # Build the rprompt string
    local rp="${zb}◈${zf} ${zm}\$${COST_FMT}${zf}"
    rp+=" ${zctx_color}${CONTEXT_INT}%%${zf}"
    [ -n "$zgit" ] && rp+=" ${zgit}"
    [ -n "$five_hour_pct" ] && rp+=" ${zrl_color}${five_hour_pct}%%${zf}"

    # Write to file for zsh to pick up
    # Usage: add to .zshrc:
    #   _claude_rprompt() {
    #     local f=~/.claude/rprompt.txt
    #     [[ -f "$f" ]] || return
    #     local age=$(( $(date +%s) - $(stat -f %m "$f") ))
    #     (( age > 300 )) && { RPROMPT=""; return }
    #     RPROMPT="$(cat "$f")"
    #   }
    #   add-zsh-hook precmd _claude_rprompt
    echo "$rp" > "$HOME/.claude/rprompt.txt"

    # Also emit sigil format to stdout for Claude Code's own status area
    render_sigil
}

# ── Render: sparkline (default + history strips) ──────────
render_sparkline() {
    local history_file="$HOME/.claude/session-history.jsonl"

    # Append current state to history (dedupe by session)
    if [ -n "$SESSION_ID" ]; then
        local ts
        ts=$(date +%s)
        local entry
        entry=$(printf '{"ts":%s,"sid":"%s","cost":%s,"tokens":%s,"sub":%s,"ctx":%s,"rate":%s,"acct":"%s"}' \
            "$ts" "$SESSION_ID" "$COST" "$((INPUT_TOKENS + OUTPUT_TOKENS))" "${SUBAGENT_TOKENS:-0}" "$CONTEXT_INT" "${five_hour_pct:-0}" "${ACCT_TAG:-}")
        echo "$entry" >> "$history_file"

        # Prune: keep only last entry per session, max 100 entries
        if [ -f "$history_file" ] && [ "$(wc -l < "$history_file")" -gt 200 ]; then
            # Dedupe by sid (keep last), then tail 100
            local tmpf
            tmpf=$(mktemp "${history_file}.XXXXXX")
            awk -F'"sid":"' '{split($2,a,"\""); sid=a[1]; lines[sid]=$0} END {for(s in lines) print lines[s]}' \
                "$history_file" | tail -100 > "$tmpf" && mv "$tmpf" "$history_file"
        fi
    fi

    # Build sparkline from history
    local sparkline_cost="" sparkline_rate=""
    if [ -f "$history_file" ]; then
        local spark_chars=(▁ ▂ ▃ ▄ ▅ ▆ ▇ █)

        # Read last 15 unique sessions' cost values (POSIX awk compatible)
        local costs
        costs=$(awk -F'"sid":"' '{
            split($2,a,"\""); sid=a[1]
            n=split($0,parts,"\"cost\":")
            if(n>1) {split(parts[2],cv,","); sub(/}/,"",cv[1]); lines[sid]=cv[1]+0}
        } END {for(s in lines) print lines[s]}' "$history_file" | tail -15)

        if [ -n "$costs" ] && [ "$(echo "$costs" | wc -l)" -gt 2 ]; then
            local min_c max_c
            min_c=$(echo "$costs" | sort -n | head -1)
            max_c=$(echo "$costs" | sort -n | tail -1)
            local range_c
            range_c=$(awk "BEGIN {r=$max_c - $min_c; print (r > 0 ? r : 1)}")

            while IFS= read -r val; do
                local idx
                idx=$(awk "BEGIN {i=int(($val - $min_c) / $range_c * 7); if(i>7) i=7; if(i<0) i=0; print i}")
                sparkline_cost+="${spark_chars[$idx]}"
            done <<< "$costs"
        fi

        # Rate limit sparkline (POSIX awk compatible)
        local rates
        rates=$(awk -F'"rate":' '{
            split($2,a,"}"); val=a[1]+0
            if(val > 0) {
                n=split($0,parts,"\"sid\":\"")
                if(n>1) {split(parts[2],sv,"\""); lines[sv[1]]=val}
            }
        } END {for(s in lines) print lines[s]}' "$history_file" | tail -15)

        if [ -n "$rates" ] && [ "$(echo "$rates" | wc -l)" -gt 2 ]; then
            local min_r max_r
            min_r=$(echo "$rates" | sort -n | head -1)
            max_r=$(echo "$rates" | sort -n | tail -1)
            local range_r
            range_r=$(awk "BEGIN {r=$max_r - $min_r; print (r > 0 ? r : 1)}")

            while IFS= read -r val; do
                local idx
                idx=$(awk "BEGIN {i=int(($val - $min_r) / $range_r * 7); if(i>7) i=7; if(i<0) i=0; print i}")
                sparkline_rate+="${spark_chars[$idx]}"
            done <<< "$rates"
        fi
    fi

    # Render default format first
    render_default

    # Append sparklines if we have data
    if [ -n "$sparkline_cost" ]; then
        printf "\n${dim}${white}$(printf "%-7s" "trend")${reset} ${magenta}cost${reset}${dim}${sparkline_cost}${reset}"
        [ -n "$sparkline_rate" ] && printf "  ${cyan}rate${reset}${dim}${sparkline_rate}${reset}"
    fi
}

# ── Render: iterm2 (terminal-native status bar) ───────────
render_iterm2() {
    # Emit iTerm2 user variables via OSC 1337
    emit_iterm2_var() {
        local name="$1" value="$2"
        local encoded
        encoded=$(printf '%s' "$value" | base64 | tr -d '\n')
        printf '\033]1337;SetUserVar=%s=%s\007' "$name" "$encoded"
    }

    # Emit Kitty window title via OSC 2
    emit_kitty_title() {
        local title="$1"
        printf '\033]2;%s\007' "$title"
    }

    # Detect terminal
    if [ -n "$ITERM_SESSION_ID" ]; then
        # Push structured data to iTerm2 status bar components
        emit_iterm2_var "claude_model" "${MODEL}${effort_val:+.${effort_val}}"
        emit_iterm2_var "claude_cost" "\$${COST_FMT}${DAILY_SUFFIX:+ ($DAILY_FMT/d)}"
        emit_iterm2_var "claude_ctx" "ctx:${CONTEXT_INT}%"

        local git_val=""
        [ -n "$BRANCH" ] && {
            git_val="$BRANCH"
            $IS_DIRTY && git_val+="*"
            [ "$AHEAD" -gt 0 ] 2>/dev/null && git_val+=" ↑${AHEAD}"
            [ "$BEHIND" -gt 0 ] 2>/dev/null && git_val+=" ↓${BEHIND}"
        }
        emit_iterm2_var "claude_git" "$git_val"
        emit_iterm2_var "claude_rate" "${five_hour_pct:-0}% / ${seven_day_pct:-0}%"
        emit_iterm2_var "claude_timer" "${SESSION_TIME:-0:00}"

    elif [ -n "$KITTY_WINDOW_ID" ]; then
        # Set window title to a plain-text sigil line
        local title="◈ ${MODEL}"
        title+=" · \$${COST_FMT}"
        title+=" · ctx:${CONTEXT_INT}%"
        [ -n "$BRANCH" ] && {
            title+=" · ${BRANCH}"
            $IS_DIRTY && title+="*"
        }
        [ -n "$five_hour_pct" ] && title+=" · rate:${five_hour_pct}%"
        emit_kitty_title "$title"
    fi

    # Always emit sigil format to stdout as fallback for Claude Code's status area
    render_sigil
}

# ── Notification Center alerts ─────────────────────────────
# Fires macOS notifications at key thresholds with once-per-event dedup.
# Thresholds: rate limit 80/90/95%, context 80/95%, budget 90/100%
notify_check() {
    command -v osascript >/dev/null 2>&1 || return
    local state_file="/tmp/claude/statusline-notif-state.json"
    [ ! -f "$state_file" ] && echo '{}' > "$state_file"

    local state
    state=$(cat "$state_file" 2>/dev/null)
    local changed=false
    local now_ts
    now_ts=$(date +%s)

    # Helper: fire notification if threshold crossed and not already fired at this tier
    check_threshold() {
        local key="$1" pct="$2" tier="$3" title="$4" msg="$5"
        [ -z "$pct" ] || [ "$pct" -lt "$tier" ] 2>/dev/null && return
        local fired_tier
        fired_tier=$(echo "$state" | jq -r --arg k "$key" '.[$k].tier // 0' 2>/dev/null)
        [ "$fired_tier" -ge "$tier" ] 2>/dev/null && return

        # Fire notification
        osascript -e "display notification \"$msg\" with title \"Claude Code\" subtitle \"$title\"" 2>/dev/null &
        state=$(echo "$state" | jq --arg k "$key" --argjson t "$tier" --argjson ts "$now_ts" \
            '.[$k] = {"tier": $t, "ts": $ts}' 2>/dev/null)
        changed=true
    }

    # Reset fired state when value drops well below threshold
    reset_if_below() {
        local key="$1" pct="$2" reset_below="$3"
        [ -z "$pct" ] && return
        [ "$pct" -lt "$reset_below" ] 2>/dev/null && {
            state=$(echo "$state" | jq --arg k "$key" 'del(.[$k])' 2>/dev/null)
            changed=true
        }
    }

    # Rate limit checks
    if [ -n "$five_hour_pct" ]; then
        reset_if_below "rate" "$five_hour_pct" 50
        check_threshold "rate" "$five_hour_pct" 80 "Rate Limit Warning" "5-hour window at ${five_hour_pct}%"
        check_threshold "rate" "$five_hour_pct" 90 "Rate Limit High" "5-hour window at ${five_hour_pct}% — consider pausing"
        check_threshold "rate" "$five_hour_pct" 95 "Rate Limit Critical" "5-hour window at ${five_hour_pct}% — near limit"
    fi

    # Context checks
    if [ -n "$CONTEXT_INT" ]; then
        check_threshold "ctx" "$CONTEXT_INT" 80 "Context Window" "Context at ${CONTEXT_INT}% — consider /compact"
        check_threshold "ctx" "$CONTEXT_INT" 95 "Context Critical" "Context at ${CONTEXT_INT}% — compact now or lose session"
    fi

    # Budget checks
    if [ "$DAILY_BUDGET" -gt 0 ] 2>/dev/null; then
        local bpct
        bpct=$(awk "BEGIN {printf \"%.0f\", $DAILY_COST * 100 / $DAILY_BUDGET}")
        reset_if_below "budget" "$bpct" 50
        check_threshold "budget" "$bpct" 90 "Budget Warning" "Daily spend at \$${DAILY_FMT} of \$${DAILY_BUDGET} (${bpct}%)"
        check_threshold "budget" "$bpct" 100 "Budget Exceeded" "Daily spend \$${DAILY_FMT} exceeds \$${DAILY_BUDGET} budget"
    fi

    $changed && echo "$state" > "$state_file"
}
notify_check

# ── Format dispatch ───────────────────────────────────────
FORMAT="${STATUSLINE_FORMAT:-${FORMAT:-default}}"

case "$FORMAT" in
    sigil)     render_sigil ;;
    rprompt)   render_rprompt ;;
    sparkline) render_sparkline ;;
    iterm2)    render_iterm2 ;;
    *)         render_default ;;
esac

exit 0
