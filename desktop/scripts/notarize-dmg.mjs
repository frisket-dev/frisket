import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';

const [dmg] = process.argv.slice(2);
const required = ['APPLE_API_KEY', 'APPLE_API_KEY_ID', 'APPLE_API_ISSUER'];
const missing = required.filter((name) => !process.env[name]);

if (!dmg || !existsSync(dmg)) {
  throw new Error('Pass the distributable DMG path.');
}
if (missing.length) {
  throw new Error(`DMG notarization requires ${missing.join(', ')}`);
}

const credentials = [
  '--key', process.env.APPLE_API_KEY,
  '--key-id', process.env.APPLE_API_KEY_ID,
  '--issuer', process.env.APPLE_API_ISSUER,
];

// electron-builder has signed the app and its outer distribution container.
// Submit and staple only that final DMG, never the intermediate app.
execFileSync('xcrun', ['notarytool', 'submit', dmg, ...credentials, '--wait'], {
  stdio: 'inherit',
});
execFileSync('xcrun', ['stapler', 'staple', dmg], { stdio: 'inherit' });
