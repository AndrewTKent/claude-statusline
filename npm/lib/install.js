const fs = require('fs');
const path = require('path');
const { patchSettings } = require('./patch-settings');

const CLAUDE_DIR = path.join(process.env.HOME, '.claude');
const ASSETS_DIR = path.join(__dirname, '..', 'assets');

function install() {
  // Check prerequisites
  if (!fs.existsSync(CLAUDE_DIR)) {
    console.error('Error: ~/.claude/ not found. Install Claude Code first.');
    process.exit(1);
  }

  try {
    require('child_process').execSync('which jq', { stdio: 'ignore' });
  } catch {
    console.error('Error: jq is required. Install with: brew install jq');
    process.exit(1);
  }

  console.log('Installing claude-statusline...\n');

  // Copy script
  const scriptSrc = path.join(ASSETS_DIR, 'statusline.sh');
  const scriptDst = path.join(CLAUDE_DIR, 'statusline.sh');
  fs.copyFileSync(scriptSrc, scriptDst);
  fs.chmodSync(scriptDst, 0o755);
  console.log('  Installed ~/.claude/statusline.sh');

  // Copy config (if not exists)
  const confDst = path.join(CLAUDE_DIR, 'statusline.conf');
  if (!fs.existsSync(confDst)) {
    const confSrc = path.join(ASSETS_DIR, 'statusline.conf.example');
    fs.copyFileSync(confSrc, confDst);
    console.log('  Created ~/.claude/statusline.conf');
  } else {
    console.log('  Config already exists — skipping');
  }

  // Patch settings
  patchSettings();

  console.log('\nDone! Restart Claude Code to see the status line.');
  console.log('\nConfigure: edit ~/.claude/statusline.conf');
  console.log('  DAILY_BUDGET=20    # budget bar');
  console.log('  FORMAT=sigil       # compact mode');
}

install();
