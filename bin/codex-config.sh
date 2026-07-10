#!/usr/bin/env bash

load_codex_statusline_config() {
    local config_file key line value
    config_file="${CODEX_STATUSLINE_CONFIG:-${CODEX_HOME:-$HOME/.codex}/statusline.conf}"
    [[ -f "$config_file" ]] || return 0

    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ ! "$line" =~ ^[[:space:]]*(export[[:space:]]+)?(CODEX_[A-Z0-9_]+|SESSION_TOKEN_GOAL|DAILY_TOKEN_GOAL|WEEKLY_TOKEN_GOAL|LIFETIME_TOKEN_GOAL)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
            continue
        fi
        key="${BASH_REMATCH[2]}"
        [[ -z "${!key:-}" ]] || continue
        value="${BASH_REMATCH[3]}"
        value="${value%"${value##*[![:space:]]}"}"
        if [[ "$value" == \"*\" && "$value" == *\" ]]; then
            value="${value:1:${#value}-2}"
        elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
            value="${value:1:${#value}-2}"
        fi
        printf -v "$key" '%s' "$value"
        export "${key?}"
    done < "$config_file"
}
