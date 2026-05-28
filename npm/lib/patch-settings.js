const fs = require('fs');
const path = require('path');

const SETTINGS_PATH = path.join(process.env.HOME, '.claude', 'settings.json');
const STATUSLINE_CONFIG = { type: 'command', command: '~/.claude/statusline.sh', padding: 0 };

function patchSettings() {
  let data = {};

  if (fs.existsSync(SETTINGS_PATH)) {
    const backup = SETTINGS_PATH + '.bak';
    fs.copyFileSync(SETTINGS_PATH, backup);
    console.log(`  Backed up settings.json → settings.json.bak`);

    try {
      data = JSON.parse(fs.readFileSync(SETTINGS_PATH, 'utf8'));
    } catch (e) {
      console.error(`  Error parsing settings.json: ${e.message}`);
      return false;
    }

    if (data.statusLine) {
      if (data.statusLine.command === '~/.claude/statusline.sh') {
        console.log('  settings.json already configured');
        return true;
      }
      console.log(`  Warning: settings.json has a different statusLine command: ${data.statusLine.command}`);
      console.log('  Not overwriting. Set manually if desired.');
      return true;
    }
  }

  data.statusLine = STATUSLINE_CONFIG;

  const tmp = SETTINGS_PATH + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + '\n');
  fs.renameSync(tmp, SETTINGS_PATH);
  console.log('  Updated settings.json');
  return true;
}

function unpatchSettings() {
  if (!fs.existsSync(SETTINGS_PATH)) return;

  let data;
  try {
    data = JSON.parse(fs.readFileSync(SETTINGS_PATH, 'utf8'));
  } catch (e) {
    return;
  }

  if (!data.statusLine) return;

  fs.copyFileSync(SETTINGS_PATH, SETTINGS_PATH + '.bak');
  delete data.statusLine;

  const tmp = SETTINGS_PATH + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + '\n');
  fs.renameSync(tmp, SETTINGS_PATH);
  console.log('  Removed statusLine from settings.json');
}

module.exports = { patchSettings, unpatchSettings };
