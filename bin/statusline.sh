#!/bin/bash
# shellcheck disable=SC2059,SC2034,SC2154,SC2153,SC1090,SC2329,SC2016
set -f

input=$(cat)

# Config: ~/.claude/statusline.conf (sourced as bash)
#   HOURLY_RATE=150           # Billing rate in $/hour (enables cost tracking)
#   DAILY_BUDGET=20           # Daily cost ceiling in $ (enables budget bar)
#   CHALLENGE_GOAL_M=100      # Token goal in millions (enables challenge progress line)
#   CHALLENGE_START=...       # ISO date (YYYY-MM-DD) for challenge window start
#   CHALLENGE_LABEL=100m      # Label shown on the challenge line
#   NARROW_THRESHOLD=60       # default render auto-falls-through to narrow
#                             # when detected terminal cols are below this
#   MAX_COLS=80               # force a specific terminal width (overrides
#                             # auto-detection — useful when Claude Code's
#                             # status panel is narrower than the terminal)

if [ -z "$input" ]; then
    printf "Claude"
    exit 0
fi

# ── Config ──────────────────────────────────────────────
CONFIG_FILE="$HOME/.claude/statusline.conf"
HOURLY_RATE=0
DAILY_BUDGET=0
CHALLENGE_GOAL_M=0                      # Challenge goal in millions (0 = disabled)
CHALLENGE_LABEL="goal"
[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"

# Export classifier config so scan-tokens.py (spawned in background) sees it.
export WORK_PATHS PERSONAL_PATHS WORK_KEYWORDS PERSONAL_KEYWORDS
export EMAIL_PAYER_MAP CHALLENGE_START BOUNTY_TARGET_TOKENS
export BOUNTY_LOOKBACK_DAYS BOUNTY_SESSION_GAP_MIN
export BRANCH_PREFIX_STRIP MAX_BRANCH LABEL_COLORS

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
dim='\033[38;2;220;220;220m'
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

# fmt_duration_m MINUTES — m / h / d depending on size. Always takes minutes.
fmt_duration_m() {
    awk "BEGIN {
        m = $1; if (m < 0) m = -m;
        if (m >= 2880) printf \"%.2fd\", m / 1440;
        else if (m >= 60) printf \"%.2fh\", m / 60;
        else printf \"%.0fm\", m
    }"
}

# pad_right TEXT WIDTH — print TEXT then trailing spaces so total visible width is WIDTH.
# Bash's ${#var} counts characters (not bytes) under UTF-8 locales, so unicode like "→" counts as 1.
pad_right() {
    local text="$1" width=$2
    local n=$(( width - ${#text} ))
    [ "$n" -lt 0 ] && n=0
    printf "%s%*s" "$text" "$n" ""
}

# fmt_pct VAL — format VAL as a 6-char left-aligned pct (including the "%" sign):
#   99.0   →   "99%   "  (trailing zeros stripped; API only gives 1-dec precision)
#   99.5   →   "99.5% "
#   38.28  →   "38.28%"  (interpolation gave us genuine 2-dec precision)
#   100    →   "100%  "
# Shows precision the value actually has, no fake trailing zeros.
fmt_pct() {
    awk -v v="$1" 'BEGIN {
        s = sprintf("%.2f", v)
        if (s ~ /\./) { sub(/0+$/, "", s); sub(/\.$/, "", s) }
        printf "%-6s", s "%"
    }'
}

# secs_since_last_user SID CWD — seconds since the last user-role message in the
# session JSONL. Bounded tail scan (last 200 lines). Empty if no match.
secs_since_last_user() {
    local sid="$1" cwd="$2"
    [ -z "$sid" ] || [ -z "$cwd" ] && return
    local project_dir
    project_dir=$(echo "$cwd" | tr '/' '-')
    local session_file="$HOME/.claude/projects/${project_dir}/${sid}.jsonl"
    [ -f "$session_file" ] || return
    local ts
    ts=$(tail -n 200 "$session_file" 2>/dev/null | grep '"type":"user"' | tail -1 | jq -r '.timestamp // empty' 2>/dev/null)
    [ -z "$ts" ] && return
    local ts_epoch
    ts_epoch=$(iso_to_epoch "$ts")
    [ -z "$ts_epoch" ] && return
    echo $(( $(date +%s) - ts_epoch ))
}

# Live topic for the tab title: the freshest substantive user message in this
# session's transcript. Claude Code's own session_name is generated once at
# session start and never refreshed, so a long session's title goes stale.
session_topic() {
    local sid="$1" cwd="$2"
    [ -z "$sid" ] || [ -z "$cwd" ] && return
    # A /retitle pin (keyed by TTY, written by set-tab-title.sh) outranks the
    # derived topic so both title mechanisms agree.
    local tpid=$$ ttty
    for _ in 1 2 3 4; do
        ttty=$(ps -o tty= -p "$tpid" 2>/dev/null | tr -d ' ')
        if [ -n "$ttty" ] && [ "$ttty" != "??" ]; then
            if [ -f "/tmp/claude/tab-title-pin-${ttty}.txt" ]; then
                cat "/tmp/claude/tab-title-pin-${ttty}.txt"
                return
            fi
            break
        fi
        tpid=$(ps -o ppid= -p "$tpid" 2>/dev/null | tr -d ' ')
        [ -z "$tpid" ] || [ "$tpid" = "0" ] || [ "$tpid" = "1" ] && break
    done
    local project_dir session_file cache
    project_dir=$(echo "$cwd" | tr '/' '-')
    session_file="$HOME/.claude/projects/${project_dir}/${sid}.jsonl"
    [ -f "$session_file" ] || return
    cache="/tmp/claude/statusline-topic-${sid}.txt"
    if [ -f "$cache" ] && [ "$cache" -nt "$session_file" ]; then
        cat "$cache"
        return
    fi
    local topic
    topic=$(tail -n 300 "$session_file" 2>/dev/null | grep '"type":"user"' | jq -r '
        (.message.content // empty)
        | if type == "string" then .
          else ([.[]? | select(.type == "text") | .text] | join(" ")) end
    ' 2>/dev/null | awk '
        { gsub(/[[:space:]]+/, " "); sub(/^ /, ""); sub(/ $/, "") }
        length($0) >= 18 && substr($0,1,1) != "<" && substr($0,1,1) != "[" { last = $0 }
        END { if (last != "") { if (length(last) > 48) last = substr(last, 1, 47) "…"; print last } }
    ')
    [ -n "$topic" ] && printf '%s' "$topic" > "$cache" && printf '%s\n' "$topic"
}

#   PCT        integer 0..100
#   DIRECTION  "high-bad" (default) — green low, red at 90+: usage, context, rate
#              "low-bad"             — green high, red at 10-: remaining, headroom
color_for_pct() {
    local pct=$1
    local dir="${2:-high-bad}"
    if [ "$dir" = "low-bad" ]; then
        if   [ "$pct" -le 10 ] 2>/dev/null; then printf "$red"
        elif [ "$pct" -le 30 ] 2>/dev/null; then printf "$yellow"
        elif [ "$pct" -le 50 ] 2>/dev/null; then printf "$orange"
        else printf "$green"
        fi
    else
        if   [ "$pct" -ge 90 ] 2>/dev/null; then printf "$red"
        elif [ "$pct" -ge 70 ] 2>/dev/null; then printf "$yellow"
        elif [ "$pct" -ge 50 ] 2>/dev/null; then printf "$orange"
        else printf "$green"
        fi
    fi
}

#   DIRECTION optional, passed through to color_for_pct ("high-bad" default).
build_bar() {
    local pct=$1
    local width=$2
    local dir="${3:-high-bad}"
    [ "$pct" -lt 0 ] 2>/dev/null && pct=0
    [ "$pct" -gt 100 ] 2>/dev/null && pct=100

    local filled=$(( pct * width / 100 ))
    local empty=$(( width - filled ))
    local bar_color
    bar_color=$(color_for_pct "$pct" "$dir")

    local filled_str="" empty_str=""
    for ((i=0; i<filled; i++)); do filled_str+="●"; done
    for ((i=0; i<empty; i++)); do empty_str+="○"; done

    printf "${bar_color}${filled_str}${dim}${empty_str}${reset}"
}

# Sweet-spot zones for context fill (fighting-game combo meter style).
# Override via env: CTX_SWEET_LO / CTX_SWEET_HI / CTX_HOT (ints, 0-100).
CTX_SWEET_LO="${CTX_SWEET_LO:-30}"
CTX_SWEET_HI="${CTX_SWEET_HI:-70}"
CTX_HOT="${CTX_HOT:-85}"

# color_for_context PCT — sweet-spot palette. Below sweet = cool (blue, "loading in"),
# in sweet = green ("in the zone"), above sweet but below hot = yellow ("wrap up"),
# at/above hot = red ("compact now").
color_for_context() {
    local pct=$1
    if   [ "$pct" -ge "$CTX_HOT" ] 2>/dev/null;      then printf "$red"
    elif [ "$pct" -gt "$CTX_SWEET_HI" ] 2>/dev/null; then printf "$yellow"
    elif [ "$pct" -ge "$CTX_SWEET_LO" ] 2>/dev/null; then printf "$green"
    else printf "$blue"
    fi
}

# build_context_bar PCT WIDTH — sweet-spot meter. The empty track marks the
# sweet-spot band (dim green ○) and the hot zone (dim red ○); filled cells are
# colored by current zone. Makes the "target range" visible at a glance.
build_context_bar() {
    local pct=$1 width=$2
    [ "$pct" -lt 0 ] 2>/dev/null && pct=0
    [ "$pct" -gt 100 ] 2>/dev/null && pct=100

    local filled=$(( pct * width / 100 ))
    local sweet_lo_idx=$(( CTX_SWEET_LO * width / 100 ))
    local sweet_hi_idx=$(( CTX_SWEET_HI * width / 100 ))
    local hot_idx=$(( CTX_HOT * width / 100 ))

    local fill_color
    fill_color=$(color_for_context "$pct")

    local out="" i cell
    for ((i=0; i<width; i++)); do
        if [ "$i" -lt "$filled" ]; then
            cell="${fill_color}●${reset}"
        else
            # Empty cell — tint track to show the target band.
            if   [ "$i" -ge "$hot_idx" ];      then cell="${red}${dim:+}○${reset}"
            elif [ "$i" -ge "$sweet_lo_idx" ] && [ "$i" -lt "$sweet_hi_idx" ]; then
                cell="${green}○${reset}"
            else cell="${dim}○${reset}"
            fi
        fi
        out+="$cell"
    done
    printf "%b" "$out"
}

# build_ratio_bar WORK_PCT PERSONAL_PCT WIDTH EMPTY_CHAR
# Two-color stacked ratio bar: cyan work + magenta personal + dim empty.
build_ratio_bar() {
    local work_pct=$1 personal_pct=$2 width=$3 empty_char=$4
    local work_dots=$(( work_pct * width / 100 ))
    local personal_dots=$(( personal_pct * width / 100 ))
    [ $((work_dots + personal_dots)) -gt "$width" ] && personal_dots=$((width - work_dots))
    local empty_dots=$((width - work_dots - personal_dots))
    [ "$empty_dots" -lt 0 ] && empty_dots=0
    local work_str="" personal_str="" empty_str="" i
    for ((i=0; i<work_dots; i++)); do work_str+="●"; done
    for ((i=0; i<personal_dots; i++)); do personal_str+="●"; done
    for ((i=0; i<empty_dots; i++)); do empty_str+="$empty_char"; done
    printf "${cyan}${work_str}${magenta}${personal_str}${dim}${empty_str}${reset}"
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
    local org_uuid="${2:-}"
    [ -z "$email" ] && return

    # Patterns: "tag:email" matches by email; "tag:email|uuid" requires an
    # exact uuid match (for emails shared across orgs, e.g. company seat +
    # personal Max plan). UUID-qualified hits beat bare hits.
    if [ -n "$ACCOUNT_LABELS" ]; then
        local pair label pattern pat_email pat_uuid bare_match=""
        for pair in $ACCOUNT_LABELS; do
            label="${pair%%:*}"
            pattern="${pair#*:}"
            if [[ "$pattern" == *"|"* ]]; then
                pat_email="${pattern%%|*}"
                pat_uuid="${pattern#*|}"
                # shellcheck disable=SC2254
                case "$email" in $pat_email)
                    [ "$org_uuid" = "$pat_uuid" ] && { echo "$label"; return; }
                    ;;
                esac
            else
                # shellcheck disable=SC2254
                case "$email" in $pattern)
                    [ -z "$bare_match" ] && bare_match="$label"
                    ;;
                esac
            fi
        done
        [ -n "$bare_match" ] && { echo "$bare_match"; return; }
    fi

    echo "$email"
}

# ── Reusable ledger writer ───────────────────────────────
# Usage: update_ledger <mode> <file> <session_id> <value> <today> [acct]
#
# mode=cost:  monotonic values (e.g. cost). Stores {baseline, current} per
#   session; daily delta (sum of current-baseline) via LEDGER_RESULT, this
#   session's delta via LEDGER_SESSION_DELTA.
# mode=token: non-monotonic values (context-window tokens). Accumulates
#   positive deltas — growth adds the increment, a drop (compaction) adds 0 —
#   storing {last_seen, accumulated} per session; daily total via
#   TOKEN_LEDGER_RESULT, session total via TOKEN_LEDGER_SESSION.
LEDGER_RESULT=0
LEDGER_SESSION_DELTA=0
TOKEN_LEDGER_RESULT=0
TOKEN_LEDGER_SESSION=0
update_ledger() {
    local mode="$1" file="$2" sid="$3" value="$4" today="$5" acct="${6:-}"

    local ledger_date="" has_baseline=""
    if [ -f "$file" ]; then
        if [ "$mode" = token ]; then
            ledger_date=$(jq -r '.date // ""' "$file" 2>/dev/null)
        else
            local info
            info=$(jq -r --arg sid "$sid" '[.date // "", (.sessions[$sid].baseline // empty | tostring)] | join("|")' "$file" 2>/dev/null)
            ledger_date="${info%%|*}"
            has_baseline="${info#*|}"
        fi
    fi

    if [ ! -f "$file" ] || [ "$ledger_date" != "$today" ]; then
        # New day or first ever write — reset all sessions.
        local acct_tail=""
        [ -n "$acct" ] && acct_tail=$(printf ',"acct":"%s"' "$acct")
        if [ "$mode" = token ]; then
            printf '{"date":"%s","sessions":{"%s":{"last_seen":%s,"accumulated":0%s}}}' \
                "$today" "$sid" "$value" "$acct_tail" > "$file"
            TOKEN_LEDGER_RESULT=0
            TOKEN_LEDGER_SESSION=0
        else
            printf '{"date":"%s","sessions":{"%s":{"baseline":%s,"current":%s%s}}}' \
                "$today" "$sid" "$value" "$value" "$acct_tail" > "$file"
            LEDGER_RESULT=0
            LEDGER_SESSION_DELTA=0
        fi
        return
    fi

    # Same day, file exists — update this session in place.
    if [ "$mode" = token ]; then
        # Read last_seen, compute delta, accumulate, write back in one call.
        jq_update "$file" --arg sid "$sid" --argjson val "$value" --arg acct "$acct" '
            (.sessions[$sid].last_seen // $val) as $prev |
            (if $val > $prev then $val - $prev else 0 end) as $delta |
            .sessions[$sid] = ((.sessions[$sid] // {}) + {
                "last_seen": $val,
                "accumulated": ((.sessions[$sid].accumulated // 0) + $delta)
            } + (if $acct != "" then {"acct": $acct} else {} end))'
        eval "$(jq -r --arg sid "$sid" '
            "TOKEN_LEDGER_RESULT=" + ([.sessions[] | .accumulated // 0] | add // 0 | tostring),
            "TOKEN_LEDGER_SESSION=" + (.sessions[$sid].accumulated // 0 | tostring)
        ' "$file" 2>/dev/null)"
        [ -z "$TOKEN_LEDGER_RESULT" ] && TOKEN_LEDGER_RESULT=0
        [ -z "$TOKEN_LEDGER_SESSION" ] && TOKEN_LEDGER_SESSION=0
        return
    fi

    if [ -z "$has_baseline" ]; then
        # First time seeing this session today — seed baseline from existing
        # current (if any) so delta counts from NOW forward.
        jq_update "$file" --arg sid "$sid" --argjson val "$value" --arg acct "$acct" \
            '.sessions[$sid] = ((.sessions[$sid] // {}) + {"baseline": (.sessions[$sid].current // $val), "current": $val} + (if $acct != "" then {"acct": $acct} else {} end))'
    else
        jq_update "$file" --arg sid "$sid" --argjson val "$value" --arg acct "$acct" \
            '.sessions[$sid].current = $val | (if $acct != "" then .sessions[$sid].acct = $acct else . end)'
    fi
    # Null-coalesce .current and .baseline so a legacy {current: N} row with no
    # baseline doesn't make jq throw on "number - null" and blank both vars.
    eval "$(jq -r --arg sid "$sid" '
        "LEDGER_RESULT=" + ([.sessions[] | (.current // 0) - (.baseline // 0)] | add // 0 | tostring),
        "LEDGER_SESSION_DELTA=" + ((.sessions[$sid].current // 0) - (.sessions[$sid].baseline // 0) | tostring)
    ' "$file" 2>/dev/null)"
    [ -z "$LEDGER_RESULT" ] && LEDGER_RESULT=0
    [ -z "$LEDGER_SESSION_DELTA" ] && LEDGER_SESSION_DELTA=0
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
        # Sum input + output tokens across subagent files.
        # set +f locally: script-wide `set -f` (L3) blocks glob expansion otherwise.
        set +f
        local agent_files=( "$subagent_path"/agent-*.jsonl )
        set -f
        if [ -e "${agent_files[0]}" ]; then
            SUBAGENT_TOKENS=$(jq -s '[.[].message.usage | select(.) | (.input_tokens // 0) + (.output_tokens // 0)] | add // 0' "${agent_files[@]}" 2>/dev/null)
            [ -z "$SUBAGENT_TOKENS" ] && SUBAGENT_TOKENS=0
        fi
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

# Format an epoch as a strftime string, portable across GNU (Linux) and BSD
# (macOS) date. GNU takes `-d @<epoch>`, BSD takes `-r <epoch>`. Detected once.
# Prior code chained `date -j -r ... | sed | tr || date -d ...`, but the pipe
# made the pipeline exit status tr's (0), so the GNU fallback never fired on
# Linux and every formatted time rendered blank.
if date -d @0 +%s >/dev/null 2>&1; then
    _DATE_IS_GNU=1
else
    _DATE_IS_GNU=0
fi

# Display timezone for reset times. The Claude session may run on a host whose
# system clock is UTC (e.g. hound), so resolve a real zone rather than showing
# UTC. Precedence: $STATUSLINE_TZ  →  ~/.claude/statusline-tz file  →  $TZ  →
# home default. When travelling, set the zone for "where you are", e.g.:
#   echo Europe/Rome    > ~/.claude/statusline-tz   # Portofino
#   echo America/Denver > ~/.claude/statusline-tz   # Jackson Hole
#   rm ~/.claude/statusline-tz                       # back to home (PT)
_SL_TZ="${STATUSLINE_TZ:-}"
if [ -z "$_SL_TZ" ] && [ -f "$HOME/.claude/statusline-tz" ]; then
    _SL_TZ=$(tr -d '[:space:]' < "$HOME/.claude/statusline-tz" 2>/dev/null)
fi
[ -z "$_SL_TZ" ] && _SL_TZ="${TZ:-America/Los_Angeles}"

fmt_epoch() {  # $1=epoch  $2=strftime format — rendered in $_SL_TZ
    if [ "$_DATE_IS_GNU" = 1 ]; then
        TZ="$_SL_TZ" date -d "@$1" +"$2" 2>/dev/null
    else
        TZ="$_SL_TZ" date -r "$1" +"$2" 2>/dev/null
    fi
}

format_reset_time() {
    local iso_str="$1"
    local style="$2"
    [ -z "$iso_str" ] || [ "$iso_str" = "null" ] && return

    local epoch
    epoch=$(iso_to_epoch "$iso_str")
    [ -z "$epoch" ] && return

    # If the reset time is in the past, project forward in 5-hour increments.
    # 30s grace so a reset that just elapsed rolls forward instead of
    # displaying as "now" for half a minute.
    local now
    now=$(date +%s)
    while [ "$epoch" -le "$((now + 30))" ]; do
        epoch=$((epoch + 18000))
    done

    local tz
    tz=$(fmt_epoch "$epoch" "%Z")

    case "$style" in
        time)
            local raw
            raw=$(fmt_epoch "$epoch" "%l:%M%p" | sed 's/^ //; s/\.//g' | tr '[:upper:]' '[:lower:]')
            [ -n "$raw" ] && printf '%s %s' "$raw" "$tz"
            ;;
        datetime)
            local raw
            raw=$(fmt_epoch "$epoch" "%b %-d, %l:%M%p" | sed 's/  / /g; s/^ //; s/\.//g' | tr '[:upper:]' '[:lower:]')
            [ -n "$raw" ] && printf '%s %s' "$raw" "$tz"
            ;;
        date)
            fmt_epoch "$epoch" "%b %-d" | tr '[:upper:]' '[:lower:]'
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
ACCT_ORG_UUID=""
profile_cache_file="/tmp/claude/statusline-profile-cache.json"
if [ -f "$profile_cache_file" ]; then
    ACCT_EMAIL=$(jq -r '.account.email // empty' "$profile_cache_file" 2>/dev/null)
    ACCT_ORG_UUID=$(jq -r '.organization.uuid // empty' "$profile_cache_file" 2>/dev/null)
    ACCT_TAG=$(resolve_account_label "$ACCT_EMAIL" "$ACCT_ORG_UUID")
fi

# ── Daily cost ledger ──────────────────────────────────
DAILY_LEDGER="$HOME/.claude/daily-cost.json"
TODAY=$(date +%Y-%m-%d)
DAILY_COST="$COST"
if [ -n "$SESSION_ID" ] && [ "$(awk "BEGIN {print ($COST > 0)}")" = "1" ]; then
    update_ledger cost "$DAILY_LEDGER" "$SESSION_ID" "$COST" "$TODAY" "$ACCT_TAG"
    DAILY_COST="${LEDGER_RESULT:-0}"
fi
DAILY_FMT=$(printf "%.2f" "$DAILY_COST")

# ── Token challenge tracker (reads token-scan-cache.json) ─────
TOKEN_DISPLAY=""

IDLE_DISPLAY=""
if [ -n "$SESSION_ID" ]; then
    # total_input_tokens already includes cache reads (Anthropic API contract).
    # current_usage.cache_* are per-window, not cumulative — mixing them in
    # made SESSION_TOKENS non-monotonic and caused negative session deltas.
    SESSION_TOKENS=$((INPUT_TOKENS + OUTPUT_TOKENS))
    get_subagent_tokens "$SESSION_ID" "$CWD"

    # Time since last user message — signals how long the current turn has been running.
    # Only show when idle > 30s, to avoid flicker during fast back-and-forth.
    _idle_s=$(secs_since_last_user "$SESSION_ID" "$CWD")
    if [ -n "$_idle_s" ] && [ "$_idle_s" -gt 30 ] 2>/dev/null; then
        _idle_m=$(( _idle_s / 60 ))
        if [ "$_idle_s" -lt 60 ]; then
            IDLE_DISPLAY=" ${dim}idle ${_idle_s}s${reset}"
        else
            IDLE_DISPLAY=" ${dim}idle $(fmt_duration_m "$_idle_m")${reset}"
        fi
    fi

    # Two separate displays:
    #   TOKEN_DISPLAY      — all-time work/personal ratio (100% bar, no goal)
    #   CHALLENGE_DISPLAY  — since Mar 23 progress toward 100M goal
    # Source: token-scan-summary.json (~200B, fast) with fallback to token-scan-cache.json (30MB+).
    # Both written by scan-tokens.py. Summary is preferred to avoid re-parsing the big cache every render.
    SCAN_CACHE="$HOME/.claude/token-scan-cache.json"
    SCAN_SUMMARY="$HOME/.claude/token-scan-summary.json"
    # Resolution order:
    #   1. $SCAN_SCRIPT env (from statusline.conf)
    #   2. bin/scan-tokens.py next to this script (repo-local)
    if [ -z "${SCAN_SCRIPT:-}" ]; then
        SCAN_SCRIPT="${BASH_SOURCE[0]%/*}/scan-tokens.py"
    fi
    CHALLENGE_DISPLAY=""

    # Trigger background scan if cache is stale (>180s) or missing.
    # Tokens don't move fast; scanner walks every JSONL so keep frequency low per terminal.
    if [ -f "$SCAN_SCRIPT" ]; then
        scan_mtime=0
        [ -f "$SCAN_CACHE" ] && scan_mtime=$(stat -f %m "$SCAN_CACHE" 2>/dev/null || stat -c %Y "$SCAN_CACHE" 2>/dev/null || echo 0)
        scan_age=$(( now - scan_mtime ))
        if [ "$scan_age" -gt 180 ]; then
            # Pass config vars through so the scanner can classify w/o re-sourcing.
            (python3 "$SCAN_SCRIPT" --quiet >/dev/null 2>&1 &) >/dev/null 2>&1
        fi
    fi

    G_WORK=0 G_PERSONAL=0 G_UNKNOWN=0 G_TOTAL=0
    C_WORK=0 C_PERSONAL=0 C_TOTAL=0
    T_TOTAL=0
    R_SESSIONS=0 R_RANGES=0
    scan_src=""
    [ -f "$SCAN_SUMMARY" ] && scan_src="$SCAN_SUMMARY"
    [ -z "$scan_src" ] && [ -f "$SCAN_CACHE" ] && scan_src="$SCAN_CACHE"
    if [ -n "$scan_src" ]; then
        eval "$(jq -r '
            "G_WORK=" + (.global.work_tokens // 0 | tostring),
            "G_PERSONAL=" + (.global.personal_tokens // 0 | tostring),
            "G_UNKNOWN=" + (.global.unknown_tokens // 0 | tostring),
            "G_TOTAL=" + (.global.total_tokens // 0 | tostring),
            "G_RECOVERED=" + (.recovered_pre_scan_tokens // 0 | tostring),
            "C_WORK=" + (.challenge.work_tokens // 0 | tostring),
            "C_PERSONAL=" + (.challenge.personal_tokens // 0 | tostring),
            "C_TOTAL=" + (.challenge.total_tokens // 0 | tostring),
            "T_TOTAL=" + (.today.total_tokens // 0 | tostring),
            "R_SESSIONS=" + (.redactions.sessions // 0 | tostring),
            "R_RANGES=" + (.redactions.ranges // 0 | tostring),
            "BOUNTY_ETA_H=" + (.bounty.eta_hours // "" | tostring),
            "BOUNTY_TARGET=" + (.bounty.target // 0 | tostring),
            "BOUNTY_CLEARED=" + (.bounty.cleared // false | tostring),
            "BOUNTY_RATE=" + (.bounty.tokens_per_min // 0 | tostring)
        ' "$scan_src" 2>/dev/null)"
    fi

    DAILY_TOKEN_LEDGER="$HOME/.claude/daily-tokens.json"
    update_ledger token "$DAILY_TOKEN_LEDGER" "$SESSION_ID" "$SESSION_TOKENS" "$TODAY" "$ACCT_TAG"
    DAILY_TOKENS="${TOKEN_LEDGER_RESULT:-0}"
    SESSION_DELTA="${TOKEN_LEDGER_SESSION:-0}"
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
    SHARED_SUFFIX=" ${magenta}+${SESSION_TOKEN_FMT}${reset}"
    if [ "$SUBAGENT_TOKENS" -gt 0 ] 2>/dev/null; then
        sub_fmt=$(format_tokens "$SUBAGENT_TOKENS")
        SHARED_SUFFIX+=" ${dim}+${sub_fmt} sub${reset}"
    fi
    if [ "$DAILY_TOKENS" -gt "$SESSION_DELTA" ] 2>/dev/null; then
        SHARED_SUFFIX+=" ${dim}(+${DAILY_TOKEN_FMT}/d)${reset}"
    fi

    # ── Global display: 100% ratio bar (work vs personal, all-time) ──
    # Empty slots use "●" (filled, dim) so bar is always 100% full — it's a ratio, not progress.
    if [ "$G_TOTAL" -gt 0 ] 2>/dev/null; then
        # One awk call emits all 5 values — saves 4 subprocess spawns.
        eval "$(awk -v w="$G_WORK" -v p="$G_PERSONAL" -v t="$G_TOTAL" 'BEGIN {
            printf "G_WORK_PCT=%.0f\nG_PERSONAL_PCT=%.0f\nG_WORK_M=%.2f\nG_PERSONAL_M=%.2f\nG_TOTAL_M=%.2f\n",
                w*100/t, p*100/t, w/1e6, p/1e6, t/1e6
        }')"
        G_BAR=$(build_ratio_bar "$G_WORK_PCT" "$G_PERSONAL_PCT" 10 "●")
        # %2d pct parts so the compound is always 9 chars ("84%w/13%p" or " 5%w/95%p").
        # Matches 100m's padded pct width below, so (breakdown) aligns across both rows.
        gw=$(printf "%2d" "$G_WORK_PCT")
        gp=$(printf "%2d" "$G_PERSONAL_PCT")
        TOKEN_DISPLAY="${G_BAR} ${cyan}${gw}%w${reset}${dim}/${reset}${magenta}${gp}%p${reset} ${dim}(${reset}${cyan}${G_WORK_M}w${reset}${dim}+${reset}${magenta}${G_PERSONAL_M}p${reset}${dim}=${reset}${G_TOTAL_M}M${dim})${reset}"
    fi

    # ── Challenge display: progress toward goal (opt-in via config) ──
    # Only renders when CHALLENGE_GOAL_M is set in ~/.claude/statusline.conf.
    # Empty slots use "○" because the bar represents progress toward a goal.
    if [ "$C_TOTAL" -gt 0 ] 2>/dev/null && [ "$CHALLENGE_GOAL_M" -gt 0 ] 2>/dev/null; then
        GOAL_M="$CHALLENGE_GOAL_M"
        # One awk call emits all 6 values — saves 5 subprocess spawns.
        eval "$(awk -v w="$C_WORK" -v p="$C_PERSONAL" -v t="$C_TOTAL" -v g="$GOAL_M" 'BEGIN {
            printf "C_PCT=%.0f\nC_WORK_PCT=%.0f\nC_PERSONAL_PCT=%.0f\nC_WORK_M=%.2f\nC_PERSONAL_M=%.2f\nC_TOTAL_M=%.2f\n",
                t/(g*10000), w/(g*10000), p/(g*10000), w/1e6, p/1e6, t/1e6
        }')"
        [ "$C_PCT" -gt 100 ] 2>/dev/null && C_PCT=100
        C_BAR=$(build_ratio_bar "$C_WORK_PCT" "$C_PERSONAL_PCT" 10 "○")

        C_PCT_COLOR=$(color_for_pct "$C_PCT")
        # Pad pct to 9 cols so (breakdown) starts at the same column as tokens row.
        c_pct_padded=$(pad_right "$(printf "%3d%%" "$C_PCT")" 9)
        CHALLENGE_DISPLAY="${C_BAR} ${C_PCT_COLOR}${c_pct_padded}${reset} ${dim}(${reset}${cyan}${C_WORK_M}w${reset}${dim}+${reset}${magenta}${C_PERSONAL_M}p${reset}${dim}=${reset}${C_TOTAL_M}M${dim})/${GOAL_M}M${reset}${SHARED_SUFFIX}"
    fi

    # ── Bounty ETA (only while un-cleared and a rate signal exists) ──
    # Shows active-hours remaining until work tokens reach the bounty floor,
    # using a gap-aware rate over the last 3 days (computed by scan-tokens.py).
    BOUNTY_DISPLAY=""
    if [ "$BOUNTY_CLEARED" = "true" ]; then
        bounty_target_m=$(awk "BEGIN { printf \"%.0f\", $BOUNTY_TARGET/1000000 }")
        BOUNTY_DISPLAY="${green}✓${reset} ${dim}cleared ${bounty_target_m}M${reset}"
    elif [ -n "$BOUNTY_ETA_H" ] && [ "$BOUNTY_ETA_H" != "null" ] && [ "$BOUNTY_ETA_H" != "0" ]; then
        bounty_target_m=$(awk "BEGIN { printf \"%.0f\", $BOUNTY_TARGET/1000000 }")
        bounty_gap_m=$(awk "BEGIN { printf \"%.2f\", ($BOUNTY_TARGET - $C_WORK)/1000000 }")
        bounty_rate_kh=$(awk "BEGIN { printf \"%.0f\", $BOUNTY_RATE*60/1000 }")
        BOUNTY_DISPLAY="${cyan}→${bounty_target_m}M${reset} ${dim}~${reset}${BOUNTY_ETA_H}h ${dim}active (${reset}${bounty_gap_m}M left @ ${bounty_rate_kh}k/h${dim})${reset}"
    fi

    # ── Unified usage line (replaces tokens/100m/bounty in default render) ──
    # Shows today (since local midnight) · current session · lifetime total,
    # each formatted human-readably. All three are already computed above.
    USAGE_DISPLAY=""
    _usage_fmt() {
        awk -v t="$1" 'BEGIN {
            if (t >= 1e9) printf "%.2fB", t/1e9;
            else if (t >= 1e6) printf "%.2fM", t/1e6;
            else if (t >= 1e3) printf "%.2fk", t/1e3;
            else printf "%d", t
        }'
    }
    # Prefer scan-based T_TOTAL (summed from JSONL, includes cache reads) —
    # DAILY_TOKENS is a context-window-snapshot proxy that undercounts by
    # ~1000x on heavy sessions.
    _today_src=${T_TOTAL:-0}
    [ "$_today_src" -eq 0 ] && _today_src=${DAILY_TOKENS:-0}
    _today_fmt=$(_usage_fmt "$_today_src")
    _session_fmt=$(_usage_fmt "${SESSION_DELTA:-0}")
    _lifetime_total=$(( ${G_TOTAL:-0} + ${G_RECOVERED:-0} ))
    _lifetime_fmt=$(_usage_fmt "$_lifetime_total")
    USAGE_DISPLAY="${dim}today${reset} ${cyan}${_today_fmt}${reset} ${dim}·${reset} ${dim}session${reset} ${magenta}${_session_fmt}${reset} ${dim}·${reset} ${dim}lifetime${reset} ${green}${_lifetime_fmt}${reset}"
fi

# ── Cost ────────────────────────────────────────────────
COST_FMT=$(printf "%.2f" "$COST")

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

# ── Context % with visual bar ──────────────────────────
CONTEXT_INT=$(printf "%.0f" "$CONTEXT_PCT")
CTX_BAR=$(build_context_bar "$CONTEXT_INT" 15)
CTX_COLOR=$(color_for_context "$CONTEXT_INT")

# ── Effort level ───────────────────────────────────────
# Claude Code 2.1+ emits effort as `.effort.level`; older builds used
# `.effort_level`. Accept both, then fall back to settings.json.
EFFORT=""
effort_val=$(echo "$input" | jq -r '.effort.level // .effort_level // empty' 2>/dev/null)
if [ -z "$effort_val" ]; then
    settings_path="$HOME/.claude/settings.json"
    [ -f "$settings_path" ] && effort_val=$(jq -r '.effortLevel // empty' "$settings_path" 2>/dev/null)
fi
case "$effort_val" in
    low)    EFFORT="${dim}.low${reset}" ;;
    medium) EFFORT="${orange}.medium${reset}" ;;
    high)   EFFORT="${red}.high${reset}" ;;
    xhigh)  EFFORT="${red}.xhigh${reset}" ;;
    max)    EFFORT="${red}.max${reset}" ;;
esac

# ── Fast mode ──────────────────────────────────────────
FAST_MODE=""
settings_fast=$(jq -r '.fastMode // false' "$HOME/.claude/settings.json" 2>/dev/null)
if [ "$settings_fast" = "true" ]; then
    FAST_MODE=" ${yellow}⚡fast${reset}"
fi

# ── Focus mode ──────────────────────────────────────────
FOCUS=""
[ -f "$FOCUS_FILE" ] && FOCUS=" ${red}[FOCUS]${reset}"

# ── Git info ────────────────────────────────────────────
GIT_INFO=""
IS_DIRTY=false
BRANCH=""
IN_WORKTREE=false
WORKTREE_NAME=""
if [ -d "$CWD" ] && git -C "$CWD" rev-parse --git-dir > /dev/null 2>&1; then
    BRANCH=$(git -C "$CWD" branch --show-current 2>/dev/null)
    # Detect worktree: git-common-dir differs from git-dir when in a worktree
    GIT_DIR=$(git -C "$CWD" rev-parse --git-dir 2>/dev/null)
    GIT_COMMON=$(git -C "$CWD" rev-parse --git-common-dir 2>/dev/null)
    if [ -n "$GIT_DIR" ] && [ -n "$GIT_COMMON" ]; then
        # Normalize paths for comparison
        GIT_DIR_REAL=$(cd "$CWD" && cd "$GIT_DIR" 2>/dev/null && pwd)
        GIT_COMMON_REAL=$(cd "$CWD" && cd "$GIT_COMMON" 2>/dev/null && pwd)
        if [ "$GIT_DIR_REAL" != "$GIT_COMMON_REAL" ]; then
            IN_WORKTREE=true
            WORKTREE_NAME="${CWD##*/}"
        fi
    fi
    if [ -n "$BRANCH" ]; then
        # Use git diff-index for fast dirty check (single call, no untracked scan)
        if ! git -C "$CWD" diff-index --quiet HEAD -- 2>/dev/null; then
            IS_DIRTY=true
        fi

        if $IN_WORKTREE; then
            local_wt="${magenta}⌥${WORKTREE_NAME}${reset} "
            if $IS_DIRTY; then
                GIT_INFO=" ${local_wt}${orange}(${BRANCH}${red}*${orange})${reset}"
            else
                GIT_INFO=" ${local_wt}${green}(${BRANCH})${reset}"
            fi
        elif $IS_DIRTY; then
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

# ── Directory name ──────────────────────────────────────
# Strip to last path component. Handle both / (Unix) and \ (Windows/MSYS).
DIR_NAME="${CWD##*/}"
DIR_NAME="${DIR_NAME##*\\}"

# ── OAuth token resolution ──────────────────────────────
get_oauth_token() {
    if [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
        echo "$CLAUDE_CODE_OAUTH_TOKEN"
        return 0
    fi

    # Keychain first, file fallback — mirrors cc's own credential store
    # ('keychain-with-plaintext-fallback'): the slot wins whenever it exists;
    # a accounts route deletes the slot, so both readers fall through to the file
    # together and the board always shows the account cc is actually using.
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

# Detect account switch: compare token hash (works with keychain + file)
token_hash_file="/tmp/claude/statusline-token-hash"
current_token=$(get_oauth_token)
if [ -n "$current_token" ] && [ "$current_token" != "null" ]; then
    current_hash=$(printf '%s' "$current_token" | shasum -a 256 2>/dev/null | cut -c1-16)
    old_hash=$(cat "$token_hash_file" 2>/dev/null)
    if [ "$old_hash" != "$current_hash" ]; then
        rm -f "$cache_file" "$profile_cache_file" "$lock_file"
        echo "$current_hash" > "$token_hash_file"
        needs_refresh=true
        # Synchronous profile fetch on account switch — avoids stale label
        p_response=$(curl -s --max-time 2 \
            -H "Accept: application/json" \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer $current_token" \
            -H "anthropic-beta: oauth-2025-04-20" \
            -H "User-Agent: claude-code/2.1.34" \
            "https://api.anthropic.com/api/oauth/profile" 2>/dev/null)
        if [ -n "$p_response" ] && echo "$p_response" | jq -e '.account' >/dev/null 2>&1; then
            echo "$p_response" > "$profile_cache_file"
        fi
        needs_profile_refresh=false
    fi
fi
# Legacy fallback: also check credentials file mtime
creds_file="$HOME/.claude/.credentials.json"
if [ -f "$creds_file" ]; then
    creds_mtime_file="/tmp/claude/statusline-creds-mtime"
    creds_mtime=$(stat -f %m "$creds_file" 2>/dev/null || stat -c %Y "$creds_file" 2>/dev/null)
    old_creds_mtime=$(cat "$creds_mtime_file" 2>/dev/null)
    if [ "$old_creds_mtime" != "$creds_mtime" ]; then
        rm -f "$cache_file" "$profile_cache_file" "$lock_file"
        echo "$creds_mtime" > "$creds_mtime_file"
        needs_refresh=true
        needs_profile_refresh=true
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
                # Write profile cache FIRST — on short-lived parents the trailing
                # write gets reaped behind the slow usage curl, leaving no account row.
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
                if $needs_refresh; then
                    response=$(curl -s --max-time 5 \
                        -H "Accept: application/json" \
                        -H "Content-Type: application/json" \
                        -H "Authorization: Bearer $token" \
                        -H "anthropic-beta: oauth-2025-04-20" \
                        -H "User-Agent: claude-code/2.1.34" \
                        "https://api.anthropic.com/api/oauth/usage" 2>/dev/null)
                    if [ -n "$response" ] && echo "$response" | jq -e '.five_hour' >/dev/null 2>&1; then
                        # Save previous poll for interpolation
                        if [ -f "$cache_file" ]; then
                            prev_ts=$(stat -f %m "$cache_file" 2>/dev/null || stat -c %Y "$cache_file" 2>/dev/null)
                            # One jq to pull all three values — saves 2 subprocess spawns per render.
                            eval "$(jq -r '"prev_5h=" + (.five_hour.utilization // 0 | tostring),
                                           "prev_7d=" + (.seven_day.utilization // 0 | tostring),
                                           "prev_extra=" + (.extra_usage.used_credits // 0 | tostring)' "$cache_file" 2>/dev/null)"
                            printf '{"ts":%s,"five_hour":%s,"seven_day":%s,"extra_used":%s}' "$prev_ts" "${prev_5h:-0}" "${prev_7d:-0}" "${prev_extra:-0}" > "/tmp/claude/statusline-usage-prev.json"
                        fi
                        echo "$response" > "$cache_file"

                        # Cross-account reset ledger: record the active account's
                        # 5h/7d reset times + utilization so other sessions can
                        # render "when does X reset" even when not logged in.
                        # Keyed by "email|org_uuid" so a single email shared
                        # across orgs (work seat + personal Max) tracks two
                        # rows independently. Eventually consistent — only
                        # the active account is updated per poll. Writes also
                        # drop legacy email-only keys for the same email.
                        ledger_email=""
                        ledger_org_uuid=""
                        if [ -f "$profile_cache_file" ]; then
                            ledger_email=$(jq -r '.account.email // empty' "$profile_cache_file" 2>/dev/null)
                            ledger_org_uuid=$(jq -r '.organization.uuid // empty' "$profile_cache_file" 2>/dev/null)
                        fi
                        if [ -n "$ledger_email" ]; then
                            ledger_file="$HOME/.claude/account-resets.json"
                            ledger_now=$(date +%s)
                            [ -f "$ledger_file" ] || echo '{}' > "$ledger_file"
                            tmp_ledger=$(mktemp "/tmp/claude/acct-resets.XXXXXX")
                            jq --arg e "$ledger_email" --arg uuid "$ledger_org_uuid" --argjson ts "$ledger_now" --argjson u "$response" \
                                '.[$e + "|" + $uuid] = {
                                    "email":           $e,
                                    "org_uuid":        $uuid,
                                    "five_hour_reset": ($u.five_hour.resets_at // null),
                                    "five_hour_pct":   ($u.five_hour.utilization // 0),
                                    "seven_day_reset": ($u.seven_day.resets_at // null),
                                    "seven_day_pct":   ($u.seven_day.utilization // 0),
                                    "fable_pct":       ([$u.limits[]? | select(.kind == "weekly_scoped") | .percent][0] // null),
                                    "fable_reset":     ([$u.limits[]? | select(.kind == "weekly_scoped") | .resets_at][0] // null),
                                    "fable_label":     ([$u.limits[]? | select(.kind == "weekly_scoped") | .scope.model.display_name][0] // null),
                                    "last_seen":       $ts
                                }
                                | with_entries(select(.key != $e))' "$ledger_file" > "$tmp_ledger" 2>/dev/null && mv "$tmp_ledger" "$ledger_file" || rm -f "$tmp_ledger"

                            # Append to history log — one JSON line per poll.
                            # This is the dataset we'll regress (util, token_spend)
                            # pairs against to derive the hidden per-account cap.
                            # Cheap (~120 bytes/line, ~60 polls/hr → ~7KB/hr).
                            # Rotate if file exceeds ~5MB (keep last ~half).
                            hist_file="$HOME/.claude/utilization-history.jsonl"
                            jq -c --arg e "$ledger_email" --arg uuid "$ledger_org_uuid" --argjson ts "$ledger_now" --argjson u "$response" -n \
                                '{
                                    ts: $ts,
                                    email: $e,
                                    org_uuid: $uuid,
                                    five_hour_pct:   ($u.five_hour.utilization // 0),
                                    five_hour_reset: ($u.five_hour.resets_at // null),
                                    seven_day_pct:   ($u.seven_day.utilization // 0),
                                    seven_day_reset: ($u.seven_day.resets_at // null),
                                    extra_used:      ($u.extra_usage.used_credits // 0),
                                    extra_pct:       ($u.extra_usage.utilization // 0),
                                    extra_limit:     ($u.extra_usage.monthly_limit // 0)
                                }' >> "$hist_file" 2>/dev/null
                            if [ -f "$hist_file" ]; then
                                hist_size=$(stat -f %z "$hist_file" 2>/dev/null || stat -c %s "$hist_file" 2>/dev/null || echo 0)
                                if [ "$hist_size" -gt 5000000 ] 2>/dev/null; then
                                    # Byte-truncate then drop the partial first line so readers
                                    # don't have to defend against a corrupt leading record.
                                    tail -c 2500000 "$hist_file" | awk 'NR>1' > "${hist_file}.tmp" && mv "${hist_file}.tmp" "$hist_file"
                                fi
                            fi
                        fi
                    fi
                fi
            fi
        ) &
    fi
fi

# Always read from cache (may be stale by one cycle — imperceptible)
usage_data=""
[ -f "$cache_file" ] && usage_data=$(cat "$cache_file" 2>/dev/null)

# Fallback when the live usage poll is rate-limited (429 → no cache, common on
# multi-session hosts): rebuild the 5h/7d bars from the current account's ledger row.
if [ -z "$usage_data" ] && [ -n "$ACCT_EMAIL" ]; then
    _ledger_file="$HOME/.claude/account-resets.json"
    if [ -f "$_ledger_file" ]; then
        usage_data=$(jq -c --arg key "${ACCT_EMAIL}|${ACCT_ORG_UUID}" --arg email "$ACCT_EMAIL" '
            (.[$key] // (to_entries | map(.value) | map(select(.email == $email)) | .[0])) as $e
            | if $e == null then empty
              else {
                  five_hour: { utilization: ($e.five_hour_pct // 0), resets_at: ($e.five_hour_reset // null) },
                  seven_day: { utilization: ($e.seven_day_pct // 0), resets_at: ($e.seven_day_reset // null) },
                  limits: (
                    if ($e.fable_pct // null) != null then
                      [{ kind: "weekly_scoped", percent: $e.fable_pct, resets_at: ($e.fable_reset // null),
                         scope: { model: { display_name: ($e.fable_label // "fable") } } }]
                    else [] end
                  )
                } end' "$_ledger_file" 2>/dev/null)
    fi
fi

# ── Account display — show full email with a color chosen by tag ──
# ACCT_EMAIL is the authenticated email; ACCT_TAG is its resolved label (from
# ACCOUNT_LABELS in statusline.conf). Color is picked by tag so the config
# controls both the tag → label mapping AND what color each tag displays as.
#
# Default tag → color mapping (override by setting LABEL_COLORS in config):
#   work     cyan     (primary / pro plan)
#   personal magenta
#   alumni   green
#   anything-else  orange (fallback)
#
# LABEL_COLORS format (space-separated pairs): "work:cyan personal:magenta alumni:green"
ACCOUNT_LABEL=""
_resolve_label_color() {
    local tag="$1"
    # Explicit config override
    if [ -n "${LABEL_COLORS:-}" ]; then
        for pair in $LABEL_COLORS; do
            case "$pair" in
                "${tag}:"*)
                    local color_name="${pair#*:}"
                    # shellcheck disable=SC2086
                    eval "printf '%s' \"\${$color_name}\""
                    return
                    ;;
            esac
        done
    fi
    # Built-in defaults
    case "$tag" in
        work)     printf '%s' "$cyan" ;;
        personal) printf '%s' "$magenta" ;;
        alumni)   printf '%s' "$green" ;;
        *)        printf '%s' "$orange" ;;
    esac
}
if [ -n "$ACCT_EMAIL" ]; then
    _label_color=$(_resolve_label_color "$ACCT_TAG")
    ACCOUNT_LABEL="${_label_color}${ACCT_EMAIL}${reset}"
elif [ -n "$ACCT_TAG" ]; then
    _label_color=$(_resolve_label_color "$ACCT_TAG")
    ACCOUNT_LABEL="${_label_color}${ACCT_TAG}${reset}"
fi

# Burn-down projection for a usage window ("5h" | "weekly"): estimate time to
# 100% from utilization velocity, appending "→full ~X" plus a survive marker
# (✓buffer / ✗downtime until reset) to rate_lines. 5h prefers the recent-poll
# rate and falls back to the window-start average; weekly uses the window-start
# average only. Int-minute (5h) vs float-hour (weekly) formatting differs by
# window on purpose — parameterized, not normalized.
burn_down_projection() {
    local window="$1"
    local pct reset_iso window_secs threshold
    if [ "$window" = 5h ]; then
        pct="$five_hour_pct"; reset_iso="$five_hour_reset_iso"; window_secs=18000; threshold=0
    else
        pct="$seven_day_pct"; reset_iso="$seven_day_reset_iso"; window_secs=604800; threshold=2
    fi

    [ "$pct" -gt "$threshold" ] 2>/dev/null && [ -n "$reset_iso" ] && [ "$reset_iso" != "" ] || return
    local epoch
    epoch=$(iso_to_epoch "$reset_iso")
    [ -n "$epoch" ] || return

    local now secs_to_reset secs_elapsed remaining_pct
    now=$(date +%s)
    secs_to_reset=$(( epoch - now ))
    [ "$secs_to_reset" -lt 0 ] && secs_to_reset=0
    secs_elapsed=$(( window_secs - secs_to_reset ))
    [ "$secs_elapsed" -lt 60 ] && secs_elapsed=60
    [ "$secs_elapsed" -gt 0 ] && [ "$pct" -gt 0 ] 2>/dev/null || return
    remaining_pct=$(( 100 - pct ))
    [ "$remaining_pct" -gt 0 ] || return

    if [ "$window" = 5h ]; then
        # Trust the recent-poll rate only when the gap is informative (>30s) and
        # utilization moved up; else fall back to the window-start average.
        local mins_to_full
        mins_to_full=$(awk "BEGIN {
            poll_interval = ${poll_interval:-0};
            prev_pct      = ${prev_5h:-0};
            cur_pct       = $pct;
            delta_pct     = cur_pct - prev_pct;
            if (poll_interval >= 30 && delta_pct > 0) {
                rate = delta_pct / poll_interval;
            } else {
                rate = cur_pct / $secs_elapsed;
            }
            if (rate > 0) printf \"%.0f\", $remaining_pct / rate / 60;
            else print 999
        }")
        [ "$mins_to_full" -gt 0 ] 2>/dev/null && [ "$mins_to_full" -lt 6000 ] 2>/dev/null || return

        local bd_color="$green"
        [ "$mins_to_full" -le 60 ] && bd_color="$orange"
        [ "$mins_to_full" -le 30 ] && bd_color="$yellow"
        [ "$mins_to_full" -le 15 ] && bd_color="$red"

        local full_display
        full_display=$(fmt_duration_m "$mins_to_full")
        rate_lines+=" ${bd_color}$(pad_right "→full ~${full_display}" 14)${reset}"

        # Survive: buffer or downtime until the window resets.
        local mins_to_reset=$(( secs_to_reset / 60 ))
        [ "$mins_to_reset" -gt 0 ] 2>/dev/null || return
        if [ "$mins_to_full" -gt "$mins_to_reset" ] 2>/dev/null; then
            local buf_display
            buf_display=$(fmt_duration_m $(( mins_to_full - mins_to_reset )))
            rate_lines+=" ${green}✓${buf_display}${reset}"
        else
            local dt_display
            dt_display=$(fmt_duration_m $(( mins_to_reset - mins_to_full )))
            rate_lines+=" ${red}✗${dt_display}${reset}"
        fi
        return
    fi

    local hrs_to_full hrs_to_reset mins_to_full_weekly display_to_full
    hrs_to_full=$(awk "BEGIN {
        rate = $pct / $secs_elapsed;
        if (rate > 0) printf \"%.2f\", $remaining_pct / rate / 3600;
        else print 999
    }")
    hrs_to_reset=$(awk "BEGIN { printf \"%.2f\", $secs_to_reset / 3600 }")
    mins_to_full_weekly=$(awk "BEGIN { printf \"%.0f\", $hrs_to_full * 60 }")
    display_to_full=$(fmt_duration_m "$mins_to_full_weekly")

    # Only show if the projection lands within the 7-day window.
    awk "BEGIN { exit ($hrs_to_full < 168) ? 0 : 1 }" 2>/dev/null || return
    local wd_color="$green"
    awk "BEGIN { exit ($hrs_to_full <= 72) ? 0 : 1 }" 2>/dev/null && wd_color="$orange"
    awk "BEGIN { exit ($hrs_to_full <= 36) ? 0 : 1 }" 2>/dev/null && wd_color="$yellow"
    awk "BEGIN { exit ($hrs_to_full <= 12) ? 0 : 1 }" 2>/dev/null && wd_color="$red"
    rate_lines+=" ${wd_color}$(pad_right "→full ~${display_to_full}" 14)${reset}"

    # Survive: buffer or downtime until the window resets.
    local weekly_gap_mins
    weekly_gap_mins=$(awk "BEGIN { printf \"%.0f\", ($hrs_to_full - $hrs_to_reset) * 60 }")
    if awk "BEGIN { exit ($hrs_to_full > $hrs_to_reset) ? 0 : 1 }" 2>/dev/null; then
        local weekly_buf_display
        weekly_buf_display=$(fmt_duration_m "$weekly_gap_mins")
        rate_lines+=" ${green}✓${weekly_buf_display}${reset}"
    else
        local weekly_dt_display
        weekly_dt_display=$(fmt_duration_m "$weekly_gap_mins")
        rate_lines+=" ${red}✗${weekly_dt_display}${reset}"
    fi
}

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
        "extra_limit_raw=" + (.extra_usage.monthly_limit // 0 | tostring | @sh),
        "fable_pct_raw=" + ([.limits[]? | select(.kind == "weekly_scoped") | .percent][0] // "" | tostring | @sh),
        "fable_reset_iso=" + ([.limits[]? | select(.kind == "weekly_scoped") | .resets_at][0] // "" | @sh),
        "fable_label=" + ([.limits[]? | select(.kind == "weekly_scoped") | .scope.model.display_name][0] // "" | @sh)
    ' 2>/dev/null)"

    # Interpolate between polls for fractional precision
    prev_poll_file="/tmp/claude/statusline-usage-prev.json"
    if [ -f "$prev_poll_file" ] && [ -f "$cache_file" ]; then
        poll_ts=$(stat -f %m "$cache_file" 2>/dev/null || stat -c %Y "$cache_file" 2>/dev/null)
        # One jq pull — saves 2 spawns per render.
        eval "$(jq -r '"prev_ts=" + (.ts // 0 | tostring),
                       "prev_5h=" + (.five_hour // 0 | tostring),
                       "prev_7d=" + (.seven_day // 0 | tostring)' "$prev_poll_file" 2>/dev/null)"
        : "${prev_ts:=0}" "${prev_5h:=0}" "${prev_7d:=0}"
        interp_now=$(date +%s)
        poll_interval=$(( poll_ts - prev_ts ))
        secs_since_poll=$(( interp_now - poll_ts ))

        if [ "$poll_interval" -gt 10 ] 2>/dev/null && [ "$secs_since_poll" -ge 0 ] 2>/dev/null; then
            five_hour_pct_display=$(awk "BEGIN {
                rate = ($five_hour_pct - $prev_5h) / $poll_interval;
                if (rate < 0) rate = 0;
                est = $five_hour_pct + rate * $secs_since_poll;
                if (est > 100) est = 100;
                printf \"%.2f\", est
            }")
            seven_day_pct_display=$(awk "BEGIN {
                rate = ($seven_day_pct_raw - $prev_7d) / $poll_interval;
                if (rate < 0) rate = 0;
                est = $seven_day_pct_raw + rate * $secs_since_poll;
                if (est > 100) est = 100;
                printf \"%.2f\", est
            }")
        else
            five_hour_pct_display=$(printf "%.2f" "$five_hour_pct" 2>/dev/null || echo "0.00")
            seven_day_pct_display=$(printf "%.2f" "$seven_day_pct_raw" 2>/dev/null || echo "0.00")
        fi
    else
        five_hour_pct_display=$(printf "%.2f" "$five_hour_pct" 2>/dev/null || echo "0.00")
        seven_day_pct_display=$(printf "%.2f" "$seven_day_pct_raw" 2>/dev/null || echo "0.00")
    fi
    five_hour_pct=$(printf "%.0f" "$five_hour_pct_display" 2>/dev/null || echo 0)
    five_hour_reset=$(format_reset_time "$five_hour_reset_iso" "time")
    five_hour_bar=$(build_bar "$five_hour_pct" "$bar_width")
    five_hour_pct_color=$(color_for_pct "$five_hour_pct")

    # Pct padded to 9 cols (6 for "85.7%" + 3 trailing) so reset/dollars/breakdown
    # all start at the same column across rate rows AND token rows.
    rate_lines+="${white}$(printf "%-7s" "current")${reset} ${five_hour_bar} ${five_hour_pct_color}$(fmt_pct "$five_hour_pct_display")${reset}   "
    # Reset-time padded to 15 so "→full ..." lines up across current / weekly.
    if [ -n "$five_hour_reset" ]; then
        rate_lines+=" ${white}$(pad_right "$five_hour_reset" 15)${reset}"
    else
        rate_lines+=" $(printf '%16s' '')"
    fi

    # When at 100%, show countdown to reset
    if [ "$five_hour_pct" -ge 100 ] 2>/dev/null && [ -n "$five_hour_reset_iso" ]; then
        countdown_epoch=$(iso_to_epoch "$five_hour_reset_iso")
        if [ -n "$countdown_epoch" ]; then
            countdown_now=$(date +%s)
            countdown_secs=$(( countdown_epoch - countdown_now ))
            [ "$countdown_secs" -lt 0 ] && countdown_secs=0
            countdown_mins=$(( countdown_secs / 60 ))
            countdown_display=$(fmt_duration_m "$countdown_mins")
            rate_lines+=" ${red}resets ${countdown_display}${reset}"
        fi
    fi

    burn_down_projection 5h

    seven_day_pct=$(printf "%.0f" "$seven_day_pct_display" 2>/dev/null || echo 0)
    seven_day_reset=$(format_reset_time "$seven_day_reset_iso" "datetime")
    seven_day_bar=$(build_bar "$seven_day_pct" "$bar_width")
    seven_day_pct_color=$(color_for_pct "$seven_day_pct")
    # Short weekly reset (date only, e.g. "apr 28") for the current-account tail.
    WEEK_RESET_SHORT=$(format_reset_time "$seven_day_reset_iso" "date" 2>/dev/null || echo "")
    if [ -z "$WEEK_RESET_SHORT" ]; then
        # Fallback: pull just the "apr 28" chunk from the long form.
        WEEK_RESET_SHORT=$(echo "$seven_day_reset" | awk -F',' '{print $1}')
    fi
    WEEK_PCT_DISPLAY="$seven_day_pct"
    WEEK_PCT_COLOR="$seven_day_pct_color"

    rate_lines+="\n${white}$(printf "%-7s" "weekly")${reset} ${seven_day_bar} ${seven_day_pct_color}$(fmt_pct "$seven_day_pct_display")${reset}   "
    if [ -n "$seven_day_reset" ]; then
        rate_lines+=" ${white}$(pad_right "$seven_day_reset" 15)${reset}"
    else
        rate_lines+=" $(printf '%16s' '')"
    fi

    burn_down_projection weekly

fi

WEEKLY_BAR_LINE=""
FABLE_BAR_LINE=""
if [ -n "${WEEK_PCT_DISPLAY:-}" ] && [ "${WEEK_PCT_DISPLAY:-0}" -gt 0 ] 2>/dev/null; then
    _wk_bar=$(build_bar "$WEEK_PCT_DISPLAY" 15)
    # fmt_pct on the fractional display preserves interpolated sub-percent
    # precision; the int WEEK_PCT_DISPLAY still drives bar fill + color.
    WEEKLY_BAR_LINE="${white}$(printf "%-7s" "weekly")${reset} ${_wk_bar} ${WEEK_PCT_COLOR}$(fmt_pct "${seven_day_pct_display:-$WEEK_PCT_DISPLAY}")${reset}"
    _week_reset_full="${seven_day_reset:-$WEEK_RESET_SHORT}"
    [ -n "$_week_reset_full" ] && WEEKLY_BAR_LINE+="  ${dim}resets ${_week_reset_full}${reset}"
fi

# Per-model weekly cap (the API's "weekly_scoped" limit, e.g. Fable). Its own
# labeled row under weekly; label tracks the scoped model's display_name.
if [ -n "${fable_pct_raw:-}" ]; then
    _fb_pct_int=$(printf "%.0f" "$fable_pct_raw" 2>/dev/null || echo 0)
    [ "$_fb_pct_int" -lt 0 ] 2>/dev/null && _fb_pct_int=0
    [ "$_fb_pct_int" -gt 100 ] 2>/dev/null && _fb_pct_int=100
    _fb_bar=$(build_bar "$_fb_pct_int" 15)
    _fb_color=$(color_for_pct "$_fb_pct_int")
    _fb_label=$(printf "%s" "${fable_label:-fable}" | tr '[:upper:]' '[:lower:]' | cut -c1-7)
    # No reset timestamp: fable shares the 5h window shown on the row above.
    FABLE_BAR_LINE="${white}$(printf "%-7s" "$_fb_label")${reset} ${_fb_bar} ${_fb_color}$(fmt_pct "$_fb_pct_int")${reset}"
fi

# ── Daily budget line ──────────────────────────────────
BUDGET_DISPLAY=""
if [ "$DAILY_BUDGET" -gt 0 ] 2>/dev/null; then
    budget_display=$(awk "BEGIN {p=$DAILY_COST * 100 / $DAILY_BUDGET; printf \"%.2f\", (p > 100 ? 100 : p)}")
    budget_pct="${budget_display%.*}"
    [ "$budget_pct" -gt 100 ] 2>/dev/null && budget_pct=100
    budget_bar=$(build_bar "$budget_pct" 10)
    budget_color=$(color_for_pct "$budget_pct")
    BUDGET_DISPLAY="${white}$(printf "%-7s" "budget")${reset} ${budget_bar} ${budget_color}$(fmt_pct "$budget_display")${reset} ${white}\$${DAILY_FMT}${dim}/${reset}${white}\$${DAILY_BUDGET}${reset}"
fi

# ── Multi-account reset ledger line ────────────────────
# Shows each tracked account's next 5-hour reset + current utilization, so
# you know which login has headroom at a glance. Opt-in via SHOW_ACCOUNT_RESETS=1
# in ~/.claude/statusline.conf. Data is written per-account during usage polls
# (see background refresh block above) and keyed by email.
#
# The current account's entry is highlighted; the soonest-to-reset gets a
# "→" marker. Reset times in the past are projected forward in 5h increments
# (matches existing format_reset_time behavior) to handle accounts you
# haven't touched in a while.
# printf's %-Ns counts BYTES not display columns, which misaligns UTF-8
# chars like "—" (3 bytes, 1 column). _pad_to_cols counts characters so
# columns stay stable across rows regardless of mixed-byte content.
_pad_to_cols() {
    local s=$1 want=$2
    local n=${#s}
    local pad=$(( want - n ))
    if [ "$pad" -gt 0 ]; then
        printf '%s' "$s"
        printf '%*s' "$pad" ''
    else
        printf '%s' "$s"
    fi
}

# Right-align by display columns (${#} counts chars, not bytes — keeps "—" aligned).
_ralign() {
    local s=$1 want=$2
    local pad=$(( want - ${#s} ))
    [ "$pad" -lt 0 ] && pad=0
    printf '%*s%s' "$pad" '' "$s"
}

ACCOUNT_ROWS=""  # per-account stacked rows (new layout); each row starts with \n
if [ "${SHOW_ACCOUNT_RESETS:-0}" = "1" ]; then
    ledger_file="$HOME/.claude/account-resets.json"
    caps_file="$HOME/.claude/account-caps.json"
    hist_file="$HOME/.claude/utilization-history.jsonl"
    # Build email -> latest (extra_pct, extra_used_cents) map from the history
    # log's tail. Bash 3.2 on macOS has no assoc arrays, so store as a
    # newline-delimited "email<TAB>pct<TAB>cents" string. Read only the tail
    # to keep it cheap. awk keeps the most recent values per email.
    EXTRA_PCT_LOOKUP=""
    if [ -f "$hist_file" ]; then
        # `tail -c` slices mid-line, so the first line of the window is a
        # fragment that makes jq abort the whole stream. Drop it with `awk
        # NR>1`. Backfill limit from used/pct when the older log format
        # omitted it. Lookup key is "email|org_uuid" (uuid may be empty for
        # legacy history entries). Pipe separator avoids tab-collapse on
        # empty uuid fields.
        EXTRA_PCT_LOOKUP=$(tail -c 200000 "$hist_file" 2>/dev/null | awk 'NR>1' | \
            jq -r 'select(.email) | [.email, (.org_uuid // ""), (.extra_pct // 0 | tostring), (.extra_used // 0 | tostring), (.extra_limit // 0 | tostring)] | join("|")' 2>/dev/null | \
            awk -F'|' '{k=$1"|"$2; pct[k]=$3; used[k]=$4; lim[k]=$5}
                END{for(k in pct) {
                    l=lim[k];
                    if (l==0 && pct[k]>0) l=used[k]*100/pct[k];
                    print k"|"pct[k]"|"used[k]"|"l
                }}')
    fi
    if [ -f "$ledger_file" ] && [ -n "$ACCT_EMAIL" ]; then
        # Collect entries: email\x1Fuuid\x1Fiso\x1Fpct per line. Use US (0x1F)
        # as the field separator — @tsv collapses consecutive tabs because
        # IFS=$'\t' is whitespace, eating empty uuids on legacy entries.
        # Legacy entries (bare email key, no .value.email) fall back to
        # splitting the key on "|".
        now_ar=$(date +%s)
        # accounts blobs: which accounts have a dead refresh token (switching to them
        # needs a fresh /login). blobs.json is the router's live source of truth;
        # refresh expiry lives inside each blob (epoch-ms). Keyed email|org to
        # match the ledger rows below.
        accounts_blobs="$HOME/.accounts/blobs.json"
        ACCOUNTS_EXPIRED_LOOKUP=""
        if [ -f "$accounts_blobs" ]; then
            ACCOUNTS_EXPIRED_LOOKUP=$(jq -r --argjson now "$now_ar" '
                (.accounts // {}) | to_entries[] | .value |
                (try ((.blob | fromjson).claudeAiOauth) catch null) as $oauth |
                (($oauth.refreshTokenExpiresAt) // null) as $exp |
                select($oauth == null or ($oauth.refreshToken // null) == null
                       or ($exp != null and ($exp / 1000) <= $now)) |
                "\(.email)|\(.org_uuid)"' "$accounts_blobs" 2>/dev/null)
        fi
        entries=$(jq -r --argjson now "$now_ar" '
            to_entries[] |
            [(.value.email // (.key | split("|") | .[0])),
             (.value.org_uuid // ((.key | split("|") | .[1]) // "")),
             (.value.five_hour_reset // ""),
             (.value.five_hour_pct // 0 | tostring),
             (.value.seven_day_reset // ""),
             (.value.seven_day_pct // 0 | tostring),
             (.value.fable_reset // ""),
             (.value.fable_pct // "" | tostring)] |
            join("")' "$ledger_file" 2>/dev/null)
        if [ -n "$entries" ]; then
            # Parse + compute projected epochs, find soonest
            parsed=""
            soonest_epoch=""
            while IFS=$'\x1f' read -r em uuid iso pct seven_day_iso weekly_pct_ledger fbl_iso fable_pct_ledger; do
                [ -z "$em" ] && continue
                ep=""
                # pct_state: ok = use stored pct; reset = fresh window, show 0%;
                # unknown = stale account, show —. Decided after rollover.
                pct_state="ok"
                if [ -n "$iso" ] && [ "$iso" != "null" ]; then
                    ep=$(iso_to_epoch "$iso")
                    if [ -n "$ep" ]; then
                        if [ "$ep" -le "$now_ar" ]; then
                            # Reset elapsed. The new window started at 0% — but
                            # only if the elapsed reset is the MOST RECENT one
                            # (within ~5h of now). If it's older than that, the
                            # account has been idle for many windows and we
                            # have no signal for the current one — unknown.
                            since_reset=$(( now_ar - ep ))
                            if [ "$since_reset" -le 18000 ]; then
                                pct_state="reset"
                            else
                                pct_state="unknown"
                            fi
                        fi
                        while [ "$ep" -le "$((now_ar + 30))" ]; do
                            ep=$((ep + 18000))
                        done
                    fi
                fi
                tag=$(resolve_account_label "$em" "$uuid")
                parsed+="${em}|${uuid}|${tag}|${ep}|${pct}|${pct_state}|${seven_day_iso}|${weekly_pct_ledger}|${fbl_iso}|${fable_pct_ledger}"$'\n'
                if [ -n "$ep" ]; then
                    if [ -z "$soonest_epoch" ] || [ "$ep" -lt "$soonest_epoch" ] 2>/dev/null; then
                        soonest_epoch="$ep"
                    fi
                fi
            done <<< "$entries"

            # Sort by projected reset epoch (soonest first). Entries with no
            # epoch sort to the end. Epoch is field 4; row now has 8 fields.
            parsed=$(printf '%s' "$parsed" | awk -F'|' 'NF>=8 { key=($4==""?"9999999999":$4); print key"\t"$0 }' | sort -n | cut -f2-)

            while IFS='|' read -r em uuid tag ep pct pct_state seven_day_iso weekly_pct_ledger fbl_iso fable_pct_ledger; do
                [ -z "$em" ] && continue
                # Match the active account on (email, org_uuid). Legacy ledger
                # entries with empty uuid only match when ACCT_ORG_UUID is also
                # empty (no profile cache) — degrades to old email-only behavior.
                if [ "$em" = "$ACCT_EMAIL" ] && [ "$uuid" = "$ACCT_ORG_UUID" ]; then
                    is_current=1
                else
                    is_current=0
                fi
                # Reset per-iteration cap vars so stale values don't leak across
                # accounts when jq returns empty for this email.
                ci_status="" ci_cur="0" ci_cap=""
                # Display time (respects the projected epoch)
                if [ -n "$ep" ]; then
                    ep_tz=$(fmt_epoch "$ep" "%Z")
                    tdisp_raw=$(fmt_epoch "$ep" "%l:%M%p" | sed 's/^ //; s/\.//g' | tr '[:upper:]' '[:lower:]')
                    if [ -n "$tdisp_raw" ]; then
                        tdisp="${tdisp_raw} ${ep_tz}"
                    else
                        tdisp="—"
                    fi
                else
                    tdisp="—"
                fi
                # Display name: friendly title-cased per-tag label, overriding
                # the lowercase tag used internally for lookups. Fallback
                # title-cases the tag so config-defined tags ("gmail",
                # "poynting", "coram-max") render cleanly without needing a
                # hardcoded case here.
                case "$tag" in
                    work|coram|coram-work) display_name="Coram"     ;;
                    coram-max)             display_name="Coram-Max" ;;
                    alumni)                display_name="Brown"     ;;
                    personal)              display_name="Andrew"    ;;
                    poynting)              display_name="Poynting"  ;;
                    gmail)                 display_name="Gmail"     ;;
                    *) display_name="$(tr '[:lower:]' '[:upper:]' <<< "${tag:0:1}")${tag:1}" ;;
                esac
                # All account tags render white; only the leading marker
                # distinguishes the current account (◉) from the others.
                label="${display_name:-$em}"
                seg="${white}${label}${reset}"
                # Leading marker: * for current, · for others. Using a visible
                # dim dot for non-current rows prevents Claude Code's status
                # panel from trimming leading whitespace and shifting columns.
                if [ "$is_current" = "1" ]; then
                    marker="${white}*${reset} "
                else
                    marker="${dim}·${reset} "
                fi
                # Utilization: for the CURRENT account, use the interpolated
                # value already computed by the rate-limit block (matches the
                # "current" row exactly). For other accounts, fall back to
                # the ledger value (no interpolation possible — we're not
                # logged into them).
                if [ "$is_current" = "1" ] && [ -n "${five_hour_pct_display:-}" ]; then
                    pct_disp="$five_hour_pct_display"
                    pct_state="ok"
                else
                    pct_disp="$pct"
                fi
                # Reset state overrides cached values: a freshly-rolled window
                # starts at 0%, an account with no recent samples is unknown.
                if [ "${pct_state:-ok}" = "reset" ]; then
                    pct_int=0
                    pct_disp="0"
                fi
                pct_int=$(printf "%.0f" "$pct_disp" 2>/dev/null || echo 0)
                pct_color=$(color_for_pct "$pct_int")
                # Display fractional util for the current account (matches the
                # "current" row); other accounts only have integer ledger data.
                if [ "${pct_state:-ok}" = "unknown" ]; then
                    pct_show="$(_pad_to_cols '—' 4)"
                    pct_color="$dim"
                    pct_int=0
                elif [ "$is_current" = "1" ]; then
                    pct_show=$(fmt_pct "$pct_disp")
                else
                    pct_show=$(printf "%d%%" "$pct_int")
                fi

                # Work-unit display from derive-cap.py. "wu" is a plan-agnostic
                # unit defined as "1 output-token-equivalent" — it's consistent
                # across accounts regardless of Pro/Max/Max-5× tier.
                #   status=ok       → "(Xwu/Ywu)"
                #   status=calibrating with current_wu → "(Xwu · calibrating)"
                #   otherwise omitted
                cap_suffix=""
                if [ -f "$caps_file" ]; then
                    cap_info=$(jq -r --arg e "$em" '.[$e] // empty |
                        if .status == "ok" and .cap_wu then
                          "ok|" + (.current_wu|tostring) + "|" + (.cap_wu|tostring)
                        elif .current_wu then
                          "cal|" + (.current_wu|tostring) + "|" + ((.best_observed_wu // 0)|tostring)
                        else "" end' "$caps_file" 2>/dev/null)
                    if [ -n "$cap_info" ]; then
                        IFS='|' read -r ci_status ci_cur ci_cap <<< "$cap_info"
                        _wu_fmt() {
                            awk -v n="$1" 'BEGIN {
                                if (n>=1e9) printf "%.1fB", n/1e9;
                                else if (n>=1e6) printf "%.1fM", n/1e6;
                                else if (n>=1e3) printf "%.0fK", n/1e3;
                                else printf "%.0f", n
                            }'
                        }
                        cur_fmt=$(_wu_fmt "$ci_cur")
                        if [ "$ci_status" = "ok" ]; then
                            cap_fmt=$(_wu_fmt "$ci_cap")
                            cap_suffix=" ${dim}(${reset}${white}${cur_fmt}wu${dim}/${white}${cap_fmt}wu${dim})${reset}"
                        elif [ "$ci_status" = "cal" ]; then
                            cap_suffix=" ${dim}(${cur_fmt}wu)${reset}"
                        fi
                    fi
                fi

                # Legacy pending-samples fallback (no wu data yet at all)
                if [ -z "$cap_suffix" ] && [ -f "$caps_file" ]; then
                    legacy=$(jq -r --arg e "$em" '.[$e] // empty |
                        if .n_points then
                          "pending|" + (.n_points|tostring) + "|" + ((.min_required // 30)|tostring)
                        else "" end' "$caps_file" 2>/dev/null)
                    if [ -n "$legacy" ]; then
                        IFS='|' read -r ci_status ci_a ci_b <<< "$legacy"
                        if [ "$ci_status" = "pending" ]; then
                            cap_suffix=" ${dim}(${ci_a}/${ci_b} samples)${reset}"
                        fi
                    fi
                fi

                # accounts: flag a dead vaulted refresh token (needs /login to use).
                exp_suffix=""
                if [ -n "$ACCOUNTS_EXPIRED_LOOKUP" ] && \
                   printf '%s\n' "$ACCOUNTS_EXPIRED_LOOKUP" | grep -qxF "${em}|${uuid}"; then
                    exp_suffix=" ${red}⚠ needs reauth${reset}"
                fi

                # ── Per-account row (new stacked layout) ──
                # Pull this account's latest extra-credit spend from the lookup
                # string we built above. Rendered as $remaining/$limit — the
                # HEADROOM left on the paid-credit bucket, which is what you
                # actually care about for planning.
                extra_pct=""
                extra_cents=""
                extra_limit_cents=""
                if [ -n "$EXTRA_PCT_LOOKUP" ]; then
                    # Prefer (email, uuid) exact match; fall back to email-only
                    # match for legacy history entries that lack org_uuid.
                    IFS='|' read -r extra_pct extra_cents extra_limit_cents < <(printf '%s\n' "$EXTRA_PCT_LOOKUP" | awk -F'|' -v e="$em" -v u="$uuid" '
                        $1==e && $2==u { ep=$3; eu=$4; el=$5; exact=1 }
                        $1==e && $2=="" { fp=$3; fu=$4; fl=$5; fall=1 }
                        END {
                            if (exact) print ep"|"eu"|"el
                            else if (fall) print fp"|"fu"|"fl
                        }')
                fi
                # Account tags appear in title-case for display (Coram / Brown
                # / Andrew) but we still key on the lowercased name for lookups.
                # Handled in the rendering block below.
                # extra_int still feeds the hard-wall warning + best-next score
                # below, even though the visible column now shows weekly util.
                if [ -n "$extra_pct" ] && [ -n "$extra_limit_cents" ] && awk "BEGIN{exit !(${extra_limit_cents:-0} > 0)}"; then
                    extra_int=$(printf "%.0f" "$extra_pct" 2>/dev/null || echo 0)
                else
                    extra_int=0
                fi

                # Weekly per-account util column. Current account uses the
                # live interpolated value (matches the standalone "weekly"
                # bar row); others fall back to the ledger snapshot. If the
                # seven-day reset has already elapsed, the snapshot is from
                # a prior window — show "—" instead of stale data.
                if [ "$is_current" = "1" ] && [ -n "${seven_day_pct_display:-}" ]; then
                    weekly_pct_disp="$seven_day_pct_display"
                    weekly_state="ok"
                else
                    weekly_pct_disp="${weekly_pct_ledger:-0}"
                    weekly_state="ok"
                    if [ -n "$seven_day_iso" ] && [ "$seven_day_iso" != "null" ]; then
                        sd_ep=$(iso_to_epoch "$seven_day_iso")
                        if [ -n "$sd_ep" ] && [ "$sd_ep" -le "$now_ar" ] 2>/dev/null; then
                            weekly_state="unknown"
                        fi
                    fi
                fi
                weekly_int=$(printf "%.0f" "$weekly_pct_disp" 2>/dev/null || echo 0)
                # Bare colored % — the header row labels the column.
                if [ "$weekly_state" = "unknown" ]; then
                    weekly_color="$dim"
                    wk_raw="—"
                else
                    weekly_color=$(color_for_pct "$weekly_int")
                    wk_raw="${weekly_int}%"
                fi
                weekly_seg="${weekly_color}$(_ralign "$wk_raw" 4)${reset}"

                # Per-account fable (weekly-scoped) util column. Mirrors weekly:
                # current account uses the live value, others the ledger
                # snapshot; an elapsed reset or absent data shows "—".
                if [ "$is_current" = "1" ] && [ -n "${fable_pct_raw:-}" ]; then
                    fable_disp="$fable_pct_raw"
                else
                    fable_disp="${fable_pct_ledger:-}"
                fi
                fable_state="ok"
                if [ -n "$fbl_iso" ] && [ "$fbl_iso" != "null" ]; then
                    fbl_ep=$(iso_to_epoch "$fbl_iso")
                    [ -n "$fbl_ep" ] && [ "$fbl_ep" -le "$now_ar" ] 2>/dev/null && fable_state="unknown"
                fi
                if [ "$fable_state" = "unknown" ] || [ -z "$fable_disp" ]; then
                    fable_color="$dim"; fb_raw="—"
                else
                    fable_int=$(printf "%.0f" "$fable_disp" 2>/dev/null || echo 0)
                    fable_color=$(color_for_pct "$fable_int"); fb_raw="${fable_int}%"
                fi
                fable_seg="${fable_color}$(_ralign "$fb_raw" 5)${reset}"

                # Hours-to-reset (computed from ep above, if known).
                _now_ep=$(date +%s)
                if [ -n "$ep" ]; then
                    _secs_to_reset=$(( ep - _now_ep ))
                    _hrs_to_reset=$(awk -v s="$_secs_to_reset" 'BEGIN { printf "%.1f", s/3600 }')
                else
                    _hrs_to_reset=""
                fi

                # Row note: one of (in priority order)
                #   ⚠ hard wall  — 5h≥90% AND extra≥99% AND not resetting soon
                #   ✓ use now    — best non-current candidate under the same
                #                  scoring model as plan CLI
                #   → resets Xh  — a reset lands within 2h (windfall)
                #   (blank)      — unremarkable
                note=""
                headroom=$(( 100 - pct_int ))
                extra_headroom=$(( 100 - extra_int ))
                has_wall=0
                if [ "$pct_int" -ge 90 ] 2>/dev/null && [ "$extra_int" -ge 99 ] 2>/dev/null; then
                    if [ -z "$_hrs_to_reset" ] || awk "BEGIN{exit !($_hrs_to_reset > 1.0)}"; then
                        note="${red}⚠ hard wall${reset}"
                        has_wall=1
                    fi
                fi

                # Compute planning score (for later pick of best non-current).
                # Matches the python plan model: window_headroom + 0.5×extra
                # + reset_windfall_if_within_2h − hard_wall_penalty.
                windfall=0
                if [ -n "$_hrs_to_reset" ]; then
                    # Bonus scaled by fraction of a 2h plan horizon that lands
                    # AFTER the reset.
                    windfall=$(awk -v h="$_hrs_to_reset" 'BEGIN {
                        if (h < 0 || h >= 2) print 0; else printf "%d", 100*(2-h)/2
                    }')
                fi
                score=$(( headroom + extra_headroom / 2 + windfall ))
                [ "$has_wall" = "1" ] && score=0
                # Weekly-capped accounts are bricks regardless of 5h headroom.
                [ "${weekly_int:-0}" -ge 100 ] 2>/dev/null && score=0
                [ -n "$exp_suffix" ] && score=0

                # Track best non-current account for the "✓ use now" marker.
                if [ "$is_current" = "0" ] && [ "$score" -gt "${_best_score:-0}" ]; then
                    _best_score=$score
                    _best_em=$em
                fi

                # 5h reset as relative time — the header labels the column;
                # absolute clock times duplicated the session row above.
                if [ -n "$ep" ] && [ "${_secs_to_reset:-0}" -gt 0 ] 2>/dev/null; then
                    if [ "$_secs_to_reset" -ge 3600 ]; then
                        five_reset_rel="$(( _secs_to_reset / 3600 ))h$(( (_secs_to_reset % 3600) / 60 ))m"
                    else
                        five_reset_rel="$(( _secs_to_reset / 60 ))m"
                    fi
                else
                    five_reset_rel="—"
                fi

                # Weekly reset, relative only ("6d" / "12h" / "45m").
                # `seven_day_iso` is the API's seven_day.resets_at via the ledger.
                if [ -n "$seven_day_iso" ] && [ "$seven_day_iso" != "null" ]; then
                    seven_day_ep=$(iso_to_epoch "$seven_day_iso")
                    if [ -n "$seven_day_ep" ]; then
                        _delta=$(( seven_day_ep - now_ar ))
                        if [ "$_delta" -le 0 ]; then
                            wk_reset_rel="now"
                        elif [ "$_delta" -lt 3600 ]; then
                            wk_reset_rel="$(( _delta / 60 ))m"
                        elif [ "$_delta" -lt 86400 ]; then
                            wk_reset_rel="$(( _delta / 3600 ))h"
                        else
                            wk_reset_rel="$(( _delta / 86400 ))d"
                        fi
                    else
                        wk_reset_rel="—"
                    fi
                else
                    wk_reset_rel="—"
                fi
                if [ "${pct_state:-ok}" = "unknown" ]; then
                    pct_raw="—"
                else
                    pct_raw="${pct_int}%"
                fi
                # Weekly-capped accounts are unusable regardless of 5h state — dim the name.
                name_color="$white"
                [ "$weekly_int" -ge 100 ] 2>/dev/null && name_color="$dim"
                row_line="${marker}${name_color}$(_pad_to_cols "$display_name" 9)${reset} ${pct_color}$(_ralign "$pct_raw" 4)${reset}  ${dim}$(_ralign "$five_reset_rel" 6)${reset}   ${weekly_seg}   ${fable_seg}  ${dim}$(_ralign "$wk_reset_rel" 6)${reset}${exp_suffix}"
                # Annotate with hard-wall warning when applicable. (Windfall
                # is implicit from the hrs_col — no extra note needed.)
                if [ "$has_wall" = "1" ]; then
                    row_line+="  ${red}⚠ hard wall${reset}"
                fi
                ACCOUNT_ROWS+="|${em}:${row_line}"
            done <<< "$parsed"

            # Second pass: annotate the "✓ best next" row and assemble final output.
            FINAL_ACCOUNT_ROWS=""
            IFS='|' read -ra _rows <<< "$ACCOUNT_ROWS"
            for r in "${_rows[@]}"; do
                [ -z "$r" ] && continue
                row_em="${r%%:*}"
                row_body="${r#*:}"
                if [ -n "${_best_em:-}" ] && [ "$row_em" = "$_best_em" ] && [ "${five_hour_pct:-0}" -ge 70 ] 2>/dev/null; then
                    row_body+="   ${green}✓ best next${reset}"
                fi
                # Prefix with the marker (already 2 cols) — no extra leading
                # whitespace. Claude Code's status panel strips leading spaces
                # on wrapped/multi-line output, which misaligned earlier when
                # the indent was "  " + marker.
                FINAL_ACCOUNT_ROWS+=$'\n'"${row_body}"
            done
        fi
    fi
fi

# ── Terminal width detection ──────────────────────────────
# The statusline runs as a non-TTY subprocess under Claude Code, so $COLUMNS
# is unset and tput cols returns 80 regardless of real width. Try in order:
#   1. MAX_COLS config override (always wins)
#   2. $COLUMNS exported by the parent shell
#   3. tput, but only when stdout is a real TTY
#   4. walk up the process ancestry looking for a controlling TTY we can stty
# Falling back to 120 (assume wide) only when nothing above worked.
detect_cols() {
    if [ -n "${MAX_COLS:-}" ] && [ "$MAX_COLS" -gt 0 ] 2>/dev/null; then
        printf '%s' "$MAX_COLS"; return
    fi
    if [ -n "${COLUMNS:-}" ] && [ "$COLUMNS" -gt 0 ] 2>/dev/null; then
        printf '%s' "$COLUMNS"; return
    fi
    if [ -t 1 ]; then
        local tcols
        tcols=$(tput cols 2>/dev/null)
        if [ -n "$tcols" ] && [ "$tcols" -gt 0 ] 2>/dev/null; then
            printf '%s' "$tcols"; return
        fi
    fi
    local pid=$$ tty size cols
    for _ in 1 2 3 4 5 6 7 8; do
        pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
        [ -z "$pid" ] || [ "$pid" = "0" ] || [ "$pid" = "1" ] && break
        tty=$(ps -o tty= -p "$pid" 2>/dev/null | tr -d ' ')
        [ -z "$tty" ] || [ "$tty" = "??" ] && continue
        size=$(stty size < "/dev/$tty" 2>/dev/null) || continue
        cols="${size##* }"
        if [ -n "$cols" ] && [ "$cols" -gt 0 ] 2>/dev/null; then
            printf '%s' "$cols"; return
        fi
    done
    printf '120'
}
COLS=$(detect_cols)
NARROW_THRESHOLD="${NARROW_THRESHOLD:-60}"

# ── Branch name compression ──────────────────────────────
# Long branches overflow the top row under Claude Code's status area (the
# status area is narrower than the terminal). If the line overflows, the rest
# of the statusline is hidden. We keep the most informative piece of the name
# (typically a ticket id) and trim aggressively.
#
# Strategy:
#   1. Strip an optional BRANCH_PREFIX_STRIP (e.g., your git username prefix).
#   2. If the remainder looks like "<TICKET>/<slug>" (e.g. "AGI-427/foo-bar"),
#      keep the ticket + a trimmed slug.
#   3. Hard-cap at MAX_BRANCH chars with an ellipsis on the end.
SHORT_GIT_INFO="$GIT_INFO"
TINY_GIT_INFO=""
if [ -n "$BRANCH" ]; then
    SHORT_BRANCH="$BRANCH"
    # Optional user-configured prefix strip (e.g. "andrew/" -> "").
    if [ -n "${BRANCH_PREFIX_STRIP:-}" ]; then
        case "$SHORT_BRANCH" in
            "$BRANCH_PREFIX_STRIP"*) SHORT_BRANCH="${SHORT_BRANCH#$BRANCH_PREFIX_STRIP}" ;;
        esac
    fi
    # If still prefixed (e.g. "feat/AGI-123/foo"), strip everything before the
    # last slash EXCEPT when the part before the last slash looks like a ticket
    # id (e.g. "AGI-123"). In that case keep "<ticket>/<slug>".
    if [[ "$SHORT_BRANCH" == */* ]]; then
        lead="${SHORT_BRANCH%/*}"
        tail="${SHORT_BRANCH##*/}"
        # detect ticket-like leading segment: LETTERS-DIGITS
        if [[ "${lead##*/}" =~ ^[A-Za-z]+-[0-9]+$ ]]; then
            SHORT_BRANCH="${lead##*/}/${tail}"
        else
            SHORT_BRANCH="$tail"
        fi
    fi

    MAX_BRANCH="${MAX_BRANCH:-24}"
    CAPPED_BRANCH="$SHORT_BRANCH"
    if [ "${#SHORT_BRANCH}" -gt "$MAX_BRANCH" ]; then
        CAPPED_BRANCH="${SHORT_BRANCH:0:$((MAX_BRANCH-1))}…"
        SHORT_BRANCH="$CAPPED_BRANCH"
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
    # Labeled identity block — one fact per row, consistent with
    # context/left/usage rows below.
    printf "${white}%-7s${reset} %b\n" "model"   "${blue}${MODEL}${reset}${EFFORT}${FAST_MODE}"
    [ -n "$SESSION_TIME" ] && \
        printf "${white}%-7s${reset} %b\n" "time"    "${dim}⏱${reset} ${white}${SESSION_TIME}${reset}${IDLE_DISPLAY}"
    [ -n "$ACCT_EMAIL" ] && \
        printf "${white}%-7s${reset} %b\n" "account" "${ACCOUNT_LABEL}"
    REPO_LABEL="${cyan}${DIR_NAME}${reset}"
    if [ -n "$BRANCH" ]; then
        if $IN_WORKTREE; then
            REPO_LABEL="${magenta}⌥ ${reset}${REPO_LABEL} ${dim}worktree${reset}"
        else
            REPO_LABEL="${REPO_LABEL} ${dim}primary${reset}"
        fi
    fi
    printf  "${white}%-7s${reset} %b"   "repo"    "${REPO_LABEL}${SHORT_GIT_INFO}${FOCUS}"

    # Detail lines (dimmer for visual hierarchy). CONTEXT_PCT carries the
    # API's sub-percent precision; CONTEXT_INT still drives bar + color.
    ctx_line="${white}$(printf "%-7s" "context")${reset} ${CTX_BAR} ${CTX_COLOR}$(fmt_pct "${CONTEXT_PCT:-$CONTEXT_INT}")${reset}"
    printf "\n%b" "$ctx_line"

    # Headroom bar for the current 5h window — how much you've got LEFT.
    # Uses interpolated five_hour_pct from the rate-limit block above.
    if [ -n "${five_hour_pct:-}" ]; then
        # Show 5h window as "used" — fills up as you consume. Uses interpolated
        # five_hour_pct_display (sub-percent precision when poll delta is
        # available; falls back to API integer otherwise). fmt_pct strips
        # trailing zeros so "93%" stays short and "93.4%" shows real precision.
        # used_int is an integer for the bar fill + color_for_pct (which needs
        # integers — floats silently fall through to green).
        used_display="${five_hour_pct_display:-$five_hour_pct}"
        used_int="${used_display%.*}"
        [ "$used_int" -lt 0 ] 2>/dev/null && used_int=0
        [ "$used_int" -gt 100 ] 2>/dev/null && used_int=100
        used_bar=$(build_bar "$used_int" 15)
        used_color=$(color_for_pct "$used_int")
        used_line="${white}$(printf "%-7s" "session")${reset} ${used_bar} ${used_color}$(fmt_pct "$used_display")${reset}"
        # Reset time (five_hour_reset set earlier via format_reset_time).
        # Local TZ formatting verified against the dashboard ("8:00pm" etc).
        [ -n "${five_hour_reset:-}" ] && used_line+="  ${dim}resets ${five_hour_reset}${reset}"
        printf "\n%b" "$used_line"
    fi
    [ -n "$WEEKLY_BAR_LINE"  ] && printf "\n%b" "$WEEKLY_BAR_LINE"
    [ -n "$FABLE_BAR_LINE" ] && [ "${SHOW_FABLE_ROW:-1}" = "1" ] && printf "\n%b" "$FABLE_BAR_LINE"
    [ -n "$BUDGET_DISPLAY" ] && printf "\n%b" "$BUDGET_DISPLAY"
    # Each opt-out defaults to 1 (show); set to 0 in statusline.conf to hide.
    [ -n "$TOKEN_DISPLAY" ] && [ "${SHOW_TOKENS_ROW:-1}" = "1" ] && printf "\n${white}$(printf "%-7s" "tokens")${reset} %b" "$TOKEN_DISPLAY"
    [ -n "$CHALLENGE_DISPLAY" ] && [ "${SHOW_CHALLENGE_ROW:-1}" = "1" ] && printf "\n${white}$(printf "%-7s" "$CHALLENGE_LABEL")${reset} %b" "$CHALLENGE_DISPLAY"
    [ -n "$BOUNTY_DISPLAY" ] && [ "${SHOW_BOUNTY_ROW:-1}" = "1" ] && printf "\n${white}$(printf "%-7s" "bounty")${reset} %b" "$BOUNTY_DISPLAY"
    [ -n "$USAGE_DISPLAY" ] && printf "\n${white}$(printf "%-7s" "usage")${reset} %b" "$USAGE_DISPLAY"
    if [ "${SHOW_BACKENDS_ROW:-0}" = "1" ]; then
        backends_line=$("${BASH_SOURCE[0]%/*}/live-state.py" --render 2>/dev/null)
        [ -n "$backends_line" ] && printf "\n${white}$(printf "%-7s" "stack")${reset} ${dim}%s${reset}" "$backends_line"
    fi
    if [ -n "$FINAL_ACCOUNT_ROWS" ]; then
        _acct_header="  $(_pad_to_cols "acct" 9) $(_ralign "5h" 4)  $(_ralign "reset" 6)   $(_ralign "week" 4)   $(_ralign "fable" 5)  $(_ralign "reset" 6)"
        printf "\n${dim}%s${reset}%b" "$_acct_header" "$FINAL_ACCOUNT_ROWS"
    fi
}

# ── Render: compact (context + used only) ─────────────────
render_compact() {
    ctx_line="${white}$(printf "%-7s" "context")${reset} ${CTX_BAR} ${CTX_COLOR}$(fmt_pct "${CONTEXT_PCT:-$CONTEXT_INT}")${reset}"
    printf "%b" "$ctx_line"

    if [ -n "${five_hour_pct:-}" ]; then
        used_display="${five_hour_pct_display:-$five_hour_pct}"
        used_int="${used_display%.*}"
        [ "$used_int" -lt 0 ] 2>/dev/null && used_int=0
        [ "$used_int" -gt 100 ] 2>/dev/null && used_int=100
        used_bar=$(build_bar "$used_int" 15)
        used_color=$(color_for_pct "$used_int")
        used_line="${white}$(printf "%-7s" "session")${reset} ${used_bar} ${used_color}$(fmt_pct "$used_display")${reset}"
        [ -n "${five_hour_reset:-}" ] && used_line+="  ${dim}resets ${five_hour_reset}${reset}"
        printf "\n%b" "$used_line"
    fi
}

# ── Render: narrow (auto-selected when terminal is too narrow for default) ─
# Keeps the same fact-per-row shape as render_default but trims aggressively:
# shorter labels, 5-char bars, and no trailing suffixes (ETA, reset times,
# breakdowns). Activates for COLS < NARROW_THRESHOLD (default 60).
render_narrow() {
    local bar_w=5
    [ "$COLS" -ge 50 ] 2>/dev/null && bar_w=8

    # Identity line: model + effort + fast (no label — it's the obvious row).
    printf "%b" "${blue}${MODEL}${reset}${EFFORT}${FAST_MODE}"

    # Repo + short branch + dirty marker. Reuse SHORT_GIT_INFO when it fits,
    # else fall back to TINY_GIT_INFO (already capped via MAX_BRANCH).
    if [ -n "$BRANCH" ]; then
        local git_seg="$TINY_GIT_INFO"
        [ "$COLS" -ge 50 ] 2>/dev/null && [ -n "$SHORT_GIT_INFO" ] && git_seg="$SHORT_GIT_INFO"
        printf "\n${cyan}%s${reset}%b" "$DIR_NAME" "$git_seg"
    fi

    # Context — bar shrinks at narrower widths, percent always shown.
    local ctx_pct="${CONTEXT_PCT:-$CONTEXT_INT}"
    local ctx_bar
    ctx_bar=$(build_context_bar "$CONTEXT_INT" "$bar_w")
    printf "\n${white}ctx${reset} %b ${CTX_COLOR}%s${reset}" "$ctx_bar" "$(fmt_pct "$ctx_pct")"

    # 5h window — same treatment, no reset suffix at narrow widths.
    if [ -n "${five_hour_pct:-}" ]; then
        local used_display="${five_hour_pct_display:-$five_hour_pct}"
        local used_int="${used_display%.*}"
        [ "$used_int" -lt 0 ] 2>/dev/null && used_int=0
        [ "$used_int" -gt 100 ] 2>/dev/null && used_int=100
        local used_bar used_color
        used_bar=$(build_bar "$used_int" "$bar_w")
        used_color=$(color_for_pct "$used_int")
        printf "\n${white}5h ${reset} %b ${used_color}%s${reset}" "$used_bar" "$(fmt_pct "$used_display")"
    fi

    # Weekly + cost on one line when we have room; cost-only otherwise.
    if [ -n "${seven_day_pct:-}" ] && [ "$seven_day_pct" -gt 0 ] 2>/dev/null; then
        local wcolor
        wcolor=$(color_for_pct "$seven_day_pct")
        printf "\n${white}7d ${reset} ${wcolor}%s${reset}" "$(fmt_pct "${seven_day_pct_display:-$seven_day_pct}")"
        printf "  ${magenta}\$%s${reset}" "$COST_FMT"
    elif [ -n "${COST_FMT:-}" ]; then
        printf "\n${magenta}\$%s${reset}" "$COST_FMT"
    fi
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

# ── Set terminal tab title ────────────────────────────────
TAB_TITLE="${DIR_NAME}"
[ -n "$BRANCH" ] && [ "$BRANCH" != "main" ] && [ "$BRANCH" != "master" ] && TAB_TITLE="${DIR_NAME} (${SHORT_BRANCH})"
SESSION_TOPIC=$(session_topic "$SESSION_ID" "$CWD")
[ -n "$SESSION_TOPIC" ] && TAB_TITLE="${SESSION_TOPIC} — ${TAB_TITLE}"
printf '\033]0;%s\007' "$TAB_TITLE"

case "$FORMAT" in
    sigil)     render_sigil ;;
    compact)   render_compact ;;
    narrow)    render_narrow ;;
    rprompt)   render_rprompt ;;
    sparkline) render_sparkline ;;
    iterm2)    render_iterm2 ;;
    *)
        # Default format wraps badly under a narrow status panel. Fall through
        # to the narrow renderer when we detect (or are told) the panel is
        # tight. NARROW_THRESHOLD is configurable in statusline.conf.
        if [ "$COLS" -lt "$NARROW_THRESHOLD" ] 2>/dev/null; then
            render_narrow
        else
            render_default
        fi
        ;;
esac

exit 0
