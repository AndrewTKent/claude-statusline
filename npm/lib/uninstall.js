const fs = require('fs');
const path = require('path');
const { unpatchSettings } = require('./patch-settings');

const CLAUDE_DIR = path.join(process.env.HOME, '.claude');

function uninstall() {
  console.log('Uninstalling claude-statusline...\n');

  // Remove script
  const script = path.join(CLAUDE_DIR, 'statusline.sh');
  if (fs.existsSync(script)) {
    fs.unlinkSync(script);
    console.log('  Removed ~/.claude/statusline.sh');
  }

  // Remove statusLine from settings
  unpatchSettings();

  // Clean runtime files
  const runtimeFiles = [
    path.join(CLAUDE_DIR, 'rprompt.txt'),
    path.join(CLAUDE_DIR, 'session-history.jsonl'),
  ];
  for (const f of runtimeFiles) {
    if (fs.existsSync(f)) fs.unlinkSync(f);
  }

  // Clean /tmp files
  const tmpDir = '/tmp/claude';
  if (fs.existsSync(tmpDir)) {
    for (const f of fs.readdirSync(tmpDir)) {
      if (f.startsWith('statusline-')) {
        fs.unlinkSync(path.join(tmpDir, f));
      }
    }
  }
  console.log('  Cleaned runtime files');

  console.log('\n  Note: ~/.claude/statusline.conf was kept. Remove manually if desired.');
  console.log('\nUninstalled. Restart Claude Code to take effect.');
}

uninstall();
