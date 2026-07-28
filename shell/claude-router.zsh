claude() {
  local claude_bin="$HOME/.local/bin/claude"
  local router="$HOME/.local/bin/claude-router"
  if [[ ! -x "$router" ]]; then
    command "$claude_bin" "$@"
    return $?
  fi
  CLAUDE_REAL_BIN="$claude_bin" command "$router" "$@"
}
