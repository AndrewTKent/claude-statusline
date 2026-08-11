claude() {
  local router="$HOME/.local/bin/claude-router"
  if [[ -x "$router" ]]; then
    command "$router" "$@"
    return $?
  fi
  command "$HOME/.local/bin/claude" "$@"
}
