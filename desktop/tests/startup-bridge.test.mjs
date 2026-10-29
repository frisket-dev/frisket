import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
import { installStartupBridge, canRetryStartup } from '../src/startup-bridge.mjs';

test('startup actions require the exact owned local top frame and resize is bounded', () => {
  const handlers = new Map();
  const calls = [];
  const url = 'file:///app/ui/startup.html';
  const frame = { url };
  const contents = { mainFrame: frame, send: (...args) => calls.push(args) };
  const window = { webContents: contents, isDestroyed: () => false };
  installStartupBridge({
    ipcMain: { on: (channel, callback) => handlers.set(channel, callback) }, pageUrl: url,
    findWindow: sender => sender === contents ? window : undefined,
    snapshot: () => ({ status: 'checking' }), retry: () => calls.push('retry'),
    close: () => calls.push('close'), copy: () => calls.push('copy'),
    details: (_window, value) => calls.push(value), resize: (_window, height) => calls.push(height),
  });
  const event = { sender: contents, senderFrame: frame };
  handlers.get('startup:get')(event);
  assert.deepEqual(calls, [['startup:state', { status: 'checking' }]]);
  for (const invalid of [
    { sender: contents, senderFrame: { url } },
    { sender: {}, senderFrame: frame },
  ]) handlers.get('startup:retry')(invalid);
  frame.url = 'frisket://app/';
  handlers.get('startup:retry')(event);
  frame.url = url;
  assert.equal(calls.length, 1);
  handlers.get('startup:details')(event, '/secret');
  handlers.get('startup:resize')(event, Infinity);
  assert.equal(calls.length, 1);
  handlers.get('startup:resize')(event, 99999);
  handlers.get('startup:resize')(event, -99);
  handlers.get('startup:retry')(event);
  assert.deepEqual(calls.slice(1), [640, 240, 'retry']);
});

test('sandbox preload exposes fixed startup calls only to local documents', async () => {
  const source = await readFile(new URL('../src/startup-preload.cjs', import.meta.url), 'utf8');
  for (const protocol of ['file:', 'frisket:']) {
    let bridge;
    const messages = [];
    const context = {
      window: { location: { protocol } },
      require: () => ({ contextBridge: { exposeInMainWorld: (_name, value) => { bridge = value; } },
        ipcRenderer: { send: (...args) => messages.push(args), on() {}, removeListener() {} } }),
    };
    vm.runInNewContext(source, context);
    if (protocol !== 'file:') { assert.equal(bridge, undefined); continue; }
    bridge.subscribe(() => {});
    bridge.retry(); bridge.close(); bridge.copyDiagnostics(); bridge.details(true); bridge.resize(391);
    assert.deepEqual(messages.map(([channel]) => channel), ['startup:get', 'startup:retry', 'startup:close', 'startup:copy', 'startup:details', 'startup:resize']);
  }
});

test('retry stays unavailable until startup and recovery cleanup have settled', () => {
  const stopped = { visible: true, busy: false, cleanupFailed: false, quitting: false, retryable: true };
  assert.equal(canRetryStartup(stopped), true);
  for (const blocked of [{ busy: true }, { cleanupFailed: true }, { quitting: true }, { retryable: false }, { visible: false }]) {
    assert.equal(canRetryStartup({ ...stopped, ...blocked }), false);
  }
});
