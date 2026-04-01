#!/usr/bin/env node

const { execSync } = require('child_process');
const path = require('path');

const command = process.argv[2];

const commands = {
  install: () => require('../lib/install'),
  uninstall: () => require('../lib/uninstall'),
  version: () => {
    const pkg = require('../package.json');
    console.log(`claude-statusline v${pkg.version}`);
  },
  help: showHelp,
};

function showHelp() {
  console.log(`
claude-statusline — Rich status bar for Claude Code

Usage:
  claude-statusline install     Install status line
  claude-statusline uninstall   Remove status line
  claude-statusline version     Show version
  claude-statusline help        Show this help
`);
}

if (!command || !commands[command]) {
  showHelp();
  process.exit(command ? 1 : 0);
} else {
  commands[command]();
}
