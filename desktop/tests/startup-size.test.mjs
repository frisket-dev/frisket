import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { createRequire } from 'node:module';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const require = createRequire(import.meta.url);
let electron = process.env.FRISKET_TEST_ELECTRON;
try { electron ||= require('electron'); } catch {}
const headless = process.platform === 'linux' && !process.env.DISPLAY;

test('native startup window follows content through expansion and contraction', {
  skip: !electron || headless ? 'Requires Electron and a desktop display (or Xvfb).' : false,
  timeout: 30_000,
}, async () => {
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;
  const args = [fileURLToPath(new URL('./fixtures/startup-size.cjs', import.meta.url))];
  // Linux root runners already isolate this process in a credential-free sandbox.
  if (process.platform === 'linux' && process.getuid?.() === 0) args.unshift('--no-sandbox');
  const { stderr } = await promisify(execFile)(electron, args, { env, timeout: 25_000 });
  assert.doesNotMatch(stderr, /AssertionError|Unable to load preload/);
});
