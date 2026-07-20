class ClaudeStatusline < Formula
  desc "Rich multi-line status bar for Claude Code terminal"
  homepage "https://github.com/AndrewTKent/claude-statusline"
  url "https://github.com/AndrewTKent/claude-statusline/archive/refs/tags/v1.0.0.tar.gz"
  sha256 "PLACEHOLDER"
  license "MIT"

  depends_on "jq"
  depends_on "bash"

  def install
    bin.install "bin/statusline.sh" => "claude-statusline"
    pkgshare.install "config/statusline.conf.example"
  end

  def post_install
    claude_dir = Pathname.new(Dir.home) / ".claude"
    return unless claude_dir.directory?

    # Symlink to expected location
    target = claude_dir / "statusline.sh"
    ln_sf bin/"claude-statusline", target

    # Create default config if missing
    conf = claude_dir / "statusline.conf"
    unless conf.exist?
      cp pkgshare/"statusline.conf.example", conf
    end

    # Patch settings.json if statusLine key missing
    settings = claude_dir / "settings.json"
    if settings.exist?
      require "json"
      begin
        data = JSON.parse(settings.read)
        unless data.key?("statusLine")
          data["statusLine"] = {
            "type" => "command",
            "command" => "~/.claude/statusline.sh",
            "padding" => 0,
          }
          settings.atomic_write(JSON.pretty_generate(data) + "\n")
        end
      rescue JSON::ParserError
        opoo "Could not parse ~/.claude/settings.json — skipping patch"
      end
    end
  end

  def caveats
    <<~EOS
      Restart Claude Code to activate the status line.

      Configure: edit ~/.claude/statusline.conf
        DAILY_BUDGET=20    # enables budget bar
        FORMAT=sigil       # single-line compact mode

      Formats: default, compact, narrow, sigil, sparkline, rprompt, iterm2
    EOS
  end

  test do
    assert_match "Claude", pipe_output(bin/"claude-statusline", "")
  end
end
